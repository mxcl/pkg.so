from __future__ import annotations

import ast
import re
import urllib.parse
from pathlib import Path
from typing import Any

from .brew import clean_summary, formula_record, repository_project_url
from .common import AGENTS_DIR, AGENTS_JSON_DIR, COMBINED_DIR, DETERMINISTIC_DIR, HUMAN_OVERRIDE_DIR, STAGE_DIR, read_json, reset_dir, sync_tree, write_text_if_changed
from .casks import read_cask_catalog
from .executables import read_executable_index
from .managers import manager_matcher, package_manager_routes
from .yaml_writer import yaml_text


CATEGORIES = {
    "ai",
    "developer-tools",
    "cloud-infrastructure",
    "security",
    "data",
    "media",
    "networking",
    "system",
    "language-runtime",
    "science",
    "productivity",
    "games",
    "toys",
    "other",
}
PATH_LOCATION_FIELDS = {"config-file-location", "credentials-file-location"}
PATH_LOCATION_PLATFORMS = ("unix", "linux", "macos", "windows")

_GEIGER_SUMMARIES: dict[str, dict[str, Any]] | None = None


def install_line_key(manager: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", manager.lower()).strip("-") or "package-manager"


def project_record(formula: dict[str, Any], executables: list[str], matcher: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    record = formula_record(formula)
    if record is None:
        return None
    name = str(formula.get("name") or "")
    if not executables:
        return None
    record["executables"] = sorted(set(executables))
    package_managers: dict[str, str] = {"brew": name}
    package_manager_match: dict[str, str] = {}
    for route in package_manager_routes(name, executables, matcher):
        key = install_line_key(str(route.get("manager_key") or route.get("manager") or ""))
        package_id = str(route.get("package_id") or "").strip()
        if not key or not package_id:
            continue
        if key not in package_managers:
            package_managers[key] = package_id
        if route.get("match_tier") == "fallback":
            package_manager_match[key] = "fallback"
    record["package-manager"] = package_managers
    if package_manager_match:
        record["package-manager-match"] = package_manager_match
    return record


def cask_project_record(token: str, metadata: dict[str, Any], executables: list[str]) -> dict[str, Any] | None:
    applications = sorted(set(item for item in metadata.get("applications", []) if isinstance(item, str) and item))
    version = metadata.get("version")
    if isinstance(version, str) and version:
        applications = [item for item in applications if version not in item]
    if not token or not (executables or applications):
        return None
    homepage = metadata.get("homepage") if isinstance(metadata.get("homepage"), str) else ""
    url = metadata.get("url") if isinstance(metadata.get("url"), str) else ""
    aliases = [alias for alias in metadata.get("aliases", []) if isinstance(alias, str) and alias]
    record = {
        "id": f"cask:{token}",
        "display-name": metadata.get("displayName") or token,
        "homepage": homepage,
        "repo": repository_project_url(homepage) or repository_project_url(url) or None,
        "package-manager": {"brew-cask": token},
        "package-manager-url": f"https://formulae.brew.sh/cask/{urllib.parse.quote(token, safe='@+/')}",
        "description": clean_summary(metadata.get("summary")),
        "provenance": {
            "provider": "brew-cask",
            "source": "https://formulae.brew.sh/api/cask.json",
            "cask": token,
        },
    }
    if executables:
        record["executables"] = sorted(set(executables))
    if applications:
        record["applications"] = applications
    if aliases:
        record["aliases"] = sorted(set(aliases))
    return record


def render_stage(formulae: list[dict[str, Any]], manager_indexes: dict[str, Any]) -> int:
    reset_dir(STAGE_DIR / "deterministic")
    executables = read_executable_index()
    matcher = manager_matcher(manager_indexes)
    count = 0
    formula_names = set()
    for formula in sorted(formulae, key=lambda item: str(item.get("name") or "")):
        name = str(formula.get("name") or "")
        if name:
            formula_names.add(name)
        record = project_record(formula, executables.get(name, []), matcher)
        if record is None:
            continue
        write_text_if_changed(STAGE_DIR / "deterministic" / f"{name}.yml", yaml_text(record))
        count += 1
    cask_entries, _apps, casks = read_cask_catalog()
    cask_executables: dict[str, list[str]] = {}
    for executable, provider in cask_entries.items():
        if not provider.startswith("cask:"):
            continue
        token = provider.split(":", 1)[1]
        cask_executables.setdefault(token, []).append(executable)
    for token, metadata in sorted(casks.items()):
        if token in formula_names:
            raise ValueError(f"Homebrew formula and cask share public project id: {token}")
        record = cask_project_record(token, metadata, cask_executables.get(token, []))
        if record is None:
            continue
        write_text_if_changed(STAGE_DIR / "deterministic" / f"{token}.yml", yaml_text(record))
        count += 1
    return count


def publish_stages() -> dict[str, Any]:
    sync_tree(STAGE_DIR / "deterministic", DETERMINISTIC_DIR)
    combined_count = render_combined_tree()
    return {"deterministic": str(DETERMINISTIC_DIR), "combined": str(COMBINED_DIR), "combined_projects": combined_count}


def render_agents_yaml_tree() -> int:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(AGENTS_JSON_DIR.glob("*.json")):
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        write_text_if_changed(AGENTS_DIR / f"{path.stem}.yml", yaml_text(agent_record_from_json(payload)))
        count += 1
    return count


def geiger_summaries() -> dict[str, dict[str, Any]]:
    global _GEIGER_SUMMARIES
    if _GEIGER_SUMMARIES is not None:
        return _GEIGER_SUMMARIES
    summaries: dict[str, dict[str, Any]] = {}
    for path in sorted(AGENTS_DIR.glob("*.yml")):
        record = parse_simple_yaml(path.read_text(encoding="utf-8"))
        package_id = str(record.get("id") or f"brew:{path.stem}").strip()
        geiger = record.get("geiger")
        if package_id and isinstance(geiger, dict):
            summaries[package_id] = dict(geiger)
    _GEIGER_SUMMARIES = summaries
    return _GEIGER_SUMMARIES


def geiger_summary_for_package_id(package_id: Any) -> dict[str, Any] | None:
    summary = geiger_summaries().get(str(package_id or ""))
    return dict(summary) if summary else None


def agent_record_from_json(result: dict[str, Any]) -> dict[str, Any]:
    history = result.get("history") if "history" in result else None
    history_sources = result.get("history_sources") or []
    if isinstance(history, dict):
        history = dict(history)
        history_sources = history.get("sources") or history_sources
        if history_sources:
            history["sources"] = history_sources
    record = {
        "id": result["id"],
        "repo": result.get("repo") or None,
        "repo-confidence": result.get("repo-confidence"),
        "display-name": result.get("display-name"),
        "display-name-confidence": result.get("display-name-confidence"),
        "docs": result.get("docs") or [],
        "docs-confidence": result.get("docs-confidence"),
        "config-file-location": result.get("config-file-location") if "config-file-location" in result else None,
        "credentials-file-location": result.get("credentials-file-location") if "credentials-file-location" in result else None,
        "history": history,
        "history-confidence": result.get("history-confidence"),
        "category-path": result.get("category_path") or [],
        "category-confidence": result.get("category-confidence"),
        "tags": result.get("tags") or [],
        "tags-confidence": result.get("tags-confidence"),
        "provenance": {
            "repo-sources": result.get("repo_sources") or [],
            "docs-sources": result.get("docs_sources") or [],
            "category-sources": result.get("category_sources") or [],
            "tags-sources": result.get("tags_sources") or [],
            "display-name-sources": result.get("display_name_sources") or [],
            "history-sources": history_sources,
        },
    }
    geiger = geiger_summary_for_package_id(record["id"])
    if geiger:
        record["geiger"] = geiger
    return record


def render_combined_tree() -> int:
    reset_dir(STAGE_DIR / "combined")
    count = 0
    for path in sorted(DETERMINISTIC_DIR.glob("*.yml")):
        record = parse_simple_yaml(path.read_text(encoding="utf-8"))
        merge_agent_layer(record, AGENTS_DIR / path.name)
        merge_human_override_layer(record, HUMAN_OVERRIDE_DIR / path.name)
        write_text_if_changed(STAGE_DIR / "combined" / path.name, yaml_text(record))
        count += 1
    failures = validate_tree(STAGE_DIR / "combined")
    if failures:
        raise ValueError("\n".join(failures[:20]))
    sync_tree(STAGE_DIR / "combined", COMBINED_DIR)
    return count


def merge_agent_layer(record: dict[str, Any], path: Path) -> None:
    if not path.exists():
        return
    agent = parse_simple_yaml(path.read_text(encoding="utf-8"))
    if record.get("repo") in (None, "", "null") and agent.get("repo") not in (None, "", "null", []):
        record["repo"] = agent["repo"]
    for key in ("display-name", "docs", "tags", "history", "config-file-location", "credentials-file-location"):
        value = agent.get(key)
        if key in PATH_LOCATION_FIELDS and key in agent:
            record[key] = combined_path_locations(value)
            continue
        if key == "history" and key in agent:
            record[key] = value
            continue
        if value not in (None, "", [], {}):
            record[key] = value
    category_path = agent.get("category-path") or agent.get("category_path")
    if isinstance(category_path, list) and category_path:
        record["category"] = category_path[0]
    elif agent.get("category") not in (None, "", [], {}):
        record["category"] = agent["category"]


def combined_path_locations(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for platform in PATH_LOCATION_PLATFORMS:
        raw_locations = value.get(platform)
        if isinstance(raw_locations, list):
            locations = [normalize_home_path(str(item).strip()) for item in raw_locations if str(item).strip()]
        elif isinstance(raw_locations, str) and raw_locations.strip():
            locations = [normalize_home_path(raw_locations.strip())]
        else:
            continue
        locations = unique_ordered(locations)
        if len(locations) == 1:
            result[platform] = locations[0]
        elif locations:
            result[platform] = locations
    return result or None


def normalize_home_path(value: str) -> str:
    if value.startswith("$HOME/"):
        return "~/" + value[len("$HOME/") :]
    return value


def unique_ordered(values: list[str]) -> list[str]:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def merge_human_override_layer(record: dict[str, Any], path: Path) -> None:
    if not path.exists():
        return
    overrides = parse_simple_yaml(path.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        if key in {"id"} and value != record.get("id"):
            continue
        if key.endswith("-confidence") or key.endswith("-sources") or key in {"category-path", "category_path"}:
            continue
        if value in (None, "", [], {}):
            continue
        record[key] = value


def validate_stage() -> list[str]:
    return validate_tree(STAGE_DIR / "deterministic")


def validate_tree(root: Path) -> list[str]:
    failures = []
    if not root.exists():
        return [f"missing staged directory {root}"]
    files = sorted(root.glob("*.yml"))
    if not files:
        return [f"no Homebrew project YAML files were staged in {root}"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        if "package-manager:" not in text:
            failures.append(f"{path}: missing package-manager")
        if "install-lines:" in text:
            failures.append(f"{path}: contains deprecated install-lines")
        if "\nversion:" in f"\n{text}":
            failures.append(f"{path}: published output must not include volatile version metadata")
        if "\nsource-archive:" in f"\n{text}":
            failures.append(f"{path}: published output must not include volatile source archive metadata")
        for deprecated in (
            "dependencies:",
            "build-dependencies:",
            "uses-from-macos:",
            "bottle:",
            "install-behavior:",
        ):
            if deprecated in text:
                failures.append(f"{path}: contains deprecated Homebrew metadata {deprecated.rstrip(':')}")
        if "executables:" not in text and "applications:" not in text:
            failures.append(f"{path}: missing executables or applications")
        if "kind: cli" in text or "exposure: global executable" in text:
            failures.append(f"{path}: contains verbose executable metadata")
        failures.extend(validate_curated_fields(path, text))
        if "\ngenerated-at:" in text:
            failures.append(f"{path}: published output must not include generated-at")
    return failures


def validate_curated_fields(path: Path, text: str) -> list[str]:
    failures = []
    record = parse_simple_yaml(text)
    category = record.get("category")
    if category is not None and category not in CATEGORIES:
        failures.append(f"{path}: category must be one of {sorted(CATEGORIES)}")
    tags = record.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or (record.get("executables") and "cli" not in tags):
            failures.append(f"{path}: executable projects' tags must include cli")
        elif tags != sorted(tags) or any(not is_slug(tag) for tag in tags):
            failures.append(f"{path}: tags must be sorted slug strings")
    docs = record.get("docs")
    if docs is not None:
        if not isinstance(docs, list) or any(not str(url).startswith(("http://", "https://")) for url in docs):
            failures.append(f"{path}: docs URLs must be HTTP(S)")
    repo = record.get("repo")
    if repo not in (None, "null", "") and not str(repo).startswith(("http://", "https://")):
        failures.append(f"{path}: repo must be null or HTTP(S)")
    return failures


def is_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value))


def parse_simple_yaml(text: str) -> dict[str, Any]:
    lines = [
        (len(raw) - len(raw.lstrip(" ")), raw.strip())
        for raw in text.splitlines()
        if raw.strip() and not raw.lstrip().startswith("#")
    ]
    value, _ = parse_yaml_mapping(lines, 0, 0)
    return value


def parse_yaml_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    record: dict[str, Any] = {}
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            index += 1
            continue
        if text.startswith("- ") or ":" not in text:
            break
        key, raw_value = text.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        index += 1
        if raw_value in {">", "|"}:
            record[key], index = parse_yaml_block_scalar(lines, index, line_indent)
            continue
        if raw_value:
            record[key] = unquote_scalar(raw_value)
            continue
        if index < len(lines) and lines[index][0] > line_indent and lines[index][1].startswith("- "):
            values, index = parse_yaml_list(lines, index, lines[index][0])
            record[key] = values
        elif index < len(lines) and lines[index][0] > line_indent:
            child_indent = lines[index][0]
            child_text = lines[index][1]
            if ": " not in child_text and not child_text.endswith(":"):
                record[key], index = parse_yaml_scalar_block(lines, index, line_indent)
            else:
                values, index = parse_yaml_mapping(lines, index, child_indent)
                record[key] = values
        else:
            record[key] = {}
    return record, index


def parse_yaml_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    values: list[Any] = []
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent != indent or not text.startswith("- "):
            break
        raw_value = text[2:].strip()
        index += 1
        if raw_value in {">", "|"}:
            value, index = parse_yaml_block_scalar(lines, index, line_indent)
            values.append(value)
        else:
            values.append(unquote_scalar(raw_value))
    return values, index


def parse_yaml_block_scalar(lines: list[tuple[int, str]], index: int, parent_indent: int) -> tuple[str, int]:
    values: list[str] = []
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent <= parent_indent:
            break
        values.append(text)
        index += 1
    return " ".join(values).strip(), index


def parse_yaml_scalar_block(lines: list[tuple[int, str]], index: int, parent_indent: int) -> tuple[str, int]:
    values: list[str] = []
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent <= parent_indent:
            break
        values.append(text)
        index += 1
    return " ".join(values).strip(), index


def unquote_scalar(value: str) -> Any:
    if value == "null":
        return None
    if value == "{}":
        return {}
    if value in {"true", "false"}:
        return value == "true"
    if len(value) >= 2 and value[0] == value[-1] and value[0] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] and value[0] == '"':
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
        return parsed if isinstance(parsed, str) else value[1:-1]
    return value


def read_formula_cache() -> list[dict[str, Any]]:
    payload = read_json(Path("cache/brew/formulae.json"))
    formulae = payload.get("formulae") if isinstance(payload, dict) else None
    if not isinstance(formulae, list):
        raise ValueError("cache/brew/formulae.json is missing formulae")
    return [item for item in formulae if isinstance(item, dict)]


def read_manager_cache() -> dict[str, Any]:
    payload = read_json(Path("cache/pkg-manager-indexes.json.gz"))
    if not isinstance(payload, dict):
        raise ValueError("cache/pkg-manager-indexes.json.gz is not an object")
    return payload
