#!/usr/bin/env -S uv run --python 3.10
from __future__ import annotations

import argparse
import datetime as dt
import gc
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
OUTPUT_PATH = Path("cache/pkg.sqlite")
PACKAGE_PAGE_SCRIPT = Path("scripts/generate-pkg-pages.py")
PKG_CROSS_ECOSYSTEM_PATH = Path("cache/pkg-cross-ecosystem.json")


@dataclass(frozen=True)
class SearchDocument:
    path: str
    locale: str
    title: str
    summary: str
    provider: str
    package_key: str
    rank: int | None
    search_text: str


@dataclass(frozen=True)
class PackageRecord:
    path: str
    provider: str
    slug: str
    package_key: str
    name: str
    display_name: str
    summary: str
    provider_label: str
    package_manager_url: str
    install_command: str
    native_install_command: str
    version: str
    category: str
    license: str
    homepage: str
    repository: str
    rank: int | None
    last_updated_at: str
    indexable: bool
    data: dict[str, Any]
    search_text: str


@dataclass(frozen=True)
class HubRecord:
    path: str
    slug: str
    title: str
    description: str
    group: str
    data: dict[str, Any]


@dataclass(frozen=True)
class HubPackageRecord:
    hub_slug: str
    package_key: str
    position: int
    reason: str


@dataclass(frozen=True)
class PackageRoute:
    path: str
    slug: str


class Terminal:
    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode

    def log(self, message: str) -> None:
        if not self.json_mode:
            print(message, file=sys.stderr)

    def step(self, message: str) -> None:
        self.log(f"> {message}")

    def ok(self, message: str) -> None:
        self.log(f"OK {message}")

    def error(self, message: str) -> None:
        self.log(f"ERROR {message}")


def ensure_cwd() -> Path:
    scripts_dir = Path(__file__).resolve().parent
    root = scripts_dir.parent
    os.chdir(root)
    return root


def load_pkg_pages_module():
    spec = importlib.util.spec_from_file_location("av_pkg_pages", PACKAGE_PAGE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {PACKAGE_PAGE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def http_date(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return format_datetime(parsed.astimezone(dt.timezone.utc), usegmt=True)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;

        CREATE TABLE metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE search_documents (
          path TEXT PRIMARY KEY,
          locale TEXT NOT NULL,
          title TEXT NOT NULL,
          summary TEXT NOT NULL,
          provider TEXT NOT NULL,
          package_key TEXT NOT NULL,
          rank INTEGER,
          search_text TEXT NOT NULL
        );

        CREATE INDEX search_documents_locale_idx
          ON search_documents(locale);

        CREATE TABLE packages (
          path TEXT PRIMARY KEY,
          provider TEXT NOT NULL,
          slug TEXT NOT NULL,
          package_key TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          display_name TEXT NOT NULL,
          summary TEXT NOT NULL,
          provider_label TEXT NOT NULL,
          package_manager_url TEXT NOT NULL,
          install_command TEXT NOT NULL,
          native_install_command TEXT NOT NULL,
          version TEXT NOT NULL,
          category TEXT NOT NULL,
          license TEXT NOT NULL,
          homepage TEXT NOT NULL,
          repository TEXT NOT NULL,
          rank INTEGER,
          last_updated_at TEXT NOT NULL,
          indexable INTEGER NOT NULL,
          data_json TEXT NOT NULL,
          search_text TEXT NOT NULL
        );

        CREATE INDEX packages_provider_slug_idx
          ON packages(provider, slug);
        CREATE INDEX packages_rank_idx
          ON packages(rank);

        CREATE TABLE hubs (
          path TEXT PRIMARY KEY,
          slug TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          description TEXT NOT NULL,
          group_name TEXT NOT NULL,
          data_json TEXT NOT NULL
        );

        CREATE TABLE hub_packages (
          hub_slug TEXT NOT NULL,
          package_key TEXT NOT NULL,
          position INTEGER NOT NULL,
          reason TEXT NOT NULL,
          PRIMARY KEY(hub_slug, package_key)
        );

        CREATE INDEX hub_packages_hub_idx
          ON hub_packages(hub_slug, position);
        """
    )


def write_sqlite(
    output_path: Path,
    search_documents: Iterable[SearchDocument],
    packages: Iterable[PackageRecord],
    hubs: Iterable[HubRecord],
    hub_packages: Iterable[HubPackageRecord],
    metadata: dict[str, Any],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)

    try:
        connection = sqlite3.connect(temp_path)
        try:
            create_schema(connection)
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                [(str(key), json.dumps(value, sort_keys=True)) for key, value in sorted(metadata.items())],
            )
            connection.executemany(
                """
                INSERT INTO search_documents(path, locale, title, summary, provider, package_key, rank, search_text)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        document.path,
                        document.locale,
                        document.title,
                        document.summary,
                        document.provider,
                        document.package_key,
                        document.rank,
                        document.search_text,
                    )
                    for document in search_documents
                ),
            )
            connection.executemany(
                """
                INSERT INTO packages(
                  path, provider, slug, package_key, name, display_name, summary,
                  provider_label, package_manager_url, install_command, native_install_command,
                  version, category, license, homepage, repository, rank, last_updated_at,
                  indexable, data_json, search_text
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        package.path,
                        package.provider,
                        package.slug,
                        package.package_key,
                        package.name,
                        package.display_name,
                        package.summary,
                        package.provider_label,
                        package.package_manager_url,
                        package.install_command,
                        package.native_install_command,
                        package.version,
                        package.category,
                        package.license,
                        package.homepage,
                        package.repository,
                        package.rank,
                        package.last_updated_at,
                        1 if package.indexable else 0,
                        json.dumps(package.data, sort_keys=True, separators=(",", ":")),
                        package.search_text,
                    )
                    for package in packages
                ),
            )
            connection.executemany(
                """
                INSERT INTO hubs(path, slug, title, description, group_name, data_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        hub.path,
                        hub.slug,
                        hub.title,
                        hub.description,
                        hub.group,
                        json.dumps(hub.data, sort_keys=True, separators=(",", ":")),
                    )
                    for hub in hubs
                ),
            )
            connection.executemany(
                """
                INSERT INTO hub_packages(hub_slug, package_key, position, reason)
                VALUES(?, ?, ?, ?)
                """,
                (
                    (record.hub_slug, record.package_key, record.position, record.reason)
                    for record in hub_packages
                ),
            )
            connection.execute("PRAGMA optimize")
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"sqlite integrity_check failed: {result}")
            connection.commit()
        finally:
            connection.close()
        os.replace(temp_path, output_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def previous_manifest_from_sqlite(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(path)
        try:
            rows = connection.execute("SELECT key, value FROM metadata").fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    metadata = {}
    for key, value in rows:
        try:
            metadata[key] = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            metadata[key] = value
    manifest = metadata.get("manifest")
    return manifest if isinstance(manifest, dict) else None


def manifest_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "manifest": manifest,
        "source_hash": manifest.get("source_hash", ""),
        "generated_at": manifest.get("generated_at", ""),
        "last_modified": http_date(manifest.get("generated_at")),
    }


def populate_manifest_counts(page_module: Any, manifest: dict[str, Any], pages: list[Any], hubs: list[tuple[Any, list[Any]]]) -> None:
    indexable_pages = [page for page in pages if page_module.is_indexable_package_page(page)]
    sitemap_names = ["sitemap-hubs.xml"] + [
        f"sitemap-{provider}.xml"
        for provider in page_module.PACKAGE_PROVIDERS
        if any(page.provider == provider for page in indexable_pages)
    ]
    manifest["hub_count"] = len(hubs)
    manifest["package_count"] = len(pages)
    manifest["indexable_page_count"] = len(indexable_pages)
    manifest["noindex_page_count"] = len(pages) - len(indexable_pages)
    manifest["markdown_page_count"] = len(indexable_pages)
    manifest["hub_markdown_page_count"] = len(hubs)
    manifest["approval_gate_count"] = sum(1 for page in pages if getattr(page, "approval_gate", None))
    manifest["sitemap_count"] = len(sitemap_names)
    manifest["sitemap_page_counts"] = {
        provider: sum(1 for page in indexable_pages if page.provider == provider)
        for provider in page_module.PACKAGE_PROVIDERS
        if any(page.provider == provider for page in indexable_pages)
    }


def page_search_text(page_module: Any, page: Any, locale: dict[str, Any] | None) -> str:
    pieces: list[str] = [
        page.display_name,
        page.name,
        page.key,
        page.slug,
        page.provider,
        page_module.package_manager_label(page),
        page_module.clean_summary(page.summary),
        page.category,
        page.license,
        page.repository,
        page.homepage,
    ]
    for locations in (
        getattr(page, "config_file_locations", {}),
        getattr(page, "credentials_file_locations", {}),
    ):
        if isinstance(locations, dict):
            for platform, values in locations.items():
                pieces.append(str(platform))
                if isinstance(values, list):
                    pieces.extend(str(value) for value in values)
                else:
                    pieces.append(str(values))
    pieces.extend(sorted(page.aliases))
    pieces.extend(str(item.get("name") or item.get("target") or item.get("source") or "") for item in page.executables if isinstance(item, dict))
    pieces.extend(str(item.get("target") or item.get("source") or "") for item in page.binaries if isinstance(item, dict))
    pieces.extend(str(item) for item in page.keywords)
    pieces.extend(str(item) for item in page.classifiers)
    for hub in page.package_hubs:
        if isinstance(hub, dict):
            pieces.extend(str(hub.get(key) or "") for key in ("slug", "label", "reason"))
    geiger = sanitized_geiger(page)
    if geiger:
        pieces.extend(str(item) for item in geiger.get("reasons") or [])
    if page.approval_gate:
        pieces.extend(str(item) for item in page.approval_gate.get("rules") or [])
    if getattr(page, "agent_safety_answer", None):
        pieces.extend(str(value) for value in page.agent_safety_answer.values())
    for related in getattr(page, "related_packages", []):
        if isinstance(related, dict):
            pieces.extend(str(related.get(key) or "") for key in ("label", "name", "reason", "rel", "evidence"))
    for match in getattr(page, "external_package_manager_matches", []):
        if not isinstance(match, dict):
            continue
        pieces.extend(
            str(match.get(key) or "")
            for key in (
                "command",
                "displayName",
                "evidence",
                "manager",
                "packageId",
                "packageName",
                "platform",
                "reason",
            )
        )
        source = match.get("source")
        if isinstance(source, dict):
            pieces.extend(str(source.get(key) or "") for key in ("sourceLabel", "sourceUrl"))
        metadata = match.get("metadata")
        if isinstance(metadata, dict):
            pieces.extend(
                str(metadata.get(key) or "")
                for key in ("description", "sourcePackage", "summary")
            )
    taxonomy = page.extra.get("pkgTaxonomy") if isinstance(page.extra.get("pkgTaxonomy"), dict) else {}
    pieces.extend(str(item) for item in page_module.taxonomy_terms(taxonomy))
    history = getattr(page, "history", None)
    if isinstance(history, dict):
        for value in history.values():
            if isinstance(value, list):
                pieces.extend(str(item) for item in value)
            else:
                pieces.append(str(value))
    return page_module.normalize_space(" ".join(str(piece or "") for piece in pieces))


def string_items(values: Any, keys: tuple[str, ...] = ("name", "target", "source", "label", "title")) -> list[str]:
    items: list[str] = []
    if not isinstance(values, (list, tuple, set)):
        return items
    for value in values:
        if isinstance(value, dict):
            for key in keys:
                text = str(value.get(key) or "").strip()
                if text:
                    items.append(text)
                    break
        else:
            text = str(value or "").strip()
            if text:
                items.append(text)
    return sorted(dict.fromkeys(items))


def package_security_signals(page_module: Any, page: Any) -> list[str]:
    signals: list[str] = []
    geiger = sanitized_geiger(page)
    if geiger:
        signals.extend(str(item) for item in geiger.get("reasons") or [])
    if page.approval_gate:
        rule_count = page.approval_gate.get("rule_count")
        if rule_count:
            signals.append(f"{rule_count} approval-gate rules")
        signals.extend(str(item) for item in page.approval_gate.get("rules") or [])
    return sorted(dict.fromkeys(item.strip() for item in signals if item and item.strip()))


STALE_NO_EXECUTABLE_REASON = "no executable entrypoint in the package index"
STALE_NO_EXECUTABLE_SIGNAL = "metadata:no-indexed-executables"


def has_executable_evidence(page: Any) -> bool:
    return bool(getattr(page, "executables", None) or getattr(page, "aliases", None) or getattr(page, "binaries", None))


def sanitized_geiger(page: Any) -> dict[str, Any] | None:
    geiger = getattr(page, "geiger", None)
    if not isinstance(geiger, dict) or not has_executable_evidence(page):
        return geiger
    cleaned = dict(geiger)
    cleaned["reasons"] = [
        reason
        for reason in (geiger.get("reasons") or [])
        if str(reason).strip() != STALE_NO_EXECUTABLE_REASON
    ]
    cleaned["signals"] = [
        signal
        for signal in (geiger.get("signals") or [])
        if str(signal).strip() != STALE_NO_EXECUTABLE_SIGNAL
    ]
    return cleaned if cleaned.get("reasons") else None


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(jsonable(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def is_scoped_npm_package(page: Any) -> bool:
    name = str(getattr(page, "name", ""))
    return str(getattr(page, "provider", "")) == "npm" and name.startswith("@") and "/" in name


def route_hash(page: Any) -> str:
    return hashlib.sha256(str(getattr(page, "key", "")).encode("utf-8")).hexdigest()[:8]


def collision_slug_candidates(page: Any, base_slug: str) -> list[str]:
    candidates: list[str] = []
    if is_scoped_npm_package(page):
        candidates.append(f"scoped-{base_slug}")
    candidates.extend([base_slug, f"{base_slug}-{route_hash(page)}"])
    return candidates


def package_routes(pages: list[Any]) -> dict[str, PackageRoute]:
    groups: dict[tuple[str, str], list[Any]] = {}
    base_slugs_by_provider: dict[str, set[str]] = {}
    for page in pages:
        provider = str(getattr(page, "provider", ""))
        slug = str(getattr(page, "slug", ""))
        groups.setdefault((provider, slug), []).append(page)
        base_slugs_by_provider.setdefault(provider, set()).add(slug)

    assigned_by_provider: dict[str, set[str]] = {}
    routes: dict[str, PackageRoute] = {}
    for (provider, base_slug), group in sorted(groups.items()):
        if len(group) > 1:
            group = sorted(group, key=lambda page: (is_scoped_npm_package(page), str(getattr(page, "name", ""))))
        assigned = assigned_by_provider.setdefault(provider, set())
        for page in group:
            candidates = [base_slug] if len(group) == 1 else collision_slug_candidates(page, base_slug)
            slug = ""
            for candidate in candidates:
                if candidate in assigned:
                    continue
                if candidate != base_slug and candidate in base_slugs_by_provider.get(provider, set()):
                    continue
                slug = candidate
                break
            if not slug:
                slug = f"{base_slug}-{route_hash(page)}"
            assigned.add(slug)
            routes[str(getattr(page, "key", ""))] = PackageRoute(
                path=f"/{provider}/{slug}/",
                slug=slug,
            )
    return routes


def full_package_data(page_module: Any, page: Any, route: PackageRoute | None = None) -> dict[str, Any]:
    extra = getattr(page, "extra", {})
    if not isinstance(extra, dict):
        extra = {}
    slug = route.slug if route else getattr(page, "slug", "")
    path = route.path if route else getattr(page, "path", "")
    return {
        "provider": getattr(page, "provider", ""),
        "name": getattr(page, "name", ""),
        "displayName": getattr(page, "display_name", ""),
        "key": getattr(page, "key", ""),
        "slug": slug,
        "path": path,
        "summary": getattr(page, "summary", ""),
        "homepage": getattr(page, "homepage", ""),
        "version": getattr(page, "version", ""),
        "lastUpdatedAt": getattr(page, "last_updated_at", ""),
        "pulseKind": getattr(page, "pulse_kind", ""),
        "url": getattr(page, "url", ""),
        "sha256": getattr(page, "sha256", ""),
        "binaries": getattr(page, "binaries", []),
        "popularity": getattr(page, "popularity", {}),
        "aliases": sorted(str(item) for item in getattr(page, "aliases", [])),
        "sourceNotes": getattr(page, "source_notes", []),
        "combinedYamlPath": getattr(page, "combined_yaml_path", ""),
        "combinedYamlUrl": getattr(page, "combined_yaml_url", ""),
        "packageManager": getattr(page, "package_manager", ""),
        "packageManagerUrl": getattr(page, "package_manager_url", ""),
        "repository": getattr(page, "repository", ""),
        "upstreamDocs": getattr(page, "upstream_docs", ""),
        "configFileLocations": getattr(page, "config_file_locations", {}),
        "credentialsFileLocations": getattr(page, "credentials_file_locations", {}),
        "history": getattr(page, "history", None),
        "category": getattr(page, "category", ""),
        "license": getattr(page, "license", ""),
        "sourceArchive": getattr(page, "source_archive", ""),
        "lastVerified": getattr(page, "last_verified", ""),
        "dependencies": getattr(page, "dependencies", []),
        "buildDependencies": getattr(page, "build_dependencies", []),
        "usesFromMacos": getattr(page, "uses_from_macos", []),
        "install": getattr(page, "install", {}),
        "installCommands": getattr(page, "install_commands", []),
        "executablesDetailed": getattr(page, "executables", []),
        "installBehavior": getattr(page, "install_behavior", {}),
        "bottle": getattr(page, "bottle", {}),
        "publishedAt": getattr(page, "published_at", ""),
        "keywords": getattr(page, "keywords", []),
        "issueTracker": getattr(page, "issue_tracker", ""),
        "classifiers": getattr(page, "classifiers", []),
        "projectUrls": getattr(page, "project_urls", {}),
        "versionFreshness": getattr(page, "version_freshness", {}),
        "geiger": sanitized_geiger(page),
        "relatedPackages": getattr(page, "related_packages", []),
        "alsoAvailableVia": getattr(page, "also_available_via", []),
        "packageHubs": getattr(page, "package_hubs", []),
        "agentSafetyAnswer": getattr(page, "agent_safety_answer", None),
        "approvalGate": getattr(page, "approval_gate", None),
        "registryInsights": extra.get("registryInsights", {}),
        "externalPackageManagerMatches": getattr(page, "external_package_manager_matches", []),
        "extra": extra,
    }


def package_data(page_module: Any, page: Any, route: PackageRoute) -> dict[str, Any]:
    data = {
        "aliases": sorted(str(item) for item in getattr(page, "aliases", [])),
        "binaries": string_items(getattr(page, "binaries", [])),
        "classifiers": string_items(getattr(page, "classifiers", [])),
        "executables": string_items(getattr(page, "executables", [])),
        "hubs": [
            {
                "slug": str(hub.get("slug") or ""),
                "label": str(hub.get("label") or ""),
                "reason": str(hub.get("reason") or ""),
            }
            for hub in getattr(page, "package_hubs", [])
            if isinstance(hub, dict)
        ],
        "keywords": string_items(getattr(page, "keywords", [])),
        "related": string_items(getattr(page, "related_packages", []), ("label", "name", "target", "package", "key")),
        "security": package_security_signals(page_module, page),
    }
    data["full"] = jsonable(full_package_data(page_module, page, route))
    return data


def package_record(page_module: Any, page: Any, route: PackageRoute, search_text: str) -> PackageRecord:
    return PackageRecord(
        path=route.path,
        provider=page.provider,
        slug=route.slug,
        package_key=page.key,
        name=page.name,
        display_name=page.display_name,
        summary=page_module.short_text(page_module.clean_summary(page.summary) or page_module.hero_sentence(page), 320),
        provider_label=page_module.package_manager_label(page),
        package_manager_url=getattr(page, "package_manager_url", ""),
        install_command=page_module.install_command(page),
        native_install_command=page_module.native_install_command(page),
        version=getattr(page, "version", ""),
        category=getattr(page, "category", ""),
        license=getattr(page, "license", ""),
        homepage=getattr(page, "homepage", ""),
        repository=getattr(page, "repository", ""),
        rank=page.popularity.get("rank") if isinstance(page.popularity, dict) else None,
        last_updated_at=getattr(page, "last_updated_at", ""),
        indexable=page_module.is_indexable_package_page(page),
        data=package_data(page_module, page, route),
        search_text=search_text,
    )


def hub_record(hub: Any) -> HubRecord:
    return HubRecord(
        path=hub.path,
        slug=hub.slug,
        title=hub.title,
        description=hub.description,
        group=hub.group,
        data={
            "match": getattr(hub, "match", ""),
            "source": getattr(hub, "source", ""),
        },
    )


def build_sqlite(
    page_module: Any,
    output_path: Path,
) -> tuple[int, int, int, dict[str, Any]]:
    sources = page_module.load_sources()
    pages_by_key = page_module.package_pages_from_sources(sources)
    from project_history import validate_history_cohort
    validate_history_cohort(pages_by_key)
    if not pages_by_key:
        raise RuntimeError("no package metadata found")
    pages = sorted(pages_by_key.values(), key=lambda page: (page.provider, page.slug, page.name))
    del sources, pages_by_key
    gc.collect()
    routes = package_routes(pages)
    hubs = page_module.package_hub_pages(pages)
    files = page_module.source_files()
    previous_manifest = previous_manifest_from_sqlite(output_path)
    manifest = page_module.build_manifest(len(pages), files, previous_manifest)
    populate_manifest_counts(page_module, manifest, pages, hubs)
    indexable_pages = [page for page in pages if page_module.is_indexable_package_page(page)]
    locales = page_module.i18n_locales()
    metadata = manifest_metadata(manifest)
    metadata["locales"] = locales
    metadata["providers"] = sorted({page.provider for page in indexable_pages})

    def package_rows() -> Iterable[PackageRecord]:
        for page in pages:
            yield package_record(
                page_module,
                page,
                routes[page.key],
                page_search_text(page_module, page, None),
            )

    def search_documents() -> Iterable[SearchDocument]:
        for locale in locales:
            locale_code = page_module.locale_code(locale)
            for page in pages:
                yield SearchDocument(
                    path=page_module.locale_path(routes[page.key].path, locale),
                    locale=locale_code,
                    title=page.display_name,
                    summary=page_module.short_text(
                        page_module.clean_summary(page.summary) or page_module.hero_sentence(page),
                        180,
                    ),
                    provider=page.provider,
                    package_key=page.key,
                    rank=page.popularity.get("rank") if isinstance(page.popularity, dict) else None,
                    search_text=page_search_text(page_module, page, locale),
                )

    def hub_package_rows() -> Iterable[HubPackageRecord]:
        for hub, hub_pages in hubs:
            for position, page in enumerate(hub_pages, start=1):
                yield HubPackageRecord(
                    hub_slug=hub.slug,
                    package_key=page.key,
                    position=position,
                    reason=page_module.hub_package_reason(page),
                )

    write_sqlite(
        output_path,
        search_documents(),
        package_rows(),
        (hub_record(hub) for hub, _hub_pages in hubs),
        hub_package_rows(),
        metadata,
    )
    return len(pages), len(hubs), len(pages) * len(locales), metadata


def check_current(page_module: Any, output_path: Path, terminal: Terminal) -> int:
    if not output_path.exists():
        terminal.error(f"Missing {output_path}. Run scripts/generate-pkg-sqlite.py.")
        return 1
    try:
        connection = sqlite3.connect(output_path)
        try:
            metadata = {
                key: json.loads(value)
                for key, value in connection.execute("SELECT key, value FROM metadata")
            }
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except (sqlite3.Error, json.JSONDecodeError) as err:
        terminal.error(f"Invalid {output_path}: {err}")
        return 1
    failures: list[str] = []
    if metadata.get("schema") != SCHEMA_VERSION:
        failures.append(f"schema is {metadata.get('schema')!r}, expected {SCHEMA_VERSION}")
    if not integrity or integrity[0] != "ok":
        failures.append(f"integrity_check failed: {integrity}")
    expected_hash, _latest = page_module.source_digest(page_module.source_files())
    if metadata.get("source_hash") != expected_hash:
        failures.append("source hash does not match current package source files")
    manifest = metadata.get("manifest") if isinstance(metadata.get("manifest"), dict) else {}
    if not manifest:
        failures.append("manifest metadata is missing")
    if failures:
        terminal.error("Package SQLite artifact is stale.")
        for failure in failures:
            terminal.log(f"  - {failure}")
        terminal.log("Run scripts/generate-pkg-sqlite.py and retry deploy.")
        return 1
    terminal.ok(f"Package SQLite artifact is current ({output_path})")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Atlas package-origin SQLite artifact.")
    parser.add_argument("--check", action="store_true", help="Validate that pkg.sqlite matches current package source data.")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help=f"Output SQLite path. Defaults to {OUTPUT_PATH}.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable status.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_cwd()
    terminal = Terminal(json_mode=args.json)
    output_path = Path(args.output)
    page_module = load_pkg_pages_module()

    if not PKG_CROSS_ECOSYSTEM_PATH.is_file():
        terminal.error(
            f"Missing {PKG_CROSS_ECOSYSTEM_PATH}; run scripts/generate-pkg-cross-ecosystem.py before building SQLite."
        )
        return 1

    if args.check:
        return check_current(page_module, output_path, terminal)

    terminal.step("Rendering package pages into SQLite")
    package_count, hub_count, document_count, metadata = build_sqlite(page_module, output_path)
    terminal.ok(
        f"Wrote {package_count:,} packages, {hub_count:,} hubs, "
        f"{document_count:,} search documents, and metadata to {output_path}"
    )
    if args.json:
        print(json.dumps({
            "ok": True,
            "output": str(output_path),
            "packages": package_count,
            "hubs": hub_count,
            "search_documents": document_count,
            "source_hash": metadata.get("source_hash"),
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
