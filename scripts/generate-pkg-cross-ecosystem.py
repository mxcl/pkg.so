#!/usr/bin/env -S uv run --python 3.10
from __future__ import annotations

import argparse
import datetime as dt
import gc
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from avdb_paths import PACKAGE_INDEX_PATH
from bootstrap.lib.crates import cargo_install_commands


SCHEMA_VERSION = 5
GENERATED_DATA_DIR = Path("cache")
OUTPUT_PATH = GENERATED_DATA_DIR / "pkg-cross-ecosystem.json"
PKG_MANAGER_INDEX_PATH = GENERATED_DATA_DIR / "pkg-manager-indexes.json.gz"
ALLOWED_PLATFORMS = {"macos", "linux", "windows", "portable"}
SOURCE_BACKED_MANAGER_CONFIDENCE = {
    "macports": 0.94,
    "nix": 0.92,
    "ubuntu": 0.92,
    "debian": 0.92,
    "dnf": 0.92,
    "pacman": 0.92,
    "apk": 0.92,
    "zypper": 0.92,
    "winget": 0.92,
    "chocolatey": 0.92,
    "scoop": 0.92,
}


class Terminal:
    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode

    def log(self, message: str) -> None:
        if not self.json_mode:
            print(message, file=sys.stderr)

    def ok(self, message: str) -> None:
        self.log(f"OK {message}")

    def error(self, message: str) -> None:
        self.log(f"ERROR {message}")


def ensure_cwd() -> Path:
    scripts_dir = Path(__file__).resolve().parent
    root = scripts_dir.parent
    os.chdir(root)
    return root


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def load_script(name: str, filename: str) -> Any:
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_files() -> list[Path]:
    files = [
        GENERATED_DATA_DIR / "pkg-page-enrichment.json",
        GENERATED_DATA_DIR / "cratesio" / "index.json",
        PACKAGE_INDEX_PATH,
        Path("data/npm.json"),
        Path("data/pip.json"),
        PKG_MANAGER_INDEX_PATH,
        Path("scripts/generate-pkg-cross-ecosystem.py"),
        Path("scripts/bootstrap/lib/crates.py"),
        Path("scripts/generate-pkg-manager-indexes.py"),
    ]
    for root in (Path("data/pkg-pages"),):
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file() and path.name != ".DS_Store")
    return sorted(path for path in files if path.exists())


def input_hash(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def normalize_name(value: str) -> str:
    value = value.lower().strip().removeprefix("@")
    value = re.sub(r"[@_/+.]+", "-", value)
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def match_name(value: Any) -> str:
    return normalize_name(str(value or ""))


def versioned_name_tiers(name: str) -> list[list[str]]:
    if "@" not in name:
        return []
    base, version = name.split("@", 1)
    base = base.strip()
    version = version.strip()
    if not base or not version:
        return []
    version_digits = re.sub(r"[^0-9]+", "", version)
    version_hyphen = re.sub(r"[^0-9]+", "-", version).strip("-")
    specific = [
        name,
        f"{base}{version_digits}" if version_digits else "",
        f"{base}{version_hyphen}" if version_hyphen else "",
        f"{base}-{version_hyphen}" if version_hyphen else "",
    ]
    if base == "python" and version_hyphen:
        specific.extend([
            f"python{version_digits}" if version_digits else "",
            f"python3{version_digits[1:]}" if version_digits.startswith("3") else "",
            f"python3-{version_hyphen[2:]}" if version_hyphen.startswith("3-") else "",
        ])
    if base == "openssl" and version_digits:
        specific.extend([f"openssl{version_digits}", f"openssl-{version_digits}", f"libssl{version_digits}"])
    fallback = [base]
    if base == "python":
        fallback.extend(["python3", "python"])
    return [dedupe_match_names(specific), dedupe_match_names(fallback)]


def dedupe_match_names(values: list[Any]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        normalized = match_name(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def package_manager_url(page: Any) -> str:
    return str(getattr(page, "package_manager_url", "") or "")


def executable_names(page: Any) -> list[str]:
    names = set(str(alias) for alias in getattr(page, "aliases", set()))
    for item in getattr(page, "executables", []) or []:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("target") or item.get("source") or "").strip()
            if name:
                names.add(name)
    for item in getattr(page, "binaries", []) or []:
        if isinstance(item, dict):
            name = str(item.get("target") or item.get("source") or "").strip()
            if name:
                names.add(name)
    return sorted(names)


def page_facts(page: Any) -> dict[str, Any]:
    return {
        "key": page.key,
        "provider": page.provider,
        "name": page.name,
        "summary": page.summary,
        "homepage": page.homepage,
        "repository": page.repository,
        "packageManagerUrl": package_manager_url(page),
        "version": page.version,
        "license": page.license,
        "executables": executable_names(page)[:24],
        "cargoRequiredFeatures": page.extra.get("cargoRequiredFeatures") or {},
        "dependencies": list(page.dependencies)[:32],
        "keywords": list(page.keywords)[:32],
        "classifiers": list(page.classifiers)[:16],
    }


def local_pages() -> dict[str, Any]:
    pages_module = load_script("generate_pkg_pages_for_cross_ecosystem", "generate-pkg-pages.py")
    sources = pages_module.load_sources()
    # Avoid a freshness cycle: this artifact feeds the graph and page generator,
    # so its package facts must not depend on generated graph or prior cross data.
    sources["pkg_graph"] = {}
    sources["pkg_cross_ecosystem"] = {}
    return pages_module.package_pages_from_sources(sources, cross_ecosystem_only=True)


def local_candidates(pages: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    normalized: dict[str, list[Any]] = defaultdict(list)
    for page in pages.values():
        normalized[normalize_name(page.name)].append(page)
    result: dict[str, list[dict[str, str]]] = {}
    for page in pages.values():
        candidates = []
        for other in sorted(normalized.get(normalize_name(page.name), []), key=lambda item: (item.provider, item.name)):
            if other.key == page.key:
                continue
            if page.provider in {"npm", "pip", "cargo"} and other.provider != "brew":
                continue
            candidates.append({
                "key": other.key,
                "provider": other.provider,
                "name": other.name,
                "summary": other.summary,
            })
        result[page.key] = candidates
    return result


def command(platform: str, manager: str, value: str, confidence: float, evidence: str, kind: str = "package_manager") -> dict[str, Any]:
    return {
        "platform": platform,
        "manager": manager,
        "command": value,
        "kind": kind,
        "confidence": round(float(confidence), 2),
        "evidence": evidence,
    }


def av_command(package_key: str) -> dict[str, Any]:
    return command(
        "portable",
        "Automic Vault",
        f"sudo av install {package_key}",
        1.0,
        "deterministic local package key",
        "automic_vault",
    )


def native_commands(facts: dict[str, Any]) -> list[dict[str, Any]]:
    provider = facts["provider"]
    name = facts["name"]
    if provider == "brew":
        return [
            command("macos", "Homebrew", f"brew install {name}", 1.0, "local Homebrew formula metadata"),
            command("linux", "Homebrew", f"brew install {name}", 0.9, "Homebrew formula metadata supports Linuxbrew where bottles or source builds are available"),
        ]
    if provider == "cask":
        return [command("macos", "Homebrew Cask", f"brew install --cask {name}", 1.0, "local Homebrew cask metadata")]
    if provider == "npm":
        return [command("portable", "npm", f"npm install -g {name}", 1.0, "local npm package metadata")]
    if provider == "pip":
        return [command("portable", "pip", f"pip install {name}", 1.0, "local PyPI package metadata")]
    if provider == "cargo":
        return [command("portable", "Cargo", value, 1.0, "local crates.io package metadata")
                for value in cargo_install_commands(name, facts.get("cargoRequiredFeatures"))]
    return []


def load_manager_indexes() -> dict[str, Any]:
    return read_json(PKG_MANAGER_INDEX_PATH, {})


def manager_matcher(
    manager_indexes: dict[str, Any],
    wanted_names: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    managers = manager_indexes.get("managers") if isinstance(manager_indexes, dict) else None
    if not isinstance(managers, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for manager_key, definition in managers.items():
        if not isinstance(definition, dict):
            continue
        packages = definition.get("packages")
        if not isinstance(packages, dict):
            continue
        display_name = str(definition.get("display_name") or manager_key)
        platform = str(definition.get("platform") or "")
        command_template = str(definition.get("command_template") or "")
        source_label = str(definition.get("source_label") or "")
        if platform not in ALLOWED_PLATFORMS or "{id}" not in command_template:
            continue
        for package_id, package in packages.items():
            if not isinstance(package, dict):
                continue
            install_id = str(package.get("id") or package_id).strip()
            if not install_id:
                continue
            match_names = package.get("match_names")
            if not isinstance(match_names, list):
                continue
            source_url = str(package.get("source_url") or "")
            source_name = str(package.get("source_name") or install_id)
            evidence = f"{source_label}: {source_name} from {source_url}" if source_url else f"{source_label}: {source_name}"
            metadata = manager_package_metadata(package)
            item = command(
                platform,
                display_name,
                command_template.format(id=install_id),
                SOURCE_BACKED_MANAGER_CONFIDENCE.get(str(manager_key), 0.9),
                evidence,
            )
            item["source"] = {
                "type": "package_manager_index",
                "manager": str(manager_key),
                "source_label": source_label,
                "package_id": install_id,
                "package_name": source_name,
                "source_url": source_url,
            }
            if metadata:
                item["source"]["metadata"] = metadata
            for match_name in match_names:
                normalized = normalize_name(str(match_name))
                if normalized and (wanted_names is None or normalized in wanted_names):
                    result[normalized].append(item)
    for normalized, items in result.items():
        result[normalized] = dedupe_commands(items)
    return result


def manager_package_metadata(package: dict[str, Any]) -> dict[str, Any]:
    skip = {"id", "match_names", "source_name", "source_url"}
    return {
        key: value
        for key, value in package.items()
        if key not in skip and value not in ("", [], None)
    }


def package_match_tiers(facts: dict[str, Any]) -> list[list[str]]:
    name = str(facts.get("name") or "")
    version_tiers = versioned_name_tiers(name)
    executable_tier = dedupe_match_names([name, *(facts.get("executables") or [])])
    if version_tiers:
        return [tier for tier in [version_tiers[0], executable_tier, *version_tiers[1:]] if tier]
    return [executable_tier] if executable_tier else []


def source_backed_manager_matches(facts: dict[str, Any], matcher: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    exact_names = set(dedupe_match_names([facts.get("name")]))
    executable_names_normalized = set(dedupe_match_names(facts.get("executables") or []))
    for tier_index, tier in enumerate(package_match_tiers(facts)):
        for normalized in tier:
            for item in matcher.get(normalized) or []:
                source = item.get("source") if isinstance(item, dict) else {}
                if not isinstance(source, dict):
                    continue
                manager = str(source.get("manager") or "").strip()
                package_id = str(source.get("package_id") or "").strip()
                if not manager or not package_id:
                    continue
                key = (manager, package_id)
                if key in seen:
                    continue
                seen.add(key)
                confidence = float(item.get("confidence") or SOURCE_BACKED_MANAGER_CONFIDENCE.get(manager, 0.9))
                if normalized in exact_names:
                    reason = "normalized package name match"
                    confidence = max(confidence, 0.95)
                elif normalized in executable_names_normalized:
                    reason = "installed executable or alias match"
                    confidence = min(max(confidence, 0.9), 0.94)
                elif tier_index > 0:
                    reason = "versioned package alias match"
                    confidence = min(confidence, 0.88)
                else:
                    reason = "package manager index match"
                match = {
                    "manager": manager,
                    "displayName": str(item.get("manager") or manager),
                    "platform": str(item.get("platform") or ""),
                    "packageId": package_id,
                    "packageName": str(source.get("package_name") or package_id),
                    "command": str(item.get("command") or ""),
                    "confidence": round(confidence, 2),
                    "matchedBy": normalized,
                    "reason": reason,
                    "evidence": str(item.get("evidence") or ""),
                    "source": {
                        "type": "package_manager_index",
                        "sourceLabel": str(source.get("source_label") or ""),
                        "sourceUrl": str(source.get("source_url") or ""),
                    },
                }
                metadata = source.get("metadata")
                if isinstance(metadata, dict) and metadata:
                    match["metadata"] = metadata
                result.append(match)
    return dedupe_external_matches(result)


def source_backed_manager_commands(facts: dict[str, Any], matcher: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    # Cross-distribution commands are anchored to Homebrew formula identity.
    # Other ecosystems can share short package names with unrelated software;
    # retain those as external matches instead of actionable commands.
    if facts.get("provider") != "brew":
        return []
    result = []
    seen_managers = set()
    for tier in package_match_tiers(facts):
        for normalized in tier:
            for item in sorted(
                matcher.get(normalized) or [],
                key=lambda item: str(((item.get("source") if isinstance(item, dict) else {}) or {}).get("package_id") or "") != normalized,
            ):
                source = item.get("source") if isinstance(item, dict) else {}
                manager = source.get("manager") if isinstance(source, dict) else item.get("manager")
                command_text = item.get("command")
                if not manager or manager in seen_managers or not command_text:
                    continue
                seen_managers.add(manager)
                result.append(item)
    return result


def local_link(candidate: dict[str, str], reason: str, evidence: str, confidence: float = 0.78) -> dict[str, Any]:
    return {
        "provider": candidate["provider"],
        "name": candidate["name"],
        "label": candidate["name"],
        "rel": "same_software_cross_ecosystem",
        "reason": reason,
        "confidence": confidence,
        "evidence": evidence,
    }


def local_curate_packet(packet: dict[str, Any], matcher: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    target = packet["target"]
    package_key = target["key"]
    commands = [av_command(package_key)]
    commands.extend(native_commands(target))
    commands.extend(source_backed_manager_commands(target, matcher or {}))
    external_matches = source_backed_manager_matches(target, matcher or {})
    links = [
        local_link(
            candidate,
            "Same normalized package name exists in another local package ecosystem.",
            "normalized local package name",
        )
        for candidate in packet.get("localCandidates") or []
    ]
    return {"commands": dedupe_commands(commands), "localLinks": links, "externalMatches": external_matches}


def dedupe_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in commands:
        key = (item.get("platform"), item.get("manager"), item.get("command"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def dedupe_external_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in matches:
        if not isinstance(item, dict):
            continue
        key = (item.get("manager"), item.get("packageId"))
        if key in seen or not key[0] or not key[1]:
            continue
        seen.add(key)
        result.append(item)
    return sorted(
        result,
        key=lambda item: (
            -float(item.get("confidence") or 0),
            str(item.get("packageId") or "") != str(item.get("matchedBy") or ""),
            str(item.get("platform") or ""),
            str(item.get("displayName") or item.get("manager") or ""),
            str(item.get("packageId") or ""),
        ),
    )


def run_agent(agent_cmd: str, packet: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        shlex.split(agent_cmd),
        input=json.dumps(packet, sort_keys=True),
        capture_output=True,
        check=True,
        text=True,
    )
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise ValueError("agent command did not return a JSON object")
    return parsed


def build_entry(
    package_key: str,
    facts: dict[str, Any],
    candidates: list[dict[str, str]],
    existing: dict[str, Any] | None,
    agent_cmd: str,
    matcher: dict[str, list[dict[str, Any]]],
    manager_index_hash: str,
) -> dict[str, Any]:
    facts_hash = stable_hash(facts)
    candidate_hash = stable_hash(candidates)
    if (
        existing
        and existing.get("schema") == SCHEMA_VERSION
        and existing.get("facts_hash") == facts_hash
        and existing.get("candidate_hash") == candidate_hash
        and existing.get("manager_index_hash") == manager_index_hash
        and isinstance(existing.get("commands"), list)
        and isinstance(existing.get("externalMatches"), list)
    ):
        return existing
    packet = {
        "task": (
            "Return install commands grouped by controlled platform keys, local cross-ecosystem links, "
            "and source-backed external package-manager matches. The first command must remain the supplied "
            "Automic Vault command. Use only supplied local link candidates."
        ),
        "allowedPlatforms": sorted(ALLOWED_PLATFORMS),
        "target": facts,
        "requiredFirstCommand": av_command(package_key),
        "localCandidates": candidates,
    }
    curated = run_agent(agent_cmd, packet) if agent_cmd else local_curate_packet(packet, matcher)
    commands = curated.get("commands") or []
    if not commands or not isinstance(commands, list):
        commands = [av_command(package_key)]
    if not commands or commands[0].get("command") != av_command(package_key)["command"]:
        commands = [av_command(package_key)] + [item for item in commands if isinstance(item, dict)]
    return {
        "target": package_key,
        "schema": SCHEMA_VERSION,
        "facts_hash": facts_hash,
        "candidate_hash": candidate_hash,
        "manager_index_hash": manager_index_hash,
        "curated_at": utc_now(),
        "curator": "agent-cmd" if agent_cmd else "codex-local-cross-ecosystem",
        "commands": dedupe_commands([item for item in commands if isinstance(item, dict)]),
        "localLinks": curated.get("localLinks") or [],
        "externalMatches": dedupe_external_matches(curated.get("externalMatches") or []),
    }


def validate_command(package_key: str, index: int, item: Any) -> list[str]:
    failures: list[str] = []
    if not isinstance(item, dict):
        return [f"{package_key}: command {index} is not an object"]
    platform = str(item.get("platform") or "")
    if platform not in ALLOWED_PLATFORMS:
        failures.append(f"{package_key}: command {index} has invalid platform {platform!r}")
    if not str(item.get("manager") or "").strip():
        failures.append(f"{package_key}: command {index} is missing manager")
    if not str(item.get("command") or "").strip():
        failures.append(f"{package_key}: command {index} is missing command")
    try:
        confidence = float(item.get("confidence"))
    except (TypeError, ValueError):
        failures.append(f"{package_key}: command {index} has invalid confidence")
    else:
        if confidence < 0 or confidence > 1:
            failures.append(f"{package_key}: command {index} has out-of-range confidence")
    if index > 0 and not str(item.get("evidence") or "").strip():
        failures.append(f"{package_key}: command {index} is missing evidence")
    if "agent-inferred" in str(item.get("evidence") or ""):
        failures.append(f"{package_key}: command {index} uses inferred evidence")
    source = item.get("source")
    if source is not None:
        if not isinstance(source, dict):
            failures.append(f"{package_key}: command {index} source must be an object")
        elif not str(source.get("source_label") or "").strip() or not str(source.get("package_id") or "").strip():
            failures.append(f"{package_key}: command {index} source is missing package provenance")
    return failures


def validate_external_match(package_key: str, index: int, item: Any) -> list[str]:
    failures: list[str] = []
    if not isinstance(item, dict):
        return [f"{package_key}: external match {index} is not an object"]
    for key in ("manager", "packageId", "displayName", "reason", "evidence"):
        if not str(item.get(key) or "").strip():
            failures.append(f"{package_key}: external match {index} is missing {key}")
    if str(item.get("platform") or "") not in ALLOWED_PLATFORMS:
        failures.append(f"{package_key}: external match {index} has invalid platform {item.get('platform')!r}")
    try:
        confidence = float(item.get("confidence"))
    except (TypeError, ValueError):
        failures.append(f"{package_key}: external match {index} has invalid confidence")
    else:
        if confidence < 0 or confidence > 1:
            failures.append(f"{package_key}: external match {index} has out-of-range confidence")
    source = item.get("source")
    if not isinstance(source, dict):
        failures.append(f"{package_key}: external match {index} source must be an object")
    elif not str(source.get("sourceLabel") or "").strip():
        failures.append(f"{package_key}: external match {index} source is missing sourceLabel")
    metadata = item.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        failures.append(f"{package_key}: external match {index} metadata must be an object")
    return failures


def validate_artifact(artifact: dict[str, Any], page_keys: set[str]) -> list[str]:
    failures: list[str] = []
    if artifact.get("schema") != SCHEMA_VERSION:
        failures.append(f"schema is {artifact.get('schema')!r}, expected {SCHEMA_VERSION}")
    packages = artifact.get("packages")
    if not isinstance(packages, dict):
        return failures + ["packages must be an object"]
    missing = sorted(page_keys - set(packages))
    if missing:
        failures.append(f"{len(missing):,} local package pages are missing cross-ecosystem entries: {', '.join(missing[:12])}")
    extra = sorted(set(packages) - page_keys)
    if extra:
        failures.append(f"{len(extra):,} cross-ecosystem entries do not exist locally: {', '.join(extra[:12])}")
    for package_key, entry in packages.items():
        if not isinstance(entry, dict):
            failures.append(f"{package_key}: entry must be an object")
            continue
        commands = entry.get("commands")
        if not isinstance(commands, list) or not commands:
            failures.append(f"{package_key}: commands must be a non-empty list")
            continue
        required = f"sudo av install {package_key}"
        if commands[0].get("command") != required or commands[0].get("kind") != "automic_vault":
            failures.append(f"{package_key}: first command must be {required!r}")
        for index, item in enumerate(commands):
            failures.extend(validate_command(package_key, index, item))
        external_matches = entry.get("externalMatches") or []
        if not isinstance(external_matches, list):
            failures.append(f"{package_key}: externalMatches must be a list")
        else:
            seen_external = set()
            for index, item in enumerate(external_matches):
                if isinstance(item, dict):
                    key = (item.get("manager"), item.get("packageId"))
                    if key in seen_external:
                        failures.append(f"{package_key}: duplicate external match {key}")
                    seen_external.add(key)
                failures.extend(validate_external_match(package_key, index, item))
        links = entry.get("localLinks") or []
        if not isinstance(links, list):
            failures.append(f"{package_key}: localLinks must be a list")
            continue
        for link in links:
            if not isinstance(link, dict):
                failures.append(f"{package_key}: localLinks contains non-object")
                continue
            target_key = f"{link.get('provider') or ''}:{link.get('name') or ''}"
            if target_key not in page_keys:
                failures.append(f"{package_key}: local link target does not exist locally: {target_key}")
            if target_key == package_key:
                failures.append(f"{package_key}: local link points at itself")
            if not str(link.get("reason") or "").strip():
                failures.append(f"{package_key}: local link to {target_key} is missing reason")
            if not str(link.get("evidence") or "").strip():
                failures.append(f"{package_key}: local link to {target_key} is missing evidence")
    return failures


def build_cross_ecosystem(agent_cmd: str = "", existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or read_json(OUTPUT_PATH, {})
    pages = local_pages()
    facts_by_key = {key: page_facts(page) for key, page in pages.items()}
    candidates_by_key = local_candidates(pages)
    page_keys = set(pages)
    del pages
    gc.collect()
    wanted_names = {
        normalized
        for facts in facts_by_key.values()
        for tier in package_match_tiers(facts)
        for normalized in tier
    }
    manager_index_hash = hashlib.sha256(PKG_MANAGER_INDEX_PATH.read_bytes()).hexdigest()
    manager_indexes = load_manager_indexes()
    matcher = manager_matcher(manager_indexes, wanted_names)
    del manager_indexes
    gc.collect()
    existing_packages = existing.get("packages") if isinstance(existing, dict) else {}
    packages: dict[str, Any] = {}
    for package_key in sorted(facts_by_key):
        packages[package_key] = build_entry(
            package_key,
            facts_by_key[package_key],
            candidates_by_key.get(package_key, []),
            (existing_packages or {}).get(package_key) if isinstance(existing_packages, dict) else None,
            agent_cmd,
            matcher,
            manager_index_hash,
        )
    files = source_files()
    artifact = {
        "schema": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "description": "Agent-oriented install command and cross-ecosystem package data for the package-origin SQLite artifact.",
        "input_hash": input_hash(files),
        "input_files": [path.as_posix() for path in files],
        "platform_definitions": {
            "macos": "macOS package managers and install surfaces.",
            "linux": "Linux package managers and install surfaces.",
            "windows": "Windows package managers and install surfaces.",
            "portable": "Automic Vault and language-level package managers that are not tied to one OS.",
        },
        "packages": packages,
    }
    failures = validate_artifact(artifact, page_keys)
    if failures:
        raise ValueError("; ".join(failures[:8]))
    return artifact


def comparable_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(artifact))
    result.pop("generated_at", None)
    for entry in (result.get("packages") or {}).values():
        if isinstance(entry, dict):
            entry.pop("curated_at", None)
    return result


def check_current(path: Path, terminal: Terminal) -> int:
    if not path.exists():
        terminal.error(f"Missing {path}. Run scripts/generate-pkg-cross-ecosystem.py.")
        return 1
    try:
        current = read_json(path)
        pages = local_pages()
        failures = validate_artifact(current, set(pages))
        expected = build_cross_ecosystem(existing=current)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as err:
        terminal.error(f"Unable to validate {path}: {err}")
        return 1
    if comparable_artifact(current) != comparable_artifact(expected):
        failures.append("cross-ecosystem artifact does not match current package facts or candidate fingerprints")
    if failures:
        terminal.error("Package cross-ecosystem data is stale.")
        for failure in failures[:24]:
            terminal.log(f"  - {failure}")
        terminal.log("Run scripts/generate-pkg-cross-ecosystem.py, regenerate the package graph, and rebuild the package-origin SQLite artifact.")
        return 1
    terminal.ok(f"Package cross-ecosystem data is current ({len(current.get('packages') or {}):,} packages)")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate cross-ecosystem install commands for package pages.")
    parser.add_argument("--check", action="store_true", help="Validate the cross-ecosystem artifact without calling an agent.")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help=f"Path to write or validate. Defaults to {OUTPUT_PATH}.")
    parser.add_argument("--agent-cmd", default=os.environ.get("PKG_CROSS_ECOSYSTEM_AGENT_CMD", ""), help="Optional command that reads a JSON packet on stdin and returns JSON commands and localLinks.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable status.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_cwd()
    terminal = Terminal(json_mode=args.json)
    output_path = Path(args.output)
    if args.check:
        return check_current(output_path, terminal)
    try:
        existing = read_json(output_path, {})
        artifact = build_cross_ecosystem(agent_cmd=args.agent_cmd, existing=existing)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as err:
        terminal.error(f"Failed to build package cross-ecosystem data: {err}")
        return 1
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    count = len(artifact.get("packages") or {})
    terminal.ok(f"Wrote {count:,} package cross-ecosystem entries to {output_path}")
    if args.json:
        print(json.dumps({"ok": True, "output": str(output_path), "package_count": count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
