"""Homebrew discovery: analytics are leads; tap source is package authority.

No tap code is executed. The literal reader intentionally rejects definitions it
cannot describe faithfully. Persistent observations live outside disposable cache/.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+@-]*(?:/[A-Za-z0-9][A-Za-z0-9_.+@-]*){0,2}\Z")
WINDOWS = (30, 90, 365)
CATEGORIES = {"brew": "install-on-request", "cask": "cask-install"}


def state_path() -> Path:
    return Path(os.environ.get("PKGDB_DISCOVERY_STATE", ROOT / "state/homebrew-discovery.json"))


def load_state(path: Path | None = None) -> dict:
    path = path or state_path()
    if not path.exists():
        return {"schema": 1, "packages": {}}
    state = json.loads(path.read_text())
    if state.get("schema") != 1 or not isinstance(state.get("packages"), dict):
        raise ValueError("Unsupported discovery state; restore its backup rather than resetting first-seen dates")
    return state


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".discovery-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def valid_name(name: str) -> bool:
    return bool(IDENTIFIER.fullmatch(name)) and len(name.split("/")) in (1, 3) and all(p not in {".", ".."} for p in name.split("/"))


def analytics_url(provider: str, days: int) -> str:
    return f"https://formulae.brew.sh/api/analytics/{CATEGORIES[provider]}/{days}d.json"


def parse_report(payload: dict, provider: str, days: int) -> dict:
    start, end = (dt.date.fromisoformat(payload[k]) for k in ("start_date", "end_date"))
    if (end - start).days != days or not isinstance(payload.get("items"), list) or not payload["items"]:
        raise ValueError(f"Invalid {provider} {days}-day analytics report")
    counts = {}
    for item in payload["items"]:
        name = item.get("cask" if provider == "cask" else "formula", "")
        # Formula reports may contain installation options; these are not identity.
        name = name.split(" ", 1)[0]
        if not valid_name(name):
            continue
        raw = str(item.get("count", "")).replace(",", "")
        if not raw.isdigit():
            raise ValueError(f"Invalid analytics count for {name}")
        counts[name] = counts.get(name, 0) + int(raw)
    return {"startDate": start.isoformat(), "endDate": end.isoformat(), "counts": counts,
            "source": analytics_url(provider, days)}


def trend(analytics: dict, today: dt.date) -> dict | None:
    a, b = (analytics.get(str(days), {}) for days in (30, 90))
    try:
        end = dt.date.fromisoformat(a["endDate"])
        if b["endDate"] != a["endDate"] or not 0 <= (today - end).days <= 3:
            return None
        recent, total = a["count"], b["count"]
        if recent < 30 or total < recent:
            return None
        prior = (total - recent) / 2
        ratio = (recent + 10) / (prior + 10)
        gain = recent - prior
        if ratio < 1.2 or gain < 10:
            return None
        return {"score": round(math.log2(ratio) * math.log1p(gain), 6),
                "growthPercent": round((recent / prior - 1) * 100, 1) if prior else None,
                "installs30d": recent, "previousMonthlyRate": prior,
                "endDate": a["endDate"], "startDate": b["startDate"],
                "sources": [a["source"], b["source"]]}
    except (KeyError, TypeError, ValueError):
        return None


def observe(state: dict, reports: dict, today: dt.date) -> None:
    """Bootstrap each provider once; an outage must not create a false new cohort."""
    baselines = state.setdefault("baselines", {})
    for provider, windows in reports.items():
        if set(windows) != set(WINDOWS):
            continue
        ends = {report["endDate"] for report in windows.values()}
        if len(ends) != 1 or not 0 <= (today - dt.date.fromisoformat(next(iter(ends)))).days <= 3:
            raise ValueError(f"Stale or misaligned {provider} analytics reports")
        bootstrap = provider not in baselines
        names = set().union(*(r["counts"] for r in windows.values()))
        for name in sorted(names):
            key = f"{provider}:{name}"
            record = state["packages"].setdefault(key, {
                "firstObservedAt": today.isoformat(), "baseline": bootstrap,
                "status": "pending" if "/" in name else "official",
            })
            record["lastObservedAt"] = today.isoformat()
            record["analytics"] = {
                str(days): {"count": report["counts"].get(name, 0),
                            **{k: v for k, v in report.items() if k != "counts"}}
                for days, report in windows.items()
            }
        baselines.setdefault(provider, today.isoformat())
    state["updatedAt"] = today.isoformat()


def literal(value: str) -> str:
    match = re.fullmatch(r'''(["'])([^\n]*?)\1''', value.strip())
    if not match or "#{" in match[2] or "\\" in match[2] or any(ord(c) < 32 for c in match[2]):
        raise ValueError("Only plain string literals are supported")
    return match[2]


def read_definition(source: str, provider: str, name: str) -> dict:
    """Conservative DSL reader, never Ruby eval. Unknown top-level syntax fails closed."""
    fields: dict[str, Any] = {}
    apps, binaries = [], []
    lines = [line.strip() for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines or lines[-1] != "end":
        raise ValueError("Missing definition boundary")
    token = name.rsplit("/", 1)[-1]
    if provider == "cask":
        if lines[0] != f'cask "{token}" do':
            raise ValueError("Cask token does not match analytics identity")
    elif not re.fullmatch(r"class [A-Za-z0-9_]+ < Formula", lines[0]):
        raise ValueError("Unsupported formula class")
    ignored_depth = 0
    install_depth = 0
    zap = False
    for line in lines[1:-1]:
        if ignored_depth or install_depth:
            if install_depth:
                match = re.fullmatch(r'bin\.install\s+(.+)', line)
                if match:
                    value = literal(match[1])
                    if "/" not in value and re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
                        binaries.append({"source": value, "target": value})
            if re.search(r"\bdo(?:\s*\|[^|]*\|)?$", line) or re.match(r"(?:if|unless|case|def)\b", line):
                if ignored_depth: ignored_depth += 1
                else: install_depth += 1
            if line == "end":
                if ignored_depth: ignored_depth -= 1
                else: install_depth -= 1
            continue
        if zap:
            if line == "]": zap = False
            continue
        if line in {"livecheck do", "test do", "bottle do"}:
            ignored_depth = 1
            continue
        if line == "def install" and provider == "brew":
            install_depth = 1
            continue
        if line == "zap trash: [":
            zap = True
            continue
        if line.startswith(("disable!", "deprecate!")):
            raise ValueError("Disabled or deprecated package")
        match = re.fullmatch(r"(name|desc|homepage|version|sha256|license|url|app|binary)\s+(.+)", line)
        if match:
            key, raw = match.groups()
            if key == "url" and "#{version}" in raw:
                version = fields.get("version", "")
                if not version or not re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
                    raise ValueError("URL interpolation requires a literal version")
                raw = raw.replace("#{version}", version)
            value = literal(raw)
            if key == "app": apps.append(value)
            elif key == "binary": binaries.append({"source": value, "target": value.rsplit("/", 1)[-1]})
            elif key in fields:
                raise ValueError(f"Ambiguous repeated {key}")
            else: fields[key] = value
            continue
        raise ValueError(f"Unsupported definition syntax: {line[:100]}")
    if ignored_depth or install_depth or zap:
        raise ValueError("Unbalanced definition")
    if not apps and not binaries:
        raise ValueError("No supported app or executable artifact")
    for key in ("desc", "homepage", "url"):
        if not fields.get(key): raise ValueError(f"Missing {key}")
    for key in ("homepage", "url"):
        parsed = urllib.parse.urlsplit(fields[key])
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"Unsupported {key}")
    repo = ""
    for url in (fields["homepage"], fields["url"]):
        parsed = urllib.parse.urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname == "github.com" and len(parts) >= 2:
            repo = "https://github.com/" + "/".join(parts[:2])
            break
    command = f'brew install {"--cask " if provider == "cask" else ""}{name}'
    return {"summary": fields["desc"], "displayName": fields.get("name", token),
            "homepage": fields["homepage"], "repository": repo, "version": fields.get("version", ""),
            "url": fields["url"], "sourceArchive": fields["url"], "sha256": fields.get("sha256", ""),
            "license": fields.get("license", ""), "applications": apps, "binaries": binaries,
            "installBehavior": {"app": bool(apps), "binary": bool(binaries)},
            "installCommands": [{"platform": "macos" if provider == "cask" else "portable",
                                 "manager": "Homebrew Cask" if provider == "cask" else "Homebrew",
                                 "command": command, "kind": "package_manager", "confidence": 1.0,
                                 "evidence": "Verified third-party tap definition"}]}


class Fetcher:
    def __init__(self, directory: Path):
        self.directory = directory
        self.trees: dict[str, dict] = {}

    def get(self, url: str, *, text: bool = False) -> Any:
        path = self.directory / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        cached = json.loads(path.read_text()) if path.exists() else {}
        headers = {"User-Agent": "pkg.so-discovery/1"}
        if cached.get("etag"): headers["If-None-Match"] = cached["etag"]
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000: raise ValueError("Source exceeds size limit")
                value = raw.decode() if text else json.loads(raw)
                save_state({"etag": response.headers.get("ETag"), "value": value}, path)
                return value
        except urllib.error.HTTPError as error:
            if error.code == 304 and "value" in cached: return cached["value"]
            raise

    def resolve(self, key: str) -> dict:
        provider, name = key.split(":", 1)
        if provider not in CATEGORIES or not valid_name(name) or name.count("/") != 2:
            raise ValueError("Expected provider:owner/tap/package")
        owner, tap, token = name.split("/")
        repo = f"{owner}/homebrew-{tap}"
        if repo not in self.trees:
            commit = self.get(f"https://api.github.com/repos/{repo}/commits/HEAD")
            tree = self.get(f"https://api.github.com/repos/{repo}/git/trees/{commit['sha']}?recursive=1")
            tree["revision"] = commit["sha"]
            self.trees[repo] = tree
        tree = self.trees[repo]
        if tree.get("truncated"): raise ValueError("Tap tree is truncated")
        prefix = "Casks/" if provider == "cask" else "Formula/"
        paths = [node["path"] for node in tree.get("tree", []) if node.get("type") == "blob"
                 and node.get("mode") == "100644" and node["path"].startswith(prefix)
                 and node["path"].rsplit("/", 1)[-1] == token + ".rb"]
        if len(paths) != 1: raise ValueError("No unique definition found in tap")
        revision = tree["revision"]
        path = paths[0]
        source = self.get(f"https://raw.githubusercontent.com/{repo}/{revision}/{urllib.parse.quote(path)}", text=True)
        metadata = read_definition(source, provider, name)
        metadata["packageManagerUrl"] = f"https://github.com/{repo}/blob/{revision}/{urllib.parse.quote(path)}"
        metadata["tapSource"] = {"tap": f"{owner}/{tap}", "repository": f"https://github.com/{repo}",
                                 "revision": revision, "path": path, "url": metadata["packageManagerUrl"]}
        return metadata


def resolve_candidates(state: dict, resolver, today: dt.date, limit: int = 30, requested: list[str] | None = None) -> None:
    candidates = []
    for key, record in state["packages"].items():
        if "/" not in key: continue
        analytics = record.get("analytics", {})
        if not record.get("metadata") and max(analytics.get("30", {}).get("count", 0), analytics.get("365", {}).get("count", 0) / 10) < 10:
            continue
        last = record.get("lastAttemptAt", "")
        if last and (today - dt.date.fromisoformat(last)).days < (7 if record.get("metadata") else 3):
            continue
        score = (trend(analytics, today) or {}).get("score", 0)
        candidates.append((last, -score, key))
    # Oldest attempted first prevents a permanently unsupported high scorer starving the queue.
    keys = list(dict.fromkeys((requested or []) + [key for _, _, key in sorted(candidates)]))[:limit]
    for key in keys:
        provider, name = key.split(":", 1)
        if provider not in CATEGORIES or not valid_name(name) or name.count("/") != 2:
            raise ValueError(f"Invalid requested package: {key}")
        record = state["packages"].setdefault(key, {"firstObservedAt": today.isoformat(), "baseline": True})
        record["lastAttemptAt"] = today.isoformat()
        try:
            metadata = resolver(key)
            record.update(metadata=metadata, status="resolved", lastVerifiedAt=today.isoformat())
            record.setdefault("admittedAt", today.isoformat())
            record.pop("error", None)
        except (ValueError, OSError, KeyError) as error:
            record["status"] = "stale" if record.get("metadata") else "unresolved"
            record["error"] = str(error)[:300]


def overlay(db: dict, state: dict | None = None, today: dt.date | None = None) -> dict:
    state = load_state() if state is None else state
    today = today or dt.datetime.now(dt.timezone.utc).date()
    for key, record in state["packages"].items():
        provider, name = key.split(":", 1)
        collection = db.setdefault("casks" if provider == "cask" else "formulas", {})
        if record.get("metadata"):
            # Stored tap authority survives falling out of analytics, and cache rebuilds.
            collection[name] = dict(record["metadata"])
        if name not in collection: continue
        metadata = collection[name]
        verified = record.get("lastVerifiedAt")
        current = record.get("status") == "official" or (record.get("status") == "resolved" and verified and (today - dt.date.fromisoformat(verified)).days <= 14)
        first = record.get("admittedAt", record["firstObservedAt"])
        recent = current and not record.get("baseline", True) and 0 <= (today - dt.date.fromisoformat(first)).days <= 30
        metadata["discovery"] = {"firstObservedAt": record["firstObservedAt"], "admittedAt": first,
                                 "recentlyDiscovered": bool(recent), "status": record["status"],
                                 "lastVerifiedAt": verified, "analytics": record.get("analytics", {}),
                                 "trend": trend(record.get("analytics", {}), today) if current else None}
    return db
