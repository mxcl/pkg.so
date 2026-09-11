from __future__ import annotations

import os
import sys
from collections import Counter
from typing import Any

from .common import CACHE_DIR, ROOT, fetch_json, read_json, stable_hash, write_json


CASKS_URL = "https://formulae.brew.sh/api/cask.json"
CASK_APP_ASSOCIATIONS_PATH = ROOT / "data" / "cask-app-associations.json"


def cask_url(token: str) -> str:
    return f"https://formulae.brew.sh/api/cask/{token}.json"


def fetch_cask_index(*, refresh: bool = False) -> list[dict[str, Any]]:
    payload = fetch_json(CASKS_URL, namespace="brew.sh", refresh=refresh)
    if not isinstance(payload, list):
        raise ValueError("Homebrew cask API payload must be a list")
    return [item for item in payload if isinstance(item, dict)]


def parse_binary_artifact(artifact: Any) -> dict[str, str | None] | None:
    if not isinstance(artifact, dict):
        return None
    if "binary" not in artifact or not set(artifact.keys()) <= {"binary", "target"}:
        return None

    value = artifact["binary"]
    target = None
    if isinstance(value, str):
        source = value
    elif isinstance(value, list) and value:
        source = value[0]
        if len(value) > 1 and isinstance(value[1], dict):
            target = value[1].get("target")
    else:
        return None

    if target is None:
        artifact_target = artifact.get("target")
        if isinstance(artifact_target, str) and artifact_target:
            target = os.path.basename(artifact_target)

    if not isinstance(source, str) or not source:
        return None
    if target is not None and (not isinstance(target, str) or not target):
        return None
    return {"source": source, "target": target}


def supported_cask_artifacts(artifacts: Any) -> list[dict[str, str | None]] | None:
    if not isinstance(artifacts, list):
        return None
    binaries = []
    for artifact in artifacts:
        parsed = parse_binary_artifact(artifact)
        if parsed is not None:
            binaries.append(parsed)
            continue
        if (
            isinstance(artifact, dict)
            and set(artifact.keys())
            <= {"generate_completions_from_executable", "zap", "uninstall"}
        ):
            continue
        return None
    return binaries if binaries else None


def cask_metadata(cask: dict[str, Any]) -> dict[str, Any] | None:
    token = cask.get("token")
    if not token or cask.get("disabled") or cask.get("deprecated"):
        return None

    binaries = supported_cask_artifacts(cask.get("artifacts") or [])
    if binaries is None:
        return None

    url = cask.get("url")
    sha256 = cask.get("sha256")
    version = cask.get("version")
    if not isinstance(url, str) or not url:
        return None
    if not isinstance(sha256, str) or not sha256:
        return None
    if not isinstance(version, str) or not version:
        return None

    depends_on = cask.get("depends_on") or {}
    formula_dependencies = (depends_on.get("formula") if isinstance(depends_on, dict) else []) or []
    if not isinstance(formula_dependencies, list):
        return None
    if not all(isinstance(dep, str) and dep for dep in formula_dependencies):
        return None

    return {
        "summary": cask.get("desc") or "",
        "homepage": cask.get("homepage") or "",
        "aliases": cask.get("old_tokens") or [],
        "url": url,
        "sourceArchive": url,
        "sha256": sha256,
        "version": version,
        "dependencies": sorted(set(formula_dependencies)),
        "binaries": binaries,
    }


def fetch_supported_casks(index: list[dict[str, Any]], *, refresh: bool = False) -> list[dict[str, Any]]:
    result = []
    candidates = 0
    for entry in index:
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            continue
        if cask_metadata(entry) is None:
            continue
        candidates += 1
        try:
            payload = fetch_json(cask_url(token), namespace="brew.sh", refresh=refresh)
        except Exception as err:
            print(f"Failed to fetch cask {token}: {err}", file=sys.stderr)
            continue
        if isinstance(payload, dict):
            result.append(payload)
    if candidates and not result:
        raise ValueError("No supported Homebrew cask metadata was fetched")
    return result


def collect_cask_entries(casks: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    entries: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for cask in casks:
        token = cask.get("token")
        if not isinstance(token, str) or not token:
            continue
        supported = cask_metadata(cask)
        if supported is None:
            continue
        metadata[token] = supported
        for binary in supported["binaries"]:
            executable = binary.get("target") or os.path.basename(binary["source"])
            if executable:
                entries.setdefault(executable, f"cask:{token}")
    return dict(sorted(entries.items())), dict(sorted(metadata.items()))


def cask_app_identity_evidence(cask: dict[str, Any]) -> dict[str, Any] | None:
    token = cask.get("token")
    artifacts = cask.get("artifacts")
    if (
        not isinstance(token, str)
        or not token
        or cask.get("disabled")
        or cask.get("deprecated")
        or not isinstance(artifacts, list)
    ):
        return None

    applications = sorted({
        os.path.basename(app)
        for artifact in artifacts
        if isinstance(artifact, dict)
        for app in artifact_values(artifact.get("app"))
    })
    if not applications:
        return None

    uninstall_quit = []
    zap_trash = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        for rule in artifact.get("uninstall") if isinstance(artifact.get("uninstall"), list) else []:
            if isinstance(rule, dict):
                uninstall_quit.extend(artifact_values(rule.get("quit")))
        for rule in artifact.get("zap") if isinstance(artifact.get("zap"), list) else []:
            if isinstance(rule, dict):
                zap_trash.extend(artifact_values(rule.get("trash")))

    return {
        "token": token,
        "applications": applications,
        "names": artifact_values(cask.get("name")),
        "summary": cask.get("desc") if isinstance(cask.get("desc"), str) else "",
        "homepage": cask.get("homepage") if isinstance(cask.get("homepage"), str) else "",
        "uninstall_quit": sorted(set(uninstall_quit)),
        "zap_trash": sorted(zap_trash),
    }


def cask_app_evidence_hash(evidence: dict[str, Any]) -> str:
    return stable_hash({
        key: evidence[key]
        for key in ("token", "applications", "uninstall_quit", "zap_trash")
    })


def cask_app_bundle_identifier_candidates(evidence: dict[str, Any]) -> set[str]:
    explicit = {value for value in evidence["uninstall_quit"] if is_bundle_identifier(value)}
    if explicit:
        parents = {
            candidate
            for candidate in explicit
            if all(other == candidate or other.startswith(f"{candidate}.") for other in explicit)
        }
        return parents or explicit
    zap_identifiers = Counter(
        bundle_identifier
        for path in evidence["zap_trash"]
        if (bundle_identifier := zap_bundle_identifier(path)) is not None
    )
    return {
        bundle_identifier
        for bundle_identifier, count in zap_identifiers.items()
        if count >= 2
    }


def read_cask_app_associations(path=CASK_APP_ASSOCIATIONS_PATH) -> dict[str, dict[str, Any]]:
    payload = read_json(path, {})
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        return {}
    associations = payload.get("associations")
    if not isinstance(associations, dict):
        return {}
    return {
        str(token): record
        for token, record in associations.items()
        if isinstance(token, str) and token and isinstance(record, dict)
    }


def curated_cask_bundle_identifier(
    evidence: dict[str, Any],
    associations: dict[str, dict[str, Any]],
) -> str | None:
    record = associations.get(evidence["token"])
    if (
        not isinstance(record, dict)
        or record.get("confidence") != "high"
        or record.get("evidence_hash") != cask_app_evidence_hash(evidence)
        or not is_bundle_identifier(record.get("bundle_identifier"))
    ):
        return None
    return record["bundle_identifier"]


def unresolved_cask_app_associations(
    casks: list[dict[str, Any]],
    associations: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    associations = associations or {}
    unresolved = []
    for cask in casks:
        evidence = cask_app_identity_evidence(cask)
        if evidence is None:
            continue
        candidates = cask_app_bundle_identifier_candidates(evidence)
        if len(candidates) == 1:
            continue
        record = associations.get(evidence["token"])
        evidence_hash = cask_app_evidence_hash(evidence)
        if isinstance(record, dict) and record.get("evidence_hash") == evidence_hash:
            continue
        unresolved.append({
            **evidence,
            "candidate_bundle_identifiers": sorted(candidates),
            "evidence_hash": evidence_hash,
        })
    return sorted(unresolved, key=lambda item: (not bool(item["candidate_bundle_identifiers"]), item["token"]))


def app_catalog_from_casks(
    casks: list[dict[str, Any]],
    associations: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    associations = associations or {}
    candidates: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for cask in casks:
        evidence = cask_app_identity_evidence(cask)
        if evidence is None:
            continue
        token = evidence["token"]
        applications = evidence["applications"]
        bundle_identifiers = cask_app_bundle_identifier_candidates(evidence)
        if len(bundle_identifiers) != 1:
            curated = curated_cask_bundle_identifier(evidence, associations)
            if curated is None:
                continue
            bundle_identifiers = {curated}
        names = artifact_values(cask.get("name"))
        url = cask.get("url")
        depends_on = cask.get("depends_on") or {}
        dependencies = depends_on.get("formula") if isinstance(depends_on, dict) else []
        metadata = {
            key: value
            for key, value in {
                "displayName": names[0] if names else token,
                "summary": cask.get("desc"),
                "homepage": cask.get("homepage"),
                "version": cask.get("version"),
                "url": url,
                "sourceArchive": url,
                "sha256": cask.get("sha256"),
            }.items()
            if isinstance(value, str) and value
        }
        metadata["applications"] = applications
        metadata["aliases"] = sorted(set(artifact_values(cask.get("old_tokens"))))
        metadata["dependencies"] = sorted(set(artifact_values(dependencies)))
        candidates.setdefault(bundle_identifiers.pop(), []).append((token, metadata))

    apps = {}
    app_casks = {}
    for bundle_identifier, matches in candidates.items():
        if len(matches) != 1:
            continue
        token, metadata = matches[0]
        apps[bundle_identifier] = {"cask": token, "version_source": "cask"}
        app_casks[token] = metadata
    return dict(sorted(apps.items())), dict(sorted(app_casks.items()))


def artifact_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def is_bundle_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.count(".") >= 2
        and all(value.split("."))
        and all(character.isalnum() or character in ".-" for character in value)
    )


def zap_bundle_identifier(path: Any) -> str | None:
    if not isinstance(path, str):
        return None
    leaf = os.path.basename(path.rstrip("/")).rstrip("*")
    for suffix in (".binarycookies", ".savedState", ".plist", ".sfl2", ".sfl"):
        if leaf.endswith(suffix):
            leaf = leaf.removesuffix(suffix).rstrip("*")
            break
    if leaf.startswith("com.apple.") or not is_bundle_identifier(leaf):
        return None
    return leaf


def write_cask_cache(casks: list[dict[str, Any]], *, all_casks: list[dict[str, Any]] | None = None) -> None:
    entries, metadata = collect_cask_entries(casks)
    write_json(CACHE_DIR / "brew" / "casks.json", {
        "schema": 1,
        "source": CASKS_URL,
        "casks": all_casks if all_casks is not None else casks,
    })
    write_json(CACHE_DIR / "brew" / "cask-entries.json", {
        "schema": 1,
        "provider": "brew-cask",
        "entries": entries,
        "casks": metadata,
    })


def read_cask_authority() -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    payload = read_json(CACHE_DIR / "brew" / "cask-entries.json", {})
    entries = payload.get("entries") if isinstance(payload, dict) else {}
    casks = payload.get("casks") if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        entries = {}
    if not isinstance(casks, dict):
        casks = {}
    return (
        {str(key): str(value) for key, value in entries.items() if isinstance(value, str)},
        {str(key): value for key, value in casks.items() if isinstance(value, dict)},
    )


def read_cask_cache() -> list[dict[str, Any]]:
    payload = read_json(CACHE_DIR / "brew" / "casks.json", {})
    casks = payload.get("casks") if isinstance(payload, dict) else payload
    if not isinstance(casks, list):
        return []
    return [item for item in casks if isinstance(item, dict)]


def read_cask_catalog() -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    entries, binary_casks = read_cask_authority()
    apps, app_casks = app_catalog_from_casks(read_cask_cache(), read_cask_app_associations())
    return entries, apps, dict(sorted({**app_casks, **binary_casks}.items()))
