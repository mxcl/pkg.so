from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path
from typing import Any

from .brew import source_archive, stable_version
from .discovery import overlay
from .common import CACHE_DIR, COMBINED_DIR, ROOT, read_json, write_json
from .casks import read_cask_cache, read_cask_catalog
from .crates import CRATES_IO_INDEX_PATH
from .executables import executable_entries_from_index, executable_index_from_project_yaml
from .render import parse_simple_yaml, read_formula_cache


DB_SCHEMA_VERSION = 8
AUTOMIC_VAULT_CACHE_DIR = CACHE_DIR / "automic-vault"
AUTOMIC_VAULT_DB_PATH = AUTOMIC_VAULT_CACHE_DIR / "db.json"
NPM_INDEX_STATE_PATH = CACHE_DIR / "npmjs" / "index.json"
PKG_TAXONOMY_PATH = ROOT / "data" / "pkg-taxonomy.json"
PKG_ECOSYSTEM_TAXONOMY_PATH = ROOT / "data" / "pkg-ecosystem-taxonomy.json"
HOMEBREW_CORE_REPO = "Homebrew/homebrew-core"
HOMEBREW_CASK_REPO = "Homebrew/homebrew-cask"
PULSE_NEW_WINDOW_DAYS = 7
PULSE_HISTORY_WINDOW_DAYS = 90
PULSE_KINDS = {"new", "updated"}
PACKAGE_TAXONOMY_FIELDS = (
    "displayName",
    "category",
    "categoryPath",
    "categoryConfidence",
    "categorySources",
    "tags",
    "tagsConfidence",
    "tagsSources",
)


def formula_lookup(formulae: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for formula in formulae:
        name = formula.get("name")
        if isinstance(name, str) and name:
            result[name] = formula
    return result


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, str) and item})


def formula_metadata_from_record(record: dict[str, Any], formula: dict[str, Any] | None) -> dict[str, Any]:
    docs = string_list(record.get("docs"))
    repo = str(record.get("repo") or "")
    metadata = {
        "summary": str(record.get("description") or ""),
        "homepage": str(record.get("homepage") or ""),
        "repository": repo,
        "docs": docs,
        "upstreamDocs": docs[0] if docs else "",
        "category": str(record.get("category") or ""),
        "version": stable_version(formula or {}),
        "sourceArchive": source_archive(formula or {}),
        "aliases": string_list((formula or {}).get("aliases")),
        "oldnames": string_list((formula or {}).get("oldnames")),
    }
    return {key: value for key, value in metadata.items() if value or key == "summary"}


def parse_timestamp(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_recent_timestamp(value: Any, cutoff: datetime.datetime) -> bool:
    parsed = parse_timestamp(value)
    return parsed is not None and parsed >= cutoff


def formula_source_path(name: str, formula: dict[str, Any] | None = None) -> str:
    source_path = (formula or {}).get("ruby_source_path")
    if isinstance(source_path, str) and source_path:
        return source_path
    return f"Formula/{name[0]}/{name}.rb"


def cask_source_path(token: str, cask: dict[str, Any] | None = None) -> str:
    source_path = (cask or {}).get("ruby_source_path")
    if isinstance(source_path, str) and source_path:
        return source_path
    return f"Casks/{token[0]}/{token}.rb"


def git_repo_cache_path(repo: str) -> Path:
    return CACHE_DIR / "brew.sh" / "git" / repo.rsplit("/", 1)[-1]


def ensure_git_repo(repo: str) -> Path | None:
    path = git_repo_cache_path(repo)
    url = f"https://github.com/{repo}.git"
    try:
        if path.exists():
            subprocess.run(
                ["git", "-C", str(path), "fetch", "--quiet", "--filter=blob:none", "origin"],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--filter=blob:none", "--no-checkout", url, str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
    except (OSError, subprocess.CalledProcessError) as err:
        if path.exists():
            print(f"Using stale Homebrew git cache for {repo}: {err}", file=sys.stderr)
            return path
        print(f"Skipping Homebrew pulse metadata for {repo}: {err}", file=sys.stderr)
        return None
    return path


def git_default_revision(repo_path: Path) -> str | None:
    for candidate in (
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
        "refs/remotes/origin/master",
    ):
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", candidate],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return candidate
    return None


def git_pulse_events(repo: str, keyed_paths: dict[str, str], scope: str) -> dict[str, dict[str, str]]:
    if not keyed_paths:
        return {}

    repo_path = ensure_git_repo(repo)
    if repo_path is None:
        return {}
    revision = git_default_revision(repo_path)
    if revision is None:
        print(f"Skipping Homebrew pulse metadata for {repo}: no fetched revision", file=sys.stderr)
        return {}

    now = datetime.datetime.now(datetime.timezone.utc)
    new_cutoff = now - datetime.timedelta(days=PULSE_NEW_WINDOW_DAYS)
    history_cutoff = now - datetime.timedelta(days=PULSE_HISTORY_WINDOW_DAYS)
    pending_latest = set(keyed_paths.keys())
    pending_additions = set(keyed_paths.keys())
    events: dict[str, dict[str, str]] = {}
    current_date: str | None = None
    current_datetime: datetime.datetime | None = None
    recent_additions: set[str] = set()
    command = [
        "git",
        "-C",
        str(repo_path),
        "log",
        revision,
        f"--since={history_cutoff.isoformat()}",
        "--format=__DATE__%cI",
        "--name-status",
        "--",
        scope,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as err:
        print(f"Skipping Homebrew pulse metadata for {repo}: {err}", file=sys.stderr)
        return {}
    try:
        if process.stdout is None:
            return {}
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            if line.startswith("__DATE__"):
                current_date = line[len("__DATE__") :]
                current_datetime = parse_timestamp(current_date)
                continue
            if current_date is None:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0]
            path = parts[-1]
            if path not in keyed_paths:
                continue
            key = keyed_paths[path]
            if path in pending_latest:
                events[key] = {
                    "last_updated_at": current_date,
                    "pulse_kind": "updated",
                }
                pending_latest.remove(path)
            if path in pending_additions:
                if status.startswith("A") and current_datetime is not None and current_datetime >= new_cutoff:
                    recent_additions.add(key)
                if status.startswith("A") or current_datetime is None or current_datetime < new_cutoff:
                    pending_additions.remove(path)
            if not pending_latest and (current_datetime is None or current_datetime < new_cutoff):
                process.terminate()
                break
    finally:
        stdout, stderr = process.communicate()
        if process.returncode not in (0, -15):
            message = stderr.strip() or stdout.strip() or f"git log failed for {repo}"
            print(f"Skipping Homebrew pulse metadata for {repo}: {message}", file=sys.stderr)
            return {}

    for key in recent_additions:
        if key in events:
            events[key]["pulse_kind"] = "new"
    return events


def apply_pulse_event(metadata: dict[str, Any], event: dict[str, str] | None) -> dict[str, Any]:
    if not event:
        return metadata
    last_updated_at = event.get("last_updated_at")
    pulse_kind = event.get("pulse_kind")
    if isinstance(last_updated_at, str) and last_updated_at:
        metadata["last_updated_at"] = last_updated_at
    if pulse_kind in PULSE_KINDS:
        metadata["pulse_kind"] = pulse_kind
    return metadata


def formula_metadata_from_project_yaml(root: Path = COMBINED_DIR, formulae: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    formulae_by_name = formula_lookup(formulae if formulae is not None else read_formula_cache())
    result: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return result
    for path in sorted(root.glob("*.yml")):
        record = parse_simple_yaml(path.read_text(encoding="utf-8"))
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier.startswith("brew:"):
            continue
        name = identifier.split(":", 1)[1]
        if name:
            result[name] = formula_metadata_from_record(record, formulae_by_name.get(name))
    return result


def cask_metadata_from_project_yaml(root: Path = COMBINED_DIR) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return result
    for path in sorted(root.glob("*.yml")):
        record = parse_simple_yaml(path.read_text(encoding="utf-8"))
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier.startswith("cask:"):
            continue
        token = identifier.split(":", 1)[1]
        docs = string_list(record.get("docs"))
        metadata = {
            "displayName": record.get("display-name"),
            "repository": record.get("repo"),
            "docs": docs,
            "upstreamDocs": docs[0] if docs else "",
            "category": record.get("category"),
            "tags": string_list(record.get("tags")),
            "configFileLocation": record.get("config-file-location"),
            "credentialsFileLocation": record.get("credentials-file-location"),
            "history": record.get("history"),
        }
        if token:
            result[token] = {key: value for key, value in metadata.items() if value not in (None, "", [], {})}
    return result


def cask_lookup(casks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for cask in casks:
        token = cask.get("token")
        if isinstance(token, str) and token:
            result[token] = cask
    return result


def overlay_homebrew_pulse_metadata(
    formulas: dict[str, dict[str, Any]],
    casks: dict[str, dict[str, Any]],
    formulae: list[dict[str, Any]],
    cask_records: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    formulae_by_name = formula_lookup(formulae)
    casks_by_token = cask_lookup(cask_records if cask_records is not None else read_cask_cache())
    formula_paths = {
        formula_source_path(name, formulae_by_name.get(name)): name
        for name in formulas
        if isinstance(name, str) and name and "/" not in name
    }
    cask_paths = {
        cask_source_path(token, casks_by_token.get(token)): token
        for token in casks
        if isinstance(token, str) and token and "/" not in token
    }
    formula_events = git_pulse_events(HOMEBREW_CORE_REPO, formula_paths, "Formula")
    cask_events = git_pulse_events(HOMEBREW_CASK_REPO, cask_paths, "Casks")
    return (
        {
            name: apply_pulse_event(dict(metadata), formula_events.get(name))
            for name, metadata in formulas.items()
        },
        {
            token: apply_pulse_event(dict(metadata), cask_events.get(token))
            for token, metadata in casks.items()
        },
    )


def stable_cask_metadata(casks: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for token, metadata in casks.items():
        item = dict(metadata)
        if "sourceArchive" not in item and isinstance(item.get("url"), str):
            item["sourceArchive"] = item["url"]
        result[token] = item
    return result


def npm_pulse_kind_from_last_updated_at(last_updated_at: Any) -> str | None:
    now = datetime.datetime.now(datetime.timezone.utc)
    if is_recent_timestamp(last_updated_at, now - datetime.timedelta(days=PULSE_HISTORY_WINDOW_DAYS)):
        return "updated"
    return None


def read_npm_metadata() -> dict[str, dict[str, Any]]:
    payload = read_json(NPM_INDEX_STATE_PATH, {})
    packages = payload.get("packages") if isinstance(payload, dict) else None
    if not isinstance(packages, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for package, metadata in sorted(packages.items()):
        if not isinstance(package, str) or not package or not isinstance(metadata, dict):
            continue
        executable = metadata.get("executable")
        if not isinstance(executable, str) or not executable:
            continue
        entry: dict[str, Any] = {"executable": executable}
        for key in ("summary", "homepage", "version"):
            value = metadata.get(key)
            if isinstance(value, str):
                entry[key] = value
        popularity = metadata.get("popularity")
        if isinstance(popularity, dict):
            entry["popularity"] = dict(popularity)
        last_updated_at = metadata.get("last_updated_at")
        if isinstance(last_updated_at, str) and last_updated_at:
            entry["last_updated_at"] = last_updated_at
        pulse_kind = metadata.get("pulse_kind")
        if pulse_kind not in PULSE_KINDS:
            pulse_kind = npm_pulse_kind_from_last_updated_at(last_updated_at)
        if pulse_kind in PULSE_KINDS:
            entry["pulse_kind"] = pulse_kind
        result[package] = entry
    return result


def npm_download_count(metadata: dict[str, Any]) -> int | float:
    popularity = metadata.get("popularity")
    if not isinstance(popularity, dict):
        return 0
    value = popularity.get("downloads_per_30_days")
    return value if isinstance(value, (int, float)) else 0


def apply_npm_entries(entries: dict[str, str], npm_metadata: dict[str, dict[str, Any]]) -> None:
    candidates = sorted(
        npm_metadata.items(),
        key=lambda item: (
            -npm_download_count(item[1]),
            item[0],
        ),
    )
    for package, metadata in candidates:
        executable = metadata.get("executable")
        if isinstance(executable, str) and executable:
            entries.setdefault(executable, f"npm:{package}")


def add_package_taxonomy(index: dict[str, dict[str, Any]], packages: Any) -> None:
    if not isinstance(packages, dict):
        return
    for key, entry in packages.items():
        if isinstance(key, str) and isinstance(entry, dict):
            index[key] = entry
            aliases = entry.get("packageManagerAliases")
            if isinstance(aliases, dict):
                for provider, name in aliases.items():
                    provider_text = str(provider or "").strip()
                    name_text = str(name or "").strip()
                    if provider_text and name_text:
                        index.setdefault(f"{provider_text}:{name_text}", entry)


def read_package_taxonomy_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in (PKG_TAXONOMY_PATH, PKG_ECOSYSTEM_TAXONOMY_PATH):
        add_package_taxonomy(index, read_json(path, {}).get("packages"))
    return index


def apply_package_taxonomy(provider: str, packages: dict[str, dict[str, Any]], taxonomy: dict[str, dict[str, Any]]) -> None:
    for name, metadata in packages.items():
        entry = taxonomy.get(f"{provider}:{name}")
        if not entry:
            continue
        for field in PACKAGE_TAXONOMY_FIELDS:
            value = entry.get(field)
            if isinstance(value, list):
                metadata[field] = list(value)
            elif isinstance(value, str) and value:
                metadata[field] = value


def read_crate_metadata() -> dict[str, dict[str, Any]]:
    payload = read_json(CRATES_IO_INDEX_PATH, {})
    crates = payload.get("crates") if isinstance(payload, dict) else None
    if not isinstance(crates, dict):
        return {}
    return {
        crate: dict(metadata)
        for crate, metadata in sorted(crates.items())
        if isinstance(crate, str) and crate and isinstance(metadata, dict)
    }


def apply_crate_entries(entries: dict[str, str], crates: dict[str, dict[str, Any]]) -> None:
    for crate, metadata in crates.items():
        executables = metadata.get("executables")
        if not isinstance(executables, list):
            continue
        for executable in executables:
            if not isinstance(executable, dict):
                continue
            name = executable.get("name")
            if isinstance(name, str) and name:
                entries.setdefault(name, f"cargo:{crate}")


def build_automic_vault_db(root: Path = COMBINED_DIR, formulae: list[dict[str, Any]] | None = None, generated_at: str | None = None) -> dict[str, Any]:
    source_formulae = formulae if formulae is not None else read_formula_cache()
    executable_index = executable_index_from_project_yaml(root)
    cask_entries, apps, casks = read_cask_catalog()
    entries = executable_entries_from_index(executable_index)
    entries.update(cask_entries)
    formulas = formula_metadata_from_project_yaml(root, source_formulae)
    formulas, casks = overlay_homebrew_pulse_metadata(formulas, casks, source_formulae)
    npms = read_npm_metadata()
    apply_npm_entries(entries, npms)
    crates = read_crate_metadata()
    apply_crate_entries(entries, crates)
    taxonomy = read_package_taxonomy_index()
    apply_package_taxonomy("cask", casks, taxonomy)
    apply_package_taxonomy("npm", npms, taxonomy)
    apply_package_taxonomy("cargo", crates, taxonomy)
    for token, metadata in cask_metadata_from_project_yaml(root).items():
        if token in casks:
            casks[token].update(metadata)
    timestamp = generated_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
    return overlay({
        "schema": DB_SCHEMA_VERSION,
        "generated_at": timestamp,
        "entries": dict(sorted(entries.items())),
        "formulas": formulas,
        "casks": stable_cask_metadata(casks),
        "apps": apps,
        "npms": npms,
        "crates": crates,
    })


def write_automic_vault_db(path: Path = AUTOMIC_VAULT_DB_PATH) -> dict[str, Any]:
    db = build_automic_vault_db()
    write_json(path, db)
    return db
