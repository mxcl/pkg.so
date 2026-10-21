#!/usr/bin/env -S uv run --python 3.10
import argparse
import datetime as dt
import gc
import html
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

from avdb_paths import PACKAGE_INDEX_PATH


SCHEMA_VERSION = 1
FORMULA_URL = "https://formulae.brew.sh/api/formula.json"
CASK_URL = "https://formulae.brew.sh/api/cask.json"
NPM_PACKAGE_URL = "https://registry.npmjs.org/{name}"
PYPI_PACKAGE_URL = "https://pypi.org/pypi/{name}/json"
CACHE_DIR = Path("cache")
GENERATED_DATA_DIR = Path("cache")
ECOSYSTEM = "brew.sh"
META_KEY = "__pkgdb_meta__"
PAYLOAD_KEY = "__pkgdb_payload__"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT = 60
USER_AGENT = "pkgdb/0.1"
FETCH_ATTEMPTS = 3
OUTPUT_PATH = GENERATED_DATA_DIR / "pkg-page-enrichment.json"


class Terminal:
    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode
        self.use_color = (
            not json_mode
            and sys.stderr.isatty()
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb"
        )
        if self.use_color:
            self.bold = "\033[1m"
            self.red = "\033[31m"
            self.green = "\033[32m"
            self.blue = "\033[34m"
            self.reset = "\033[0m"
            self.step = "◆"
            self.ok = "✓"
            self.error = "✗"
        else:
            self.bold = self.red = self.green = self.blue = self.reset = ""
            self.step = ">"
            self.ok = "OK"
            self.error = "ERROR"

    def log(self, message: str = "") -> None:
        if not self.json_mode:
            print(message, file=sys.stderr)

    def step_log(self, message: str) -> None:
        self.log(f"{self.blue}{self.step}{self.reset} {self.bold}{message}{self.reset}")

    def ok_log(self, message: str) -> None:
        self.log(f"  {self.green}{self.ok}{self.reset} {message}")

    def error_log(self, message: str) -> None:
        self.log(f"{self.red}{self.error}{self.reset} {message}")


def ensure_cwd() -> Path:
    scripts_dir = Path(__file__).resolve().parent
    root = scripts_dir.parent
    os.chdir(root)
    return root


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def cache_path_for(url: str, ecosystem: str = ECOSYSTEM) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / ecosystem / f"{digest}.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_cached_json(url: str, ecosystem: str = ECOSYSTEM) -> tuple[Any, dict[str, Any]]:
    data = read_json(cache_path_for(url, ecosystem))
    if isinstance(data, dict) and META_KEY in data and PAYLOAD_KEY in data:
        return data.get(PAYLOAD_KEY), data.get(META_KEY) or {}
    return data, {}


def write_cache(path: Path, payload: Any, etag: str | None, checked_at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                META_KEY: {"etag": etag, "checked_at": checked_at},
                PAYLOAD_KEY: payload,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def parse_http_headers(raw_headers: str) -> tuple[int, dict[str, str]]:
    blocks = [block for block in re.split(r"\r?\n\r?\n", raw_headers) if block.strip()]
    for block in reversed(blocks):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("HTTP/"):
            continue
        match = re.match(r"HTTP/\S+\s+(\d+)", lines[0])
        status = int(match.group(1)) if match else 0
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return status, headers
    return 0, {}


def fetch_url(url: str, headers: dict[str, str], timeout: int = DEFAULT_TIMEOUT) -> tuple[int, dict[str, str], bytes]:
    body_fd, body_name = tempfile.mkstemp(prefix="avdb-enrichment-body-")
    header_fd, header_name = tempfile.mkstemp(prefix="avdb-enrichment-headers-")
    os.close(body_fd)
    os.close(header_fd)
    try:
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--connect-timeout",
            str(timeout),
            "--max-time",
            str(timeout),
            "--dump-header",
            header_name,
            "--output",
            body_name,
        ]
        for key, value in headers.items():
            command.extend(["--header", f"{key}: {value}"])
        command.append(url)
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            message = result.stderr.strip() or f"curl exited with {result.returncode}"
            raise TimeoutError(message) if result.returncode == 28 else OSError(message)
        status, response_headers = parse_http_headers(Path(header_name).read_text(encoding="iso-8859-1"))
        return status, response_headers, Path(body_name).read_bytes()
    finally:
        Path(body_name).unlink(missing_ok=True)
        Path(header_name).unlink(missing_ok=True)


def fetch_json(
    url: str,
    ecosystem: str = ECOSYSTEM,
    force_refresh: bool = False,
    prefer_cache: bool = False,
    cache_only: bool = False,
) -> Any:
    path = cache_path_for(url, ecosystem)
    payload = None
    meta: dict[str, Any] = {}
    if path.exists():
        payload, meta = read_cached_json(url, ecosystem)

    if prefer_cache and payload is not None and not force_refresh:
        return payload
    if cache_only:
        return payload

    checked_at = meta.get("checked_at")
    now = int(time.time())
    if (
        not force_refresh
        and isinstance(checked_at, int)
        and now - checked_at < CHECK_INTERVAL_SECONDS
    ):
        return payload

    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    etag = meta.get("etag")
    if etag:
        headers["If-None-Match"] = etag
    last_error: BaseException | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            status, response_headers, body = fetch_url(url, headers=headers)
            if status == 304 and payload is not None:
                write_cache(path, payload, etag, now)
                return payload
            if status >= 400:
                raise urllib.error.HTTPError(url, status, f"HTTP {status}", hdrs=None, fp=None)
            payload = json.loads(body)
            write_cache(path, payload, response_headers.get("etag"), now)
            return payload
        except urllib.error.HTTPError as err:
            if payload is not None:
                print(f"Using cached data for {url}: {err}", file=sys.stderr)
                return payload
            raise
        except (http.client.IncompleteRead, json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError) as err:
            if payload is not None:
                print(f"Using cached data for {url}: {err}", file=sys.stderr)
                return payload
            last_error = err
            if attempt + 1 < FETCH_ATTEMPTS:
                time.sleep(2**attempt)
    if payload is not None:
        print(f"Using cached data for {url}: {last_error}", file=sys.stderr)
        return payload
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"failed to fetch {url}")


def normalize_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, str) and item:
            result.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("formula")
            if isinstance(name, str) and name:
                result.append(name)
    return sorted(set(result))


def normalize_dependency_names(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(name) for name in value if name)
    return normalize_list(value)


def normalize_string_map(value: Any, limit: int = 24) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, child in sorted(value.items()):
        if len(result) >= limit:
            break
        if child in ("", None, [], {}):
            continue
        rendered = render_string_map_value(child)
        if rendered:
            result[str(key)] = rendered
    return result


def render_string_map_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item or "").strip())
    if isinstance(value, dict):
        return ", ".join(
            f"{name}: {render_string_map_value(item)}"
            for name, item in sorted(value.items())
            if item not in ("", None, [], {})
        )
    return str(value)


def normalize_people(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("name") or item.get("username") or item.get("email") or "").strip()
        else:
            text = ""
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def normalized_dict_keys(value: Any, limit: int = 24) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [str(key) for key in sorted(value)[:limit] if str(key)]


def normalize_license(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " AND ".join(str(item) for item in value if item)
    if not isinstance(value, dict):
        return ""
    if "any_of" in value:
        return " OR ".join(normalize_license(item) for item in value.get("any_of") or [] if normalize_license(item))
    if "all_of" in value:
        return " AND ".join(normalize_license(item) for item in value.get("all_of") or [] if normalize_license(item))
    if "with" in value:
        base = normalize_license(value.get("with"))
        exception = normalize_license(value.get("exception"))
        return f"{base} WITH {exception}" if base and exception else base
    return ""


def stable_version(formula: dict[str, Any]) -> str:
    versions = formula.get("versions") or {}
    value = versions.get("stable") if isinstance(versions, dict) else None
    return value if isinstance(value, str) else ""


def source_archive(formula: dict[str, Any]) -> str:
    urls = formula.get("urls") or {}
    stable = urls.get("stable") if isinstance(urls, dict) else None
    if not isinstance(stable, dict):
        return ""
    url = stable.get("url")
    return url if isinstance(url, str) else ""


def clean_summary(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6])\b[^>]*>", ". ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"<[^>]*$", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"https?://\S*$", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\.\s*){2,}", ". ", text).strip(" ,-")
    match = re.search(
        r"\b([A-Z][A-Za-z0-9 .+/_-]{1,80}\s+is\s+[A-Za-z0-9])",
        text,
    )
    if match and re.search(r"^(npm|npx|pnpm|yarn|bun|brew|pip|uv)\s+", text, flags=re.IGNORECASE):
        text = text[match.start():]
    return text[:720].rsplit(" ", 1)[0].strip(" ,-") if len(text) > 720 else text


def normalize_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "web", "browse"):
            child = value.get(key)
            if isinstance(child, str) and child.strip():
                return child.strip()
    return ""


def normalize_repository(value: Any) -> str:
    url = normalize_url(value)
    if not url:
        return ""
    url = re.sub(r"^git\+", "", url)
    url = re.sub(r"^git://github\.com/", "https://github.com/", url)
    url = re.sub(r"^ssh://git@github\.com/", "https://github.com/", url)
    url = re.sub(r"^git@github\.com:", "https://github.com/", url)
    return re.sub(r"\.git$", "", url)


def npm_package_url(name: str) -> str:
    return NPM_PACKAGE_URL.format(name=urllib.parse.quote(name, safe="@"))


def pypi_package_url(name: str) -> str:
    return PYPI_PACKAGE_URL.format(name=urllib.parse.quote(name, safe=""))


def npm_latest_version(payload: dict[str, Any]) -> str:
    dist_tags = payload.get("dist-tags") or {}
    latest = dist_tags.get("latest") if isinstance(dist_tags, dict) else None
    if isinstance(latest, str) and latest:
        return latest
    versions = payload.get("versions") or {}
    if isinstance(versions, dict) and versions:
        return sorted(versions)[-1]
    return ""


def npm_latest_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    version = npm_latest_version(payload)
    versions = payload.get("versions") or {}
    manifest = versions.get(version) if isinstance(versions, dict) else None
    return manifest if isinstance(manifest, dict) else {}


def npm_executable_records(manifest: dict[str, Any], fallback: str = "") -> list[dict[str, str]]:
    binaries = manifest.get("bin")
    records: list[dict[str, str]] = []
    if isinstance(binaries, str):
        name = fallback or manifest.get("name") or ""
        if isinstance(name, str) and name:
            records.append({"name": name, "kind": "cli", "exposure": "global executable", "source": binaries})
    elif isinstance(binaries, dict):
        for name, target in sorted(binaries.items()):
            if isinstance(name, str) and name:
                record = {"name": name, "kind": "cli", "exposure": "global executable"}
                if isinstance(target, str) and target:
                    record["source"] = target
                records.append(record)
    elif fallback:
        records.append({"name": fallback, "kind": "cli", "exposure": "global executable"})
    return records


def npm_install_behavior(manifest: dict[str, Any]) -> dict[str, Any]:
    scripts = manifest.get("scripts") or {}
    lifecycle = []
    if isinstance(scripts, dict):
        for name in ("preinstall", "install", "postinstall", "prepublish", "prepare"):
            if scripts.get(name):
                lifecycle.append(name)
    return {
        "lifecycleScripts": lifecycle,
        "postInstallDefined": "postinstall" in lifecycle,
        "prepareDefined": "prepare" in lifecycle,
    }


def npm_license(payload: dict[str, Any], manifest: dict[str, Any]) -> str:
    return normalize_license(manifest.get("license") or payload.get("license"))


def pypi_project_url(info: dict[str, Any], names: tuple[str, ...]) -> str:
    project_urls = info.get("project_urls") or {}
    if not isinstance(project_urls, dict):
        return ""
    lower = {str(key).lower(): value for key, value in project_urls.items()}
    for name in names:
        value = normalize_url(lower.get(name.lower()))
        if value:
            return value
    return ""


def pypi_repository(info: dict[str, Any]) -> str:
    return normalize_repository(pypi_project_url(info, ("source", "source code", "repository", "github", "code")))


def pypi_license(info: dict[str, Any]) -> str:
    license_text = normalize_license(info.get("license"))
    if license_text:
        return license_text
    classifiers = info.get("classifiers") or []
    if isinstance(classifiers, list):
        for classifier in classifiers:
            if isinstance(classifier, str) and classifier.startswith("License ::"):
                return classifier.rsplit("::", 1)[-1].strip()
    return ""


def pypi_dependency_name(requirement: str) -> str:
    requirement = requirement.split(";", 1)[0].strip()
    requirement = re.split(r"\s|\[|<|>|=|!|~", requirement, maxsplit=1)[0].strip()
    return requirement


def pypi_dependencies(info: dict[str, Any]) -> list[str]:
    requires = info.get("requires_dist") or []
    if not isinstance(requires, list):
        return []
    return sorted({name for item in requires if isinstance(item, str) for name in [pypi_dependency_name(item)] if name})


def pypi_source_archive(payload: dict[str, Any], version: str) -> str:
    urls = payload.get("urls") or []
    if not isinstance(urls, list):
        return ""
    candidates = [item for item in urls if isinstance(item, dict) and item.get("url")]
    for packagetype in ("sdist", "bdist_wheel"):
        for item in candidates:
            if item.get("packagetype") == packagetype:
                return str(item.get("url"))
    releases = payload.get("releases") or {}
    release_urls = releases.get(version) if isinstance(releases, dict) else None
    if isinstance(release_urls, list):
        for item in release_urls:
            if isinstance(item, dict) and item.get("url"):
                return str(item.get("url"))
    return ""


def pypi_upload_time(payload: dict[str, Any], version: str) -> str:
    urls = payload.get("urls") or []
    if isinstance(urls, list):
        for item in urls:
            if isinstance(item, dict) and item.get("upload_time_iso_8601"):
                return str(item["upload_time_iso_8601"])
    releases = payload.get("releases") or {}
    release_urls = releases.get(version) if isinstance(releases, dict) else None
    if isinstance(release_urls, list):
        for item in release_urls:
            if isinstance(item, dict) and item.get("upload_time_iso_8601"):
                return str(item["upload_time_iso_8601"])
    return ""


def pypi_executable_records(name: str, info: dict[str, Any]) -> list[dict[str, str]]:
    summary = str(info.get("summary") or "").lower()
    if name.endswith("cli") or "command line" in summary or "command-line" in summary or " cli" in summary:
        return [{"name": name, "kind": "cli", "exposure": "console script"}]
    return []


def bottle_metadata(formula: dict[str, Any]) -> dict[str, Any]:
    bottle = formula.get("bottle") or {}
    stable = bottle.get("stable") if isinstance(bottle, dict) else None
    if not isinstance(stable, dict):
        return {"available": False}
    files = stable.get("files") or {}
    platforms = sorted(str(key) for key in files if key) if isinstance(files, dict) else []
    result: dict[str, Any] = {"available": bool(platforms)}
    root_url = stable.get("root_url")
    if isinstance(root_url, str) and root_url:
        result["rootUrl"] = root_url
    if platforms:
        result["platforms"] = platforms
    return result


def install_behavior(formula: dict[str, Any]) -> dict[str, Any]:
    behavior: dict[str, Any] = {
        "postInstallDefined": bool(formula.get("post_install_defined")),
        "service": "declared" if formula.get("service") else None,
    }
    caveats = formula.get("caveats")
    if isinstance(caveats, str) and caveats.strip():
        behavior["caveats"] = re.sub(r"\s+", " ", caveats).strip()
    return behavior


def formula_registry_insights(formula: dict[str, Any]) -> dict[str, Any]:
    versions = formula.get("versions") if isinstance(formula.get("versions"), dict) else {}
    bottle = formula.get("bottle") if isinstance(formula.get("bottle"), dict) else {}
    urls = formula.get("urls") if isinstance(formula.get("urls"), dict) else {}
    insights = {
        "sourceDatabase": "Homebrew formula API",
        "tap": formula.get("tap"),
        "fullName": formula.get("full_name"),
        "oldName": formula.get("oldname"),
        "aliases": normalize_list(formula.get("aliases"))[:16],
        "versionScheme": formula.get("version_scheme"),
        "revision": formula.get("revision"),
        "headVersion": versions.get("head") if isinstance(versions, dict) else "",
        "bottleStableRootUrl": ((bottle.get("stable") or {}).get("root_url") if isinstance(bottle.get("stable"), dict) else ""),
        "urlKeys": normalized_dict_keys(urls),
        "requirements": normalize_list(formula.get("requirements"))[:16],
        "conflictsWith": normalize_list(formula.get("conflicts_with"))[:16],
        "kegOnly": formula.get("keg_only"),
        "deprecated": formula.get("deprecated"),
        "disabled": formula.get("disabled"),
    }
    return insights


def cask_artifact_name(value: Any) -> str:
    if isinstance(value, str):
        return os.path.basename(value)
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str):
            return os.path.basename(first)
    if isinstance(value, dict):
        for key in ("target", "source"):
            child = value.get(key)
            if isinstance(child, str) and child:
                return os.path.basename(child)
    return ""


def cask_binary_records(cask: dict[str, Any]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for artifact in cask.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        if "binary" in artifact:
            name = cask_artifact_name(artifact.get("binary"))
            if name:
                records.append({"source": name, "target": name})
    return records


def cask_artifact_summary(cask: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for artifact in cask.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        for key in artifact:
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def cask_install_behavior(cask: dict[str, Any]) -> dict[str, Any]:
    caveats = cask.get("caveats")
    behavior: dict[str, Any] = {
        "autoUpdates": cask.get("auto_updates"),
        "artifacts": cask_artifact_summary(cask),
        "postInstallDefined": False,
    }
    if isinstance(caveats, str) and caveats.strip():
        behavior["caveats"] = re.sub(r"\s+", " ", caveats).strip()
    if cask.get("uninstall"):
        behavior["uninstallDefined"] = True
    if cask.get("zap"):
        behavior["zapDefined"] = True
    return behavior


def cask_registry_insights(cask: dict[str, Any]) -> dict[str, Any]:
    depends_on = cask.get("depends_on") if isinstance(cask.get("depends_on"), dict) else {}
    conflicts_with = cask.get("conflicts_with") if isinstance(cask.get("conflicts_with"), dict) else {}
    container = cask.get("container") if isinstance(cask.get("container"), dict) else {}
    insights = {
        "sourceDatabase": "Homebrew cask API",
        "tap": cask.get("tap"),
        "fullToken": cask.get("full_token"),
        "names": normalize_list(cask.get("name"))[:12],
        "oldTokens": normalize_list(cask.get("old_tokens"))[:16],
        "dependsOn": normalize_string_map(depends_on),
        "conflictsWith": normalize_string_map(conflicts_with),
        "container": normalize_string_map(container),
        "artifacts": cask_artifact_summary(cask),
        "autoUpdates": cask.get("auto_updates"),
        "deprecated": cask.get("deprecated"),
        "disabled": cask.get("disabled"),
    }
    return insights


def executable_index(db: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, set[str]] = {}
    entries = db.get("entries") or {}
    if not isinstance(entries, dict):
        return {}
    for executable, provider_key in entries.items():
        if not isinstance(executable, str) or not executable:
            continue
        if not isinstance(provider_key, str) or not provider_key:
            continue
        if ":" in provider_key:
            provider, name = provider_key.split(":", 1)
            if provider == "formula":
                provider = "brew"
        else:
            provider, name = "brew", provider_key
        if provider == "brew" and name:
            result.setdefault(name, set()).add(executable)
    return {name: sorted(executables) for name, executables in result.items()}


def executable_records(name: str, executables: dict[str, list[str]]) -> list[dict[str, str]]:
    return [
        {
            "name": executable,
            "kind": "cli",
            "exposure": "global executable",
        }
        for executable in executables.get(name, [])
    ]


def formula_enrichment(formula: dict[str, Any], executables: dict[str, list[str]]) -> tuple[str, dict[str, Any]] | None:
    name = formula.get("name")
    if not isinstance(name, str) or not name:
        return None
    if formula.get("disabled"):
        return None

    entry: dict[str, Any] = {
        "package": {
            "provider": "brew",
            "name": name,
            "packageManager": "Homebrew",
            "packageManagerUrl": f"https://formulae.brew.sh/formula/{name}",
        },
        "version": stable_version(formula),
        "homepage": formula.get("homepage") if isinstance(formula.get("homepage"), str) else "",
        "license": normalize_license(formula.get("license")),
        "sourceArchive": source_archive(formula),
        "dependencies": normalize_list(formula.get("dependencies")),
        "buildDependencies": normalize_list(formula.get("build_dependencies")),
        "usesFromMacos": normalize_list(formula.get("uses_from_macos")),
        "bottle": bottle_metadata(formula),
        "installBehavior": install_behavior(formula),
        "executables": executable_records(name, executables),
        "registryInsights": formula_registry_insights(formula),
    }
    return f"brew:{name}", prune(entry)


def cask_enrichment(cask: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    token = cask.get("token")
    if not isinstance(token, str) or not token:
        return None
    if cask.get("disabled") or cask.get("deprecated"):
        return None
    depends_on = cask.get("depends_on") if isinstance(cask.get("depends_on"), dict) else {}
    formula_dependencies = normalize_list(depends_on.get("formula") if isinstance(depends_on, dict) else [])
    cask_dependencies = normalize_list(depends_on.get("cask") if isinstance(depends_on, dict) else [])
    entry: dict[str, Any] = {
        "package": {
            "provider": "cask",
            "name": token,
            "packageManager": "Homebrew Cask",
            "packageManagerUrl": f"https://formulae.brew.sh/cask/{token}",
        },
        "version": cask.get("version") if isinstance(cask.get("version"), str) else "",
        "summary": clean_summary(cask.get("desc") if isinstance(cask.get("desc"), str) else ""),
        "homepage": cask.get("homepage") if isinstance(cask.get("homepage"), str) else "",
        "sourceArchive": cask.get("url") if isinstance(cask.get("url"), str) else "",
        "dependencies": [*formula_dependencies, *cask_dependencies],
        "binaries": cask_binary_records(cask),
        "installBehavior": cask_install_behavior(cask),
        "registryInsights": cask_registry_insights(cask),
    }
    if isinstance(cask.get("sha256"), str):
        entry["sha256"] = cask.get("sha256")
    return f"cask:{token}", prune(entry)


def npm_registry_insights(payload: dict[str, Any], manifest: dict[str, Any], latest: str) -> dict[str, Any]:
    times = payload.get("time") if isinstance(payload.get("time"), dict) else {}
    dist = manifest.get("dist") if isinstance(manifest.get("dist"), dict) else {}
    insights = {
        "sourceDatabase": "npm registry",
        "createdAt": times.get("created"),
        "modifiedAt": times.get("modified"),
        "latestPublishedAt": times.get(latest),
        "distTags": normalize_string_map(payload.get("dist-tags")),
        "versionCount": len(payload.get("versions") or {}) if isinstance(payload.get("versions"), dict) else 0,
        "maintainers": normalize_people(payload.get("maintainers")),
        "author": ", ".join(normalize_people(manifest.get("author") or payload.get("author"), 1)),
        "publisher": ", ".join(normalize_people((payload.get("_npmUser") or manifest.get("_npmUser")), 1)),
        "engines": normalize_string_map(manifest.get("engines")),
        "peerDependencies": normalize_dependency_names(manifest.get("peerDependencies"))[:24],
        "optionalDependencies": normalize_dependency_names(manifest.get("optionalDependencies"))[:24],
        "funding": normalize_url(manifest.get("funding") or payload.get("funding")),
        "integrity": dist.get("integrity"),
        "shasum": dist.get("shasum"),
        "unpackedSize": dist.get("unpackedSize"),
        "fileCount": len(manifest.get("files") or []) if isinstance(manifest.get("files"), list) else 0,
    }
    return insights


def npm_enrichment(name: str, db_info: dict[str, Any], payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if not name:
        return None
    manifest = npm_latest_manifest(payload)
    payload_latest = npm_latest_version(payload)
    db_latest = str(db_info.get("version") or "")
    latest = db_latest or payload_latest or ""
    payload_matches_db = not db_latest or not payload_latest or db_latest == payload_latest
    if not manifest and latest:
        manifest = {
            "name": name,
            "version": latest,
            "description": db_info.get("summary") or "",
            "homepage": db_info.get("homepage") or "",
        }
    if not payload_matches_db:
        manifest = {
            "name": name,
            "version": latest,
            "description": db_info.get("summary") or payload.get("description") or "",
            "homepage": db_info.get("homepage") or payload.get("homepage") or "",
        }
    executable = db_info.get("executable") if isinstance(db_info.get("executable"), str) else ""
    homepage = normalize_url(manifest.get("homepage")) or normalize_url(payload.get("homepage")) or str(db_info.get("homepage") or "")
    repository = normalize_repository(manifest.get("repository") or payload.get("repository"))
    bugs = normalize_url(manifest.get("bugs") or payload.get("bugs"))
    tarball = ""
    if payload_matches_db:
        tarball = normalize_url((manifest.get("dist") or {}).get("tarball") if isinstance(manifest.get("dist"), dict) else "")
    registry_insights = npm_registry_insights(payload, manifest, latest)
    if not payload_matches_db:
        dist_tags = registry_insights.get("distTags")
        if not isinstance(dist_tags, dict):
            dist_tags = {}
        dist_tags["latest"] = latest
        registry_insights["distTags"] = dist_tags
        if db_info.get("last_updated_at"):
            registry_insights["modifiedAt"] = db_info.get("last_updated_at")
            registry_insights["latestPublishedAt"] = db_info.get("last_updated_at")
    entry: dict[str, Any] = {
        "package": {
            "provider": "npm",
            "name": name,
            "packageManager": "npm",
            "packageManagerUrl": f"https://www.npmjs.com/package/{urllib.parse.quote(name, safe='@/')}",
        },
        "version": latest,
        "summary": clean_summary(manifest.get("description") or payload.get("description") or db_info.get("summary") or ""),
        "homepage": homepage,
        "repository": repository,
        "upstreamDocs": homepage,
        "license": npm_license(payload, manifest),
        "sourceArchive": tarball,
        "dependencies": normalize_dependency_names(manifest.get("dependencies")),
        "buildDependencies": normalize_dependency_names(manifest.get("devDependencies")),
        "executables": npm_executable_records(manifest, executable),
        "installBehavior": npm_install_behavior(manifest),
        "publishedAt": (
            ((payload.get("time") or {}).get(latest) if isinstance(payload.get("time"), dict) else "")
            if payload_matches_db
            else str(db_info.get("last_updated_at") or "")
        ),
        "registryInsights": registry_insights,
    }
    keywords = manifest.get("keywords")
    if isinstance(keywords, list):
        entry["keywords"] = [str(item) for item in keywords if isinstance(item, str) and item][:16]
    if bugs:
        entry["issueTracker"] = bugs
    return f"npm:{name}", prune(entry)


def pypi_registry_insights(payload: dict[str, Any], info: dict[str, Any], version: str) -> dict[str, Any]:
    urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
    releases = payload.get("releases") if isinstance(payload.get("releases"), dict) else {}
    package_types = sorted({
        str(item.get("packagetype"))
        for item in urls
        if isinstance(item, dict) and item.get("packagetype")
    })
    yanked = [item for item in urls if isinstance(item, dict) and item.get("yanked")]
    insights = {
        "sourceDatabase": "PyPI JSON API",
        "author": info.get("author"),
        "authorEmail": info.get("author_email"),
        "maintainer": info.get("maintainer"),
        "maintainerEmail": info.get("maintainer_email"),
        "requiresPython": info.get("requires_python"),
        "keywords": string_keywords(info.get("keywords")),
        "platforms": normalize_list(info.get("platform"))[:12],
        "releaseCount": len(releases),
        "filesForLatest": len(urls),
        "packageTypes": package_types,
        "yankedFileCount": len(yanked),
        "vulnerabilityCount": len(payload.get("vulnerabilities") or []) if isinstance(payload.get("vulnerabilities"), list) else 0,
        "latestSerial": payload.get("last_serial"),
        "latestUploadAt": pypi_upload_time(payload, version),
    }
    return insights


def string_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()][:24]
    if not isinstance(value, str):
        return []
    return [item for item in re.split(r",|\s+", value) if item][:24]


def pypi_enrichment(name: str, overlay: dict[str, Any], payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if not name:
        return None
    info = payload.get("info") or {}
    if not isinstance(info, dict):
        return None
    version = str(info.get("version") or "")
    homepage = normalize_url(info.get("home_page")) or pypi_project_url(info, ("homepage", "home", "documentation", "docs"))
    repository = pypi_repository(info)
    docs = pypi_project_url(info, ("documentation", "docs", "homepage", "home")) or homepage
    issue_tracker = pypi_project_url(info, ("issues", "issue tracker", "bug tracker", "bugs"))
    entry: dict[str, Any] = {
        "package": {
            "provider": "pip",
            "name": name,
            "packageManager": "PyPI",
            "packageManagerUrl": f"https://pypi.org/project/{urllib.parse.quote(name, safe='')}/",
        },
        "version": version,
        "summary": clean_summary(info.get("summary") if isinstance(info.get("summary"), str) else ""),
        "homepage": homepage,
        "repository": repository,
        "upstreamDocs": docs,
        "license": pypi_license(info),
        "sourceArchive": pypi_source_archive(payload, version),
        "dependencies": pypi_dependencies(info),
        "executables": pypi_executable_records(name, info),
        "publishedAt": pypi_upload_time(payload, version),
        "installBehavior": {
            "pythonRequires": info.get("requires_python") if isinstance(info.get("requires_python"), str) else "",
            "requiresDistCount": len(pypi_dependencies(info)),
        },
        "registryInsights": pypi_registry_insights(payload, info, version),
    }
    classifiers = info.get("classifiers") or []
    if isinstance(classifiers, list):
        entry["classifiers"] = [str(item) for item in classifiers if isinstance(item, str)][:16]
    project_urls = info.get("project_urls") or {}
    if isinstance(project_urls, dict):
        entry["projectUrls"] = {str(key): str(value) for key, value in sorted(project_urls.items()) if value}
    if issue_tracker:
        entry["issueTracker"] = issue_tracker
    if overlay.get("homebrewDeps"):
        entry["homebrewDependencies"] = overlay.get("homebrewDeps")
    if overlay.get("pythonFormula"):
        entry["pythonFormula"] = overlay.get("pythonFormula")
    return f"pip:{name}", prune(entry)


def prune(value: Any) -> Any:
    if isinstance(value, dict):
        pruned = {}
        for key, child in value.items():
            child = prune(child)
            if child is not None:
                pruned[key] = child
        return pruned or None
    if isinstance(value, list):
        pruned = []
        for child in value:
            child = prune(child)
            if child is not None:
                pruned.append(child)
        return pruned or None
    if value in ("", [], None):
        return None
    return value


def build_enrichment(
    formulae: list[Any],
    casks: list[Any],
    db: dict[str, Any],
    npm_payloads: dict[str, Any] | None = None,
    pip_overlays: dict[str, Any] | None = None,
    pypi_payloads: dict[str, Any] | None = None,
) -> dict[str, Any]:
    executables = executable_index(db)
    packages: dict[str, Any] = {}
    for formula in formulae:
        if not isinstance(formula, dict):
            continue
        enriched = formula_enrichment(formula, executables)
        if enriched is None:
            continue
        key, entry = enriched
        packages[key] = entry
    for cask in casks:
        if not isinstance(cask, dict):
            continue
        enriched = cask_enrichment(cask)
        if enriched is None:
            continue
        key, entry = enriched
        packages[key] = entry
    npm_payloads = npm_payloads or {}
    npms = db.get("npms") or {}
    if isinstance(npms, dict):
        for name, info in sorted(npms.items()):
            if not isinstance(name, str) or not isinstance(info, dict):
                continue
            payload = npm_payloads.get(name)
            if not isinstance(payload, dict):
                continue
            enriched = npm_enrichment(name, info, payload)
            if enriched is None:
                continue
            key, entry = enriched
            packages[key] = entry
    pip_overlays = pip_overlays or {}
    pypi_payloads = pypi_payloads or {}
    for name, overlay in sorted(pip_overlays.items()):
        if not isinstance(name, str) or not isinstance(overlay, dict):
            continue
        payload = pypi_payloads.get(name)
        if not isinstance(payload, dict):
            continue
        enriched = pypi_enrichment(name, overlay, payload)
        if enriched is None:
            continue
        key, entry = enriched
        packages[key] = entry
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "packages": packages,
    }


def expected_enrichment(
    force_refresh: bool = False,
    registry_cache_only: bool = False,
    previous_packages: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Cache-only applies to npm/PyPI; Homebrew snapshots must still revalidate.
    formulae = fetch_json(
        FORMULA_URL,
        force_refresh=force_refresh,
    )
    if not isinstance(formulae, list):
        raise ValueError("Homebrew formula API payload must be a list")
    casks = fetch_json(
        CASK_URL,
        force_refresh=force_refresh,
    )
    if not isinstance(casks, list):
        raise ValueError("Homebrew cask API payload must be a list")
    db = read_json(PACKAGE_INDEX_PATH)
    if not isinstance(db, dict):
        raise ValueError(f"{PACKAGE_INDEX_PATH} must contain an object")
    enrichment = build_enrichment(
        formulae,
        casks,
        db,
    )
    packages = enrichment["packages"]

    # Registry responses (especially npm packuments) can be several megabytes
    # each. Transform them one at a time instead of retaining every raw payload
    # alongside the complete output artifact.
    npms = db.get("npms") or {}
    del formulae, casks, db
    gc.collect()
    if isinstance(npms, dict):
        for name, info in sorted(npms.items()):
            if not isinstance(name, str) or not name or not isinstance(info, dict):
                continue
            payload = fetch_json(
                npm_package_url(name),
                ecosystem="registry.npmjs.org",
                prefer_cache=True,
                cache_only=registry_cache_only,
            )
            if isinstance(payload, dict):
                enriched = npm_enrichment(name, info, payload)
                if enriched is not None:
                    key, entry = enriched
                    packages[key] = entry
            del payload

    pip_overlays = read_json(Path("data/pip.json"))
    if isinstance(pip_overlays, dict):
        for name, overlay in sorted(pip_overlays.items()):
            if not isinstance(name, str) or not name or not isinstance(overlay, dict):
                continue
            payload = fetch_json(
                pypi_package_url(name),
                ecosystem="pypi.org",
                prefer_cache=True,
                cache_only=registry_cache_only,
            )
            if isinstance(payload, dict):
                enriched = pypi_enrichment(name, overlay, payload)
                if enriched is not None:
                    key, entry = enriched
                    packages[key] = entry
            del payload

    if registry_cache_only and previous_packages:
        for key, value in previous_packages.items():
            if (
                isinstance(key, str)
                and key not in packages
                and key.startswith(("npm:", "pip:"))
            ):
                packages[key] = value
    return enrichment


def check_current(path: Path, terminal: Terminal) -> int:
    if not path.exists():
        terminal.error_log(f"Missing {path}. Run scripts/generate-pkg-page-enrichment.py.")
        return 1
    try:
        current = read_json(path)
        expected = expected_enrichment()
    except (OSError, ValueError, json.JSONDecodeError) as err:
        terminal.error_log(f"Unable to validate {path}: {err}")
        return 1
    failures = []
    if current.get("schema") != SCHEMA_VERSION:
        failures.append(f"schema is {current.get('schema')!r}, expected {SCHEMA_VERSION}")
    if not current.get("generated_at"):
        failures.append("missing generated_at")
    if current.get("packages") != expected.get("packages"):
        failures.append(f"package enrichment does not match current Homebrew formula data and {PACKAGE_INDEX_PATH}")
    if failures:
        terminal.error_log("Package-origin enrichment is stale.")
        for failure in failures:
            terminal.log(f"  - {failure}")
        terminal.log("Run scripts/generate-pkg-page-enrichment.py, then rebuild the package-origin SQLite artifact.")
        return 1
    terminal.ok_log(f"Package-origin enrichment is current ({len(current.get('packages') or {}):,} packages)")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Homebrew package-page enrichment data.")
    parser.add_argument("--check", action="store_true", help="Validate that the output already matches current inputs.")
    parser.add_argument("--refresh", action="store_true", help="Refresh cached Homebrew formula API data.")
    parser.add_argument(
        "--registry-cache-only",
        action="store_true",
        help="Reuse cached npm/PyPI registry payloads and preserve existing registry entries when caches are missing.",
    )
    parser.add_argument("--output", default=str(OUTPUT_PATH), help=f"Path to write or validate. Defaults to {OUTPUT_PATH}.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable status and disable terminal styling.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_cwd()
    terminal = Terminal(json_mode=args.json)
    output_path = Path(args.output)
    if args.check:
        return check_current(output_path, terminal)
    try:
        terminal.step_log("Building Homebrew package-page enrichment")
        previous_packages = None
        if args.registry_cache_only and output_path.exists():
            current = read_json(output_path)
            existing_packages = current.get("packages") if isinstance(current, dict) else None
            if isinstance(existing_packages, dict):
                previous_packages = existing_packages
        enrichment = expected_enrichment(
            force_refresh=args.refresh,
            registry_cache_only=args.registry_cache_only,
            previous_packages=previous_packages,
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as err:
        terminal.error_log(f"Failed to build enrichment: {err}")
        return 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(enrichment, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    terminal.ok_log(f"Wrote {len(enrichment.get('packages') or {}):,} package enrichments to {output_path}")
    if args.json:
        print(json.dumps({"ok": True, "output": str(output_path), "package_count": len(enrichment.get("packages") or {})}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
