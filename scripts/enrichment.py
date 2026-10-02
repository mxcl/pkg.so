from __future__ import annotations

import json
import re
import urllib.parse
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.bootstrap.lib.common import AGENTS_DIR, AGENTS_JSON_DIR, COMBINED_DIR, DETERMINISTIC_DIR, ROOT, stable_hash, write_json
from scripts.bootstrap.lib.render import agent_record_from_json, parse_simple_yaml
from scripts.bootstrap.lib.yaml_writer import yaml_text


CONFIDENCE = {"low": 1, "medium": 2, "high": 3}
CURATED_FIELDS = (
    "repo",
    "display-name",
    "docs",
    "category",
    "tags",
    "config-file-location",
    "credentials-file-location",
    "history",
)
PATH_LOCATION_FIELDS = ("config-file-location", "credentials-file-location")
PATH_LOCATION_PLATFORMS = ("unix", "linux", "macos", "windows")
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
TAG_REPLACEMENTS = {
    "awscli": "aws",
    "cli-tool": "cli",
    "cli-tools": "cli",
    "cmdline": "cli",
    "command-line": "cli",
    "commandline": "cli",
    "k8s": "kubernetes",
}
BANNED_TAGS = {
    "app",
    "application",
    "software",
    "tool",
    "tools",
    "utility",
    "utilities",
}
SOURCE_FACT_KEYS = (
    "id",
    "provider",
    "name",
    "homepage",
    "repo",
    "description",
    "executables",
    "applications",
    "package-manager",
    "package-manager-url",
    "license",
    "provenance",
)


def today_iso() -> str:
    return date.today().isoformat()


def run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def parse_project_yaml(path: Path) -> dict[str, Any]:
    return parse_simple_yaml(path.read_text(encoding="utf-8"))


def parse_project_yaml_lists(text: str, base: dict[str, Any]) -> dict[str, Any]:
    record = deepcopy(base)
    path_stack: list[tuple[int, str]] = []
    current_list: tuple[int, dict[str, Any], str] | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        text_line = raw.strip()
        while path_stack and indent <= path_stack[-1][0]:
            path_stack.pop()
        if text_line.startswith("- ") and current_list and indent > current_list[0]:
            current_list[1].setdefault(current_list[2], []).append(unquote_scalar(text_line[2:].strip()))
            continue
        current_list = None
        if ":" not in text_line or text_line.startswith("- "):
            continue
        key, value = text_line.split(":", 1)
        key = key.strip()
        value = value.strip()
        parent = nested_parent(record, [item[1] for item in path_stack])
        if not value:
            current_list = (indent, parent, key)
            if key not in parent or parent[key] == {}:
                parent[key] = []
            path_stack.append((indent, key))
        else:
            parent[key] = unquote_scalar(value)
    return record


def nested_parent(record: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    parent = record
    for key in keys:
        child = parent.get(key)
        if not isinstance(child, dict):
            return parent
        parent = child
    return parent


def unquote_scalar(value: str) -> Any:
    if value == "null":
        return None
    if value in {"true", "false"}:
        return value == "true"
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def provider_name(record: dict[str, Any]) -> tuple[str, str]:
    project_id = str(record.get("id") or "")
    if ":" in project_id:
        provider, name = project_id.split(":", 1)
        return provider, name
    return "", project_id


def normalize_url(value: Any, *, keep_fragment: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    query = [(key, val) for key, val in query if not key.lower().startswith("utm_")]
    query = [(key, val) for key, val in query if key.lower() not in {"fbclid", "gclid"}]
    fragment = parsed.fragment if keep_fragment and parsed.fragment else ""
    normalized = urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path or "", urllib.parse.urlencode(query), fragment))
    normalized = re.sub(r"^https://github\.com/([^/]+)/([^/.]+)\.git$", r"https://github.com/\1/\2", normalized)
    normalized = re.sub(r"^https://gitlab\.com/([^/]+)/([^/.]+)\.git$", r"https://gitlab.com/\1/\2", normalized)
    return normalized


def normalize_docs(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        url = normalize_url(value, keep_fragment="README" in str(value) or "readme" in str(value))
        if url and not rejected_docs_url(url):
            result.append(url)
    return sorted(set(result), key=docs_rank)


def normalize_repo_candidate(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    text = re.sub(r"^git://github\.com/", "https://github.com/", raw)
    text = re.sub(r"^ssh://git@github\.com/", "https://github.com/", text)
    text = re.sub(r"^git@github\.com:", "https://github.com/", text)
    text = re.sub(r"^git://gitlab\.com/", "https://gitlab.com/", text)
    text = re.sub(r"^ssh://git@gitlab\.com/", "https://gitlab.com/", text)
    text = re.sub(r"^git@gitlab\.com:", "https://gitlab.com/", text)
    text = re.sub(r"^git://", "https://", text)
    text = re.sub(r"^ssh://git@", "https://", text)
    text = re.sub(r"^git@([^:]+):", r"https://\1/", text)
    return text


def normalize_repo(value: Any) -> str:
    url = normalize_url(normalize_repo_candidate(value))
    if not url:
        return ""
    if rejected_repo_url(url):
        return ""
    return url


def rejected_repo_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "formulae.brew.sh" in host:
        return True
    path_parts = {part for part in path.split("/") if part}
    if any(token in path for token in ("/blog/", "/posts/", "/tutorial")) or "wiki" in path_parts:
        return True
    return False


def rejected_docs_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "formulae.brew.sh" in host:
        return True
    if any(token in host for token in ("tutorial", "mirror", "readthedocs.io.evil")):
        return True
    if any(token in path for token in ("/blog/", "/posts/", "/tutorial")):
        return True
    return False


def docs_rank(url: str) -> tuple[int, str]:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host.startswith("docs.") or ".docs." in host or "readthedocs.io" in host:
        return (0, url)
    if any(part in path for part in ("/docs", "/documentation", "/manual")):
        return (1, url)
    if "github.com" in host and "/wiki" in path:
        return (2, url)
    if "github.com" in host and "readme" in path:
        return (3, url)
    return (4, url)


def normalize_tag(value: Any) -> str:
    tag = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return TAG_REPLACEMENTS.get(tag, tag)


def normalize_tags(values: Any, *, include_cli: bool = True) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        values = []
    tags = {"cli"} if include_cli else set()
    for value in values:
        tag = normalize_tag(value)
        if tag and tag not in BANNED_TAGS:
            tags.add(tag)
    return sorted(tags)


def normalize_category_path(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = [part for part in value.split("/") if part]
    elif isinstance(value, list):
        parts = [str(part) for part in value if str(part).strip()]
    else:
        parts = []
    parts = [re.sub(r"[^a-z0-9]+", "-", part.lower()).strip("-") for part in parts]
    if not parts or parts[0] not in CATEGORIES:
        return []
    return parts


def normalize_display_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_path_locations(
    value: Any,
    *,
    errors: list[str] | None = None,
    field: str = "path location",
    require_arrays: bool = False,
) -> dict[str, list[str]] | None:
    if value is None or value == {}:
        return None
    if value in ("", []):
        if errors is not None:
            errors.append(f"{field} must be null or an object keyed by unix, linux, macos, or windows")
        return None
    if not isinstance(value, dict):
        if errors is not None:
            errors.append(f"{field} must be null or an object keyed by unix, linux, macos, or windows")
        return None
    result: dict[str, list[str]] = {}
    for raw_key, raw_value in value.items():
        platform = str(raw_key or "").strip().lower()
        if platform not in PATH_LOCATION_PLATFORMS:
            if errors is not None:
                errors.append(f"{field} contains unsupported platform {raw_key!r}")
            continue
        locations = normalize_path_location_list(
            raw_value,
            errors=errors,
            field=field,
            platform=platform,
            require_array=require_arrays,
        )
        if locations:
            result[platform] = locations
    return {platform: result[platform] for platform in PATH_LOCATION_PLATFORMS if platform in result} or None


def normalize_path_location_list(
    value: Any,
    *,
    errors: list[str] | None = None,
    field: str,
    platform: str,
    require_array: bool = False,
) -> list[str]:
    if isinstance(value, str):
        if require_array:
            if errors is not None:
                errors.append(f"{field}.{platform} must be a non-empty array of strings")
            return []
        raw_locations = [value]
    elif isinstance(value, list):
        raw_locations = value
    else:
        if errors is not None:
            errors.append(f"{field}.{platform} must be a non-empty array of strings")
        return []

    locations: list[str] = []
    for raw_location in raw_locations:
        if not isinstance(raw_location, str):
            if errors is not None:
                errors.append(f"{field}.{platform} must contain only non-empty strings")
            continue
        location = re.sub(r"\s+", " ", raw_location).strip()
        if not location:
            if errors is not None:
                errors.append(f"{field}.{platform} must contain only non-empty strings")
            continue
        for location_part in split_location_alternatives(location):
            normalized = normalize_home_location(location_part)
            if normalized not in locations:
                locations.append(normalized)
    if not locations and errors is not None:
        errors.append(f"{field}.{platform} must be a non-empty array of strings")
    return locations


def normalize_home_location(value: str) -> str:
    if value.startswith("$HOME/"):
        return "~/" + value[len("$HOME/") :]
    return value


def split_location_alternatives(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s+or\s+", value) if part.strip()]


HISTORY_LIST_FIELDS = (
    "summary",
    "project-history",
    "adoption-history",
    "usage",
    "package-nerd-significance",
    "timeline",
    "related-projects",
    "sources",
)


def normalize_history(value: Any) -> dict[str, list[str]] | None:
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        return None
    result: dict[str, list[str]] = {}
    for field in HISTORY_LIST_FIELDS:
        items = normalize_history_list(value.get(field))
        if items:
            result[field] = items
    return result or None


def normalize_history_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text and text not in items:
            items.append(text)
    return items


def source_facts(record: dict[str, Any]) -> dict[str, Any]:
    if isinstance(record.get("__source_record"), dict):
        record = record["__source_record"]
    provider, name = provider_name(record)
    facts = {
        "id": record.get("id"),
        "provider": provider,
        "name": name,
        "homepage": normalize_url(record.get("homepage")),
        "repo": normalize_url(record.get("repo")),
        "description": re.sub(r"\s+", " ", str(record.get("description") or "")).strip(),
        "executables": sorted(str(item) for item in record.get("executables") or []),
        "applications": sorted(str(item) for item in record.get("applications") or []),
        "package-manager": normalize_string_map(record.get("package-manager")),
        "package-manager-url": normalize_url(record.get("package-manager-url")),
        "license": str(record.get("license") or ""),
        "provenance": normalize_string_map(record.get("provenance")),
    }
    return {key: facts[key] for key in SOURCE_FACT_KEYS}


def normalize_string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(val) for key, val in sorted(value.items()) if val not in (None, "", [], {})}


def curation_facts(record: dict[str, Any]) -> dict[str, Any]:
    path = normalize_category_path(record.get("category"))
    return {
        "repo": normalize_repo(record.get("repo")),
        "display-name": normalize_display_name(record.get("display-name")),
        "docs": normalize_docs(record.get("docs")),
        "config-file-location": normalize_path_locations(
            record["config-file-location"] if "config-file-location" in record else {}
        ),
        "credentials-file-location": normalize_path_locations(
            record["credentials-file-location"] if "credentials-file-location" in record else {}
        ),
        "history": normalize_history(record.get("history")),
        "category_path": path,
        "category": path[0] if path else "",
        "tags": normalize_tags(record.get("tags"), include_cli=not str(record.get("id") or "").startswith("cask:")),
    }


def hash_source_facts(record: dict[str, Any]) -> str:
    # `repo` can be filled by Codex when deterministic generation has no value,
    # so exclude it from the drift hash. It still appears in review input.
    facts = source_facts(record)
    facts.pop("repo", None)
    return stable_hash(facts)


def hash_curation_facts(record: dict[str, Any]) -> str:
    return stable_hash(curation_facts(record))


def load_projects(provider: str = "brew", projects_dir: Path = DETERMINISTIC_DIR) -> list[dict[str, Any]]:
    projects = []
    for path in sorted(projects_dir.glob("*.yml")):
        source_record = parse_project_yaml(path)
        record = deepcopy(source_record)
        combined_path = COMBINED_DIR / path.name
        if combined_path.exists():
            combined = parse_project_yaml(combined_path)
            for key in ("repo", "display-name", "docs", "category", "tags", "history", *PATH_LOCATION_FIELDS):
                if key in PATH_LOCATION_FIELDS and key in combined:
                    record[key] = combined[key]
                    continue
                if key == "history" and key in combined:
                    record[key] = combined[key]
                    continue
                if combined.get(key) not in (None, "", [], {}):
                    record[key] = combined[key]
        record["__source_record"] = source_record
        record["__path"] = path
        record["__agent_path"] = AGENTS_DIR / path.name
        record["__agent_json_path"] = AGENTS_JSON_DIR / f"{path.stem}.json"
        projects.append(record)
    return projects


def update_observed_state(state: dict[str, Any], projects: list[dict[str, Any]], today: str) -> None:
    for record in projects:
        project_id = str(record.get("id") or "")
        if not project_id:
            continue
        entry = state.setdefault(project_id, {})
        source_hash = hash_source_facts(record)
        curation_hash = hash_curation_facts(record)
        if not entry.get("first_observed"):
            entry["first_observed"] = today
        entry["last_observed"] = today
        if entry.get("source_fact_hash") != source_hash:
            previous = entry.setdefault("previous_source_hashes", [])
            if entry.get("source_fact_hash") and entry["source_fact_hash"] not in previous:
                previous.append(entry["source_fact_hash"])
            entry["last_source_change"] = today
        entry["source_fact_hash"] = source_hash
        entry["curation_hash"] = curation_hash
        mark_manual_fields(entry, record)


def mark_manual_fields(entry: dict[str, Any], record: dict[str, Any]) -> None:
    ownership = entry.setdefault("field_ownership", {})
    managed = entry.setdefault("managed_values", {})
    current = curation_facts(record)
    for field in CURATED_FIELDS:
        current_value = current.get("category" if field == "category" else field)
        if field in managed and current_value != managed[field]:
            ownership[field] = "manual"
        else:
            ownership.setdefault(field, "managed" if field in managed else "unknown")


def select_projects(
    projects: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    mode: str,
    today: str,
    include_missing_curated_fields: bool = False,
) -> list[dict[str, Any]]:
    selected = []
    cutoff = date.fromisoformat(today) - timedelta(days=90)
    for record in projects:
        project_id = str(record.get("id") or "")
        entry = state.get(project_id) or {}
        missing_curated = include_missing_curated_fields and has_missing_curated_fields(record)
        if mode == "replace":
            selected.append(record)
        elif mode == "new" and (needs_new_curation(record, entry) or missing_curated):
            selected.append(record)
        elif mode == "review-stale-updated" and (needs_stale_updated_review(entry, cutoff) or missing_curated):
            selected.append(record)
    return selected


def has_missing_curated_fields(record: dict[str, Any]) -> bool:
    return any(not has_curated_field(record, field) for field in CURATED_FIELDS)


def has_curated_field(record: dict[str, Any], field: str) -> bool:
    if field == "category":
        return "category" in record or "category_path" in record or "category-path" in record
    return field in record


def needs_new_curation(record: dict[str, Any], entry: dict[str, Any] | None = None) -> bool:
    _, name = provider_name(record)
    facts = curation_facts(record)
    confidence = (entry or {}).get("field_confidence") or {}
    verified_slug_name = (
        facts["display-name"] == name
        and bool((entry or {}).get("last_verified"))
        and confidence.get("display-name") in {"high", "medium"}
    )
    verified_missing_repo = (
        not facts["repo"]
        and bool((entry or {}).get("last_verified"))
        and confidence.get("repo") in {"high", "medium"}
    )
    has_reviewed_config_location = "config-file-location" in record or bool((entry or {}).get("last_verified"))
    has_reviewed_credentials_location = "credentials-file-location" in record or bool((entry or {}).get("last_verified"))
    return (
        (not facts["repo"] and not verified_missing_repo)
        or
        not facts["docs"]
        or not facts["category"]
        or facts["tags"] == ["cli"]
        or not facts["display-name"]
        or (facts["display-name"] == name and not verified_slug_name)
        or ("config-file-location" not in record and not has_reviewed_config_location)
        or ("credentials-file-location" not in record and not has_reviewed_credentials_location)
    )


def needs_stale_updated_review(entry: dict[str, Any], cutoff: date) -> bool:
    changed = parse_date(entry.get("last_source_change"))
    if not changed or changed < cutoff:
        return False
    verified = parse_date(entry.get("last_verified"))
    if not verified or verified < cutoff:
        return True
    confidence = entry.get("field_confidence") or {}
    return any(value == "low" for value in confidence.values())


def parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def review_input(projects: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": 1,
        "projects": [
            {
                "id": record.get("id"),
                "source_facts": source_facts(record),
                "current_curation": curation_facts(record),
            }
            for record in projects
        ],
    }


def prompt_text(input_path: Path, project_count: int | None = None) -> str:
    count_line = f"The file contains {project_count} project records." if project_count is not None else "The file contains project records."
    required_line = f"Your final JSON must contain exactly {project_count} results, one for every project id in `.projects`." if project_count is not None else "Your final JSON must contain one result for every project id in `.projects`."
    batching_line = (
        "Treat this as a goal-sized batch task. Before producing the final JSON, break the input into smaller internal batches, "
        "review each batch completely, and track which project ids have been completed. Do not switch to fallback rows because the full input is large; use batching instead."
        if project_count is None or project_count > 10
        else "Review the full input in one pass and return one result for every project id."
    )
    return f"""/goal Reliably enrich every project in the input JSON with official repository URL, official documentation URLs, category, tags, display name, config file location, credentials file location, package history, adoption history, usage notes, confidence, and source notes.

{batching_line}

Determine official repository URL, official documentation URLs, category, tags, display name, config file location, credentials file location, and package-nerd history/usage for the projects listed in this input JSON:

{input_path}

Input shape is fixed: a JSON object with top-level keys `schema` and `projects`; `projects` is the array to review. {count_line}

{required_line}

If you inspect it, use these commands:

```sh
jq '.projects | length' {input_path}
jq -r '.projects[].id' {input_path}
jq -c '.projects[] | {{id, source_facts, current_curation}}' {input_path}
```

Do not process only the first 10 projects; the commands above stream all projects. Do not probe the input as a top-level array; it is not one. Do not emit a placeholder `{{"results":[]}}` before reading the file.

Return JSON that matches the provided output schema. Do not edit files. Use official sources only.

Do not emit placeholder fallback rows. For every result:
- Prefer `unix` when official docs describe one shared Unix-like path; split into `linux` and `macos` only when the paths differ.
- Use top-level `null` for `config-file-location` when no official config file location is documented.
- Use top-level `null` for `credentials-file-location` when credentials are absent, unknown, or not applicable.
- Cite concise official source notes.
- For `history`, write neutral Wikipedia-style package history focused on package-manager culture: project origins, adoption history, common usage, and why package nerds care. Use more paragraphs for significant packages. Use `history: null` when reliable source-backed history is too thin.
- `history` fields are arrays of paragraph strings: `summary`, `project-history`, `adoption-history`, `usage`, `package-nerd-significance`, `timeline`, `related-projects`, and `sources`. Use concise timeline strings like `2016: First public release`.
- If you rely on supplied input rather than web research, cite the specific input field, such as `source_facts.description`, `source_facts.homepage`, or `current_curation.category`.

Only determine repo when source_facts.repo is empty, missing, or null. If source_facts.repo already has a value, return repo as null and do not second-guess it. Repositories must be official HTTP(S) source-control project URLs, not package-manager pages, mirrors, tutorials, blogs, wikis, or documentation pages. Never return `git://`, `ssh://`, or `git@host:` clone URLs; convert them to the corresponding official HTTP(S) repository URL or return null if no official HTTP(S) repo page exists.

Documentation URL priority:
1. dedicated official docs domain
2. official /docs, /documentation, or equivalent path
3. official GitHub/GitLab wiki
4. official README anchor
5. homepage fallback only when it is the best official documentation surface

Reject random tutorials, blog posts, package-manager pages, mirrors, scraped docs sites, SEO aggregators, and unrelated vendor pages.

Use confidence values high, medium, or low for each field, and include concise provenance/source notes.

The first item in category_path must be exactly one of these lowercase taxonomy roots:
{", ".join(sorted(CATEGORIES))}

You may add later category_path items for future hierarchy, but keep them lowercase slug strings.
"""


def validate_codex_payload(payload: Any, expected_ids: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    normalized, errors, _invalid = validate_codex_payload_partial(payload, expected_ids)
    return normalized, errors


def validate_codex_payload_partial(payload: Any, expected_ids: set[str]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    errors = []
    if not isinstance(payload, dict):
        return [], ["codex output must be a JSON object"], []
    results = payload.get("results")
    if not isinstance(results, list):
        return [], ["codex output must contain results list"], []
    normalized = []
    invalid = []
    accepted_ids = set()
    invalid_ids = set()
    observed_counts: Counter[str] = Counter()
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            errors.append(f"results[{index}] must be an object")
            continue
        project_id = str(item.get("id") or "")
        if project_id not in expected_ids:
            errors.append(f"{project_id or f'results[{index}]'}: unexpected id")
            continue
        observed_counts[project_id] += 1
        if project_id in accepted_ids:
            continue
        normalized_item, item_errors = normalize_codex_result(item)
        if item_errors:
            if project_id not in invalid_ids:
                errors.extend(f"{project_id}: {error}" for error in item_errors)
                invalid.append({"id": project_id, "errors": item_errors, "raw": item})
                invalid_ids.add(project_id)
            continue
        normalized.append(normalized_item)
        accepted_ids.add(project_id)
    missing = sorted(expected_ids - set(observed_counts))
    if missing:
        errors.append(f"missing results for {len(missing)} project ids: {', '.join(missing[:20])}")
    for project_id, count in sorted(observed_counts.items()):
        if count > 1:
            errors.append(f"{project_id}: duplicate result repeated {count - 1} times")
    return normalized, errors, invalid


def validation_rejection_count(expected_ids: set[str], normalized: list[dict[str, Any]]) -> int:
    accepted_ids = {str(item.get("id") or "") for item in normalized}
    return len(expected_ids - accepted_ids)


def validation_error_summary(errors: list[str]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for error in errors:
        if ": " in error and not error.startswith("missing results for "):
            _project_id, message = error.split(": ", 1)
        else:
            message = error
        counts[message] += 1
    return [{"error": message, "count": count} for message, count in counts.most_common()]


def normalize_codex_result(item: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors = []
    category_path = normalize_category_path(item.get("category_path"))
    if not category_path:
        errors.append("category_path must start with a known top-level category")
    docs = normalize_docs(item.get("docs"))
    repo = normalize_repo(item.get("repo"))
    if item.get("repo") not in (None, "") and not repo:
        errors.append("repo must be an official HTTP(S) source repository URL or null")
    tags = normalize_tags(item.get("tags"), include_cli=not str(item.get("id") or "").startswith("cask:"))
    display_name = normalize_display_name(item.get("display-name"))
    if not display_name:
        errors.append("display-name is required")
    if "config-file-location" not in item:
        errors.append("config-file-location is required")
    if "credentials-file-location" not in item:
        errors.append("credentials-file-location is required")
    if "history" not in item:
        errors.append("history is required")
    config_file_location = normalize_path_locations(
        item.get("config-file-location"),
        errors=errors,
        field="config-file-location",
        require_arrays=True,
    )
    credentials_file_location = normalize_path_locations(
        item.get("credentials-file-location"),
        errors=errors,
        field="credentials-file-location",
        require_arrays=True,
    )
    history = normalize_history(item.get("history"))
    if item.get("history") not in (None, {}) and history is None:
        errors.append("history must be null or an object with supported string-list fields")
    history_sources = normalize_sources((history or {}).get("sources")) or normalize_sources(item.get("history_sources"))
    if history and history_sources:
        history = dict(history)
        history["sources"] = history_sources
    result = {
        "id": str(item.get("id")),
        "repo": repo,
        "repo-confidence": normalize_confidence(item.get("repo-confidence"), errors, "repo-confidence"),
        "display-name": display_name,
        "display-name-confidence": normalize_confidence(item.get("display-name-confidence"), errors, "display-name-confidence"),
        "category_path": category_path,
        "category-confidence": normalize_confidence(item.get("category-confidence"), errors, "category-confidence"),
        "docs": docs,
        "docs-confidence": normalize_confidence(item.get("docs-confidence"), errors, "docs-confidence"),
        "config-file-location": config_file_location,
        "credentials-file-location": credentials_file_location,
        "history": history,
        "history-confidence": normalize_confidence(item.get("history-confidence"), errors, "history-confidence"),
        "tags": tags,
        "tags-confidence": normalize_confidence(item.get("tags-confidence"), errors, "tags-confidence"),
        "docs_sources": normalize_sources(item.get("docs_sources")),
        "repo_sources": normalize_sources(item.get("repo_sources")),
        "category_sources": normalize_sources(item.get("category_sources")),
        "tags_sources": normalize_sources(item.get("tags_sources")),
        "display_name_sources": normalize_sources(item.get("display_name_sources")),
        "history_sources": history_sources,
    }
    for field in ("docs_sources", "repo_sources", "category_sources", "tags_sources", "display_name_sources", "history_sources"):
        if has_placeholder_source(result[field]):
            errors.append(f"{field} must cite official sources, not placeholder provenance")
    required_sources = {
        "category_sources": bool(category_path),
        "tags_sources": bool(tags),
        "display_name_sources": bool(display_name),
        "docs_sources": bool(docs),
        "repo_sources": bool(repo),
        "history_sources": bool(history),
    }
    for field, required in required_sources.items():
        if required and not result[field]:
            errors.append(f"{field} must cite at least one source")
    return result, errors


def has_placeholder_source(sources: list[str]) -> bool:
    return any(re.search(r"\b(input id|not completed|placeholder|unknown)\b", source, re.IGNORECASE) for source in sources)


def normalize_confidence(value: Any, errors: list[str], field: str) -> str:
    confidence = str(value or "").lower()
    if confidence not in CONFIDENCE:
        errors.append(f"{field} must be high, medium, or low")
        return "low"
    return confidence


def normalize_sources(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return sorted({re.sub(r"\s+", " ", str(item)).strip() for item in value if str(item).strip()})


def confidence_allows(value: str, threshold: str) -> bool:
    return CONFIDENCE[value] >= CONFIDENCE[threshold]


def apply_results(
    projects_by_id: dict[str, dict[str, Any]],
    state: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    confidence_threshold: str,
    today: str,
    dry_run: bool = False,
    preserve_existing_fields: bool = False,
) -> dict[str, int]:
    summary = {"reviewed": len(results), "changed": 0, "rejected": 0, "skipped_low_confidence": 0, "no_op": 0}
    for result in results:
        project_id = result["id"]
        record = projects_by_id.get(project_id)
        if record is None:
            summary["rejected"] += 1
            continue
        entry = state.setdefault(project_id, {})
        original = deepcopy(record)
        changed, skipped = merge_result(
            record,
            entry,
            result,
            confidence_threshold,
            preserve_existing_fields=preserve_existing_fields,
        )
        entry["last_verified"] = today
        entry["field_confidence"] = {
            "repo": result["repo-confidence"],
            "display-name": result["display-name-confidence"],
            "category": result["category-confidence"],
            "docs": result["docs-confidence"],
            "tags": result["tags-confidence"],
            "history": result["history-confidence"],
        }
        entry["provenance"] = {
            "repo_sources": result["repo_sources"],
            "docs_sources": result["docs_sources"],
            "category_sources": result["category_sources"],
            "tags_sources": result["tags_sources"],
            "display_name_sources": result["display_name_sources"],
            "history_sources": result["history_sources"],
        }
        entry["curation_hash"] = hash_curation_facts(record)
        if skipped:
            summary["skipped_low_confidence"] += skipped
        if not dry_run:
            output_result = result_for_agent_record(original, result) if preserve_existing_fields else result
            write_agent_json_record(record, output_result)
            write_agent_record(record, output_result)
        if curation_facts(original) == curation_facts(record):
            summary["no_op"] += 1
            continue
        summary["changed"] += 1
    return summary


def write_agent_record(record: dict[str, Any], result: dict[str, Any]) -> None:
    path = record.get("__agent_path")
    if not isinstance(path, Path):
        path = AGENTS_DIR / f"{provider_name(record)[1]}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_text(agent_record_from_json(result)), encoding="utf-8")


def write_agent_json_record(record: dict[str, Any], result: dict[str, Any]) -> None:
    path = record.get("__agent_json_path")
    if not isinstance(path, Path):
        path = AGENTS_JSON_DIR / f"{provider_name(record)[1]}.json"
    write_json(path, result)


def agent_record_from_result(result: dict[str, Any]) -> dict[str, Any]:
    return agent_record_from_json(result)


def merge_result(
    record: dict[str, Any],
    entry: dict[str, Any],
    result: dict[str, Any],
    threshold: str,
    *,
    preserve_existing_fields: bool = False,
) -> tuple[bool, int]:
    skipped = 0
    ownership = entry.setdefault("field_ownership", {})
    managed = entry.setdefault("managed_values", {})

    field_values = {
        "repo": normalize_repo(result.get("repo")),
        "display-name": normalize_display_name(result["display-name"]),
        "category": normalize_category_path(result["category_path"])[0],
        "docs": normalize_docs(result["docs"]),
        "config-file-location": normalize_path_locations(result.get("config-file-location")),
        "credentials-file-location": normalize_path_locations(result.get("credentials-file-location")),
        "history": normalize_history(result.get("history")),
        "tags": normalize_tags(result["tags"], include_cli=not str(result.get("id") or "").startswith("cask:")),
    }
    confidence_fields = {
        "repo": result["repo-confidence"],
        "display-name": result["display-name-confidence"],
        "category": result["category-confidence"],
        "docs": result["docs-confidence"],
        "tags": result["tags-confidence"],
        "history": result["history-confidence"],
    }
    changed = False
    for field, new_value in field_values.items():
        current_value = curation_facts(record).get("category" if field == "category" else field)
        if field == "repo" and current_value:
            continue
        if field == "repo" and not new_value:
            managed[field] = ""
            ownership[field] = "managed" if ownership.get(field) != "manual" else ownership[field]
            continue
        is_empty = missing_curated_value(field, current_value)
        if preserve_existing_fields and not is_empty:
            continue
        if field in confidence_fields and not confidence_allows(confidence_fields[field], threshold) and not is_empty:
            skipped += 1
            continue
        if ownership.get(field) == "manual" and not is_empty:
            if field == "tags":
                new_value = sorted(set(current_value) | set(new_value))
            elif field == "docs":
                new_value = sorted(set(current_value) | set(new_value), key=docs_rank)
            else:
                continue
        if current_value == new_value:
            managed[field] = new_value
            ownership[field] = "managed" if ownership.get(field) != "manual" else ownership[field]
            continue
        record[field] = new_value
        managed[field] = new_value
        ownership[field] = "managed" if ownership.get(field) != "manual" else ownership[field]
        changed = True
    return changed, skipped


def missing_curated_value(field: str, value: Any) -> bool:
    return value in ("", [], None) or (field == "tags" and value == ["cli"])


def result_for_agent_record(original: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    agent_path = original.get("__agent_path")
    current = parse_project_yaml(agent_path) if isinstance(agent_path, Path) and agent_path.exists() else {}
    merged = result if not current else json_result_from_agent_record(current, result)
    facts = curation_facts(original)
    field_pairs = (
        ("repo", "repo"),
        ("display-name", "display-name"),
        ("category", "category_path"),
        ("docs", "docs"),
        ("config-file-location", "config-file-location"),
        ("credentials-file-location", "credentials-file-location"),
        ("history", "history"),
        ("tags", "tags"),
    )
    for curated_field, result_field in field_pairs:
        if missing_curated_value(curated_field, facts.get(curated_field)):
            merged[result_field] = result[result_field]
            confidence_field = f"{curated_field}-confidence"
            if confidence_field in result:
                merged[confidence_field] = result[confidence_field]
            source_field = {
                "repo": "repo_sources",
                "display-name": "display_name_sources",
                "category": "category_sources",
                "docs": "docs_sources",
                "tags": "tags_sources",
                "history": "history_sources",
            }.get(curated_field)
            if source_field and source_field in result:
                merged[source_field] = result[source_field]
    return merged


def json_result_from_agent_record(record: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    category_path = record.get("category-path") or record.get("category_path") or []
    return {
        "id": record.get("id") or fallback["id"],
        "repo": record.get("repo"),
        "repo-confidence": record.get("repo-confidence") or fallback["repo-confidence"],
        "display-name": record.get("display-name") or fallback["display-name"],
        "display-name-confidence": record.get("display-name-confidence") or fallback["display-name-confidence"],
        "category_path": category_path or fallback["category_path"],
        "category-confidence": record.get("category-confidence") or fallback["category-confidence"],
        "docs": record.get("docs") or [],
        "docs-confidence": record.get("docs-confidence") or fallback["docs-confidence"],
        "config-file-location": record.get("config-file-location") if "config-file-location" in record else None,
        "credentials-file-location": record.get("credentials-file-location") if "credentials-file-location" in record else None,
        "history": record.get("history") if "history" in record else None,
        "history-confidence": record.get("history-confidence") or fallback["history-confidence"],
        "tags": record.get("tags") or [],
        "tags-confidence": record.get("tags-confidence") or fallback["tags-confidence"],
        "repo_sources": provenance.get("repo-sources") or [],
        "docs_sources": provenance.get("docs-sources") or [],
        "category_sources": provenance.get("category-sources") or [],
        "tags_sources": provenance.get("tags-sources") or [],
        "display_name_sources": provenance.get("display-name-sources") or [],
        "history_sources": provenance.get("history-sources") or [],
    }


def write_run_artifacts(run_dir: Path, input_payload: dict[str, Any], prompt: str) -> tuple[Path, Path]:
    input_path = run_dir / "input.json"
    prompt_path = run_dir / "prompt.md"
    write_json(input_path, input_payload)
    prompt_path.write_text(prompt, encoding="utf-8")
    return input_path, prompt_path
