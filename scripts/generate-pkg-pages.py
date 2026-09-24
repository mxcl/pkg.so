#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import textwrap
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
AV_DB_ROOT = SCRIPT_DIR.parent
for import_dir in (SCRIPT_DIR,):
    if str(import_dir) not in sys.path:
        sys.path.insert(0, str(import_dir))

from avdb_paths import (
    PACKAGE_INDEX_PATH,
    ISOTOPE_REPO_CACHE_DIR,
    ISOTOPES_JSON_PATH,
    RADIOISOTOPES_REPO_DIR,
)
from bootstrap.lib.render import parse_simple_yaml
from geiger_agent_data import load_agent_geiger_data
from pkg_hub_data import graph_hub_definitions, load_pkg_taxonomy_index, taxonomy_brief, taxonomy_for_package, taxonomy_terms


SCHEMA_VERSION = 1
SITE_ORIGIN = "https://pkg.so"
SITE_NAME = "pkg.so"
OUTPUT_DIR = Path("cache/pkg-pages-render")
STATIC_PACKAGE_PAGE_OPT_IN = "AV_ALLOW_STATIC_PKG_PAGES"
MANIFEST_NAME = ".manifest.json"
GENERATED_DATA_DIR = Path("cache")
PKG_PAGE_ENRICHMENT_PATH = GENERATED_DATA_DIR / "pkg-page-enrichment.json"
PKG_VERSION_FRESHNESS_PATH = GENERATED_DATA_DIR / "pkg-version-freshness.json"
PKG_GRAPH_PATH = GENERATED_DATA_DIR / "pkg-graph.json"
PKG_GRAPH_CURATION_PATH = GENERATED_DATA_DIR / "pkg-graph-curation.json"
PKG_CROSS_ECOSYSTEM_PATH = GENERATED_DATA_DIR / "pkg-cross-ecosystem.json"
CRATES_IO_INDEX_PATH = GENERATED_DATA_DIR / "cratesio" / "index.json"
PKG_AGENT_SAFETY_ANSWERS_PATH = Path("data/pkg-agent-safety-answers.json")
I18N_LOCALES_PATH = Path("data/pkg-i18n/locales.json")
I18N_PKG_TEMPLATES_PATH = Path("data/pkg-i18n/templates.json")
COMBINED_YAML_DIR = Path("combined")
COMBINED_YAML_GITHUB_BASE = "https://github.com/mxcl/pkgdb/blob/main"
INDEXABLE_MIN_SIGNAL_COUNT = 2
PACKAGE_PROVIDERS = ("brew", "cask", "npm", "pip", "cargo")
GOOGLE_TAG = """  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-CMPY7XG4QH"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){dataLayer.push(arguments);}
    gtag('js', new Date());

    gtag('config', 'G-CMPY7XG4QH');
  </script>"""

PUBLIC_COPY_REPLACEMENTS = (
    ("isotopes-only", "detector-only"),
    ("isotope-only", "detector-only"),
    ("Radioisotope coverage", "Protected-tool coverage"),
    ("radioisotope coverage", "protected-tool coverage"),
    ("Radioisotope Coverage", "Protected-tool Coverage"),
    ("Radioisotope secret handling", "protected-tool secret handling"),
    ("radioisotope secret handling", "protected-tool secret handling"),
    ("Radioisotope security manifest", "Secret-handling manifest"),
    ("radioisotope security manifest", "secret-handling manifest"),
    ("Radioisotope manifests", "secret-handling manifests"),
    ("radioisotope manifests", "secret-handling manifests"),
    ("Radioisotope manifest", "secret-handling manifest"),
    ("radioisotope manifest", "secret-handling manifest"),
    ("Radioisotope READMEs", "secret-handling READMEs"),
    ("radioisotope READMEs", "secret-handling READMEs"),
    ("Radioisotope README", "secret-handling README"),
    ("radioisotope README", "secret-handling README"),
    ("Radioisotopes", "Protected tools"),
    ("radioisotopes", "protected tools"),
    ("Radioisotope", "Protected-tool coverage"),
    ("radioisotope", "protected-tool coverage"),
    ("Automic Vault isotope checks", "Automic Vault package-specific checks"),
    ("Automic Vault isotope detectors", "Automic Vault package-specific detectors"),
    ("package-specific isotope detectors", "package-specific detectors"),
    ("isotope detectors", "package-specific detectors"),
    ("Isotope detectors", "Package-specific detectors"),
    ("isotope checks", "package-specific checks"),
    ("Isotope checks", "Package-specific checks"),
    ("local isotope README", "local coverage README"),
    ("Local isotope README", "Local coverage README"),
    ("isotopes", "protected tools"),
    ("Isotopes", "Protected tools"),
    ("isotope", "protected tool"),
    ("Isotope", "Protected tool"),
)


def public_copy(value: Any) -> str:
    text = str(value or "")
    for old, new in PUBLIC_COPY_REPLACEMENTS:
        text = text.replace(old, new)
    return text

_I18N_LOCALES_CACHE: list[dict[str, Any]] | None = None
_I18N_PKG_TEMPLATES_CACHE: dict[str, dict[str, str]] | None = None

REQUIRED_I18N_PKG_KEYS = {
    "additionalInstallCommands",
    "agentRiskAssessment",
    "agentRiskQuestion",
    "agentRiskWhy",
    "alsoAvailableVia",
    "approvalGatesKicker",
    "approvalRules",
    "atAGlance",
    "binaries",
    "bottle",
    "buildDependencies",
    "catalogCounts",
    "catalogEyebrow",
    "catalogHubsAria",
    "catalogHubsCopy",
    "catalogHubsKicker",
    "catalogHubsTitle",
    "catalogListAria",
    "catalogPagesCopy",
    "catalogPagesKicker",
    "catalogPagesTitle",
    "catalogSearchCopy",
    "classifierConfidence",
    "classifiers",
    "command",
    "commandsAndAliases",
    "copy",
    "copyManagerInstallCommand",
    "coverageSource",
    "crawlableCatalog",
    "dependencies",
    "executableDataMissing",
    "executables",
    "executablesCount",
    "executablesTitle",
    "exposure",
    "freshness",
    "freshnessCopy",
    "freshnessTitle",
    "generatedFromRepositoryData",
    "generatedSource",
    "generatedSourceCopy",
    "geigerRisk",
    "heroPanelAria",
    "homepage",
    "homepageMissing",
    "hubCounts",
    "hubDescription",
    "hubApprovalGatedTitle",
    "hubEcosystemGroupTitle",
    "hubHighSignalTitle",
    "hubIndexedPagesTitle",
    "hubPackageReasonAlias",
    "hubPackageReasonApproval",
    "hubPackageReasonDefault",
    "hubProtectedToolsTitle",
    "hubReviewCopy",
    "hubReviewModel",
    "hubRelatedHubsTitle",
    "hubRepresentativeSpokesTitle",
    "hubSchemaDescription",
    "hubSecurityGroupTitle",
    "hubSummaryTitle",
    "hubTopicalGroupTitle",
    "install",
    "installBehavior",
    "installBehaviorTitle",
    "installCommand",
    "installRoutes",
    "installSourceMissing",
    "installSourcePrefix",
    "installSupportTitle",
    "installSupportCopy",
    "issueTracker",
    "kind",
    "keywords",
    "license",
    "localData",
    "localReadmeExcerpt",
    "manager",
    "managerUpdated",
    "managerVersion",
    "markdownEvidence",
    "metadataEmpty",
    "metadataTitle",
    "noAliases",
    "noApprovalRules",
    "noCaveats",
    "noClassifierReasons",
    "noClassifierSignals",
    "noCrossEcosystem",
    "noFreshnessWarnings",
    "noHubMembership",
    "noRelated",
    "note",
    "overview",
    "packageCatalogTitle",
    "packageFacts",
    "packageGraph",
    "packageGraphCopy",
    "packageHubs",
    "packageManagers",
    "packageManagersCount",
    "packageKey",
    "packageManager",
    "packageManagerPage",
    "packageMetadata",
    "packageMetadataKicker",
    "packageSummary",
    "packageTaxonomy",
    "pageGenerated",
    "platformInstallCommands",
    "popularPackages",
    "published",
    "pythonFormula",
    "pythonRequires",
    "radioisotopeCoverage",
    "radioisotopeKicker",
    "radioisotopeMissingHeading",
    "radioisotopeMissingSummary",
    "recommendedReview",
    "recommendedReviewCopy",
    "recommendedHumanReviewItems",
    "related",
    "relatedLinks",
    "relatedPackages",
    "relatedTools",
    "repository",
    "reviewed",
    "risk",
    "riskClassifier",
    "riskLevel",
    "schemaHowToName",
    "schemaHowToStep",
    "schemaTechArticleHeadline",
    "schemaFaqName",
    "securityNotes",
    "securityPosture",
    "service",
    "serviceNone",
    "signals",
    "sameWorkflow",
    "source",
    "sourceArchive",
    "sourceDatabaseAria",
    "sourceExcerpt",
    "sourceSummary",
    "sourceTrail",
    "sources",
    "sourcesCopy",
    "status",
    "summaryFallback",
    "summaryPlainFallback",
    "supportedOn",
    "taxonomyEvidence",
    "topicalHubs",
    "upstream",
    "upstreamDocs",
    "upstreamLatestDetected",
    "usedSources",
    "usesFromMacos",
    "verified",
    "version",
    "versionAndFreshness",
    "why",
}


def i18n_locales() -> list[dict[str, Any]]:
    global _I18N_LOCALES_CACHE
    if _I18N_LOCALES_CACHE is not None:
        return _I18N_LOCALES_CACHE
    try:
        data = read_json(I18N_LOCALES_PATH)
    except FileNotFoundError:
        _I18N_LOCALES_CACHE = [{"code": "en", "slug": "", "htmlLang": "en", "hreflang": "en", "nativeName": "English"}]
        return _I18N_LOCALES_CACHE
    _I18N_LOCALES_CACHE = [
        item
        for item in data.get("locales", [])
        if item.get("enabled") and item.get("code")
    ]
    return _I18N_LOCALES_CACHE


def non_default_i18n_locales() -> list[dict[str, Any]]:
    return [locale for locale in i18n_locales() if locale.get("code") != "en"]


def i18n_pkg_templates() -> dict[str, dict[str, str]]:
    global _I18N_PKG_TEMPLATES_CACHE
    if _I18N_PKG_TEMPLATES_CACHE is not None:
        return _I18N_PKG_TEMPLATES_CACHE
    try:
        _I18N_PKG_TEMPLATES_CACHE = read_json(I18N_PKG_TEMPLATES_PATH, {})
    except FileNotFoundError:
        _I18N_PKG_TEMPLATES_CACHE = {}
    return _I18N_PKG_TEMPLATES_CACHE


def locale_code(locale: dict[str, Any] | None) -> str:
    return str((locale or {}).get("code") or "en")


def locale_slug(locale: dict[str, Any] | None) -> str:
    return str((locale or {}).get("slug") or "")


def locale_path(path: str, locale: dict[str, Any] | None = None) -> str:
    if path in {"/pkg", "/pkg/"}:
        path = "/"
    elif path.startswith("/pkg/"):
        path = path[4:]
    slug = locale_slug(locale)
    if not slug:
        return path
    if path == "/":
        return f"/{slug}/"
    return f"/{slug}{path}"


def locale_url(path: str, locale: dict[str, Any] | None = None) -> str:
    return f"{SITE_ORIGIN}{locale_path(path, locale)}"


def tx(locale: dict[str, Any] | None, key: str, default: str, **kwargs: Any) -> str:
    code = locale_code(locale)
    templates = i18n_pkg_templates().get(code, {})
    if code != "en" and key in REQUIRED_I18N_PKG_KEYS and key not in templates:
        raise KeyError(f"missing package i18n template: {code}.{key}")
    template = templates.get(key, default)
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError):
        return default.format(**kwargs)


def validate_i18n_pkg_templates() -> list[str]:
    failures: list[str] = []
    templates = i18n_pkg_templates()
    for locale in non_default_i18n_locales():
        code = locale_code(locale)
        locale_templates = templates.get(code)
        if not isinstance(locale_templates, dict):
            failures.append(f"missing package i18n templates for {code}")
            continue
        missing = sorted(REQUIRED_I18N_PKG_KEYS - set(locale_templates))
        if missing:
            failures.append(f"{code} package i18n templates are missing keys: {', '.join(missing[:24])}")
    return failures


LOCALIZED_UI_ENGLISH_SNIPPETS = (
    ">Package summary<",
    ">Installed executables<",
    ">Version and freshness<",
    ">Package metadata<",
    ">Related packages<",
    ">Generated from repository data<",
    ">Security Notes<",
    ">Package Facts<",
)


def localized_package_ui_leaks(html_text: str) -> list[str]:
    return [snippet for snippet in LOCALIZED_UI_ENGLISH_SNIPPETS if snippet in html_text]


@dataclass
class PackagePage:
    provider: str
    name: str
    summary: str = ""
    homepage: str = ""
    version: str = ""
    last_updated_at: str = ""
    pulse_kind: str = ""
    url: str = ""
    sha256: str = ""
    binaries: list[dict[str, Any]] = field(default_factory=list)
    popularity: dict[str, Any] = field(default_factory=dict)
    aliases: set[str] = field(default_factory=set)
    source_notes: list[str] = field(default_factory=list)
    package_manager: str = ""
    package_manager_url: str = ""
    repository: str = ""
    upstream_docs: str = ""
    config_file_locations: dict[str, Any] = field(default_factory=dict)
    credentials_file_locations: dict[str, Any] = field(default_factory=dict)
    history: dict[str, Any] | None = None
    combined_yaml_path: str = ""
    combined_yaml_url: str = ""
    category: str = ""
    license: str = ""
    source_archive: str = ""
    last_verified: str = ""
    dependencies: list[str] = field(default_factory=list)
    build_dependencies: list[str] = field(default_factory=list)
    uses_from_macos: list[str] = field(default_factory=list)
    install: dict[str, Any] = field(default_factory=dict)
    install_commands: list[dict[str, Any]] = field(default_factory=list)
    executables: list[dict[str, Any]] = field(default_factory=list)
    install_behavior: dict[str, Any] = field(default_factory=dict)
    bottle: dict[str, Any] = field(default_factory=dict)
    published_at: str = ""
    keywords: list[str] = field(default_factory=list)
    issue_tracker: str = ""
    classifiers: list[str] = field(default_factory=list)
    project_urls: dict[str, str] = field(default_factory=dict)
    version_freshness: dict[str, Any] = field(default_factory=dict)
    geiger: dict[str, Any] | None = None
    related_packages: list[dict[str, Any]] = field(default_factory=list)
    also_available_via: list[dict[str, Any]] = field(default_factory=list)
    external_package_manager_matches: list[dict[str, Any]] = field(default_factory=list)
    package_hubs: list[dict[str, Any]] = field(default_factory=list)
    agent_safety_answer: dict[str, str] | None = None
    isotope: dict[str, Any] | None = None
    isotope_readme: str = ""
    isotope_readme_html: str = ""
    isotope_readme_source: str = ""
    approval_gate: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.name}"

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def path(self) -> str:
        return f"/{self.provider}/{self.slug}/"

    @property
    def display_name(self) -> str:
        if self.provider == "npm" and self.name.startswith("@"):
            return self.name
        return self.name


@dataclass(frozen=True)
class ReadmeExcerpt:
    summary: str
    html: str
    source: str


@dataclass(frozen=True)
class PackageHub:
    slug: str
    title: str
    kicker: str
    description: str
    query_terms: tuple[str, ...] = ()
    package_names: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    category_paths: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    group: str = "topical"
    priority: int = 100
    reason: str = ""
    risk_hub: bool = False

    @property
    def path(self) -> str:
        return f"/{self.slug}/"


def load_package_hubs() -> tuple[PackageHub, ...]:
    hubs = []
    for slug, info in graph_hub_definitions().items():
        hubs.append(PackageHub(
            slug=slug,
            title=str(info.get("label") or slug),
            kicker=str(info.get("kicker") or "package hub"),
            description=str(info.get("description") or "Generated package hub."),
            query_terms=tuple(info.get("terms") or ()),
            package_names=tuple(info.get("names") or ()),
            providers=tuple(info.get("providers") or ()),
            categories=tuple(info.get("categories") or ()),
            category_paths=tuple(info.get("categoryPaths") or ()),
            tags=tuple(info.get("tags") or ()),
            group=str(info.get("group") or "topical"),
            priority=int(info.get("priority") or 100),
            reason=str(info.get("reason") or ""),
            risk_hub=bool(info.get("riskHub")),
        ))
    return tuple(sorted(hubs, key=lambda hub: (hub.priority, hub.title.lower(), hub.slug)))


PACKAGE_HUBS = load_package_hubs()


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
            self.dim = "\033[2m"
            self.red = "\033[31m"
            self.green = "\033[32m"
            self.blue = "\033[34m"
            self.yellow = "\033[33m"
            self.reset = "\033[0m"
            self.step = "◆"
            self.ok = "✓"
            self.warn = "!"
            self.error = "✗"
        else:
            self.bold = self.dim = self.red = self.green = self.blue = self.yellow = self.reset = ""
            self.step = ">"
            self.ok = "OK"
            self.warn = "WARN"
            self.error = "ERROR"

    def log(self, message: str = "") -> None:
        if not self.json_mode:
            print(message, file=sys.stderr)

    def header(self, title: str, detail: str) -> None:
        self.log(f"{self.bold}{title}{self.reset}")
        self.log(f"{self.dim}{detail}{self.reset}")

    def step_log(self, message: str) -> None:
        self.log(f"{self.blue}{self.step}{self.reset} {self.bold}{message}{self.reset}")

    def ok_log(self, message: str) -> None:
        self.log(f"  {self.green}{self.ok}{self.reset} {message}")

    def warn_log(self, message: str) -> None:
        self.log(f"  {self.yellow}{self.warn}{self.reset} {message}")

    def error_log(self, message: str) -> None:
        self.log(f"{self.red}{self.error}{self.reset} {message}")


@dataclass
class RenderStats:
    written: int = 0
    unchanged: int = 0
    deleted: int = 0
    directories_removed: int = 0


def ensure_cwd() -> Path:
    os.chdir(AV_DB_ROOT)
    return AV_DB_ROOT


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    value = value.lower().strip()
    if value.startswith("@"):
        value = value[1:]
    value = value.replace("@", "-")
    value = value.replace("+", "plus")
    value = value.replace("/", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = value.strip("-")
    return value or "package"


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def npm_ranks_by_downloads(items: dict[str, Any]) -> dict[str, int]:
    candidates: list[tuple[int, str]] = []
    for name, info in items.items():
        if not isinstance(name, str) or not isinstance(info, dict):
            continue
        popularity = info.get("popularity") or {}
        if not isinstance(popularity, dict):
            continue
        downloads = positive_int(popularity.get("downloads_per_30_days"))
        if downloads is not None:
            candidates.append((downloads, name))
    return {
        name: rank
        for rank, (_downloads, name) in enumerate(
            sorted(candidates, key=lambda item: (-item[0], item[1])),
            start=1,
        )
    }


def short_text(value: Any, limit: int = 220) -> str:
    text = normalize_space(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def paragraph_text(value: Any, limit: int = 720) -> str:
    text = normalize_space(value)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "."


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
    text = normalize_space(text)
    text = re.sub(r"(\.\s*){2,}", ". ", text).strip(" ,-")
    match = re.search(
        r"\b([A-Z][A-Za-z0-9 .+/_-]{1,80}\s+is\s+[A-Za-z0-9])",
        text,
    )
    if match and re.search(r"^(npm|npx|pnpm|yarn|bun|brew|pip|uv)\s+", text, flags=re.IGNORECASE):
        text = text[match.start():]
    return paragraph_text(text, 720)


def html_escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def attr(value: Any) -> str:
    return html_escape(value)


def fmt_int(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def fmt_date(value: str) -> str:
    if not value:
        return ""
    return value[:10]


def load_sources() -> dict[str, Any]:
    return {
        "db": read_json(PACKAGE_INDEX_PATH, {}),
        "geiger": load_agent_geiger_data(),
        "isotopes": read_json(ISOTOPES_JSON_PATH, {}),
        "npm": read_json(Path("data/npm.json"), {}),
        # These are the two largest inputs. Load them one at a time after the
        # base pages exist instead of retaining both expanded JSON trees for
        # the entire build.
        "pkg_graph": None,
        "pkg_cross_ecosystem": None,
        "pkg_page_enrichment": read_json(PKG_PAGE_ENRICHMENT_PATH, {}),
        "pkg_version_freshness": read_json(PKG_VERSION_FRESHNESS_PATH, {}),
        "pkg_agent_safety_answers": read_json(PKG_AGENT_SAFETY_ANSWERS_PATH, {}),
        "pip": read_json(Path("data/pip.json"), {}),
        "crates": read_json(CRATES_IO_INDEX_PATH, {}),
    }


def normalize_path_locations(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for platform, raw_locations in sorted(value.items(), key=lambda item: str(item[0])):
        platform_text = normalize_space(platform)
        if not platform_text:
            continue
        locations: list[str] = []
        if isinstance(raw_locations, list):
            locations = [normalize_space(item) for item in raw_locations]
        elif isinstance(raw_locations, str):
            locations = [normalize_space(raw_locations)]
        locations = [item for item in dict.fromkeys(locations) if item]
        if len(locations) == 1:
            normalized[platform_text] = locations[0]
        elif locations:
            normalized[platform_text] = locations
    return normalized


def load_combined_yaml_records(root: Path = COMBINED_YAML_DIR) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return records
    for path in sorted(root.glob("*.yml")):
        try:
            record = parse_simple_yaml(path.read_text(encoding="utf-8"))
        except Exception as err:
            raise ValueError(f"could not read {path}: {err}") from err
        package_id = normalize_space(record.get("id"))
        if package_id:
            record["_combined_yaml_path"] = path.as_posix()
            record["_combined_yaml_url"] = f"{COMBINED_YAML_GITHUB_BASE}/{path.as_posix()}"
            records[package_id] = record
    return records


def apply_combined_yaml_locations(pages: dict[str, PackagePage]) -> None:
    records = load_combined_yaml_records()
    for package_key, page in pages.items():
        record = records.get(package_key)
        if not record:
            continue
        page.combined_yaml_path = normalize_space(record.get("_combined_yaml_path"))
        page.combined_yaml_url = normalize_space(record.get("_combined_yaml_url"))
        config_locations = normalize_path_locations(record.get("config-file-location"))
        if config_locations:
            page.config_file_locations = config_locations
        credentials_locations = normalize_path_locations(record.get("credentials-file-location"))
        if credentials_locations:
            page.credentials_file_locations = credentials_locations
        history = record.get("history")
        if isinstance(history, dict):
            page.history = history
        if config_locations or credentials_locations:
            page.source_notes.append("curated configuration and credential file locations")
        if page.history:
            page.source_notes.append("curated package history")


def package_pages_from_sources(
    sources: dict[str, Any],
    *,
    cross_ecosystem_only: bool = False,
) -> dict[str, PackagePage]:
    pages: dict[str, PackagePage] = {}
    db = sources.get("db") or {}

    def get_page(provider: str, name: str) -> PackagePage:
        key = f"{provider}:{name}"
        page = pages.get(key)
        if page is None:
            page = PackagePage(provider=provider, name=name)
            pages[key] = page
        return page

    for provider, section in (("brew", "formulas"), ("cask", "casks"), ("npm", "npms")):
        items = db.get(section) or {}
        if not isinstance(items, dict):
            continue
        npm_ranks = npm_ranks_by_downloads(items) if provider == "npm" else {}
        for name, info in items.items():
            if not isinstance(info, dict):
                continue
            page = get_page(provider, name)
            page.summary = clean_summary(info.get("summary") or page.summary)
            page.homepage = info.get("homepage") or page.homepage
            page.repository = info.get("repository") or info.get("repo") or page.repository
            docs = info.get("docs")
            if not page.upstream_docs and isinstance(docs, list) and docs:
                page.upstream_docs = str(docs[0])
            page.upstream_docs = info.get("upstreamDocs") or page.upstream_docs
            page.config_file_locations = (
                normalize_path_locations(info.get("config-file-location"))
                or page.config_file_locations
            )
            page.credentials_file_locations = (
                normalize_path_locations(info.get("credentials-file-location"))
                or page.credentials_file_locations
            )
            if isinstance(info.get("history"), dict):
                page.history = info["history"]
            page.category = info.get("category") or page.category
            page.version = info.get("version") or page.version
            page.last_updated_at = info.get("last_updated_at") or page.last_updated_at
            page.pulse_kind = info.get("pulse_kind") or page.pulse_kind
            page.url = info.get("url") or page.url
            page.source_archive = info.get("sourceArchive") or page.source_archive
            page.sha256 = info.get("sha256") or page.sha256
            page.binaries = info.get("binaries") or page.binaries
            if info.get("dependencies"):
                page.dependencies = info.get("dependencies") or page.dependencies
            popularity = info.get("popularity") or page.popularity
            if provider == "npm" and isinstance(popularity, dict):
                popularity = dict(popularity)
                rank = npm_ranks.get(name)
                if rank is not None:
                    popularity["rank"] = rank
            page.popularity = popularity
            page.source_notes.append("pkg.so package database")

    for name, info in (sources.get("npm") or {}).items():
        if isinstance(info, dict):
            page = get_page("npm", name)
            page.extra.update({f"npm_{key}": value for key, value in info.items()})
            page.source_notes.append("npm overlay metadata")

    for name, info in (sources.get("pip") or {}).items():
        if isinstance(info, dict):
            page = get_page("pip", name)
            page.extra.update(info)
            page.source_notes.append("Python package overlay metadata")

    crates_index = sources.get("crates") or {}
    crates = crates_index.get("crates") if isinstance(crates_index, dict) else None
    if isinstance(crates, dict):
        for name, info in sorted(crates.items()):
            if not isinstance(name, str) or not name or not isinstance(info, dict):
                continue
            page = get_page("cargo", name)
            page.summary = clean_summary(info.get("summary") or page.summary)
            page.homepage = info.get("homepage") or page.homepage
            page.repository = info.get("repository") or page.repository
            page.upstream_docs = info.get("upstreamDocs") or page.upstream_docs
            page.version = info.get("version") or page.version
            page.last_updated_at = info.get("last_updated_at") or page.last_updated_at
            page.license = info.get("license") or page.license
            page.source_archive = info.get("sourceArchive") or page.source_archive
            page.sha256 = info.get("sha256") or page.sha256
            page.published_at = info.get("publishedAt") or page.published_at
            page.executables = info.get("executables") or page.executables
            page.popularity = info.get("popularity") or page.popularity
            page.package_manager = info.get("packageManager") or "Cargo"
            page.package_manager_url = info.get("packageManagerUrl") or f"https://crates.io/crates/{urllib.parse.quote(name, safe='')}"
            page.extra["registryInsights"] = info.get("registryInsights") or page.extra.get("registryInsights")
            page.source_notes.append("crates.io package index")

    entries = db.get("entries") or {}
    if isinstance(entries, dict):
        for executable, provider_key in entries.items():
            if not isinstance(provider_key, str):
                continue
            if ":" in provider_key:
                provider, name = provider_key.split(":", 1)
                if provider == "formula":
                    provider = "brew"
            else:
                provider, name = "brew", provider_key
            if provider in PACKAGE_PROVIDERS:
                get_page(provider, name).aliases.add(executable)

    if cross_ecosystem_only:
        apply_package_page_enrichment(pages, sources.get("pkg_page_enrichment") or {})
        apply_package_page_supplements(pages)
        return executable_package_pages(pages)

    stub_exclusions = sources.get("stub_exclusions") or {}
    if isinstance(stub_exclusions, dict):
        for package_key, excluded in stub_exclusions.items():
            if not isinstance(package_key, str) or ":" not in package_key:
                continue
            provider, name = package_key.split(":", 1)
            if provider in PACKAGE_PROVIDERS and isinstance(excluded, list):
                page = get_page(provider, name)
                page.extra["stub_exclusions"] = sorted(str(item) for item in excluded)

    geiger_packages = (sources.get("geiger") or {}).get("packages") or {}
    if isinstance(geiger_packages, dict):
        for name, geiger in geiger_packages.items():
            if isinstance(geiger, dict):
                page = get_page("brew", name)
                page.geiger = geiger
                page.source_notes.append("Geiger risk classifier")

    isotope_by_package = isotope_metadata_by_package(sources.get("isotopes") or {})
    for package_key, isotope in isotope_by_package.items():
        provider, name = package_key.split(":", 1)
        page = get_page(provider, name)
        page.isotope = isotope
        page.source_notes.append("secret-handling manifest")

    readmes = radioisotope_readmes()
    fork_readmes = isotope_fork_readmes()
    for page in pages.values():
        if page.isotope:
            isotope_name = str(page.isotope.get("name") or "").removeprefix("isotope:")
            readme = readmes.get(isotope_name)
            if not readme:
                repository_name = str(page.isotope.get("repository") or "").rsplit("/", 1)[-1]
                directory_name = str(page.isotope.get("directory") or "")
                readme = fork_readmes.get(directory_name) or fork_readmes.get(repository_name)
            if readme:
                page.isotope_readme = readme.summary
                page.isotope_readme_html = readme.html
                page.isotope_readme_source = tx(None, "localCoverageNotes", "local coverage notes")
                page.source_notes.append("local coverage README")

    for package_key, gate in approval_gate_metadata_by_package().items():
        provider, name = package_key.split(":", 1)
        page = get_page(provider, name)
        page.approval_gate = gate
        page.source_notes.append("approval-gate seed metadata")

    apply_package_page_enrichment(pages, sources.get("pkg_page_enrichment") or {})
    apply_combined_yaml_locations(pages)
    apply_package_version_freshness(pages, sources.get("pkg_version_freshness") or {})
    apply_package_page_supplements(pages)
    apply_package_taxonomy(pages)
    pages = executable_package_pages(pages)
    graph = sources.get("pkg_graph", {})
    if graph is None:
        graph = read_json(PKG_GRAPH_PATH, {})
    apply_package_graph(pages, graph or {})
    del graph

    cross_ecosystem = sources.get("pkg_cross_ecosystem", {})
    if cross_ecosystem is None:
        cross_ecosystem = read_json(PKG_CROSS_ECOSYSTEM_PATH, {})
    apply_package_cross_ecosystem(pages, cross_ecosystem or {})
    del cross_ecosystem
    prune_missing_relationship_targets(pages)
    verify_local_install_commands(pages)
    apply_agent_safety_answers(pages, sources.get("pkg_agent_safety_answers") or {})

    return pages


AGENT_SAFETY_FIELDS = (
    "summary",
    "credentialAccess",
    "remoteMutation",
    "publishOrArtifactRisk",
    "recommendedControl",
    "agentUseGuidance",
)


def apply_agent_safety_answers(pages: dict[str, PackagePage], data: dict[str, Any]) -> None:
    if not data:
        return
    if int(data.get("schema") or 0) != 1:
        raise ValueError("data/pkg-agent-safety-answers.json schema must be 1")
    priority_keys = data.get("priorityPackageKeys")
    answers = data.get("answers")
    if not isinstance(priority_keys, list) or not all(isinstance(item, str) and item for item in priority_keys):
        raise ValueError("pkg agent safety priorityPackageKeys must be a non-empty string array")
    if len(priority_keys) != len(set(priority_keys)):
        raise ValueError("pkg agent safety priorityPackageKeys contains duplicate keys")
    if not isinstance(answers, dict):
        raise ValueError("pkg agent safety answers must be an object")
    priority_set = set(priority_keys)
    answer_set = set(str(key) for key in answers)
    if answer_set != priority_set:
        missing = sorted(priority_set - answer_set)
        extra = sorted(answer_set - priority_set)
        details = []
        if missing:
            details.append(f"missing answers: {', '.join(missing[:12])}")
        if extra:
            details.append(f"unexpected answers: {', '.join(extra[:12])}")
        raise ValueError("pkg agent safety answers must exactly match priorityPackageKeys (" + "; ".join(details) + ")")
    missing_pages = sorted(key for key in priority_keys if key not in pages)
    if missing_pages:
        print(
            "WARN Skipping pkg agent safety answers for missing package pages: "
            + ", ".join(missing_pages[:12]),
            file=sys.stderr,
        )
    for package_key in priority_keys:
        if package_key not in pages:
            continue
        answer = answers.get(package_key)
        if not isinstance(answer, dict):
            raise ValueError(f"pkg agent safety answer for {package_key} must be an object")
        cleaned: dict[str, str] = {}
        for field in AGENT_SAFETY_FIELDS:
            value = answer.get(field)
            if not isinstance(value, str) or not normalize_space(value):
                raise ValueError(f"pkg agent safety answer for {package_key} missing {field}")
            cleaned[field] = normalize_space(value)
        pages[package_key].agent_safety_answer = cleaned
        pages[package_key].source_notes.append("curated agent safety answer")


def apply_package_taxonomy(pages: dict[str, PackagePage]) -> None:
    taxonomy_index = load_pkg_taxonomy_index()
    for page in pages.values():
        taxonomy = taxonomy_for_package(taxonomy_index, page.provider, page.name)
        if not taxonomy:
            continue
        brief = taxonomy_brief(taxonomy)
        page.extra["pkgTaxonomy"] = brief
        if brief.get("category"):
            page.category = str(brief["category"])
        page.source_notes.append("pkgdb category and tag curation")


def apply_package_page_enrichment(pages: dict[str, PackagePage], enrichment: dict[str, Any]) -> None:
    packages = enrichment.get("packages") if isinstance(enrichment, dict) else None
    if not isinstance(packages, dict):
        return
    for package_key, info in packages.items():
        if not isinstance(package_key, str) or ":" not in package_key or not isinstance(info, dict):
            continue
        provider, name = package_key.split(":", 1)
        if provider not in PACKAGE_PROVIDERS or not name:
            continue
        page = pages.setdefault(package_key, PackagePage(provider=provider, name=name))
        package = info.get("package") or {}
        if isinstance(package, dict):
            page.package_manager = package.get("packageManager") or page.package_manager
            page.package_manager_url = package.get("packageManagerUrl") or page.package_manager_url
        raw_summary = info.get("summary") or page.summary
        page.install_commands = merge_install_command_entries(
            page.install_commands,
            install_commands_from_summary(page, raw_summary),
        )
        page.summary = clean_summary(raw_summary)
        page.homepage = info.get("homepage") or page.homepage
        page.repository = info.get("repository") or page.repository
        page.upstream_docs = info.get("upstreamDocs") or page.upstream_docs
        page.config_file_locations = (
            normalize_path_locations(info.get("config-file-location"))
            or normalize_path_locations(info.get("configFileLocations"))
            or page.config_file_locations
        )
        page.credentials_file_locations = (
            normalize_path_locations(info.get("credentials-file-location"))
            or normalize_path_locations(info.get("credentialsFileLocations"))
            or page.credentials_file_locations
        )
        page.version = info.get("version") or page.version
        page.license = info.get("license") or page.license
        page.source_archive = info.get("sourceArchive") or page.source_archive
        page.sha256 = info.get("sha256") or page.sha256
        page.dependencies = info.get("dependencies") or page.dependencies
        page.build_dependencies = info.get("buildDependencies") or page.build_dependencies
        page.uses_from_macos = info.get("usesFromMacos") or page.uses_from_macos
        page.binaries = info.get("binaries") or page.binaries
        page.executables = info.get("executables") or page.executables
        page.install_behavior = info.get("installBehavior") or page.install_behavior
        page.bottle = info.get("bottle") or page.bottle
        page.published_at = info.get("publishedAt") or page.published_at
        page.keywords = info.get("keywords") or page.keywords
        page.issue_tracker = info.get("issueTracker") or page.issue_tracker
        page.classifiers = info.get("classifiers") or page.classifiers
        page.project_urls = info.get("projectUrls") or page.project_urls
        page.extra["homebrewDeps"] = info.get("homebrewDependencies") or page.extra.get("homebrewDeps")
        page.extra["pythonFormula"] = info.get("pythonFormula") or page.extra.get("pythonFormula")
        page.extra["registryInsights"] = info.get("registryInsights") or page.extra.get("registryInsights")
        if provider == "cask":
            page.package_hubs = merge_hub_links(page.package_hubs, cask_package_hubs(page))
        page.source_notes.append("package-page enrichment")


def cask_package_hubs(page: PackagePage) -> list[dict[str, Any]]:
    insights = page.extra.get("registryInsights") if isinstance(page.extra.get("registryInsights"), dict) else {}
    artifacts = insights.get("artifacts") if isinstance(insights.get("artifacts"), dict) else {}
    hubs = [
        {
            "slug": "homebrew-cask-packages",
            "label": "Homebrew Cask packages",
            "kicker": "package manager family",
            "description": "Homebrew Cask packages with generated Automic Vault metadata.",
            "reason": "Generated from Homebrew Cask package-manager metadata.",
        }
    ]
    if artifacts.get("app"):
        hubs.append({
            "slug": "homebrew-cask-applications",
            "label": "Homebrew Cask applications",
            "kicker": "macOS applications",
            "description": "macOS application casks with generated package metadata and install routes.",
            "reason": "Cask artifact metadata includes an app bundle.",
        })
    if artifacts.get("binary"):
        hubs.append({
            "slug": "homebrew-cask-binaries",
            "label": "Homebrew Cask binaries",
            "kicker": "cask command surface",
            "description": "Homebrew Cask packages that expose command-line binaries.",
            "reason": "Cask artifact metadata includes a binary.",
        })
    return hubs


def apply_package_version_freshness(pages: dict[str, PackagePage], freshness: dict[str, Any]) -> None:
    packages = freshness.get("packages") if isinstance(freshness, dict) else None
    if not isinstance(packages, dict):
        return
    for package_key, info in packages.items():
        if not isinstance(package_key, str) or ":" not in package_key or not isinstance(info, dict):
            continue
        provider, name = package_key.split(":", 1)
        if provider not in PACKAGE_PROVIDERS or not name:
            continue
        page = pages.setdefault(package_key, PackagePage(provider=provider, name=name))
        page.version_freshness = info
        page.source_notes.append("package version freshness")


def apply_package_page_supplements(pages: dict[str, PackagePage]) -> None:
    base = Path("data/pkg-pages")
    if not base.exists():
        return
    for path in sorted(base.glob("*/*.json")):
        supplement = read_json(path, {})
        package = supplement.get("package") or {}
        provider = package.get("provider") or path.parent.name
        name = package.get("name") or path.stem
        if provider not in PACKAGE_PROVIDERS or not name:
            continue
        page = pages.setdefault(f"{provider}:{name}", PackagePage(provider=provider, name=name))
        raw_summary = supplement.get("summary") or page.summary
        page.install_commands = merge_install_command_entries(
            page.install_commands,
            install_commands_from_summary(page, raw_summary),
        )
        page.summary = clean_summary(raw_summary)
        page.homepage = supplement.get("homepage") or page.homepage
        page.version = supplement.get("version") or page.version
        page.last_verified = supplement.get("lastVerified") or page.last_verified
        page.package_manager = package.get("packageManager") or page.package_manager
        page.package_manager_url = package.get("packageManagerUrl") or page.package_manager_url
        page.repository = supplement.get("repository") or page.repository
        page.upstream_docs = supplement.get("upstreamDocs") or page.upstream_docs
        page.license = supplement.get("license") or page.license
        page.source_archive = supplement.get("sourceArchive") or page.source_archive
        page.dependencies = supplement.get("dependencies") or page.dependencies
        page.build_dependencies = supplement.get("buildDependencies") or page.build_dependencies
        page.uses_from_macos = supplement.get("usesFromMacos") or page.uses_from_macos
        page.install = supplement.get("install") or page.install
        page.executables = supplement.get("executables") or page.executables
        page.install_behavior = supplement.get("installBehavior") or page.install_behavior
        page.bottle = supplement.get("bottle") or page.bottle
        page.published_at = supplement.get("publishedAt") or page.published_at
        page.keywords = supplement.get("keywords") or page.keywords
        page.issue_tracker = supplement.get("issueTracker") or page.issue_tracker
        page.related_packages = supplement.get("relatedPackages") or page.related_packages
        page.also_available_via = supplement.get("alsoAvailableVia") or page.also_available_via
        page.source_notes.append(f"package-page supplement {path.as_posix()}")


def apply_package_graph(pages: dict[str, PackagePage], graph: dict[str, Any]) -> None:
    packages = graph.get("packages") if isinstance(graph, dict) else None
    if not isinstance(packages, dict):
        return
    for package_key, graph_entry in packages.items():
        if not isinstance(package_key, str) or ":" not in package_key or not isinstance(graph_entry, dict):
            continue
        provider, name = package_key.split(":", 1)
        if provider not in PACKAGE_PROVIDERS or not name:
            continue
        page = pages.get(package_key)
        if page is None:
            continue
        identity = graph_entry.get("identity") or {}
        if isinstance(identity, dict):
            page.repository = identity.get("repository") or page.repository
            if not page.upstream_docs and identity.get("homepage") and identity.get("homepage") != page.repository:
                page.upstream_docs = identity.get("homepage") or page.upstream_docs
        intents = graph_entry.get("linkIntents") or {}
        if not isinstance(intents, dict):
            continue
        page.related_packages = merge_related_links(
            page.related_packages,
            intents.get("relatedPackages") if isinstance(intents.get("relatedPackages"), list) else [],
        )
        page.also_available_via = merge_related_links(
            page.also_available_via,
            intents.get("alsoAvailableVia") if isinstance(intents.get("alsoAvailableVia"), list) else [],
        )
        page.package_hubs = merge_hub_links(
            page.package_hubs,
            intents.get("packageHubs") if isinstance(intents.get("packageHubs"), list) else [],
        )
        page.source_notes.append("package relationship graph")


def apply_package_cross_ecosystem(pages: dict[str, PackagePage], cross_ecosystem: dict[str, Any]) -> None:
    packages = cross_ecosystem.get("packages") if isinstance(cross_ecosystem, dict) else None
    if not isinstance(packages, dict):
        return
    for package_key, entry in packages.items():
        if not isinstance(package_key, str) or not isinstance(entry, dict):
            continue
        page = pages.get(package_key)
        if page is None:
            continue
        commands = entry.get("commands")
        if isinstance(commands, list):
            page.install_commands = merge_install_command_entries(
                [item for item in commands if isinstance(item, dict)],
                page.install_commands,
            )
        page.also_available_via = merge_related_links(
            page.also_available_via,
            entry.get("localLinks") if isinstance(entry.get("localLinks"), list) else [],
            limit=12,
        )
        external_matches = entry.get("externalMatches")
        if isinstance(external_matches, list):
            page.external_package_manager_matches = merge_external_package_manager_matches(
                page.external_package_manager_matches,
                [item for item in external_matches if isinstance(item, dict)],
            )
            if page.external_package_manager_matches:
                page.source_notes.append("external package-manager database matches")
        page.source_notes.append("cross-ecosystem install command graph")


def prune_missing_relationship_targets(pages: dict[str, PackagePage]) -> None:
    page_keys = set(pages)
    for page in pages.values():
        page.related_packages = valid_related_targets(page, page.related_packages, page_keys)
        page.also_available_via = valid_related_targets(page, page.also_available_via, page_keys)


def valid_related_targets(page: PackagePage, items: list[dict[str, Any]], page_keys: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        name = str(item.get("name") or "").strip()
        target_key = f"{provider}:{name}"
        if not provider or not name or target_key == page.key or target_key not in page_keys:
            continue
        key = (provider, name)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def merge_related_links(existing: list[dict[str, Any]], generated: list[Any], limit: int = 24) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in list(existing) + [item for item in generated if isinstance(item, dict)]:
        provider = str(item.get("provider") or "").strip()
        name = str(item.get("name") or "").strip()
        if not provider or not name:
            continue
        key = (provider, name)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def merge_external_package_manager_matches(
    existing: list[dict[str, Any]],
    generated: list[dict[str, Any]],
    limit: int = 24,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in list(existing) + [item for item in generated if isinstance(item, dict)]:
        manager = str(item.get("manager") or "").strip()
        package_id = str(item.get("packageId") or "").strip()
        if not manager or not package_id:
            continue
        key = (manager, package_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    result.sort(
        key=lambda item: (
            -external_match_confidence(item),
            str(item.get("platform") or ""),
            str(item.get("displayName") or item.get("manager") or ""),
            str(item.get("packageId") or ""),
        )
    )
    return result[:limit]


def external_match_confidence(item: dict[str, Any]) -> float:
    try:
        return float(item.get("confidence"))
    except (TypeError, ValueError):
        return 0.0


def merge_hub_links(existing: list[dict[str, Any]], generated: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(existing) + [item for item in generated if isinstance(item, dict)]:
        slug = str(item.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        result.append(item)
    return result


def native_command_package_key(command_text: str) -> tuple[str, str] | None:
    text = normalize_space(command_text)
    patterns = (
        (r"^brew\s+install\s+--cask\s+([A-Za-z0-9@._/+~-]+)$", "cask"),
        (r"^brew\s+install\s+(?!--cask\b)([A-Za-z0-9@._/+~-]+)$", "brew"),
        (r"^(?:npm\s+(?:install|i)\s+-g|npm\s+-g\s+(?:install|i))\s+(@?[A-Za-z0-9._/+~-]+)$", "npm"),
        (r"^pip\s+install\s+([A-Za-z0-9._/+~-]+)$", "pip"),
        (r"^cargo\s+install\s+([A-Za-z0-9._+~-]+)$", "cargo"),
    )
    for pattern, provider in patterns:
        match = re.match(pattern, text)
        if match:
            return provider, match.group(1)
    return None


def verified_install_evidence(provider: str) -> str:
    return {
        "brew": "local Homebrew formula metadata",
        "cask": "local Homebrew cask metadata",
        "npm": "local npm package metadata",
        "pip": "local PyPI package metadata",
        "cargo": "local crates.io package metadata",
    }.get(provider, "local package metadata")


def verify_local_install_commands(pages: dict[str, PackagePage]) -> None:
    for page in pages.values():
        verified_commands: list[dict[str, Any]] = []
        verified_links: list[dict[str, Any]] = []
        for item in page.install_commands:
            if not isinstance(item, dict):
                continue
            command_key = native_command_package_key(str(item.get("command") or ""))
            if command_key is None:
                verified_commands.append(item)
                continue
            provider, name = command_key
            target_key = f"{provider}:{name}"
            target = pages.get(target_key)
            if target is None or target.key == page.key:
                verified_commands.append(item)
                continue
            verified_commands.append({
                **item,
                "confidence": 1.0,
                "evidence": verified_install_evidence(provider),
            })
            verified_links.append({
                "provider": provider,
                "name": name,
                "label": target.display_name,
                "rel": "same_software_cross_ecosystem",
                "reason": "Install command points at a matching local package page.",
                "confidence": 1.0,
                "evidence": verified_install_evidence(provider),
            })
        page.install_commands = merge_install_command_entries(verified_commands)
        if verified_links:
            page.also_available_via = merge_related_links(page.also_available_via, verified_links, limit=12)
            page.source_notes.append("local install command verification")


def merge_install_command_entries(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            command_text = normalize_space(item.get("command") or "")
            if not command_text or command_text in seen:
                continue
            seen.add(command_text)
            result.append({**item, "command": command_text})
    return result


def install_commands_from_summary(page: PackagePage, value: Any) -> list[dict[str, Any]]:
    text = html.unescape(str(value or ""))
    if not text:
        return []
    text = re.sub(r"<[^>]+>", " ", text)
    text = normalize_space(text)
    commands: list[dict[str, Any]] = []

    def add(platform: str, manager: str, command_text: str, provider: str) -> None:
        if provider == page.provider:
            return
        commands.append({
            "platform": platform,
            "manager": manager,
            "command": command_text,
            "kind": "package_manager",
            "confidence": 0.72,
            "evidence": "package summary install note",
        })

    for match in re.finditer(r"(?<!\S)brew\s+install\s+--cask\s+([A-Za-z0-9@._/+~-]+)", text):
        add("macos", "Homebrew Cask", f"brew install --cask {match.group(1)}", "cask")
    for match in re.finditer(r"(?<!\S)brew\s+install\s+(?!--cask\b)([A-Za-z0-9@._/+~-]+)", text):
        add("macos", "Homebrew", f"brew install {match.group(1)}", "brew")
    for match in re.finditer(r"(?<!\S)(?:npm\s+(?:install|i)\s+-g|npm\s+-g\s+(?:install|i))\s+(@?[A-Za-z0-9._/+~-]+)", text):
        add("portable", "npm", f"npm install -g {match.group(1)}", "npm")
    for match in re.finditer(r"(?<!\S)pip\s+install\s+([A-Za-z0-9._/+~-]+)", text):
        add("portable", "pip", f"pip install {match.group(1)}", "pip")
    for match in re.finditer(r"(?<!\S)cargo\s+install\s+([A-Za-z0-9._+~-]+)", text):
        add("portable", "Cargo", f"cargo install {match.group(1)}", "cargo")
    return merge_install_command_entries(commands)


def isotope_metadata_by_package(isotopes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for isotope_name, isotope in isotopes.items():
        if not isinstance(isotope, dict):
            continue
        modifies = isotope.get("modifies") or isotope.get("replaces")
        if not isinstance(modifies, str) or ":" not in modifies:
            continue
        provider, name = modifies.split(":", 1)
        if provider == "formula":
            provider = "brew"
        if provider not in PACKAGE_PROVIDERS:
            continue
        enriched = dict(isotope)
        enriched.setdefault("directory", isotope_name)
        result[f"{provider}:{name}"] = enriched
    return result


def radioisotope_readmes() -> dict[str, ReadmeExcerpt]:
    readmes: dict[str, ReadmeExcerpt] = {}
    base = RADIOISOTOPES_REPO_DIR
    if not base.exists():
        return readmes
    for path in base.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        readme = path / "README.md"
        if readme.exists():
            text = readme.read_text(encoding="utf-8", errors="replace")
            readmes[path.name] = ReadmeExcerpt(
                summary=summarize_markdown(text),
                html=render_markdown_excerpt(text),
                source=readme.as_posix(),
            )
    return readmes


def isotope_fork_readmes() -> dict[str, ReadmeExcerpt]:
    readmes: dict[str, ReadmeExcerpt] = {}
    base = ISOTOPE_REPO_CACHE_DIR
    if not base.exists():
        return readmes
    for readme in sorted(base.glob("*/README.md")):
        text = trim_isotope_fork_readme(readme.read_text(encoding="utf-8", errors="replace"))
        if not text.strip():
            continue
        readmes[readme.parent.name] = ReadmeExcerpt(
            summary=summarize_markdown(text),
            html=render_markdown_excerpt(text),
            source=readme.as_posix(),
        )
    return readmes


def trim_isotope_fork_readme(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if re.search(r"\b(remainder|rest) of this README\b.*\boriginal upstream\b", line, flags=re.IGNORECASE):
            break
        lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and re.fullmatch(r"-{3,}", lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip() + "\n" if lines else ""


def approval_gate_metadata_by_package() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(Path("data/approval-gates").glob("*/*.yaml")):
        text = path.read_text(encoding="utf-8", errors="replace")
        namespace = match_yaml_scalar(text, r"package:\s*(?:\n|\r\n)(?:.*\n)*?\s+namespace:\s*([^\n#]+)")
        name = match_yaml_scalar(text, r"package:\s*(?:\n|\r\n)(?:.*\n)*?\s+name:\s*([^\n#]+)")
        if namespace == "formula":
            namespace = "brew"
        if not namespace or not name:
            namespace = path.parent.name
            name = path.stem
        if namespace == "brew":
            provider = "brew"
        elif namespace in {"cask", "npm", "pip"}:
            provider = namespace
        else:
            continue
        rules = parse_approval_rules(text)
        descriptions = [rule.get("description", "") for rule in rules if rule.get("description")]
        severities = [rule.get("severity", "") for rule in rules if rule.get("severity")]
        entrypoints = re.findall(r"^\s+-\s+name:\s*([^\n#]+)", text, flags=re.MULTILINE)
        analytics_rank = match_yaml_scalar(text, r"^\s+rank:\s*([^\n#]+)")
        reviewed_at = match_yaml_scalar(text, r"^\s+reviewedAt:\s*([^\n#]+)")
        coverage_status = match_yaml_scalar(text, r"^\s+status:\s*([^\n#]+)")
        result[f"{provider}:{name}"] = {
            "path": str(path),
            "rule_count": len(rules),
            "rules": [clean_yaml_scalar(item) for item in descriptions[:7]],
            "severities": sorted({clean_yaml_scalar(item) for item in severities}),
            "entrypoints": sorted({clean_yaml_scalar(item) for item in entrypoints[:8]}),
            "analytics_rank": clean_yaml_scalar(analytics_rank),
            "reviewed_at": clean_yaml_scalar(reviewed_at),
            "coverage_status": clean_yaml_scalar(coverage_status),
        }
    return result


def parse_approval_rules(text: str) -> list[dict[str, str]]:
    rules: list[dict[str, str]] = []
    in_rules = False
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if re.match(r"^rules:\s*$", line):
            in_rules = True
            continue
        if not in_rules:
            continue
        if line and not line.startswith(" "):
            break
        rule_id = re.match(r"^\s+-\s+id:\s*([^\n#]+)", line)
        if rule_id:
            current = {"id": clean_yaml_scalar(rule_id.group(1))}
            rules.append(current)
            continue
        if current is None:
            continue
        description = re.match(r"^\s+description:\s*(.+)$", line)
        if description:
            current["description"] = clean_yaml_scalar(description.group(1))
            continue
        severity = re.match(r"^\s+severity:\s*([^\n#]+)", line)
        if severity:
            current["severity"] = clean_yaml_scalar(severity.group(1))
            continue
    return rules


def match_yaml_scalar(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        return ""
    return clean_yaml_scalar(match.group(1))


def clean_yaml_scalar(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith(('"', "'")) and text.endswith(('"', "'")):
        text = text[1:-1]
    return text.strip()


def summarize_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("|"):
            continue
        stripped = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", stripped)
        stripped = stripped.replace("`", "")
        lines.append(stripped)
    return paragraph_text(" ".join(lines), 720)


def render_markdown_excerpt(text: str) -> str:
    html_blocks: list[str] = []
    paragraph: list[str] = []
    list_type = ""
    code_lines: list[str] | None = None

    def close_paragraph() -> None:
        if paragraph:
            html_blocks.append(f"<p>{render_inline_markdown(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            html_blocks.append(f"</{list_type}>")
            list_type = ""

    def open_list(kind: str) -> None:
        nonlocal list_type
        close_paragraph()
        if list_type != kind:
            close_list()
            html_blocks.append(f"<{kind}>")
            list_type = kind

    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if code_lines is not None:
            if stripped.startswith("```"):
                html_blocks.append(f"<pre><code>{html_escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = None
            else:
                code_lines.append(line)
            continue
        if stripped.startswith("```"):
            close_paragraph()
            close_list()
            code_lines = []
            continue
        if not stripped:
            close_paragraph()
            close_list()
            continue
        if re.match(r"^\[[^\]]+\]:\s+", stripped):
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            close_paragraph()
            close_list()
            tag = "h3" if len(heading.group(1)) == 1 else "h4"
            html_blocks.append(f"<{tag}>{render_inline_markdown(heading.group(2))}</{tag}>")
            continue
        unordered = re.match(r"^[-*]\s+(.+)$", stripped)
        if unordered:
            open_list("ul")
            html_blocks.append(f"<li>{render_inline_markdown(unordered.group(1))}</li>")
            continue
        ordered = re.match(r"^\d+\.\s+(.+)$", stripped)
        if ordered:
            open_list("ol")
            html_blocks.append(f"<li>{render_inline_markdown(ordered.group(1))}</li>")
            continue
        close_list()
        paragraph.append(stripped)

    if code_lines is not None:
        html_blocks.append(f"<pre><code>{html_escape(chr(10).join(code_lines))}</code></pre>")
    close_paragraph()
    close_list()
    return "\n".join(html_blocks)


def render_inline_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    pieces: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "`":
            end = text.find("`", index + 1)
            if end != -1:
                pieces.append(f"<code>{html_escape(text[index + 1:end])}</code>")
                index = end + 1
                continue
        if text[index] == "[":
            close = text.find("]", index + 1)
            if close != -1 and close + 1 < len(text) and text[close + 1] == "(":
                url_end = text.find(")", close + 2)
                if url_end != -1:
                    label = text[index + 1:close]
                    url = text[close + 2:url_end].strip()
                    if is_public_url(url):
                        pieces.append(f'<a href="{attr(url)}">{render_inline_markdown(label)}</a>')
                    else:
                        pieces.append(html_escape(label))
                    index = url_end + 1
                    continue
        pieces.append(html_escape(text[index]))
        index += 1
    return "".join(pieces)


def is_public_url(url: str) -> bool:
    return url.startswith("https://") or url.startswith("http://")


def manifest_count(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.glob("*/automic-vault.yml") if path.is_file())


def local_radioisotope_manifest_count() -> int:
    return manifest_count(RADIOISOTOPES_REPO_DIR)


def local_full_isotope_manifest_count() -> int:
    return manifest_count(ISOTOPE_REPO_CACHE_DIR)


def path_for_digest(path: Path) -> Path:
    try:
        return path.resolve().relative_to(AV_DB_ROOT)
    except ValueError:
        return Path(os.path.relpath(path.resolve(), AV_DB_ROOT))


def source_files() -> list[Path]:
    files: list[Path] = []
    files.extend((
        PKG_PAGE_ENRICHMENT_PATH,
        PKG_VERSION_FRESHNESS_PATH,
        PKG_GRAPH_PATH,
        PKG_GRAPH_CURATION_PATH,
        PKG_CROSS_ECOSYSTEM_PATH,
        CRATES_IO_INDEX_PATH,
        path_for_digest(Path(__file__)),
        Path("scripts/generate-pkg-pages.py"),
        Path("scripts/pkg_hub_data.py"),
        PACKAGE_INDEX_PATH,
        ISOTOPES_JSON_PATH,
    ))
    data = Path("data")
    for path in data.iterdir() if data.exists() else []:
        if path.is_file() and path.suffix in {".json", ".jsonc", ".md"}:
            files.append(path)
    if COMBINED_YAML_DIR.exists():
        files.extend(path for path in COMBINED_YAML_DIR.glob("*.yml") if path.is_file())
    for root in (RADIOISOTOPES_REPO_DIR, Path("data/approval-gates")):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            parts = set(path.parts)
            if ".git" in parts or path.name == ".DS_Store":
                continue
            files.append(path)
    supplement_root = Path("data/pkg-pages")
    if supplement_root.exists():
        files.extend(path for path in supplement_root.rglob("*.json") if path.is_file())
    i18n_root = Path("data/pkg-i18n")
    if i18n_root.exists():
        files.extend(path for path in i18n_root.rglob("*.json") if path.is_file())
    isotope_root = ISOTOPE_REPO_CACHE_DIR
    if isotope_root.exists():
        files.extend(path for path in isotope_root.glob("*/README.md") if path.is_file())
        files.extend(path for path in isotope_root.glob("*/automic-vault.yml") if path.is_file())
    return sorted(set(files))


def source_digest(files: list[Path]) -> tuple[str, int]:
    digest = hashlib.sha256()
    latest = 0
    for path in files:
        rel = path.as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if path.exists():
            stat = path.stat()
            latest = max(latest, stat.st_mtime_ns)
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest(), latest


def read_existing_manifest(output_dir: Path) -> dict[str, Any] | None:
    try:
        manifest = read_json(output_dir / MANIFEST_NAME)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if isinstance(manifest, dict):
        return manifest
    return None


def reusable_previous_manifest(
    previous_manifest: dict[str, Any] | None,
    digest: str,
    source_file_count: int,
    page_count: int,
) -> dict[str, Any] | None:
    if not previous_manifest:
        return None
    try:
        previous_source_file_count = int(previous_manifest.get("source_file_count") or -1)
        previous_page_count = int(previous_manifest.get("page_count") or -1)
    except (TypeError, ValueError):
        return None
    if (
        previous_manifest.get("schema") == SCHEMA_VERSION
        and previous_manifest.get("source_hash") == digest
        and previous_source_file_count == source_file_count
        and previous_page_count == page_count
    ):
        return previous_manifest
    return None


def build_manifest(
    page_count: int,
    files: list[Path],
    previous_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    digest, latest = source_digest(files)
    reusable_manifest = reusable_previous_manifest(previous_manifest, digest, len(files), page_count)
    latest_dt = dt.datetime.fromtimestamp(latest / 1_000_000_000, dt.timezone.utc)
    generated_at = reusable_manifest.get("generated_at") if reusable_manifest else ""
    latest_source_mtime_ns = reusable_manifest.get("latest_source_mtime_ns") if reusable_manifest else None
    latest_source_mtime = reusable_manifest.get("latest_source_mtime") if reusable_manifest else ""
    radioisotope_count = local_radioisotope_manifest_count()
    full_isotope_count = local_full_isotope_manifest_count()
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at or utc_now(),
        "source_hash": digest,
        "source_file_count": len(files),
        "latest_source_mtime_ns": latest_source_mtime_ns if latest_source_mtime_ns is not None else latest,
        "latest_source_mtime": latest_source_mtime or latest_dt.replace(microsecond=0).isoformat(),
        "page_count": page_count,
        "radioisotope_manifest_count": radioisotope_count,
        "full_isotope_manifest_count": full_isotope_count,
        "isotope_manifest_count": radioisotope_count + full_isotope_count,
    }


def package_index_signals(page: PackagePage) -> list[str]:
    signals: list[str] = []
    for attr_name in (
        "summary",
        "homepage",
        "version",
        "license",
        "package_manager_url",
        "repository",
        "upstream_docs",
        "category",
        "source_archive",
        "issue_tracker",
        "published_at",
    ):
        if getattr(page, attr_name):
            signals.append(attr_name)
    for attr_name in (
        "dependencies",
        "build_dependencies",
        "uses_from_macos",
        "keywords",
        "classifiers",
    ):
        if getattr(page, attr_name):
            signals.append(attr_name)
    if page.executables or page.aliases or page.binaries:
        signals.append("commands")
    if any(value for value in page.install_behavior.values()):
        signals.append("install_behavior")
    if page.config_file_locations or page.credentials_file_locations:
        signals.append("local_file_locations")
    if page.bottle and (page.bottle.get("available") or page.bottle.get("platforms")):
        signals.append("bottle")
    if page.isotope:
        signals.append("protected_tool_coverage")
    if page.approval_gate:
        signals.append("approval_gate")
    if page.geiger:
        signals.append("geiger")
    if page.related_packages or page.also_available_via or page.package_hubs:
        signals.append("relationships")
    if page.external_package_manager_matches:
        signals.append("external_package_manager_matches")
    if page.extra.get("registryInsights"):
        signals.append("registry_insights")
    if any(page.extra.get(key) for key in ("homebrewDeps", "pythonFormula", "stub_exclusions")):
        signals.append("local_overlay")
    return signals


def is_indexable_package_page(page: PackagePage) -> bool:
    if page.isotope or page.approval_gate or page.geiger:
        return True
    return len(package_index_signals(page)) >= INDEXABLE_MIN_SIGNAL_COUNT


def has_executable_surface(page: PackagePage) -> bool:
    return bool(page.aliases or page.executables or page.binaries)


def has_package_page_surface(page: PackagePage) -> bool:
    return has_executable_surface(page) or any(value for value in page.install_behavior.values())


def executable_package_pages(pages: dict[str, PackagePage]) -> dict[str, PackagePage]:
    return {
        key: page
        for key, page in pages.items()
        if has_package_page_surface(page)
    }


def package_hub_pages(pages: list[PackagePage]) -> list[tuple[PackageHub, list[PackagePage]]]:
    hubs: list[tuple[PackageHub, list[PackagePage]]] = []
    static_slugs = {hub.slug for hub in PACKAGE_HUBS}
    for hub in PACKAGE_HUBS:
        matches = sorted(
            [page for page in pages if package_matches_hub(page, hub) or page_has_hub(page, hub.slug)],
            key=hub_sort_key,
        )
        if matches:
            hubs.append((hub, matches))
    dynamic_hubs: dict[str, dict[str, str]] = {}
    for page in pages:
        for item in page.package_hubs:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "").strip()
            if not slug or slug in static_slugs:
                continue
            dynamic_hubs.setdefault(slug, {
                "title": str(item.get("label") or slug.replace("-", " ").title()),
                "kicker": str(item.get("kicker") or "package graph cluster"),
                "description": str(item.get("description") or item.get("reason") or "Package graph hub generated from local package facts."),
            })
    for slug, info in sorted(dynamic_hubs.items(), key=lambda item: item[1]["title"].lower()):
        hub = PackageHub(
            slug=slug,
            title=info["title"],
            kicker=info["kicker"],
            description=info["description"],
        )
        matches = sorted(
            [
                page for page in pages
                if any(isinstance(item, dict) and item.get("slug") == slug for item in page.package_hubs)
            ],
            key=hub_sort_key,
        )
        if matches:
            hubs.append((hub, matches))
    return hubs


def page_has_hub(page: PackagePage, slug: str) -> bool:
    return any(isinstance(item, dict) and item.get("slug") == slug for item in page.package_hubs)


def package_matches_hub(page: PackagePage, hub: PackageHub) -> bool:
    if hub.providers and page.provider not in hub.providers:
        return False
    if hub.risk_hub:
        if page.isotope or page.approval_gate:
            return True
        level = str((page.geiger or {}).get("level") or "").lower()
        return level not in {"", "green", "low", "unknown"}
    if (
        hub.providers
        and not hub.package_names
        and not hub.query_terms
        and not hub.categories
        and not hub.category_paths
    ):
        return True
    names = {page.name.lower(), page.slug.lower(), page.display_name.lower()}
    names.update(alias.lower() for alias in page.aliases)
    names.update(str(item.get("name") or "").lower() for item in page.executables if isinstance(item, dict))
    if any(name in names for name in hub.package_names):
        return True
    taxonomy = page.extra.get("pkgTaxonomy") if isinstance(page.extra.get("pkgTaxonomy"), dict) else {}
    taxonomy_values = taxonomy_terms(taxonomy)
    category = str(taxonomy.get("category") or page.category or "").strip()
    category_path = {str(item).strip() for item in taxonomy.get("categoryPath") or [] if str(item or "").strip()}
    tags = {str(item).strip() for item in taxonomy.get("tags") or [] if str(item or "").strip()}
    if hub.categories and category in set(hub.categories):
        return True
    if hub.category_paths and category_path & set(hub.category_paths):
        return True
    if hub.tags and tags & set(hub.tags):
        return True
    haystack = " ".join(
        str(value or "")
        for value in (
            page.name,
            clean_summary(page.summary),
            page.category,
            " ".join(page.aliases),
            " ".join(sorted(taxonomy_values)),
        )
    ).lower()
    return any(hub_term_matches(haystack, term) for term in hub.query_terms)


def hub_term_matches(haystack: str, term: str) -> bool:
    escaped = re.escape(term.lower())
    if re.search(r"\s", term):
        return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", haystack) is not None
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", haystack) is not None


def hub_sort_key(page: PackagePage) -> tuple[int, int, int, str, str]:
    risk_rank = {"critical": 0, "high": 1, "medium": 2, "yellow": 3, "low": 4, "green": 5}
    level = str((page.geiger or {}).get("level") or "").lower()
    coverage = 0 if page.isotope else 1
    gated = 0 if page.approval_gate else 1
    rank = int(page.popularity.get("rank") or 999999)
    return (coverage, gated, risk_rank.get(level, 6), rank, page.display_name.lower())


def internal_package_link_count(page: PackagePage) -> int:
    package_links = [
        item for item in list(page.related_packages) + list(page.also_available_via)
        if isinstance(item, dict)
        and str(item.get("provider") or "").strip()
        and str(item.get("name") or "").strip()
        and f"{item.get('provider')}:{item.get('name')}" != page.key
    ]
    inferred = inferred_related_links(page)
    hubs = [item for item in page.package_hubs if isinstance(item, dict) and str(item.get("slug") or "").strip()]
    guides = core_security_guide_links(page)
    return len(package_links) + len(inferred) + len(hubs) + len(guides)


def has_strong_internal_package_navigation(page: PackagePage) -> bool:
    return bool(page.package_hubs) and internal_package_link_count(page) >= 3


def localized_package_output_dir(output_dir: Path, locale: dict[str, Any] | None) -> Path:
    return output_dir if locale_code(locale) == "en" else output_dir.parent / locale_slug(locale) / "pkg"


def write_text_if_changed(path: Path, content: str, stats: RenderStats) -> None:
    data = content.encode("utf-8")
    try:
        if path.read_bytes() == data:
            stats.unchanged += 1
            return
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stats.written += 1


def write_generated_text(path: Path, content: str, expected_files: set[Path], stats: RenderStats) -> None:
    expected_files.add(path)
    write_text_if_changed(path, content, stats)


def prune_stale_generated_files(root: Path, expected_files: set[Path], stats: RenderStats) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and path not in expected_files and path.name != ".DS_Store":
            path.unlink()
            stats.deleted += 1
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        try:
            path.rmdir()
        except OSError:
            continue
        stats.directories_removed += 1


def render_all(pages: dict[str, PackagePage], manifest: dict[str, Any], output_dir: Path) -> RenderStats:
    locales = i18n_locales()
    expected_files_by_dir: dict[Path, set[Path]] = {
        localized_package_output_dir(output_dir, locale): set()
        for locale in locales
    }
    stats = RenderStats()
    ordered = sorted(pages.values(), key=lambda page: (page.provider, page.slug, page.name))
    hubs = package_hub_pages(ordered)
    indexable_pages = [page for page in ordered if is_indexable_package_page(page)]
    sitemap_names = ["sitemap-hubs.xml"] + [
        f"sitemap-{provider}.xml"
        for provider in PACKAGE_PROVIDERS
        if any(page.provider == provider for page in indexable_pages)
    ]
    manifest["hub_count"] = len(hubs)
    manifest["indexable_page_count"] = len(indexable_pages)
    manifest["noindex_page_count"] = len(ordered) - len(indexable_pages)
    manifest["markdown_page_count"] = len(indexable_pages)
    manifest["hub_markdown_page_count"] = len(hubs)
    manifest["sitemap_count"] = len(sitemap_names)
    manifest["sitemap_page_counts"] = {
        provider: sum(1 for page in indexable_pages if page.provider == provider)
        for provider in PACKAGE_PROVIDERS
        if any(page.provider == provider for page in indexable_pages)
    }
    for locale in locales:
        localized_dir = localized_package_output_dir(output_dir, locale)
        expected_files = expected_files_by_dir.setdefault(localized_dir, set())
        write_generated_text(localized_dir / "styles.css", render_css(), expected_files, stats)
        for page in ordered:
            page_dir = localized_dir / page.provider / page.slug
            write_generated_text(page_dir / "index.html", render_package_page(page, manifest, locale), expected_files, stats)
            if is_indexable_package_page(page):
                write_generated_text(page_dir / "index.md", render_package_markdown(page, manifest, locale), expected_files, stats)
        for hub, hub_pages in hubs:
            hub_dir = localized_dir / hub.slug
            write_generated_text(hub_dir / "index.html", render_hub_page(hub, hub_pages, manifest, locale), expected_files, stats)
            write_generated_text(hub_dir / "index.md", render_hub_markdown(hub, hub_pages, manifest, locale), expected_files, stats)
        write_generated_text(localized_dir / "index.html", render_index(ordered, hubs, manifest, locale), expected_files, stats)
        if locale_code(locale) == "en":
            write_generated_text(localized_dir / "sitemap.xml", render_sitemap_index(sitemap_names, manifest), expected_files, stats)
            write_generated_text(localized_dir / "sitemap-hubs.xml", render_hub_sitemap(hubs, manifest), expected_files, stats)
            for provider in PACKAGE_PROVIDERS:
                provider_pages = [page for page in indexable_pages if page.provider == provider]
                if provider_pages:
                    write_generated_text(
                        localized_dir / f"sitemap-{provider}.xml",
                        render_package_sitemap(provider_pages, manifest),
                        expected_files,
                        stats,
                    )
        write_generated_text(localized_dir / MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n", expected_files, stats)
    for generated_dir, expected_files in expected_files_by_dir.items():
        prune_stale_generated_files(generated_dir, expected_files, stats)
    return stats


def render_index(
    pages: list[PackagePage],
    hubs: list[tuple[PackageHub, list[PackagePage]]],
    manifest: dict[str, Any],
    locale: dict[str, Any] | None = None,
) -> str:
    top_pages = sorted(
        pages,
        key=lambda page: int(page.popularity.get("rank") or 999999),
    )[:72]
    package_links = "\n".join(
        f'<a class="package-row" href="{locale_path(page.path, locale)}"><span>{html_escape(page.display_name)}</span><small>{html_escape(label_for(page, locale))}</small></a>'
        for page in top_pages
    )
    hub_links = hub_group_sections(hubs, locale)
    search_placeholder = json.dumps(tx(locale, "searchPlaceholder", "Search awscli, gh, .env, npm publish"), ensure_ascii=False)
    return html_doc(
        title=tx(locale, "packageCatalogTitle", "Package catalog") + f" | {SITE_NAME}",
        description=tx(locale, "packageCatalogDescription", (
            "Source-backed package intelligence for executable tools, including install metadata, "
            "aliases, popularity, and upstream facts."
        )),
        canonical=locale_url("/", locale),
        alternates_path="/",
        locale=locale,
        body=f"""
{nav('../', locale)}
<main>
  <section class="pkg-hero pkg-hero-index" aria-labelledby="pkg-title">
    <div class="hero-copy">
      <p class="eyebrow">{html_escape(tx(locale, 'catalogEyebrow', 'pkg.so package intelligence'))}</p>
      <h1 id="pkg-title">{html_escape(tx(locale, "packageCatalogTitle", "Package catalog"))}</h1>
      <p class="lede">{html_escape(tx(locale, 'catalogPagesCopy', 'Generated pages for executable packages in the pkg.so catalog, with install routes, popularity, executable aliases, and upstream package facts.'))}</p>
    </div>
    <aside class="hero-panel" aria-label="{attr(tx(locale, 'catalogCounts', 'Catalog counts'))}">
      {metric(tx(locale, 'packages', 'packages'), fmt_int(len(pages)))}
      {metric(tx(locale, 'packageHubs', 'package hubs'), fmt_int(len(hubs)))}
      {metric(tx(locale, 'sourceFiles', 'source files'), fmt_int(manifest.get('source_file_count')))}
    </aside>
  </section>
  <section class="pkg-section pkg-search-section" aria-labelledby="pkg-search-title">
    <div class="search-copy">
      <p class="section-kicker">{html_escape(tx(locale, 'siteSearch', 'site search'))}</p>
      <h2 id="pkg-search-title">{html_escape(tx(locale, 'findPackageCoverage', 'Find package coverage'))}</h2>
      <p>{html_escape(tx(locale, 'catalogSearchCopy', 'Search the package catalog, documentation, and source-backed metadata from one index.'))}</p>
    </div>
    <div id="search" class="pkg-search" data-pagefind-ui></div>
  </section>
  <section class="pkg-section" aria-labelledby="pkg-hubs-title">
    <p class="section-kicker">{html_escape(tx(locale, 'catalogHubsKicker', 'package hubs'))}</p>
    <h2 id="pkg-hubs-title">{html_escape(tx(locale, 'catalogHubsTitle', 'Package groups by ecosystem and workflow'))}</h2>
    <p>{html_escape(tx(locale, 'catalogHubsCopy', 'These crawlable hubs group related package families, including cloud CLIs, source-control tools, package publishers, MCP tools, and language ecosystems.'))}</p>
    <div class="hub-groups" aria-label="{attr(tx(locale, 'catalogHubsAria', 'Package category hubs'))}">
      {hub_links}
    </div>
  </section>
  <section class="pkg-section split-section">
    <div>
      <p class="section-kicker">{html_escape(tx(locale, 'catalogPagesKicker', 'crawlable catalog'))}</p>
      <h2>{html_escape(tx(locale, 'catalogPagesTitle', 'Package pages from local source data'))}</h2>
      <p>{html_escape(tx(locale, 'crawlableCatalog', 'pkg.so package metadata, generated package inventories, executable indexes, and package-manager facts are served by the Atlas package origin so search and answer engines can find specific tools.'))}</p>
    </div>
    <div class="package-list" aria-label="{attr(tx(locale, 'popularPackages', 'Popular packages'))}">
      {package_links}
    </div>
  </section>
</main>
{footer('../', locale)}
""",
        stylesheet_href=locale_path("/pkg/styles.css", locale),
        favicon_href="/favicon.ico",
        extra_head='  <link rel="stylesheet" href="/pagefind/pagefind-ui.css">',
        extra_body=f'''  <script src="/pagefind/pagefind-ui.js"></script>
  <script>
    window.addEventListener("DOMContentLoaded", () => {{
      new PagefindUI({{
        element: "#search",
        showImages: false,
        showSubResults: true,
        pageSize: 8,
        excerptLength: 24,
        resetStyles: false,
        translations: {{
          placeholder: {search_placeholder}
        }}
      }});
      const focusSearch = () => {{
        if (window.location.hash !== "#search") return;
        window.requestAnimationFrame(() => {{
          const root = document.querySelector("#search");
          const input = root?.querySelector("input");
          root?.scrollIntoView({{ block: "center" }});
          input?.focus({{ preventScroll: true }});
        }});
      }};
      window.addEventListener("hashchange", focusSearch);
      focusSearch();
    }});
  </script>''',
        schema={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": tx(locale, "packageCatalogTitle", "Package catalog"),
            "url": locale_url("/", locale),
            "inLanguage": (locale or {}).get("htmlLang") or "en",
            "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": SITE_ORIGIN + "/"},
            "about": tx(locale, "packageCatalogDescription", "Source-backed package intelligence for executable tools, install routes, and package metadata"),
        },
    )


def hub_group_sections(
    hubs: list[tuple[PackageHub, list[PackagePage]]],
    locale: dict[str, Any] | None = None,
) -> str:
    labels = {
        "security": tx(locale, "hubSecurityGroupTitle", "Tooling hubs"),
        "topical": tx(locale, "hubTopicalGroupTitle", "Topical hubs"),
        "ecosystem": tx(locale, "hubEcosystemGroupTitle", "Ecosystem hubs"),
    }
    groups: dict[str, list[tuple[PackageHub, list[PackagePage]]]] = {"security": [], "topical": [], "ecosystem": []}
    for hub, pages in hubs:
        groups.setdefault(hub.group, []).append((hub, pages))
    sections = []
    for group in ("security", "topical", "ecosystem"):
        items = sorted(groups.get(group) or [], key=lambda item: (item[0].priority, item[0].title.lower()))
        if not items:
            continue
        cards = "\n".join(
            f'<a class="hub-card" href="{locale_path(hub.path, locale)}"><span>{html_escape(hub.title)}</span><strong>{fmt_int(len(hub_pages))}</strong><small>{html_escape(hub.kicker)}</small></a>'
            for hub, hub_pages in items
        )
        sections.append(f'<section class="hub-group"><h3>{html_escape(labels.get(group, group.title()))}</h3><div class="hub-grid">{cards}</div></section>')
    return "\n".join(sections)


def render_hub_page(
    hub: PackageHub,
    pages: list[PackagePage],
    manifest: dict[str, Any],
    locale: dict[str, Any] | None = None,
) -> str:
    updated = fmt_date(manifest.get("generated_at", ""))
    top = pages[:72]
    secured = [page for page in pages if page.isotope]
    gated = [page for page in pages if page.approval_gate]
    rows = "\n".join(hub_package_row(page, locale) for page in top)
    description = short_text(
        tx(locale, "hubSchemaDescription", "{description} Browse {count} package pages with install commands, metadata, and security notes.", description=hub.description, count=len(pages)),
        155,
    )
    return html_doc(
        title=f"{hub.title} | {SITE_NAME} package catalog",
        description=description,
        canonical=locale_url(hub.path, locale),
        alternates_path=hub.path,
        locale=locale,
        body=f"""
{nav('../../', locale)}
<main>
  <nav class="breadcrumbs" aria-label="Breadcrumbs">
    <a href="../../">{html_escape(tx(locale, 'home', 'Home'))}</a>
    <span>/</span>
    <a href="../">{html_escape(tx(locale, 'packages', 'Packages'))}</a>
    <span>/</span>
    <span>{html_escape(hub.title)}</span>
  </nav>
  <section class="pkg-hero pkg-hero-index" aria-labelledby="hub-title">
    <div class="hero-copy">
      <p class="eyebrow">{html_escape(hub.kicker)}</p>
      <h1 id="hub-title">{html_escape(hub.title)}</h1>
      <p class="lede">{html_escape(hub.description)}</p>
    </div>
    <aside class="hero-panel" aria-label="{attr(tx(locale, 'hubCounts', 'Hub counts'))}">
      {metric(tx(locale, 'packages', 'packages'), fmt_int(len(pages)))}
      {metric(tx(locale, 'radioisotopes', 'protected tools'), fmt_int(len(secured)))}
      {metric(tx(locale, 'approvalGates', 'approval gates'), fmt_int(len(gated)))}
      {metric(tx(locale, 'updated', 'updated'), updated, pagefind_ignore=True)}
    </aside>
  </section>
  <section class="pkg-section split-section">
    <div>
      <p class="section-kicker">{html_escape(tx(locale, 'packageSummary', 'summary'))}</p>
      <h2>{html_escape(tx(locale, 'hubSummaryTitle', 'Why this package group is here'))}</h2>
      <p>{html_escape(hub_description_detail(hub, pages, locale))}</p>
    </div>
    <div class="detail-stack">
      <article>
        <h3>{html_escape(tx(locale, 'generatedSource', 'Generated source'))}</h3>
        <p>{html_escape(tx(locale, 'generatedSourceCopy', 'This hub uses the same local package data as individual package pages: pkg.so package metadata, Homebrew enrichment, Geiger classifier output, secret-handling manifests, and approval-gate seeds where available.'))}</p>
      </article>
      <article>
        <h3>{html_escape(tx(locale, 'hubReviewModel', 'Review model'))}</h3>
        <p>{html_escape(tx(locale, 'hubReviewCopy', 'Use the hub to find command families that need tighter secret injection, approval gates, or manual review before agents run them.'))}</p>
      </article>
    </div>
  </section>
  {render_hub_cluster_sections(hub, pages, locale)}
  <section class="pkg-section">
    <p class="section-kicker">{html_escape(tx(locale, 'packages', 'packages'))}</p>
    <h2>{html_escape(tx(locale, 'hubIndexedPagesTitle', 'Indexed package pages'))}</h2>
    <div class="table-wrap hub-table">
      <table>
        <thead><tr><th>{html_escape(tx(locale, 'package', 'Package'))}</th><th>{html_escape(tx(locale, 'manager', 'Manager'))}</th><th>{html_escape(tx(locale, 'signals', 'Signals'))}</th><th>{html_escape(tx(locale, 'why', 'Why it appears here'))}</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </section>
</main>
{footer('../../', locale)}
""",
        stylesheet_href=locale_path("/pkg/styles.css", locale),
        favicon_href="/favicon.ico",
        schema=schema_for_hub(hub, pages, description, updated, locale),
        extra_head=markdown_alternate_head(hub.path, locale),
    )


def render_hub_markdown(
    hub: PackageHub,
    pages: list[PackagePage],
    manifest: dict[str, Any],
    locale: dict[str, Any] | None = None,
) -> str:
    updated = fmt_date(manifest.get("generated_at", ""))
    secured = sum(1 for page in pages if page.isotope)
    gated = sum(1 for page in pages if page.approval_gate)
    risked = sum(1 for page in pages if page.geiger and str(page.geiger.get("level") or "").lower() not in {"", "green", "low", "unknown"})
    lines = [
        f"# {md_text(hub.title)}",
        "",
        md_text(hub.description),
        "",
        f"- **{md_text(tx(locale, 'packages', 'Packages'))}:** {fmt_int(len(pages))}",
        f"- **{md_text(tx(locale, 'radioisotopes', 'Protected tools'))}:** {fmt_int(secured)}",
        f"- **{md_text(tx(locale, 'approvalGates', 'Approval gates'))}:** {fmt_int(gated)}",
        f"- **{md_text(tx(locale, 'risk', 'Non-low risk'))}:** {fmt_int(risked)}",
        f"- **{md_text(tx(locale, 'updated', 'Updated'))}:** {md_text(updated)}",
        "",
        f"## {md_text(tx(locale, 'hubSummaryTitle', 'Why this package group is here'))}",
        "",
        md_text(hub_description_detail(hub, pages, locale)),
        "",
        f"## {md_text(tx(locale, 'generatedSource', 'Generated source'))}",
        "",
        md_text(tx(locale, 'generatedSourceCopy', 'This hub uses the same local package data as individual package pages: pkg.so package metadata, Homebrew enrichment, Geiger classifier output, secret-handling manifests, and approval-gate seeds where available.')),
        "",
        f"## {md_text(tx(locale, 'hubReviewModel', 'Review model'))}",
        "",
        md_text(tx(locale, 'hubReviewCopy', 'Use the hub to find command families that need tighter secret injection, approval gates, or manual review before agents run them.')),
        "",
        f"## {md_text(tx(locale, 'hubIndexedPagesTitle', 'Indexed package pages'))}",
        "",
    ]
    for page in pages[:72]:
        reason = hub_package_reason(page, locale)
        path = locale_url(page.path, locale)
        lines.append(f"- [{md_text(page.display_name)}]({path}) - {md_text(reason)}")
    return "\n".join(lines).rstrip() + "\n"


def render_hub_cluster_sections(
    hub: PackageHub,
    pages: list[PackagePage],
    locale: dict[str, Any] | None = None,
) -> str:
    high_signal = pages[:12]
    protected = [page for page in pages if page.isotope][:8]
    gated = [page for page in pages if page.approval_gate][:8]
    spokes = sorted(pages, key=lambda page: (int(page.popularity.get("rank") or 999999), page.display_name.lower()))[:16]
    related_hubs = related_hub_links(hub, pages, locale)
    blocks = [
        hub_cluster_block(tx(locale, "hubHighSignalTitle", "High-signal tools"), high_signal, locale),
        hub_cluster_block(tx(locale, "hubProtectedToolsTitle", "Protected tools"), protected, locale),
        hub_cluster_block(tx(locale, "hubApprovalGatedTitle", "Approval-gated tools"), gated, locale),
        hub_related_block(tx(locale, "hubRelatedHubsTitle", "Related hubs"), related_hubs),
        hub_cluster_block(tx(locale, "hubRepresentativeSpokesTitle", "Representative package spokes"), spokes, locale),
    ]
    return "\n".join(block for block in blocks if block)


def hub_cluster_block(title: str, pages: list[PackagePage], locale: dict[str, Any] | None = None) -> str:
    if not pages:
        return ""
    cards = "\n".join(hub_spoke_card(page, locale) for page in pages)
    return f"""
<section class="pkg-section hub-cluster">
  <h2>{html_escape(title)}</h2>
  <div class="package-list hub-spoke-list">{cards}</div>
</section>
"""


def hub_related_block(title: str, links: list[str]) -> str:
    if not links:
        return ""
    return f"""
<section class="pkg-section hub-cluster">
  <h2>{html_escape(title)}</h2>
  <div class="hub-related-list">{''.join(links)}</div>
</section>
"""


def hub_spoke_card(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    return (
        f'<a class="package-row" href="{locale_path(page.path, locale)}">'
        f"<span>{html_escape(page.display_name)}</span>"
        f"<small>{html_escape(hub_package_reason(page, locale))}</small>"
        "</a>"
    )


def related_hub_links(hub: PackageHub, pages: list[PackagePage], locale: dict[str, Any] | None = None) -> list[str]:
    counts: dict[str, tuple[str, str, int]] = {}
    for page in pages:
        for item in page.package_hubs:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "")
            if not slug or slug == hub.slug:
                continue
            label = str(item.get("label") or slug.replace("-", " ").title())
            reason = str(item.get("reason") or "")
            _label, _reason, count = counts.get(slug, (label, reason, 0))
            counts[slug] = (label, reason or _reason, count + 1)
    ranked = sorted(counts.items(), key=lambda item: (-item[1][2], item[1][0].lower()))[:8]
    return [
        (
            f'<a class="hub-related-card" href="{locale_path(f"/pkg/{slug}/", locale)}">'
            f"<span>{html_escape(label)}</span>"
            f"<small>{html_escape(reason or tx(locale, 'packageGraph', 'package graph'))}</small>"
            f"<strong>{fmt_int(count)}</strong>"
            "</a>"
        )
        for slug, (label, reason, count) in ranked
    ]


def hub_description_detail(hub: PackageHub, pages: list[PackagePage], locale: dict[str, Any] | None = None) -> str:
    secured = sum(1 for page in pages if page.isotope)
    gated = sum(1 for page in pages if page.approval_gate)
    risked = sum(1 for page in pages if page.geiger and str(page.geiger.get("level") or "").lower() not in {"", "green", "low", "unknown"})
    return tx(
        locale,
        "hubDescription",
        "{title} currently includes {count} package catalog entries. {secured} have protected-tool coverage, {gated} have approval-gate metadata, and {risked} have non-low Geiger classifier findings. The grouping comes from package metadata, so it can stay current as that metadata changes.",
        title=hub.title,
        count=len(pages),
        secured=secured,
        gated=gated,
        risked=risked,
    )


def hub_package_row(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    signals = []
    if page.isotope:
        signals.append(tx(locale, "radioisotopeKicker", "protected-tool coverage"))
    if page.approval_gate:
        signals.append(tx(locale, "approvalGatesKicker", "approval gate"))
    if page.geiger:
        signals.append(tx(locale, "riskLevel", "{level} risk", level=geiger_level_label(page.geiger)))
    if page.version:
        signals.append(f"v{page.version}")
    reason = hub_package_reason(page, locale)
    return (
        f'<tr><td><a href="{attr(page.path)}">{html_escape(page.display_name)}</a></td>'
        f"<td>{html_escape(package_manager_label(page))}</td>"
        f"<td>{html_escape(', '.join(signals) or label_for(page, locale))}</td>"
        f"<td>{html_escape(reason)}</td></tr>"
    )


def hub_package_reason(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    if page.isotope:
        title = (page.isotope.get("justification") or {}).get("title")
        if title:
            return public_copy(title)
    if page.approval_gate:
        return tx(locale, "hubPackageReasonApproval", "{count} approval-gate rules are present.", count=page.approval_gate.get("rule_count") or "Local")
    if page.geiger:
        reasons = page.geiger.get("reasons") or []
        if reasons:
            return short_text(reasons[0], 140)
    if page.summary:
        return short_text(clean_summary(page.summary), 140)
    aliases = sorted(page.aliases)
    if aliases:
        return tx(locale, "hubPackageReasonAlias", "Executable aliases include {aliases}.", aliases=", ".join(aliases[:4]))
    return tx(locale, "hubPackageReasonDefault", "Matched package metadata for this hub.")


def schema_for_hub(hub: PackageHub, pages: list[PackagePage], description: str, updated: str, locale: dict[str, Any] | None = None) -> dict[str, Any]:
    url = locale_url(hub.path, locale)
    return {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "@id": f"{SITE_ORIGIN}/#website", "name": SITE_NAME, "url": f"{SITE_ORIGIN}/"},
            {"@type": "Organization", "@id": f"{SITE_ORIGIN}/#organization", "name": SITE_NAME, "url": f"{SITE_ORIGIN}/"},
            {"@type": "Person", "@id": "https://github.com/mxcl#person", "name": "Max Howell", "url": "https://github.com/mxcl"},
            {
                "@type": "CollectionPage",
                "@id": f"{url}#webpage",
                "name": hub.title,
                "headline": hub.title,
                "url": url,
                "description": description,
                "inLanguage": (locale or {}).get("htmlLang") or "en",
                "dateModified": updated,
                "isPartOf": {"@id": f"{SITE_ORIGIN}/#website"},
                "about": {"@id": f"{SITE_ORIGIN}/#software"},
                "author": {"@id": "https://github.com/mxcl#person"},
                "reviewedBy": {"@id": "https://github.com/mxcl#person"},
                "publisher": {"@id": f"{SITE_ORIGIN}/#organization"},
                "mainEntity": {
                    "@type": "ItemList",
                    "numberOfItems": len(pages),
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": index + 1,
                            "url": f"{SITE_ORIGIN}{page.path}",
                            "name": page.display_name,
                            "description": schema_item_description(page, locale),
                        }
                        for index, page in enumerate(pages[:50])
                    ],
                },
            },
            {
                "@type": "BreadcrumbList",
                "@id": f"{url}#breadcrumbs",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": tx(locale, "home", "Home"), "item": locale_url("/", locale)},
                    {"@type": "ListItem", "position": 2, "name": tx(locale, "packages", "Packages"), "item": locale_url("/pkg/", locale)},
                    {"@type": "ListItem", "position": 3, "name": hub.title, "item": url},
                ],
            },
        ],
    }


def schema_item_description(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    taxonomy = page.extra.get("pkgTaxonomy") if isinstance(page.extra.get("pkgTaxonomy"), dict) else {}
    parts = []
    if taxonomy.get("category"):
        parts.append(tx(locale, "packageTaxonomy", "Taxonomy: {value}", value=taxonomy["category"]))
    tags = [str(item) for item in taxonomy.get("tags") or [] if str(item or "").strip()]
    if tags:
        parts.append(tx(locale, "taxonomyEvidence", "Tags: {value}", value=", ".join(tags[:5])))
    reason = hub_package_reason(page, locale)
    if reason:
        parts.append(reason)
    return short_text(" ".join(parts), 220)


def render_package_page(
    page: PackagePage,
    manifest: dict[str, Any],
    locale: dict[str, Any] | None = None,
) -> str:
    title = package_install_title(page, locale)
    description = meta_description(page, locale)
    updated = fmt_date(page.last_verified) or fmt_date(page.last_updated_at) or fmt_date(manifest.get("generated_at", ""))
    install_section = render_install(page, locale)
    sections = [
        render_install_support(page, locale),
        install_section,
        render_agent_risk_assessment(page, locale),
        render_overview(page, locale),
        render_executables(page, locale),
        render_freshness(page, manifest, locale),
        render_install_metadata(page, locale),
        render_registry_insights(page, locale),
        render_external_package_manager_matches(page, locale),
        render_faq(page, locale),
        render_related(page, locale),
        render_sources(page, locale),
    ]
    breadcrumbs = f"""
<nav class="breadcrumbs" aria-label="Breadcrumbs">
  <a href="../../../">{html_escape(tx(locale, 'home', 'Home'))}</a>
  <span>/</span>
  <a href="../../">{html_escape(tx(locale, 'packages', 'Packages'))}</a>
  <span>/</span>
  <span>{html_escape(page.display_name)}</span>
</nav>
"""
    return html_doc(
        title=title,
        description=description,
        canonical=locale_url(page.path, locale),
        alternates_path=page.path,
        locale=locale,
        robots="index,follow" if is_indexable_package_page(page) else "noindex,follow",
        body=f"""
{nav('../../../', locale)}
<main>
  {breadcrumbs}
  <section class="pkg-hero" aria-labelledby="pkg-title">
    <div class="hero-copy">
      <p class="eyebrow">{html_escape(tx(locale, 'packageIntelligence', '{provider} package intelligence', provider=page.provider))}</p>
      <h1 id="pkg-title">{html_escape(tx(locale, 'installHeading', 'Install {name}', name=page.display_name))}</h1>
      <div class="summary-card">
        <p class="section-kicker">{html_escape(tx(locale, 'packageSummary', 'Package summary'))}</p>
        <p>{html_escape(plain_package_summary(page, locale))}</p>
      </div>
      <div class="hero-actions">
        <a class="button primary" href="#install">{html_escape(tx(locale, 'installAction', 'Install command'))}</a>
        <a class="button secondary" href="#security">{html_escape(tx(locale, 'agentRiskAssessment', 'Agent Risk Assessment'))}</a>
      </div>
    </div>
    <aside class="hero-panel" aria-label="{attr(tx(locale, 'packageFacts', 'Package facts'))}">
      {render_at_a_glance(page, locale)}
    </aside>
  </section>
  {''.join(sections)}
</main>
{footer('../../../', locale)}
""",
        stylesheet_href=locale_path("/pkg/styles.css", locale),
        favicon_href="/favicon.ico",
        schema=schema_for_package(page, description, updated, locale),
        extra_head=markdown_alternate_head(page.path, locale) if is_indexable_package_page(page) else "",
        extra_body=copy_script(locale),
    )


def render_agent_safety_answer(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    answer = page.agent_safety_answer
    if not answer:
        return ""
    rows = [
        (tx(locale, "agentSafetyCredentialAccess", "Credential access"), answer["credentialAccess"]),
        (tx(locale, "agentSafetyRemoteMutation", "Remote mutation"), answer["remoteMutation"]),
        (tx(locale, "agentSafetyPublishRisk", "Publish/artifact risk"), answer["publishOrArtifactRisk"]),
        (tx(locale, "agentSafetyControl", "Recommended control"), answer["recommendedControl"]),
        (tx(locale, "agentSafetyGuidance", "Agent-use guidance"), answer["agentUseGuidance"]),
    ]
    articles = "\n".join(
        f"<article><h3>{html_escape(label)}</h3><p>{html_escape(value)}</p></article>"
        for label, value in rows
    )
    return f"""
<section class="pkg-section split-section agent-safety-section" aria-labelledby="agent-safety-title">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'agentSafetyKicker', 'agent safety'))}</p>
    <h2 id="agent-safety-title">{html_escape(tx(locale, 'agentSafetyTitle', 'Agent safety answer'))}</h2>
    <p>{html_escape(answer["summary"])}</p>
  </div>
  <div class="detail-stack">
    {articles}
  </div>
</section>
"""


def render_at_a_glance(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    rows = [
        (tx(locale, "packageKey", "Package"), page.display_name),
        (tx(locale, "kind", "Category"), human_category_label(page, locale)),
        (tx(locale, "version", "Latest Version"), page.version or tx(locale, "unknown", "unknown")),
        (tx(locale, "executablesCount", "Executables"), fmt_int(package_executable_count(page))),
        (tx(locale, "packageManagersCount", "Package Managers"), fmt_int(package_manager_count(page))),
        (tx(locale, "risk", "Risk Level"), agent_risk_level(page, locale)),
    ]
    metrics = "".join(metric(label, value) for label, value in rows)
    return f"""
<div class="at-a-glance">
  <p class="section-kicker">{html_escape(tx(locale, 'atAGlance', 'At a glance'))}</p>
  {metrics}
</div>
"""


def render_install_support(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    coverage = package_manager_coverage(page)
    if not coverage:
        return ""
    items = "".join(
        f"<li><strong>{html_escape(platform)}</strong><span>{html_escape(', '.join(managers))}</span></li>"
        for platform, managers in coverage
    )
    return f"""
<section class="pkg-section support-section" aria-labelledby="support-title">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'supportedOn', 'supported on'))}</p>
    <h2 id="support-title">{html_escape(tx(locale, 'installSupportTitle', 'Install {name} on', name=page.display_name))}</h2>
    <p>{html_escape(tx(locale, 'installSupportCopy', 'This page collects package-manager commands for the major ecosystems found in local package metadata.'))}</p>
  </div>
  <ul class="support-list">{items}</ul>
</section>
"""


def agent_risk_level(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    if page.geiger:
        return geiger_level_label(page.geiger)
    if page.agent_safety_answer:
        return tx(locale, "reviewed", "reviewed")
    return tx(locale, "unknown", "unknown")


def agent_risk_confidence(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    if page.geiger:
        return geiger_confidence_label(page.geiger)
    if page.agent_safety_answer:
        return tx(locale, "reviewed", "reviewed")
    return tx(locale, "unknownConfidence", "unknown confidence")


def agent_risk_why(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    answer = page.agent_safety_answer
    if answer:
        return answer["summary"]
    if page.geiger:
        return security_summary(page, locale)
    summary = plain_package_summary(page, locale)
    return tx(
        locale,
        "agentRiskWhy",
        "{summary} No dedicated protected-tool manifest is present yet, so review command behavior before unattended agent use.",
        summary=summary,
    )


def agent_review_items(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    items: list[str] = []
    answer = page.agent_safety_answer
    if answer:
        for field in ("credentialAccess", "remoteMutation", "publishOrArtifactRisk", "recommendedControl", "agentUseGuidance"):
            items.append(answer[field])
    if page.isotope:
        caveats = [public_copy(item) for item in page.isotope.get("caveats") or [] if str(item or "").strip()]
        items.extend(caveats[:4])
    if page.approval_gate:
        items.extend(str(rule) for rule in (page.approval_gate.get("rules") or [])[:4] if str(rule or "").strip())
    if page.geiger:
        items.extend(str(reason) for reason in (page.geiger.get("reasons") or [])[:3] if str(reason or "").strip())
    behavior = page.install_behavior or {}
    if behavior.get("service"):
        items.append(tx(locale, "signalService", "Formula metadata declares a service or daemon block."))
    if behavior.get("postInstallDefined") is True:
        items.append(tx(locale, "signalHomebrewPostinstall", "Package metadata declares a post-install hook."))
    if not items:
        items.append(tx(locale, "recommendedReviewCopy", "Before unattended agent use, check whether the tool reads plaintext credentials, writes remote state, publishes artifacts, or shells out to plugins."))
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = paragraph_text(item, 220)
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        result.append(text)
        if len(result) >= 6:
            break
    return result


def render_agent_risk_assessment(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    review_items = "".join(f"<li>{html_escape(item)}</li>" for item in agent_review_items(page, locale))
    geiger = render_geiger(page, locale)
    install_signals = render_install_behavior_signals(page, locale)
    protected = ""
    if page.isotope:
        justification = page.isotope.get("justification") or {}
        title = public_copy(justification.get("title") or tx(locale, "radioisotopeCoverage", "Protected-tool coverage"))
        detail = public_copy(paragraph_text(justification.get("detail") or page.isotope_readme or tx(locale, "radioisotopeManifestFallback", "Automic Vault has a local secret-handling manifest for this package.")))
        caveats = page.isotope.get("caveats") or []
        caveat_items = "".join(f"<li>{html_escape(public_copy(item))}</li>" for item in caveats[:8])
        protected = f"""
    <article>
      <h3>{html_escape(title)}</h3>
      <p>{html_escape(detail)}</p>
      <ul>{caveat_items or f'<li>{html_escape(tx(locale, "noCaveats", "No caveats were listed in the local manifest."))}</li>'}</ul>
    </article>
"""
    return f"""
<section id="security" class="pkg-section security-section agent-risk-section">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'agentRiskQuestion', 'Should an AI agent run this tool unattended?'))}</p>
    <h2>{html_escape(tx(locale, 'agentRiskAssessment', 'Agent Risk Assessment'))}</h2>
    <p>{html_escape(agent_risk_why(page, locale))}</p>
    {geiger}
    {install_signals}
    {render_readme_excerpt(page, locale)}
  </div>
  <div class="detail-stack">
    <article>
      <h3>{html_escape(tx(locale, 'riskLevel', 'Risk level: {level}', level=agent_risk_level(page, locale)))}</h3>
      <p>{html_escape(tx(locale, 'classifierConfidence', 'Classifier confidence'))}: {html_escape(agent_risk_confidence(page, locale))}</p>
    </article>
    <article>
      <h3>{html_escape(tx(locale, 'recommendedHumanReviewItems', 'Recommended human review items'))}</h3>
      <ul>{review_items}</ul>
    </article>
    {protected}
  </div>
</section>
{render_gate(page, locale)}
"""


def markdown_alternate_head(path: str, locale: dict[str, Any] | None = None) -> str:
    return f'  <link rel="alternate" type="text/markdown" href="{attr(f"{locale_url(path, locale)}index.md")}">'


def render_package_markdown(
    page: PackagePage,
    manifest: dict[str, Any],
    locale: dict[str, Any] | None = None,
) -> str:
    updated = fmt_date(page.last_verified) or fmt_date(page.last_updated_at) or fmt_date(manifest.get("generated_at", ""))
    lines = [
        f"# {md_text(tx(locale, 'installHeading', 'Install {name}', name=page.display_name))}",
        "",
        md_text(plain_package_summary(page, locale)),
        "",
        f"## {md_text(tx(locale, 'install', 'Install'))}",
        "",
        "```sh",
        install_command(page),
        "```",
        "",
    ]
    lines.extend(md_install_support_section(page, locale))
    lines.extend(md_agent_risk_section(page, locale))
    lines.extend(md_install_command_groups(page, locale))
    lines.extend([f"## {md_text(tx(locale, 'packageFacts', 'Package Facts'))}", ""])
    fact_rows = [
        (tx(locale, "packageKey", "Package key"), page.key),
        (tx(locale, "packageManager", "Package manager"), package_manager_label(page)),
        (tx(locale, "packageManagerPage", "Package manager URL"), page.package_manager_url),
        (tx(locale, "version", "Version"), page.version),
        (tx(locale, "sourceSummary", "Source summary"), clean_summary(page.summary)),
        (tx(locale, "homepage", "Homepage"), page.homepage),
        (tx(locale, "repository", "Repository"), page.repository),
        (tx(locale, "upstreamDocs", "Upstream docs"), page.upstream_docs),
        (tx(locale, "license", "License"), page.license),
        (tx(locale, "sourceArchive", "Source archive"), page.source_archive),
        (tx(locale, "issueTracker", "Issue tracker"), page.issue_tracker),
        (tx(locale, "published", "Published"), page.published_at),
        (tx(locale, "verified", "Last verified"), page.last_verified),
        (tx(locale, "updated", "Last updated"), page.last_updated_at),
        (tx(locale, "generatedSource", "Generated"), manifest.get("generated_at")),
    ]
    lines.extend(md_fact_lines(fact_rows))
    lines.extend(md_section_list(tx(locale, "executables", "Executables"), executable_markdown_items(page, locale)))
    lines.extend(md_section_list(tx(locale, "dependencies", "Dependencies"), [*page.dependencies]))
    lines.extend(md_section_list(tx(locale, "buildDependencies", "Build Dependencies"), [*page.build_dependencies]))
    lines.extend(md_section_list(tx(locale, "usesFromMacos", "macOS Provided Libraries"), [*page.uses_from_macos]))
    lines.extend(md_install_behavior_section(page, locale))
    lines.extend(md_registry_insights_section(page, locale))
    lines.extend(md_external_manager_matches_section(page, locale))
    lines.extend(md_freshness_section(page, manifest, locale))
    lines.extend(md_faq_section(page, locale))
    lines.extend(md_related_section(page, locale))
    lines.extend(md_section_list(tx(locale, "sources", "Sources"), page.source_notes))
    return "\n".join(lines).rstrip() + "\n"


def md_install_support_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    coverage = package_manager_coverage(page)
    if not coverage:
        return []
    lines = [f"## {md_text(tx(locale, 'installSupportTitle', 'Install {name} on', name=page.display_name))}", ""]
    for platform, managers in coverage:
        lines.append(f"- **{md_text(platform)}:** {md_text(', '.join(managers))}")
    lines.append("")
    return lines


def md_agent_risk_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    lines = [
        f"## {md_text(tx(locale, 'agentRiskAssessment', 'Agent Risk Assessment'))}",
        "",
        md_text(agent_risk_why(page, locale)),
        "",
        f"- **{md_text(tx(locale, 'risk', 'Risk'))}:** {md_text(agent_risk_level(page, locale))}",
        f"- **{md_text(tx(locale, 'classifierConfidence', 'Classifier confidence'))}:** {md_text(agent_risk_confidence(page, locale))}",
        "",
        f"### {md_text(tx(locale, 'recommendedHumanReviewItems', 'Recommended human review items'))}",
        "",
    ]
    lines.extend(f"- {md_text(item)}" for item in agent_review_items(page, locale))
    lines.append("")
    if page.geiger:
        lines.append(f"- **{md_text(tx(locale, 'geigerRisk', 'Geiger risk'))}:** {md_text(geiger_level_label(page.geiger))} / {md_text(geiger_confidence_label(page.geiger))}")
        for reason in (page.geiger.get("reasons") or [])[:5]:
            lines.append(f"- {md_value(reason)}")
        lines.append("")
    return lines


def md_faq_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    entries = faq_entries(page, locale)
    if not entries:
        return []
    lines = [f"## {md_text(tx(locale, 'schemaFaqName', 'Package FAQ'))}", ""]
    for item in entries:
        lines.extend([f"### {md_text(item['question'])}", "", md_text(item["answer"]), ""])
    return lines


def md_agent_safety_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    answer = page.agent_safety_answer
    if not answer:
        return []
    lines = [
        f"## {md_text(tx(locale, 'agentSafetyTitle', 'Agent safety answer'))}",
        "",
        md_text(answer["summary"]),
        "",
    ]
    for label, field in [
        (tx(locale, "agentSafetyCredentialAccess", "Credential access"), "credentialAccess"),
        (tx(locale, "agentSafetyRemoteMutation", "Remote mutation"), "remoteMutation"),
        (tx(locale, "agentSafetyPublishRisk", "Publish/artifact risk"), "publishOrArtifactRisk"),
        (tx(locale, "agentSafetyControl", "Recommended control"), "recommendedControl"),
        (tx(locale, "agentSafetyGuidance", "Agent-use guidance"), "agentUseGuidance"),
    ]:
        lines.append(f"- **{md_text(label)}:** {md_text(answer[field])}")
    lines.append("")
    return lines


def md_install_command_groups(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    commands = package_manager_install_entries(page)
    if not commands:
        return []
    labels = {
        "macos": "macOS",
        "linux": "Linux",
        "windows": "Windows",
        "portable": "Portable and language managers",
    }
    grouped: dict[str, list[dict[str, Any]]] = {"macos": [], "linux": [], "windows": [], "portable": []}
    for item in commands:
        platform = str(item.get("platform") or "portable")
        if platform not in grouped:
            platform = "portable"
        grouped[platform].append(item)
    lines: list[str] = [f"{md_text(tx(locale, 'additionalInstallCommands', 'Additional install commands'))}:", ""]
    for platform in ("macos", "linux", "windows", "portable"):
        items = grouped.get(platform) or []
        if not items:
            continue
        lines.extend([f"### {labels[platform]}", ""])
        for item in items:
            manager = md_text(item.get("manager") or "shell")
            command_text = md_text(item.get("command") or "")
            try:
                confidence_text = f"{float(item.get('confidence')):.0%}"
            except (TypeError, ValueError):
                confidence_text = tx(locale, "unknownConfidence", "unknown confidence")
            evidence = md_text(item.get("evidence") or "")
            lines.extend([f"- {manager} ({confidence_text}):", "", "```sh", command_text, "```"])
            if evidence:
                lines.extend(["", f"  {md_text(tx(locale, 'markdownEvidence', 'Evidence'))}: {evidence}"])
            lines.append("")
    return lines


def md_fact_lines(rows: list[tuple[str, Any]]) -> list[str]:
    lines: list[str] = []
    for label, value in rows:
        if value:
            lines.append(f"- **{md_text(label)}:** {md_value(value)}")
    lines.append("")
    return lines


def md_section_list(title: str, items: list[Any]) -> list[str]:
    values = [md_value(item) for item in items if str(item or "").strip()]
    if not values:
        return []
    lines = [f"## {md_text(title)}", ""]
    lines.extend(f"- {value}" for value in values)
    lines.append("")
    return lines


def executable_markdown_items(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    items: list[str] = []
    for item in page.executables:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("target") or item.get("source") or "").strip()
        if not name:
            continue
        kind = str(item.get("kind") or item.get("type") or tx(locale, "executable", "executable")).strip()
        note = str(item.get("note") or item.get("source") or "").strip()
        detail = f"{name} ({kind})"
        if note:
            detail += f": {note}"
        items.append(detail)
    for binary in page.binaries:
        if not isinstance(binary, dict):
            continue
        name = str(binary.get("target") or binary.get("source") or "").strip()
        if name:
            items.append(f"{name} ({tx(locale, 'binary', 'binary')})")
    for alias_name in sorted(page.aliases):
        items.append(f"{alias_name} ({tx(locale, 'alias', 'alias')})")
    return items


def md_install_behavior_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    behavior = page.install_behavior or {}
    items: list[str] = []
    if behavior.get("postInstallDefined") is not None:
        items.append(f"{tx(locale, 'postInstallHook', 'Post-install hook')}: {tx(locale, 'defined', 'defined') if behavior.get('postInstallDefined') else tx(locale, 'notDefined', 'not defined')}")
    if behavior.get("service"):
        items.append(f"{tx(locale, 'service', 'Service')}: {behavior.get('service')}")
    if behavior.get("caveats"):
        items.append(f"{tx(locale, 'caveats', 'Caveats')}: {behavior.get('caveats')}")
    if behavior.get("lifecycleScripts"):
        items.append(f"{tx(locale, 'lifecycleScripts', 'Lifecycle scripts')}: {', '.join(str(item) for item in behavior.get('lifecycleScripts') or [])}")
    if behavior.get("pythonRequires"):
        items.append(f"{tx(locale, 'pythonRequires', 'Python requires')}: {behavior.get('pythonRequires')}")
    if behavior.get("requiresDistCount") is not None:
        items.append(f"{tx(locale, 'pypiDependencySpecs', 'PyPI dependency specs')}: {behavior.get('requiresDistCount')}")
    bottle = page.bottle or {}
    if bottle:
        available = bottle.get("available")
        bottle_detail = tx(locale, "available", "available") if available else tx(locale, "notAvailable", "not available")
        platforms = bottle.get("platforms") or []
        if platforms:
            bottle_detail += f" {tx(locale, 'onPlatforms', 'on')} {', '.join(str(item) for item in platforms[:12])}"
        items.append(f"{tx(locale, 'bottle', 'Bottle')}: {bottle_detail}")
    return md_section_list(tx(locale, "installBehavior", "Install Behavior"), items)


def md_registry_insights_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    insights = page.extra.get("registryInsights") if isinstance(page.extra.get("registryInsights"), dict) else {}
    if not insights:
        return []
    rows = registry_insight_rows(insights)
    if not rows:
        return []
    lines = [f"## {md_text(tx(locale, 'registryInsightsTitle', 'Source Database Details'))}", ""]
    for label, value in rows[:28]:
        lines.append(f"- **{md_text(label)}:** {md_metadata_value(value)}")
    lines.append("")
    return lines


def md_external_manager_matches_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    matches = page.external_package_manager_matches[:16]
    if not matches:
        return []
    lines = [f"## {md_text(tx(locale, 'otherPackageManagersTitle', 'Other Package-Manager Records'))}", ""]
    for item in matches:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        bits = [
            str(item.get("displayName") or item.get("manager") or ""),
            str(item.get("packageId") or ""),
        ]
        if metadata.get("version"):
            bits.append(str(metadata.get("version")))
        line = " - ".join(md_text(bit) for bit in bits if bit)
        details = []
        if item.get("reason"):
            details.append(str(item.get("reason")))
        if metadata.get("summary"):
            details.append(short_text(metadata.get("summary"), 180))
        if metadata.get("homepage"):
            details.append(str(metadata.get("homepage")))
        if item.get("evidence"):
            details.append(str(item.get("evidence")))
        suffix = f": {md_text(' | '.join(details))}" if details else ""
        lines.append(f"- {line}{suffix}")
    lines.append("")
    return lines


def md_metadata_value(value: Any) -> str:
    if isinstance(value, dict):
        return md_text(", ".join(f"{key}: {child}" for key, child in value.items() if child not in ("", [], {}, None)))
    if isinstance(value, list):
        return md_text(", ".join(str(item) for item in value if str(item or "").strip()))
    if isinstance(value, bool):
        return md_text("yes" if value else "no")
    if isinstance(value, (int, float)):
        return md_text(fmt_int(value) if isinstance(value, int) else str(value))
    return md_value(value)


def md_freshness_section(page: PackagePage, manifest: dict[str, Any], locale: dict[str, Any] | None = None) -> list[str]:
    freshness = page.version_freshness or {}
    manager = freshness.get("packageManager") if isinstance(freshness.get("packageManager"), dict) else {}
    site = freshness.get("siteData") if isinstance(freshness.get("siteData"), dict) else {}
    upstream = freshness.get("upstream") if isinstance(freshness.get("upstream"), dict) else {}
    warnings = freshness.get("warnings") if isinstance(freshness.get("warnings"), list) else []
    items = [
        f"{tx(locale, 'pageGenerated', 'Page generated')}: {fmt_date(manifest.get('generated_at', '')) or tx(locale, 'unknown', 'unknown')}",
        f"{tx(locale, 'managerVersion', 'Package-manager version')}: {manager.get('version') or page.version or tx(locale, 'unknown', 'unknown')}",
    ]
    if manager.get("updatedAt"):
        items.append(f"{tx(locale, 'managerUpdated', 'Package-manager updated')}: {fmt_date(manager.get('updatedAt')) or manager.get('updatedAt')}")
    if site.get("status"):
        items.append(f"{tx(locale, 'localData', 'Local data status')}: {site.get('status')}")
    if upstream.get("repository"):
        items.append(f"{tx(locale, 'upstreamRepository', 'Upstream repository')}: {upstream.get('repository')}")
    if upstream.get("latestVersion"):
        items.append(f"{tx(locale, 'upstreamLatestDetected', 'Upstream latest detected')}: {upstream.get('latestVersion')} ({upstream.get('comparison') or tx(locale, 'unknown', 'unknown')})")
    for item in warnings[:8]:
        if isinstance(item, dict):
            items.append(f"{item.get('severity', 'info')}: {item.get('message')}")
    return md_section_list(tx(locale, "freshness", "Freshness"), items)


def md_security_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    lines = [f"## {md_text(tx(locale, 'securityNotes', 'Security Notes'))}", "", md_text(security_summary(page, locale)), ""]
    if page.geiger:
        lines.append(f"- **{md_text(tx(locale, 'geigerRisk', 'Geiger risk'))}:** {md_text(geiger_level_label(page.geiger))} / {md_text(geiger_confidence_label(page.geiger))}")
        for reason in (page.geiger.get("reasons") or [])[:5]:
            lines.append(f"- {md_value(reason)}")
    if page.isotope:
        justification = page.isotope.get("justification") or {}
        title = public_copy(justification.get("title") or tx(locale, "radioisotopeCoverage", "Protected-tool coverage"))
        lines.append(f"- **{md_text(tx(locale, 'radioisotopeKicker', 'Protected-tool coverage'))}:** {md_value(title)}")
    if page.approval_gate:
        lines.append(f"- **{md_text(tx(locale, 'approvalRules', 'Approval gate rules'))}:** {md_value(page.approval_gate.get('rule_count'))}")
    lines.append("")
    return lines


def md_related_section(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    workflow_rels = {"adjacent_workflow", "format_peer", "language_runtime_peer", "command_surface_peer", "security_surface_peer", "domain_peer"}
    related = filtered_related_items(page, page.related_packages, exclude_rels=workflow_rels, limit=8)
    workflow = filtered_related_items(page, page.related_packages, include_rels=workflow_rels, limit=6)
    also = filtered_related_items(page, page.also_available_via, limit=4)
    if not related and not workflow and not also and not page.package_hubs:
        return []
    lines = [f"## {md_text(tx(locale, 'relatedLinks', 'Related Links'))}", ""]
    for hub in page.package_hubs[:4]:
        if not isinstance(hub, dict):
            continue
        slug = str(hub.get("slug") or "").strip()
        label = str(hub.get("label") or slug).strip()
        reason = str(hub.get("reason") or "").strip()
        if slug and label:
            suffix = f" - {md_text(reason)}" if reason else ""
            lines.append(f"- [{md_text(label)}]({SITE_ORIGIN}/pkg/{slug}/){suffix}")
    for item in [*related, *workflow, *also][:18]:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        name = str(item.get("name") or "").strip()
        label = str(item.get("label") or name).strip()
        if not provider or not name:
            continue
        reason = str(item.get("reason") or item.get("rel") or "").strip()
        href = f"{SITE_ORIGIN}/pkg/{provider}/{slugify(name)}/"
        suffix = f" - {md_text(reason)}" if reason else ""
        lines.append(f"- [{md_text(label)}]({href}){suffix}")
    lines.append("")
    return lines


def md_value(value: Any) -> str:
    text = md_text(value)
    if re.match(r"^https?://", text):
        return f"<{text}>"
    return text


def md_text(value: Any) -> str:
    return normalize_space(value).replace("|", "\\|")


def hero_sentence(page: PackagePage) -> str:
    summary = clean_summary(page.summary)
    if summary and install_command(page):
        alternate = alternate_install_sentence(page)
        alternate_text = f" {alternate}" if alternate else ""
        return f"{sentence_text(summary)} Version {page.version or 'unknown'} via {package_manager_label(page)}; verified {fmt_date(page.last_verified) or fmt_date(page.last_updated_at) or 'from local package data'}.{alternate_text}"
    if page.isotope:
        title = ((page.isotope.get("justification") or {}).get("title") or "secret handling").rstrip(".")
        return f"Automic Vault tracks {page.display_name} because {public_copy(title).lower()} affects agent-run command-line tools on macOS."
    if page.approval_gate:
        return f"Automic Vault has approval-gate metadata for {page.display_name}, including high-risk commands and recommended human review points."
    if summary:
        return f"Package data for {page.display_name}: {summary}"
    return f"Package metadata for {page.display_name}, from local pkg.so sources."


def localized_hero_sentence(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    if locale_code(locale) == "en":
        return hero_sentence(page)
    return tx(
        locale,
        "heroSentence",
        "View install routes, executables, metadata, and security notes for {name}.",
        name=page.display_name,
    )


def sentence_text(value: str) -> str:
    text = normalize_space(value)
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text


def meta_description(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    managers = package_manager_title_list(page)
    coverage = package_manager_coverage(page)
    platforms = ", ".join(platform for platform, _managers in coverage[:6])
    if locale_code(locale) != "en":
        return short_text(
            tx(
                locale,
                "metaDescription",
                "Install {name} with {managers}. View package-manager commands, executables, version freshness, and agent risk.",
                name=page.display_name,
                manager=package_manager_label(page),
                managers=managers,
                platforms=platforms,
            ),
            155,
        )
    parts = [f"Install {page.display_name} with {managers}."]
    if platforms:
        parts.append(f"Covers {platforms}.")
    if page.executables or page.aliases:
        parts.append("Includes executables, version freshness, and agent risk.")
    summary = plain_package_summary(page, locale)
    if summary:
        parts.append(summary)
    if page.isotope:
        title = (page.isotope.get("justification") or {}).get("title")
        if title:
            parts.append(f"Protected-tool coverage: {public_copy(title)}.")
    if page.approval_gate:
        parts.append(f"Includes {page.approval_gate.get('rule_count')} approval-gate rules.")
    return short_text(" ".join(parts), 155)


def alternate_install_command(page: PackagePage) -> dict[str, Any] | None:
    for item in install_command_entries(page):
        if not isinstance(item, dict) or item.get("kind") == "automic_vault":
            continue
        command_key = native_command_package_key(str(item.get("command") or ""))
        if command_key is None:
            continue
        provider, _ = command_key
        if provider != page.provider:
            return item
    return None


def alternate_install_sentence(page: PackagePage) -> str:
    alternate = alternate_install_command(page)
    if not alternate:
        return ""
    manager = str(alternate.get("manager") or "another package manager")
    command_text = str(alternate.get("command") or "").strip()
    if not command_text:
        return ""
    return f"Also installable with {manager}: {command_text}."


def label_for(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    labels = [page.provider]
    if page.isotope:
        labels.append(tx(locale, "radioisotopeKicker", "protected-tool coverage"))
    if page.approval_gate:
        labels.append(tx(locale, "approvalGatesKicker", "approval gates"))
    rank = page.popularity.get("rank")
    if rank:
        labels.append(f"{tx(locale, 'rank', 'rank')} {rank}")
    return " / ".join(labels)


def package_facts(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    facts = [metric(tx(locale, "manager", "manager"), package_manager_label(page))]
    if page.version:
        facts.append(metric(tx(locale, "version", "version"), page.version))
    if page.license:
        facts.append(metric(tx(locale, "license", "license"), page.license))
    if page.geiger:
        facts.append(metric(tx(locale, "risk", "risk"), geiger_level_label(page.geiger)))
        facts.append(metric(tx(locale, "classifierConfidence", "classifier confidence"), geiger_confidence_label(page.geiger)))
    rank = page.popularity.get("rank")
    if rank:
        facts.append(metric(tx(locale, "rank", "rank"), fmt_int(rank)))
    installs = (
        page.popularity.get("installs_per_365_days")
        or page.popularity.get("downloads_per_30_days")
        or page.popularity.get("recent_downloads")
    )
    if installs:
        if page.popularity.get("installs_per_365_days"):
            label = tx(locale, "installs365d", "365d installs")
        elif page.popularity.get("downloads_per_30_days"):
            label = tx(locale, "downloads30d", "30d downloads")
        else:
            window = page.popularity.get("recent_download_window_days") or 90
            label = tx(locale, "recentDownloads", "{days}d downloads", days=window)
        facts.append(metric(label, fmt_int(installs)))
    if page.isotope:
        facts.append(metric(tx(locale, "radioisotopeKicker", "protected-tool coverage"), tx(locale, "covered", "covered")))
    if page.approval_gate:
        facts.append(metric(tx(locale, "approvalRules", "approval rules"), fmt_int(page.approval_gate.get("rule_count"))))
    if page.last_verified:
        facts.append(metric(tx(locale, "verified", "verified"), fmt_date(page.last_verified)))
    elif page.last_updated_at:
        facts.append(metric(tx(locale, "updated", "updated"), fmt_date(page.last_updated_at)))
    return "".join(facts)


def metric(label: str, value: Any, *, pagefind_ignore: bool = False) -> str:
    pagefind_attr = ' data-pagefind-ignore="all"' if pagefind_ignore else ""
    return f'<div class="metric"{pagefind_attr}><span>{html_escape(label)}</span><strong>{html_escape(value)}</strong></div>'


def package_manager_label(page: PackagePage) -> str:
    if page.package_manager:
        return page.package_manager
    return {
        "brew": "Homebrew",
        "cask": "Homebrew Cask",
        "npm": "npm",
        "pip": "PyPI",
        "cargo": "Cargo",
    }.get(page.provider, page.provider)


def install_command(page: PackagePage) -> str:
    commands = install_command_entries(page)
    if commands:
        return str(commands[0].get("command") or "")
    return f"sudo av install {page.key}"


def native_install_command(page: PackagePage) -> str:
    if page.provider == "brew":
        return f"brew install {page.name}"
    if page.provider == "cask":
        return f"brew install --cask {page.name}"
    if page.provider == "npm":
        return f"npm install -g {page.name}"
    if page.provider == "pip":
        return f"pip install {page.name}"
    if page.provider == "cargo":
        return f"cargo install {page.name}"
    return ""


def install_command_entries(page: PackagePage) -> list[dict[str, Any]]:
    if page.install_commands:
        return page.install_commands
    entries = [
        {
            "platform": "portable",
            "manager": "Automic Vault",
            "command": f"sudo av install {page.key}",
            "kind": "automic_vault",
            "confidence": 1.0,
            "evidence": "deterministic local package key",
        }
    ]
    native = native_install_command(page)
    if native:
        platform = "macos" if page.provider in {"brew", "cask"} else "portable"
        manager = package_manager_label(page)
        entries.append({
            "platform": platform,
            "manager": manager,
            "command": native,
            "kind": "package_manager",
            "confidence": 1.0,
            "evidence": f"{manager} package metadata",
        })
    return entries


def package_manager_install_entries(page: PackagePage) -> list[dict[str, Any]]:
    return [
        item for item in install_command_entries(page)
        if item.get("kind") != "automic_vault" and str(item.get("manager") or "").lower() != "automic vault"
    ]


def source_backed_schema_commands(page: PackagePage) -> list[dict[str, Any]]:
    result = []
    for item in package_manager_install_entries(page):
        command_text = str(item.get("command") or "").strip()
        evidence = str(item.get("evidence") or "")
        if not command_text or "agent-inferred" in evidence:
            continue
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.9:
            continue
        result.append(item)
    return result


MANAGER_TITLE_PRIORITY = (
    "homebrew",
    "apt",
    "pacman",
    "dnf",
    "nix",
    "npm",
    "pip",
    "macports",
    "apk",
    "zypper",
    "chocolatey",
    "winget",
    "scoop",
    "cargo",
)

MANAGER_TITLE_LABELS = {
    "homebrew": "Homebrew",
    "homebrew cask": "Homebrew",
    "apt": "apt",
    "debian": "apt",
    "debian apt": "apt",
    "ubuntu": "apt",
    "ubuntu apt": "apt",
    "pacman": "pacman",
    "arch linux pacman": "pacman",
    "dnf": "dnf",
    "fedora dnf": "dnf",
    "nix": "Nix",
    "nixpkgs": "Nix",
    "macports": "MacPorts",
    "apk": "apk",
    "alpine linux apk": "apk",
    "zypper": "zypper",
    "opensuse zypper": "zypper",
    "chocolatey": "Chocolatey",
    "winget": "winget",
    "windows package manager": "winget",
    "scoop": "Scoop",
    "npm": "npm",
    "pip": "pip",
    "python pip": "pip",
    "cargo": "Cargo",
}

PLATFORM_GROUPS = (
    ("macos", "macOS"),
    ("debian-ubuntu", "Debian/Ubuntu"),
    ("fedora", "Fedora"),
    ("arch", "Arch Linux"),
    ("alpine", "Alpine Linux"),
    ("opensuse", "openSUSE"),
    ("nixos", "NixOS"),
    ("windows", "Windows"),
    ("portable", "Portable"),
)


def manager_key_for_item(item: dict[str, Any]) -> str:
    source = item.get("source")
    source_manager = ""
    if isinstance(source, dict):
        source_manager = str(source.get("manager") or "").strip()
    manager = str(item.get("manager") or "").strip()
    label = install_command_manager_label(item)
    text = (source_manager or manager or label).lower()
    text = text.replace("linux ", "").replace(" package manager", "")
    if text in {"debian", "ubuntu"}:
        return "apt"
    if text in {"fedora"}:
        return "dnf"
    if text in {"arch"}:
        return "pacman"
    if text in {"opensuse", "open suse"}:
        return "zypper"
    return text


def title_manager_label(item: dict[str, Any]) -> str:
    key = manager_key_for_item(item)
    manager = str(item.get("manager") or "").strip()
    label = install_command_manager_label(item)
    return MANAGER_TITLE_LABELS.get(key) or MANAGER_TITLE_LABELS.get(manager.lower()) or MANAGER_TITLE_LABELS.get(label.lower()) or manager or label


def package_manager_title_list(page: PackagePage, limit: int = 5) -> str:
    labels: dict[str, str] = {}
    for item in source_backed_schema_commands(page):
        if item.get("kind") == "automic_vault":
            continue
        label = title_manager_label(item)
        if not label:
            continue
        normalized = label.lower()
        labels.setdefault(normalized, label)
    if not labels:
        labels[package_manager_label(page).lower()] = package_manager_label(page)
    ordered = sorted(
        labels.values(),
        key=lambda label: (
            MANAGER_TITLE_PRIORITY.index(label.lower()) if label.lower() in MANAGER_TITLE_PRIORITY else 999,
            label.lower(),
        ),
    )
    return ", ".join(ordered[:limit])


def package_install_title(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    managers = package_manager_title_list(page)
    return tx(
        locale,
        "installTitle",
        "How to Install {name} | {managers}",
        name=page.display_name,
        manager=package_manager_label(page),
        managers=managers,
    )


def plain_package_summary(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    source = clean_summary(page.summary)
    if locale_code(locale) != "en":
        return tx(
            locale,
            "summaryPlainFallback",
            "{name} package page with install commands, executables, freshness, and agent risk metadata.",
            name=page.display_name,
        )
    taxonomy = page.extra.get("pkgTaxonomy") if isinstance(page.extra.get("pkgTaxonomy"), dict) else {}
    tags = {str(item).lower() for item in taxonomy.get("tags") or []}
    tags.update(str(item).lower() for item in page.keywords)
    haystack = " ".join([page.name, source, " ".join(tags)]).lower()
    name = page.display_name
    if {"ssh", "sftp", "fuse", "filesystem"} & tags or ("ssh file transfer protocol" in haystack and "file system" in haystack):
        return f"{name} mounts remote machines over SSH/SFTP and exposes them as local filesystems."
    if "version-control" in tags or "source-control" in tags or "git" in tags:
        return f"{name} helps inspect or manage source-code repositories from the command line."
    if "cloud" in tags or "infrastructure-as-code" in tags or "kubernetes" in tags:
        return f"{name} manages cloud or infrastructure workflows from a local command line."
    if "linter" in tags or "static-analysis" in tags:
        return f"{name} analyzes source code and reports issues before changes ship."
    if "formatter" in tags or "text-processing" in tags:
        return f"{name} formats, searches, or transforms text and structured files."
    if "compression" in tags or "archive" in tags:
        return f"{name} creates, extracts, or inspects compressed archive files."
    if "database" in tags or "sql" in tags:
        return f"{name} works with databases or data files from the command line."
    if "server" in tags:
        return f"{name} runs or supports a local service or network server."
    if source:
        lowered = source[:1].lower() + source[1:]
        if name.lower() in lowered.lower()[: max(len(name) + 8, 20)]:
            return sentence_text(source)
        return sentence_text(f"{name} is {lowered}")
    return tx(
        locale,
        "summaryPlainFallback",
        "{name} package page with install commands, executables, freshness, and agent risk metadata.",
        name=page.display_name,
    )


def platform_group_for_manager(item: dict[str, Any]) -> str:
    key = manager_key_for_item(item)
    platform = str(item.get("platform") or "").lower()
    if key in {"homebrew", "homebrew cask", "macports"}:
        return "macos"
    if key in {"apt", "debian apt", "ubuntu apt"}:
        return "debian-ubuntu"
    if key in {"dnf", "fedora dnf"}:
        return "fedora"
    if key in {"pacman", "arch linux pacman"}:
        return "arch"
    if key in {"apk", "alpine linux apk"}:
        return "alpine"
    if key in {"zypper", "opensuse zypper"}:
        return "opensuse"
    if key in {"nix", "nixpkgs"}:
        return "nixos"
    if key in {"chocolatey", "winget", "scoop"} or platform == "windows":
        return "windows"
    return "portable"


def package_manager_coverage(page: PackagePage) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[str]] = {key: [] for key, _label in PLATFORM_GROUPS}
    seen: set[tuple[str, str]] = set()
    for item in source_backed_schema_commands(page):
        if item.get("kind") == "automic_vault":
            continue
        group = platform_group_for_manager(item)
        manager = title_manager_label(item)
        if not manager:
            continue
        key = (group, manager.lower())
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(group, []).append(manager)
    result = []
    for key, label in PLATFORM_GROUPS:
        managers = grouped.get(key) or []
        if managers:
            result.append((label, managers))
    return result


def package_manager_count(page: PackagePage) -> int:
    return sum(len(managers) for _platform, managers in package_manager_coverage(page))


def package_executable_count(page: PackagePage) -> int:
    return len({item for item in executable_markdown_items(page) if item})


def human_category_label(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    taxonomy = page.extra.get("pkgTaxonomy") if isinstance(page.extra.get("pkgTaxonomy"), dict) else {}
    category = str(taxonomy.get("category") or page.category or "").strip()
    if not category:
        return tx(locale, "uncategorized", "uncategorized")
    return category.replace("-", " ").replace("_", " ").title()


def geiger_level_label(geiger: dict[str, Any]) -> str:
    level = geiger.get("level") or "unknown"
    return str(level)


def geiger_confidence_label(geiger: dict[str, Any]) -> str:
    confidence = geiger.get("confidence") or ""
    return str(confidence or "unknown")


def render_install(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    platform_html = render_platform_install_commands(package_manager_install_entries(page), locale)
    if not platform_html:
        return ""
    return f"""
<section id="install" class="pkg-section install-section" aria-labelledby="install-title">
  <div class="install-command-panel">
    <div>
      <p class="section-kicker">{html_escape(tx(locale, 'install', 'install'))}</p>
      <h2 id="install-title">{html_escape(tx(locale, 'additionalInstallCommands', 'Additional install commands'))}</h2>
    </div>
    {platform_html}
  </div>
</section>
"""


def render_platform_install_commands(commands: list[dict[str, Any]], locale: dict[str, Any] | None = None) -> str:
    grouped: dict[str, list[dict[str, Any]]] = {"macos": [], "linux": [], "windows": [], "portable": []}
    for item in commands:
        platform = str(item.get("platform") or "portable")
        if platform not in grouped:
            platform = "portable"
        grouped[platform].append(item)
    labels = {
        "macos": "macOS",
        "linux": "Linux",
        "windows": "Windows",
        "portable": "Portable and language managers",
    }
    sections = []
    for platform in ("macos", "linux", "windows", "portable"):
        items = grouped.get(platform) or []
        if not items:
            continue
        rows = "".join(install_command_row(item, locale) for item in items)
        sections.append(f"""
<article>
  <h3>{html_escape(labels[platform])}</h3>
  <div class="install-command-list">{rows}</div>
</article>
""")
    if not sections:
        return ""
    return f"""
<div class="platform-install-grid" aria-label="{attr(tx(locale, 'platformInstallCommands', 'Platform install commands'))}">
  {''.join(sections)}
</div>
"""


def install_command_row(item: dict[str, Any], locale: dict[str, Any] | None = None) -> str:
    command_text = str(item.get("command") or "")
    manager = str(item.get("manager") or "shell")
    manager_label = install_command_manager_label(item)
    source_html = install_command_source_html(item, locale)
    confidence = item.get("confidence")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0
    confidence_label = tx(locale, "verified", "verified") if confidence_value >= 0.9 else tx(locale, "inferred", "inferred")
    return f"""
<div class="install-command-row">
  <div class="install-command-head">
    <strong class="install-command-eyebrow">{html_escape(manager_label)}</strong>
    <span>{html_escape(confidence_label)} · {html_escape(f'{confidence_value:.0%}')}</span>
  </div>
  <div class="install-command-shell">
    <code>{html_escape(command_text)}</code>
    <button class="copy-button" type="button" data-copy="{attr(command_text)}" aria-label="{attr(tx(locale, 'copyManagerInstallCommand', 'Copy {manager} install command', manager=manager_label))}">{html_escape(tx(locale, 'copy', 'Copy'))}</button>
  </div>
  {source_html}
</div>
"""


def install_command_manager_label(item: dict[str, Any]) -> str:
    manager = str(item.get("manager") or "shell").strip()
    source = item.get("source")
    source_manager = ""
    if isinstance(source, dict):
        source_manager = str(source.get("manager") or "").strip()
    manager_key = (source_manager or manager).lower()
    labels = {
        "apk": "Alpine Linux apk",
        "apt": "Debian apt",
        "chocolatey": "Chocolatey",
        "dnf": "Fedora dnf",
        "macports": "MacPorts",
        "nix": "Nix",
        "pacman": "Arch Linux pacman",
        "scoop": "Scoop",
        "winget": "Windows Package Manager",
        "zypper": "openSUSE zypper",
        "npm": "npm",
        "pip": "Python pip",
        "cargo": "Cargo",
    }
    return labels.get(manager_key, manager or "shell")


def install_command_source_html(item: dict[str, Any], locale: dict[str, Any] | None = None) -> str:
    source = item.get("source")
    if isinstance(source, dict):
        label = str(source.get("source_label") or "").strip()
        package_name = str(source.get("package_name") or source.get("package_id") or "").strip()
        source_url = str(source.get("source_url") or "").strip()
        pieces = [html_escape(piece) for piece in (label, package_name) if piece]
        text = " · ".join(pieces)
        if source_url:
            link_label = source_host_label(source_url)
            link = f'<a href="{attr(source_url)}" aria-label="{attr(tx(locale, "sourceDatabaseAria", "Open source database on {host}", host=link_label))}">{html_escape(tx(locale, "installSourcePrefix", "source"))}: {html_escape(link_label)}</a>'
            text = f"{text} · {link}" if text else link
        return f'<p class="install-command-source">{text}</p>' if text else ""
    evidence = str(item.get("evidence") or "").strip()
    manager = str(item.get("manager") or "").strip()
    if evidence == f"{manager} package metadata":
        evidence = tx(locale, "managerPackageMetadata", "{manager} package metadata", manager=manager)
    return f'<p class="install-command-source">{html_escape(evidence)}</p>' if evidence else ""


def source_host_label(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "source"
    host = parsed.netloc.strip()
    return host or "source"


def render_overview(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    aliases = sorted(page.aliases)[:32]
    alias_html = "".join(f"<li>{html_escape(alias)}</li>" for alias in aliases)
    alias_block = f"<ul class=\"chip-list\">{alias_html}</ul>" if aliases else f"<p>{html_escape(tx(locale, 'noAliases', 'No executable aliases were found in the local package database.'))}</p>"
    homepage = f'<a href="{attr(page.homepage)}">{html_escape(page.homepage)}</a>' if page.homepage else html_escape(tx(locale, "homepageMissing", "Not present in the local metadata."))
    source_summary = clean_summary(page.summary)
    summary = html_escape(source_summary if locale_code(locale) == "en" else tx(locale, "summaryFallback", "Automic Vault publishes package-specific install routes, executable facts, and security metadata for {name} from local package data.", name=page.display_name))
    source_summary_html = ""
    if locale_code(locale) != "en" and source_summary:
        source_summary_html = f"""
    <article>
      <h3>{html_escape(tx(locale, 'sourceSummary', 'Source summary'))}</h3>
      <p>{html_escape(source_summary)}</p>
    </article>
"""
    return f"""
<section class="pkg-section split-section">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'overview', 'overview'))}</p>
    <h2>{html_escape(tx(locale, 'packageSummary', 'Package summary'))}</h2>
    <p>{summary}</p>
  </div>
  <div class="detail-stack">
    <article>
      <h3>{html_escape(tx(locale, 'homepage', 'Homepage'))}</h3>
      <p>{homepage}</p>
    </article>
    <article>
      <h3>{html_escape(tx(locale, 'commandsAndAliases', 'Commands and aliases'))}</h3>
      {alias_block}
    </article>
    {source_summary_html}
  </div>
</section>
"""


def render_security(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    geiger = render_geiger(page, locale)
    install_signals = render_install_behavior_signals(page, locale)
    if page.isotope:
        justification = page.isotope.get("justification") or {}
        title = html_escape(public_copy(justification.get("title") or tx(locale, "radioisotopeCoverage", "Protected-tool coverage")))
        detail = html_escape(public_copy(paragraph_text(justification.get("detail") or page.isotope_readme or tx(locale, "radioisotopeManifestFallback", "Automic Vault has a local secret-handling manifest for this package."))))
        caveats = page.isotope.get("caveats") or []
        caveat_items = "".join(f"<li>{html_escape(public_copy(item))}</li>" for item in caveats[:8])
        readme = render_readme_excerpt(page, locale)
        release_html = html_escape(tx(locale, "localRadioisotopeManifest", "Local secret-handling manifest"))
        return f"""
<section id="security" class="pkg-section security-section">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'radioisotopeKicker', 'protected-tool coverage'))}</p>
    <h2>{title}</h2>
    <p>{detail}</p>
    {geiger}
    {install_signals}
    {readme}
  </div>
  <div class="detail-stack">
    <article>
      <h3>{html_escape(tx(locale, 'coverageSource', 'Coverage source'))}</h3>
      <p>{release_html}</p>
    </article>
    <article>
      <h3>{html_escape(tx(locale, 'caveats', 'Caveats'))}</h3>
      <ul>{caveat_items or f'<li>{html_escape(tx(locale, "noCaveats", "No caveats were listed in the local manifest."))}</li>'}</ul>
    </article>
  </div>
</section>
{render_gate(page, locale)}
"""
    return render_gate(page, locale) or f"""
<section id="security" class="pkg-section security-section">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'securityPosture', 'security posture'))}</p>
    <h2>{html_escape(security_heading(page, locale))}</h2>
    <p>{html_escape(security_summary(page, locale))}</p>
    {geiger}
    {install_signals}
  </div>
  <div class="detail-stack">
    <article>
      <h3>{html_escape(tx(locale, 'recommendedReview', 'Recommended review'))}</h3>
      <p>{html_escape(tx(locale, 'recommendedReviewCopy', 'Before unattended agent use, check whether the tool reads plaintext credentials, writes remote state, publishes artifacts, or shells out to plugins.'))}</p>
    </article>
  </div>
</section>
"""


def security_heading(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    if page.geiger:
        return tx(locale, "riskLevel", "Risk level: {level}", level=geiger_level_label(page.geiger))
    return tx(locale, "radioisotopeMissingHeading", "No protected-tool coverage found yet")


def security_summary(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    if page.geiger:
        reasons = page.geiger.get("reasons") or []
        if reasons:
            return " ".join(str(reason).rstrip(".") + "." for reason in reasons[:2])
    return tx(
        locale,
        "radioisotopeMissingSummary",
        "No matching local secret-handling manifest was found for {name}. Package metadata is still published here so future coverage has a stable package URL.",
        name=page.display_name,
    )


def render_geiger(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    if not page.geiger:
        return ""
    reasons = "".join(f"<li>{html_escape(reason)}</li>" for reason in (page.geiger.get("reasons") or [])[:5])
    signals = "".join(f"<li>{html_escape(signal)}</li>" for signal in (page.geiger.get("signals") or [])[:5])
    return f"""
<div class="signal-grid" aria-label="{attr(tx(locale, 'geigerSignalsAria', 'Geiger classifier signals'))}">
  <article>
    <h3>{html_escape(tx(locale, 'riskClassifier', 'Risk classifier'))}</h3>
    <p><strong>{html_escape(geiger_level_label(page.geiger))}</strong> {html_escape(tx(locale, 'risk', 'risk'))} · {html_escape(geiger_confidence_label(page.geiger))} {html_escape(tx(locale, 'confidence', 'confidence'))} · {html_escape(page.geiger.get('category') or tx(locale, 'uncategorized', 'uncategorized'))}</p>
  </article>
  <article>
    <h3>{html_escape(tx(locale, 'why', 'Why'))}</h3>
    <ul>{reasons or f'<li>{html_escape(tx(locale, "noClassifierReasons", "No classifier reasons were present."))}</li>'}</ul>
  </article>
  <article>
    <h3>{html_escape(tx(locale, 'signals', 'Signals'))}</h3>
    <ul>{signals or f'<li>{html_escape(tx(locale, "noClassifierSignals", "No classifier signals were present."))}</li>'}</ul>
  </article>
</div>
"""


def render_install_behavior_signals(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    signals: list[str] = []
    behavior = page.install_behavior or {}
    lifecycle = behavior.get("lifecycleScripts") or []
    if lifecycle:
        signals.append(tx(locale, "signalNpmLifecycle", "npm lifecycle scripts are declared: {scripts}.", scripts=", ".join(str(item) for item in lifecycle[:5])))
    if behavior.get("postInstallDefined") is True:
        label = tx(locale, "signalNpmPostinstall", "npm package metadata declares a postinstall script.") if page.provider == "npm" else tx(locale, "signalHomebrewPostinstall", "Homebrew declares a post-install hook for this formula.")
        signals.append(label)
    elif behavior.get("postInstallDefined") is False:
        label = tx(locale, "signalNoNpmPostinstall", "No npm postinstall script is recorded in package metadata.") if page.provider == "npm" else tx(locale, "signalNoHomebrewPostinstall", "No Homebrew post-install hook is recorded in formula metadata.")
        signals.append(label)
    if behavior.get("prepareDefined") is True:
        signals.append(tx(locale, "signalNpmPrepare", "npm package metadata declares a prepare script."))
    if behavior.get("pythonRequires"):
        signals.append(tx(locale, "signalPythonRequires", "PyPI metadata requires Python {version}.", version=behavior.get("pythonRequires")))
    if behavior.get("requiresDistCount"):
        signals.append(tx(locale, "signalRequiresDistCount", "PyPI metadata lists {count} dependency specifications.", count=behavior.get("requiresDistCount")))
    if behavior.get("service"):
        signals.append(tx(locale, "signalService", "Formula metadata declares a service or daemon block."))
    if page.bottle:
        if page.bottle.get("available"):
            platforms = page.bottle.get("platforms") or []
            if platforms:
                signals.append(tx(locale, "signalBottlePlatforms", "Homebrew bottle metadata is available for {count} platform targets.", count=len(platforms)))
            else:
                signals.append(tx(locale, "signalBottleAvailable", "Homebrew bottle metadata is available."))
        else:
            signals.append(tx(locale, "signalNoBottle", "No Homebrew bottle metadata was recorded."))
    if page.dependencies:
        signals.append(tx(locale, "signalDependencies", "Installs with {count} runtime dependencies.", count=len(page.dependencies)))
    if page.build_dependencies:
        signals.append(tx(locale, "signalBuildDependencies", "Build metadata lists {count} build dependencies.", count=len(page.build_dependencies)))
    if not signals:
        return ""
    items = "".join(f"<li>{html_escape(signal)}</li>" for signal in signals[:6])
    return f"""
<div class="signal-grid install-signal-grid" aria-label="{attr(tx(locale, 'installBehaviorSignals', 'Install behavior signals'))}">
  <article>
    <h3>{html_escape(tx(locale, 'installBehaviorTitle', 'Install behavior'))}</h3>
    <ul>{items}</ul>
  </article>
</div>
"""


def render_readme_excerpt(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    if not page.isotope_readme_html:
        return ""
    source = f"<p class=\"readme-source\">{html_escape(tx(locale, 'source', 'Source'))}: <code>{html_escape(page.isotope_readme_source)}</code></p>" if page.isotope_readme_source else ""
    return f"""
<div class="readme-excerpt">
  <p class="readme-label">{html_escape(tx(locale, 'localReadmeExcerpt', 'Local README excerpt'))}</p>
  {public_copy(page.isotope_readme_html)}
  {source}
</div>
"""


def render_gate(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    gate = page.approval_gate
    if not gate:
        return ""
    rules = "".join(f"<li>{html_escape(rule)}</li>" for rule in gate.get("rules", []))
    severities = ", ".join(gate.get("severities") or []) or tx(locale, "notSpecified", "not specified")
    entrypoints = ", ".join(gate.get("entrypoints") or []) or page.display_name
    coverage = gate.get("coverage_status") or tx(locale, "unknown", "unknown")
    reviewed = gate.get("reviewed_at") or ""
    return f"""
<section class="pkg-section split-section gate-section">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'approvalGatesKicker', 'approval gates'))}</p>
    <h2>{html_escape(tx(locale, 'approvalGateHeading', 'Human review metadata for risky commands'))}</h2>
    <p>{html_escape(tx(locale, 'approvalGateCopy', 'The local approval-gate seed includes {count} rules for {name}. Covered entrypoints: {entrypoints}. Severity labels: {severities}. Coverage: {coverage}{reviewed}.', count=gate.get('rule_count'), name=page.display_name, entrypoints=entrypoints, severities=severities, coverage=coverage, reviewed=(f', {tx(locale, "reviewed", "reviewed")} {reviewed}' if reviewed else '')))}</p>
  </div>
  <div class="detail-stack">
    <article>
      <h3>{html_escape(tx(locale, 'exampleGatedActions', 'Example gated actions'))}</h3>
      <ul>{rules or f'<li>{html_escape(tx(locale, "noApprovalRules", "No rule descriptions were present."))}</li>'}</ul>
    </article>
  </div>
</section>
"""


def render_executables(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    executable_rows: list[str] = []
    seen: set[str] = set()
    for item in page.executables:
        name = str(item.get("name") or item.get("target") or item.get("source") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        executable_rows.append(executable_row(name, item.get("kind") or tx(locale, "executable", "executable"), item.get("exposure") or tx(locale, "globalExecutable", "global executable"), item.get("note") or ""))
    for item in page.binaries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("target") or item.get("source") or "").strip()
        if name and name not in seen:
            seen.add(name)
            executable_rows.append(executable_row(name, tx(locale, "binary", "binary"), tx(locale, "homebrewCaskBinary", "Homebrew cask binary"), str(item.get("source") or "")))
    for alias in sorted(page.aliases):
        if alias not in seen:
            seen.add(alias)
            exposure = tx(locale, "stubExcluded", "Automic Vault stub excluded") if alias in page.extra.get("stub_exclusions", []) else tx(locale, "indexedExecutable", "indexed executable")
            executable_rows.append(executable_row(alias, tx(locale, "executable", "executable"), exposure, tx(locale, "localExecutableIndex", "Discovered from the local executable index.")))
    body = "".join(executable_rows)
    return f"""
<section class="pkg-section" aria-labelledby="executables-title">
  <p class="section-kicker">{html_escape(tx(locale, 'executables', 'executables'))}</p>
  <h2 id="executables-title">{html_escape(tx(locale, 'executablesTitle', 'Installed executables'))}</h2>
  <div class="table-wrap executable-table">
    <table>
      <thead><tr><th>{html_escape(tx(locale, 'command', 'Command'))}</th><th>{html_escape(tx(locale, 'kind', 'Kind'))}</th><th>{html_escape(tx(locale, 'exposure', 'Exposure'))}</th><th>{html_escape(tx(locale, 'note', 'Note'))}</th></tr></thead>
      <tbody>{body or f'<tr><td colspan="4">{html_escape(tx(locale, "executableDataMissing", "No executable data was present."))}</td></tr>'}</tbody>
    </table>
  </div>
</section>
"""


def freshness_item(item: dict[str, Any], locale: dict[str, Any] | None = None) -> str:
    severity = str(item.get("severity") or "info")
    kind = str(item.get("kind") or "freshness")
    message = str(item.get("message") or "").strip()
    evidence = str(item.get("evidence") or "").strip()
    confidence = str(item.get("confidence") or "").strip()
    evidence_html = link_value(evidence) if evidence else ""
    return f"""
<li class="freshness-item freshness-{attr(severity)}">
  <strong>{html_escape(severity)}</strong>
  <span>{html_escape(message or kind)}</span>
  {f'<small>{evidence_html}</small>' if evidence_html else ''}
  {f'<em>{html_escape(confidence)} {html_escape(tx(locale, "confidence", "confidence"))}</em>' if confidence else ''}
</li>
"""


def render_freshness(page: PackagePage, manifest: dict[str, Any], locale: dict[str, Any] | None = None) -> str:
    freshness = page.version_freshness or {}
    manager = freshness.get("packageManager") if isinstance(freshness.get("packageManager"), dict) else {}
    site = freshness.get("siteData") if isinstance(freshness.get("siteData"), dict) else {}
    upstream = freshness.get("upstream") if isinstance(freshness.get("upstream"), dict) else {}
    warnings = freshness.get("warnings") if isinstance(freshness.get("warnings"), list) else []
    version = manager.get("version") or page.version or tx(locale, "unknown", "unknown")
    manager_updated = fmt_date(str(manager.get("updatedAt") or page.last_updated_at or ""))
    site_status = site.get("status") or tx(locale, "unknown", "unknown")
    upstream_comparison = upstream.get("comparison") or tx(locale, "notAvailable", "not available")
    upstream_latest = upstream.get("latestVersion") or tx(locale, "notDetected", "not detected")
    repository = upstream.get("repository") or ""
    warning_items = "".join(freshness_item(item, locale) for item in warnings if isinstance(item, dict))
    if not warning_items:
        warning_items = f'<li class="freshness-item freshness-info"><strong>ok</strong><span>{html_escape(tx(locale, "noFreshnessWarnings", "No freshness warnings were generated."))}</span></li>'
    return f"""
<section class="pkg-section split-section freshness-section" aria-labelledby="freshness-title" data-pagefind-ignore="all">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'freshness', 'freshness'))}</p>
    <h2 id="freshness-title">{html_escape(tx(locale, 'freshnessTitle', 'Version and freshness'))}</h2>
    <p>{html_escape(tx(locale, 'freshnessCopy', 'These signals separate page generation age, package-manager activity, and upstream release comparison. Version lag is warned only when an evidence URL and comparable versions are present.'))}</p>
  </div>
  <div>
    <div class="freshness-metrics">
      <div><span>{html_escape(tx(locale, 'pageGenerated', 'page generated'))}</span><strong>{html_escape(fmt_date(manifest.get("generated_at", "")) or tx(locale, "unknown", "unknown"))}</strong></div>
      <div><span>{html_escape(tx(locale, 'managerVersion', 'manager version'))}</span><strong>{html_escape(version)}</strong></div>
      <div><span>{html_escape(tx(locale, 'managerUpdated', 'manager updated'))}</span><strong>{html_escape(manager_updated or tx(locale, "unknown", "unknown"))}</strong></div>
      <div><span>{html_escape(tx(locale, 'localData', 'local data'))}</span><strong>{html_escape(site_status)}</strong></div>
      <div><span>{html_escape(tx(locale, 'upstream', 'upstream'))}</span><strong>{html_escape(upstream_comparison)}</strong></div>
      <div><span>{html_escape(tx(locale, 'upstreamLatestDetected', 'latest detected'))}</span><strong>{html_escape(upstream_latest)}</strong></div>
    </div>
    {f'<p class="freshness-repo"><a href="{attr(repository)}">{html_escape(repository)}</a></p>' if repository else ''}
    <ul class="freshness-list">{warning_items}</ul>
  </div>
</section>
"""


def executable_row(name: str, kind: Any, exposure: Any, note: Any) -> str:
    return f"<tr><td><code>{html_escape(name)}</code></td><td>{html_escape(kind)}</td><td>{html_escape(exposure)}</td><td>{html_escape(note)}</td></tr>"


def render_install_metadata(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    rows: list[tuple[str, str]] = []
    for label, value in (
        (tx(locale, "packageKey", "Package key"), page.key),
        (tx(locale, "version", "Version"), page.version),
        (tx(locale, "packageManager", "Package manager"), package_manager_label(page)),
        (tx(locale, "packageManagerPage", "Package manager page"), page.package_manager_url),
        (tx(locale, "homepage", "Homepage"), page.homepage),
        (tx(locale, "repository", "Repository"), page.repository),
        (tx(locale, "upstreamDocs", "Upstream docs"), page.upstream_docs),
        (tx(locale, "license", "License"), page.license),
        (tx(locale, "sourceArchive", "Source archive"), page.source_archive),
        (tx(locale, "issueTracker", "Issue tracker"), page.issue_tracker),
        (tx(locale, "updated", "Last updated"), page.last_updated_at),
        (tx(locale, "verified", "Last verified"), page.last_verified),
        (tx(locale, "published", "Published"), page.published_at),
        ("Pulse", page.pulse_kind),
        ("SHA-256", page.sha256),
        (tx(locale, "downloadUrl", "Download URL"), page.url),
    ):
        if value:
            rows.append((label, value))
    if page.binaries:
        rows.append((tx(locale, "binaries", "Binaries"), ", ".join(sorted({item.get("target") or item.get("source") or "" for item in page.binaries if isinstance(item, dict)}))))
    if page.dependencies:
        rows.append((tx(locale, "dependencies", "Dependencies"), ", ".join(page.dependencies)))
    if page.build_dependencies:
        rows.append((tx(locale, "buildDependencies", "Build dependencies"), ", ".join(page.build_dependencies)))
    if page.uses_from_macos:
        rows.append((tx(locale, "usesFromMacos", "Uses from macOS"), ", ".join(page.uses_from_macos)))
    if page.bottle:
        bottle = tx(locale, "available", "available") if page.bottle.get("available") else tx(locale, "notRecorded", "not recorded")
        platforms = ", ".join(page.bottle.get("platforms") or [])
        rows.append((tx(locale, "bottle", "Bottle"), f"{bottle}{f' ({platforms})' if platforms else ''}"))
    if page.install_behavior:
        post_install = page.install_behavior.get("postInstallDefined")
        if post_install is not None:
            label = tx(locale, "npmPostinstall", "npm postinstall") if page.provider == "npm" else tx(locale, "homebrewPostinstall", "Homebrew post-install")
            rows.append((label, tx(locale, "defined", "defined") if post_install else tx(locale, "notDefined", "not defined")))
        service = page.install_behavior.get("service")
        rows.append((tx(locale, "service", "Service"), service if service else tx(locale, "serviceNone", "none declared")))
        caveats = page.install_behavior.get("caveats")
        if caveats:
            rows.append((tx(locale, "caveats", "Caveats"), caveats))
        lifecycle = page.install_behavior.get("lifecycleScripts")
        if lifecycle:
            rows.append((tx(locale, "lifecycleScripts", "npm lifecycle scripts"), ", ".join(str(item) for item in lifecycle)))
        python_requires = page.install_behavior.get("pythonRequires")
        if python_requires:
            rows.append((tx(locale, "pythonRequires", "Python requires"), str(python_requires)))
        requires_dist_count = page.install_behavior.get("requiresDistCount")
        if requires_dist_count:
            rows.append((tx(locale, "pypiDependencySpecs", "PyPI dependency specs"), fmt_int(requires_dist_count)))
    if page.keywords:
        rows.append((tx(locale, "keywords", "Keywords"), ", ".join(page.keywords[:16])))
    if page.classifiers:
        rows.append((tx(locale, "classifiers", "Classifiers"), ", ".join(page.classifiers[:12])))
    deps = page.extra.get("homebrewDeps") or page.extra.get("npm_homebrewDeps")
    if deps:
        rows.append((tx(locale, "homebrewDependencies", "Homebrew dependencies"), ", ".join(deps)))
    python_formula = page.extra.get("pythonFormula")
    if python_formula:
        rows.append((tx(locale, "pythonFormula", "Python formula"), python_formula))
    row_html = "".join(f"<tr><th>{html_escape(label)}</th><td>{link_value(value)}</td></tr>" for label, value in rows)
    return f"""
<section class="pkg-section">
  <p class="section-kicker">{html_escape(tx(locale, 'packageMetadataKicker', 'install metadata'))}</p>
  <h2>{html_escape(tx(locale, 'metadataTitle', 'Package metadata'))}</h2>
  <div class="table-wrap">
    <table>
      <tbody>{row_html or f'<tr><th>{html_escape(tx(locale, "status", "Status"))}</th><td>{html_escape(tx(locale, "metadataEmpty", "No resolver details were present."))}</td></tr>'}</tbody>
    </table>
  </div>
</section>
"""


def render_registry_insights(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    insights = page.extra.get("registryInsights") if isinstance(page.extra.get("registryInsights"), dict) else {}
    if not insights:
        return ""
    rows = registry_insight_rows(insights)
    if not rows:
        return ""
    row_html = "".join(
        f"<tr><th>{html_escape(label)}</th><td>{metadata_value_html(value)}</td></tr>"
        for label, value in rows[:28]
    )
    return f"""
<section class="pkg-section registry-insights-section">
  <p class="section-kicker">{html_escape(tx(locale, 'registryFacts', 'registry facts'))}</p>
  <h2>{html_escape(tx(locale, 'registryInsightsTitle', 'Source database details'))}</h2>
  <div class="table-wrap registry-insights-table">
    <table>
      <tbody>{row_html}</tbody>
    </table>
  </div>
</section>
"""


def registry_insight_rows(insights: dict[str, Any]) -> list[tuple[str, Any]]:
    preferred = [
        "sourceDatabase",
        "tap",
        "fullName",
        "fullToken",
        "names",
        "aliases",
        "oldName",
        "oldTokens",
        "versionScheme",
        "revision",
        "headVersion",
        "distTags",
        "versionCount",
        "releaseCount",
        "filesForLatest",
        "packageTypes",
        "maintainers",
        "author",
        "maintainer",
        "publisher",
        "engines",
        "requiresPython",
        "peerDependencies",
        "optionalDependencies",
        "dependsOn",
        "conflictsWith",
        "requirements",
        "artifacts",
        "autoUpdates",
        "funding",
        "integrity",
        "shasum",
        "unpackedSize",
        "fileCount",
        "latestSerial",
        "latestUploadAt",
        "vulnerabilityCount",
        "yankedFileCount",
    ]
    rows = []
    seen = set()
    for key in preferred:
        value = insights.get(key)
        if value in ("", [], {}, None):
            continue
        seen.add(key)
        rows.append((human_metadata_label(key), value))
    for key in sorted(insights):
        if key in seen:
            continue
        value = insights.get(key)
        if value not in ("", [], {}, None):
            rows.append((human_metadata_label(key), value))
    return rows


def human_metadata_label(key: str) -> str:
    text = re.sub(r"(?<!^)([A-Z])", r" \1", key).replace("_", " ").replace("-", " ")
    text = text.replace("Url", "URL").replace("Sha", "SHA")
    return text[:1].upper() + text[1:]


def metadata_value_html(value: Any) -> str:
    if isinstance(value, bool):
        return html_escape("yes" if value else "no")
    if isinstance(value, (int, float)):
        return html_escape(fmt_int(value) if isinstance(value, int) else str(value))
    if isinstance(value, list):
        items = [str(item) for item in value if str(item or "").strip()]
        if not items:
            return ""
        return '<ul class="chip-list compact-chip-list">' + "".join(f"<li>{html_escape(item)}</li>" for item in items[:32]) + "</ul>"
    if isinstance(value, dict):
        items = [(str(key), child) for key, child in value.items() if child not in ("", [], {}, None)]
        if not items:
            return ""
        return '<dl class="metadata-pair-list">' + "".join(
            f"<div><dt>{html_escape(key)}</dt><dd>{metadata_value_html(child)}</dd></div>"
            for key, child in items[:24]
        ) + "</dl>"
    return link_value(str(value))


def render_external_package_manager_matches(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    matches = page.external_package_manager_matches[:16]
    if not matches:
        return ""
    match_html = "".join(external_package_match_card(item, locale) for item in matches)
    return f"""
<section class="pkg-section split-section manager-match-section" aria-labelledby="manager-match-title">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'sourceDatabaseMatches', 'source database matches'))}</p>
    <h2 id="manager-match-title">{html_escape(tx(locale, 'otherPackageManagersTitle', 'Other package-manager records'))}</h2>
    <p>{html_escape(tx(locale, 'otherPackageManagersCopy', 'Matches are pulled from external package-manager indexes and kept separate from local Automic Vault package links.'))}</p>
  </div>
  <div class="manager-match-grid">
    {match_html}
  </div>
</section>
"""


def external_package_match_card(item: dict[str, Any], locale: dict[str, Any] | None = None) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    version = metadata.get("version") or ""
    summary = metadata.get("summary") or metadata.get("description") or ""
    homepage = metadata.get("homepage") or ""
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    source_url = str(source.get("sourceUrl") or "").strip()
    source_label = str(source.get("sourceLabel") or "").strip()
    fact_bits = []
    for key in ("license", "section", "category", "architecture", "sourcePackage"):
        value = metadata.get(key)
        if value:
            fact_bits.append(f"{human_metadata_label(key)}: {value}")
    for key, label in (("dependencies", "dependencies"), ("provides", "provides"), ("optionalDependencies", "optional deps")):
        values = metadata.get(key)
        if isinstance(values, list) and values:
            fact_bits.append(f"{len(values)} {label}")
    fact_html = "".join(f"<li>{html_escape(bit)}</li>" for bit in fact_bits[:6])
    source_link = f'<a href="{attr(source_url)}">{html_escape(source_host_label(source_url))}</a>' if source_url else html_escape(source_label)
    command = str(item.get("command") or "").strip()
    return f"""
<article class="manager-match-card">
  <div class="manager-match-head">
    <strong>{html_escape(item.get('displayName') or item.get('manager') or '')}</strong>
    <span>{html_escape(str(item.get('confidence') or ''))}</span>
  </div>
  <p><code>{html_escape(item.get('packageId') or '')}</code>{f' <em>{html_escape(version)}</em>' if version else ''}</p>
  {f'<p>{html_escape(short_text(summary, 180))}</p>' if summary else ''}
  {f'<p><a href="{attr(homepage)}">{html_escape(homepage)}</a></p>' if homepage else ''}
  {f'<pre><code>{html_escape(command)}</code></pre>' if command else ''}
  <ul>{fact_html or f'<li>{html_escape(item.get("reason") or "")}</li>'}</ul>
  <small>{html_escape(source_label)}{f' · {source_link}' if source_url else ''}</small>
</article>
"""


def render_related(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    workflow_rels = {"adjacent_workflow", "format_peer", "language_runtime_peer", "command_surface_peer", "security_surface_peer", "domain_peer"}
    related_items = filtered_related_items(page, page.related_packages, exclude_rels=workflow_rels, limit=8)
    workflow_items = filtered_related_items(page, page.related_packages, include_rels=workflow_rels, limit=6)
    also_items = filtered_related_items(page, page.also_available_via, limit=4)
    if not related_items and not workflow_items and not also_items:
        related_items = inferred_related_items(page, limit=8)
    hubs = [hub_link(item, locale) for item in page.package_hubs[:4] if isinstance(item, dict) and item.get("slug")]
    guides = core_security_guide_links(page, locale)
    articles = [
        related_article(tx(locale, "topicalHubs", "Topical hubs"), hubs),
        related_article(tx(locale, "relatedTools", "Related tools"), [related_link(item, locale) for item in related_items]),
        related_article(tx(locale, "sameWorkflow", "Same workflow"), [related_link(item, locale) for item in [*workflow_items, *also_items][:6]]),
        related_article(tx(locale, "agentSecurityGuides", "Agent security guides"), guides),
    ]
    article_html = "".join(article for article in articles if article)
    return f"""
<section class="pkg-section split-section related-section" aria-labelledby="related-title">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'packageGraph', 'package graph'))}</p>
    <h2 id="related-title">{html_escape(tx(locale, 'internalLinks', 'Internal package links'))}</h2>
    <p>{html_escape(tx(locale, 'packageGraphCopy', 'Links come from deterministic package relationships, pkgdb category and tag curation, ecosystem matches, and package hub membership.'))}</p>
  </div>
  <div class="related-columns">
    {article_html}
  </div>
</section>
"""


def related_article(title: str, items: list[str]) -> str:
    content = "".join(item for item in items if item)
    if not content:
        return ""
    return f"<article><h3>{html_escape(title)}</h3><ul>{content}</ul></article>"


def filtered_related_items(
    page: PackagePage,
    items: list[dict[str, Any]],
    *,
    include_rels: set[str] | None = None,
    exclude_rels: set[str] | None = None,
    limit: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        name = str(item.get("name") or "").strip()
        rel = str(item.get("rel") or "").strip()
        if not provider or not name or f"{provider}:{name}" == page.key:
            continue
        if include_rels is not None and rel not in include_rels:
            continue
        if exclude_rels is not None and rel in exclude_rels:
            continue
        key = (provider, name)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def inferred_related_items(page: PackagePage, limit: int) -> list[dict[str, Any]]:
    if page.provider != "brew":
        return []
    return [
        {
            "provider": page.provider,
            "name": dependency,
            "label": dependency,
            "reason": f"{package_manager_label(page)} dependency.",
        }
        for dependency in page.dependencies[:limit]
    ]


def core_security_guide_links(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    links = [
        (
            locale_path("/", locale) + "#threats",
            tx(locale, "aiAgentSecretScanner", "AI agent secret scanner"),
            tx(locale, "aiAgentSecretScannerCopy", "Find plaintext credentials before an agent run starts."),
        ),
        (
            locale_path("/", locale) + "#controls",
            tx(locale, "aiAgentApprovalGates", "AI agent approval gates"),
            tx(locale, "aiAgentApprovalGatesCopy", "Put approvals in front of risky package and tool actions."),
        ),
        (
            "/docs/",
            tx(locale, "secretInjectionDocs", "Secret injection docs"),
            tx(locale, "secretInjectionDocsCopy", "Move supported secrets out of plaintext files and inject them into approved tools."),
        ),
    ]
    provider = page.provider.lower()
    haystack = " ".join([page.name, page.display_name, page.summary, " ".join(page.aliases), " ".join(page.keywords)]).lower()
    if provider == "brew" or "homebrew" in haystack:
        links.append((
            "/Automic%20Vault.dmg",
            tx(locale, "secureHomebrewTools", "Secure Homebrew tools"),
            tx(locale, "secureHomebrewToolsCopy", "Install Vault and scan the tools your Mac already uses."),
        ))
    if "aws" in haystack or "cloud" in haystack:
        links.append((
            locale_path("/", locale),
            tx(locale, "secureAwsCliCredentials", "Secure AWS CLI credentials"),
            tx(locale, "secureAwsCliCredentialsCopy", "Keep cloud keys out of ambient config files."),
        ))
    if "github" in haystack or "gh" in page.aliases:
        links.append((
            locale_path("/", locale),
            tx(locale, "githubCliTokenSecurity", "GitHub CLI token security"),
            tx(locale, "githubCliTokenSecurityCopy", "Protect source and release tokens used by local tools."),
        ))
    return [
        f'<li><a href="{attr(url)}">{html_escape(label)}</a><span>{html_escape(copy)}</span></li>'
        for url, label, copy in links[:5]
    ]


def inferred_related_links(page: PackagePage, locale: dict[str, Any] | None = None) -> list[str]:
    links: list[str] = []
    if page.provider != "brew":
        return links
    for dependency in page.dependencies[:6]:
        links.append(related_link({
            "provider": page.provider,
            "name": dependency,
            "label": dependency,
            "reason": f"{package_manager_label(page)} dependency.",
        }, locale))
    return links


def has_internal_package_navigation(page: PackagePage) -> bool:
    return has_strong_internal_package_navigation(page)


def related_link(item: dict[str, Any], locale: dict[str, Any] | None = None) -> str:
    provider = str(item.get("provider") or "").strip()
    name = str(item.get("name") or "").strip()
    label = str(item.get("label") or name).strip()
    reason = str(item.get("reason") or "").strip()
    if not provider or not name:
        return ""
    href = f"../../{attr(provider)}/{attr(slugify(name))}/"
    return f'<li><a href="{href}">{html_escape(label)}</a>{f"<span>{html_escape(reason)}</span>" if reason else ""}</li>'


def hub_link(item: dict[str, Any], locale: dict[str, Any] | None = None) -> str:
    slug = str(item.get("slug") or "").strip()
    label = human_hub_link_label(str(item.get("label") or slug).strip(), slug, locale)
    reason = str(item.get("reason") or "").strip()
    if not slug:
        return ""
    return f'<li><a href="../../{attr(slug)}/">{html_escape(label)}</a>{f"<span>{html_escape(reason)}</span>" if reason else ""}</li>'


def human_hub_link_label(label: str, slug: str, locale: dict[str, Any] | None = None) -> str:
    slug = slug.strip().lower()
    overrides = {
        "networking-protocol-tools": tx(locale, "moreNetworkingTools", "More networking tools"),
        "source-control-tools": tx(locale, "moreSourceControlTools", "More source control tools"),
        "package-publishers": tx(locale, "morePackagePublishingTools", "More package publishing tools"),
        "cloud-clis": tx(locale, "moreCloudCliTools", "More cloud CLI tools"),
        "security-crypto-tools": tx(locale, "moreSecurityTools", "More security tools"),
        "developer-build-tools": tx(locale, "moreBuildTools", "More build tools"),
        "terminal-utilities": tx(locale, "moreTerminalTools", "More terminal tools"),
    }
    if slug in overrides:
        return overrides[slug]
    text = normalize_space(label)
    text = re.sub(r"\bpackages\b", "tools", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpackage\b", "tool", text, flags=re.IGNORECASE)
    if text.lower().startswith("more "):
        return text
    return tx(locale, "moreToolsLikeThis", "More {label}", label=text[:1].lower() + text[1:] if text else "tools like this")


def link_value(value: str) -> str:
    if value.startswith("https://") or value.startswith("http://"):
        return f'<a href="{attr(value)}">{html_escape(value)}</a>'
    return html_escape(value)


def render_sources(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    notes = sorted(set(page.source_notes)) or [tx(locale, "localPackageGenerator", "local package generator")]
    note_html = "".join(f"<li>{html_escape(public_copy(note))}</li>" for note in notes)
    source_file_html = ""
    if page.combined_yaml_url:
        source_file_html = f"""
    <article>
      <h3>{html_escape(tx(locale, 'combinedYamlSource', 'Combined YAML source'))}</h3>
      <p>{html_escape(tx(locale, 'combinedYamlSourceCopy', 'View the package source record on GitHub.'))}</p>
      <p><a href="{attr(page.combined_yaml_url)}"><code>{html_escape(page.combined_yaml_path or page.combined_yaml_url)}</code></a></p>
    </article>"""
    return f"""
<section class="pkg-section split-section sources-section">
  <div>
    <p class="section-kicker">{html_escape(tx(locale, 'sourceTrail', 'source trail'))}</p>
    <h2>{html_escape(tx(locale, 'generatedFromRepositoryData', 'Generated from repository data'))}</h2>
    <p>{tx(locale, 'sourcesCopy', 'This page is served by <code>av-web</code> from the private package SQLite artifact generated by <code>scripts/generate-pkg-sqlite.py</code>.')}</p>
  </div>
  <div class="detail-stack">
{source_file_html}
    <article>
      <h3>{html_escape(tx(locale, 'usedSources', 'Used sources'))}</h3>
      <ul>{note_html}</ul>
    </article>
  </div>
</section>
"""


def command_for_manager(page: PackagePage, manager_keys: set[str]) -> dict[str, Any] | None:
    for item in source_backed_schema_commands(page):
        if item.get("kind") == "automic_vault":
            continue
        key = manager_key_for_item(item)
        label = title_manager_label(item).lower()
        if key in manager_keys or label in manager_keys:
            return item
    return None


def faq_entries(page: PackagePage, locale: dict[str, Any] | None = None) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []

    def add(question: str, answer: str) -> None:
        question_text = normalize_space(question)
        answer_text = paragraph_text(answer, 360)
        if not question_text or not answer_text:
            return
        if any(item["question"].lower() == question_text.lower() for item in entries):
            return
        entries.append({"question": question_text, "answer": answer_text})

    add(
        tx(locale, "faqWhatIs", "What is {name}?", name=page.display_name),
        plain_package_summary(page, locale),
    )
    primary = next((item for item in source_backed_schema_commands(page) if item.get("kind") != "automic_vault"), None)
    if primary:
        add(
            tx(locale, "faqInstallWithManager", "How do I install {name} with {manager}?", name=page.display_name, manager=title_manager_label(primary)),
            tx(
                locale,
                "faqInstallAnswer",
                "Run {command}. This command comes from {manager} package metadata on this page.",
                command=str(primary.get("command") or ""),
                manager=title_manager_label(primary),
            ),
        )
    homebrew = command_for_manager(page, {"homebrew", "homebrew cask"})
    if homebrew and homebrew is not primary:
        add(
            tx(locale, "faqInstallHomebrew", "How do I install {name} with Homebrew?", name=page.display_name),
            tx(locale, "faqInstallAnswer", "Run {command}. This command comes from {manager} package metadata on this page.", command=str(homebrew.get("command") or ""), manager=title_manager_label(homebrew)),
        )
    apt = command_for_manager(page, {"apt", "debian apt", "ubuntu apt"})
    if apt:
        add(
            tx(locale, "faqInstallUbuntu", "How do I install {name} on Ubuntu?", name=page.display_name),
            tx(locale, "faqInstallAnswer", "Run {command}. This command comes from {manager} package metadata on this page.", command=str(apt.get("command") or ""), manager=title_manager_label(apt)),
        )
    executables: list[str] = []
    seen_executables: set[str] = set()
    for item in executable_markdown_items(page, locale):
        name = item.split(" (", 1)[0]
        if not name or name.lower() in seen_executables:
            continue
        seen_executables.add(name.lower())
        executables.append(name)
    if executables:
        shown = ", ".join(executables[:6])
        add(
            tx(locale, "faqExecutables", "Which executable does {name} install?", name=page.display_name),
            tx(locale, "faqExecutablesAnswer", "{name} installs these executables in the local package metadata: {executables}.", name=page.display_name, executables=shown),
        )
    add(
        tx(locale, "faqAgentSafety", "Is {name} safe for AI agents to use?", name=page.display_name),
        tx(
            locale,
            "faqAgentSafetyAnswer",
            "The current agent risk level is {level} with {confidence} confidence. Review the listed human review items before unattended execution.",
            level=agent_risk_level(page, locale),
            confidence=agent_risk_confidence(page, locale),
        ),
    )
    return entries[:6]


def render_faq(page: PackagePage, locale: dict[str, Any] | None = None) -> str:
    entries = faq_entries(page, locale)
    if not entries:
        return ""
    items = "".join(
        f"<article><h3>{html_escape(item['question'])}</h3><p>{html_escape(item['answer'])}</p></article>"
        for item in entries
    )
    return f"""
<section class="pkg-section faq-section" aria-labelledby="faq-title">
  <p class="section-kicker">{html_escape(tx(locale, 'schemaFaqName', 'Package FAQ'))}</p>
  <h2 id="faq-title">{html_escape(tx(locale, 'schemaFaqName', 'Package FAQ'))}</h2>
  <div class="faq-grid">{items}</div>
</section>
"""


def schema_for_package(page: PackagePage, description: str, updated: str, locale: dict[str, Any] | None = None) -> dict[str, Any]:
    url = locale_url(page.path, locale)
    software: dict[str, Any] = {
        "@type": "SoftwareApplication",
        "@id": f"{url}#software",
        "name": page.display_name,
        "applicationCategory": "DeveloperApplication",
        "operatingSystem": "macOS",
        "url": url,
        "description": description,
        "dateModified": updated,
        "inLanguage": (locale or {}).get("htmlLang") or "en",
        "isPartOf": {"@id": f"{SITE_ORIGIN}/#website"},
    }
    if page.homepage:
        software["sameAs"] = page.homepage
    if page.version:
        software["softwareVersion"] = page.version
    if page.license:
        software["license"] = page.license
    if page.repository:
        software["codeRepository"] = page.repository
    if page.dependencies:
        software["softwareRequirements"] = ", ".join(page.dependencies[:16])

    article = {
        "@type": "TechArticle",
        "@id": f"{url}#article",
        "headline": tx(locale, "schemaTechArticleHeadline", "Install {name} with {manager}", name=page.display_name, manager=package_manager_label(page)),
        "description": description,
        "dateModified": updated,
        "inLanguage": (locale or {}).get("htmlLang") or "en",
        "author": {"@id": "https://github.com/mxcl#person"},
        "reviewedBy": {"@id": "https://github.com/mxcl#person"},
        "publisher": {"@id": f"{SITE_ORIGIN}/#organization"},
        "mainEntity": {"@id": f"{url}#software"},
    }
    breadcrumb = {
        "@type": "BreadcrumbList",
        "@id": f"{url}#breadcrumbs",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": tx(locale, "home", "Home"), "item": locale_url("/", locale)},
            {"@type": "ListItem", "position": 2, "name": tx(locale, "packages", "Packages"), "item": locale_url("/pkg/", locale)},
            {"@type": "ListItem", "position": 3, "name": page.display_name, "item": url},
        ],
    }
    how_to = {
        "@type": "HowTo",
        "@id": f"{url}#install-howto",
        "name": tx(locale, "schemaHowToName", "Install {name}", name=page.display_name),
        "step": [
            {
                "@type": "HowToStep",
                "position": index + 1,
                "name": tx(locale, "schemaHowToStep", "Run {manager} command", manager=item.get("manager") or tx(locale, "install", "install")),
                "text": str(item.get("command") or ""),
            }
            for index, item in enumerate(source_backed_schema_commands(page)[:12])
            if str(item.get("command") or "").strip()
        ],
    }
    faq = {
        "@type": "FAQPage",
        "@id": f"{url}#faq",
        "name": tx(locale, "schemaFaqName", "Package FAQ"),
        "mainEntity": [
            {
                "@type": "Question",
                "name": item["question"],
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": item["answer"],
                },
            }
            for item in faq_entries(page, locale)
        ],
    }
    return {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "@id": f"{SITE_ORIGIN}/#website", "name": SITE_NAME, "url": f"{SITE_ORIGIN}/"},
            {"@type": "Organization", "@id": f"{SITE_ORIGIN}/#organization", "name": SITE_NAME, "url": f"{SITE_ORIGIN}/"},
            {"@type": "Person", "@id": "https://github.com/mxcl#person", "name": "Max Howell", "url": "https://github.com/mxcl"},
            software,
            article,
            breadcrumb,
            how_to,
            faq,
        ],
    }


def copy_script(locale: dict[str, Any] | None = None) -> str:
    copied = json.dumps(tx(locale, "copied", "Copied"), ensure_ascii=False)
    failed = json.dumps(tx(locale, "copyFailed", "Copy failed"), ensure_ascii=False)
    return f"""  <script>
    document.addEventListener("click", async (event) => {{
      const button = event.target.closest("[data-copy]");
      if (!button) return;
      try {{
        await navigator.clipboard.writeText(button.getAttribute("data-copy"));
        const previous = button.textContent;
        button.textContent = {copied};
        button.setAttribute("data-state", "copied");
        window.setTimeout(() => {{
          button.textContent = previous;
          button.removeAttribute("data-state");
        }}, 1600);
      }} catch (_error) {{
        button.textContent = {failed};
        button.setAttribute("data-state", "error");
      }}
    }});
  </script>"""


def render_sitemap_index(sitemap_names: list[str], manifest: dict[str, Any]) -> str:
    lastmod = fmt_date(manifest.get("generated_at", ""))
    entries = "\n".join(
        f"  <sitemap>\n    <loc>{SITE_ORIGIN}/pkg/{name}</loc>\n    <lastmod>{lastmod}</lastmod>\n  </sitemap>"
        for name in sitemap_names
    )
    return '<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + entries + "\n</sitemapindex>\n"


def render_hub_sitemap(hubs: list[tuple[PackageHub, list[PackagePage]]], manifest: dict[str, Any]) -> str:
    lastmod = fmt_date(manifest.get("generated_at", ""))
    urls = [sitemap_url(f"{SITE_ORIGIN}/", lastmod, "/")]
    urls.extend(sitemap_url(f"{SITE_ORIGIN}{hub.path}", lastmod, hub.path) for hub, _hub_pages in hubs)
    return render_urlset(urls)


def render_package_sitemap(pages: list[PackagePage], manifest: dict[str, Any]) -> str:
    lastmod = fmt_date(manifest.get("generated_at", ""))
    urls = [
        sitemap_url(f"{SITE_ORIGIN}{page.path}", fmt_date(page.last_updated_at) or lastmod, page.path)
        for page in pages
    ]
    return render_urlset(urls)


def sitemap_url(loc: str, lastmod: str, path: str | None = None) -> str:
    lines = ["  <url>", f"    <loc>{loc}</loc>", f"    <lastmod>{lastmod}</lastmod>"]
    if path:
        lines.extend(sitemap_hreflang_lines(path))
    lines.append("  </url>")
    return "\n".join(lines)


def render_urlset(urls: list[str]) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n' + "\n".join(urls) + "\n</urlset>\n"


def sitemap_hreflang_lines(path: str) -> list[str]:
    lines = [f'    <xhtml:link rel="alternate" hreflang="en" href="{SITE_ORIGIN}{path}" />']
    for locale in non_default_i18n_locales():
        lines.append(
            f'    <xhtml:link rel="alternate" hreflang="{locale.get("hreflang")}" href="{locale_url(path, locale)}" />'
        )
    lines.append(f'    <xhtml:link rel="alternate" hreflang="x-default" href="{SITE_ORIGIN}{path}" />')
    return lines


def nav(root: str, locale: dict[str, Any] | None = None) -> str:
    return f"""
<header class="masthead">
  <a class="brand" href="{root}" aria-label="pkg.so package catalog">
    <span class="brand-mark" aria-hidden="true">pkg</span>
    <span class="brand-lockup">
      <span class="brand-type">pkg.so</span>
      <span class="brand-tagline">open package index</span>
    </span>
  </a>
  <nav class="nav" aria-label="Main navigation">
    <a class="nav-primary" href="{root}#search">Search</a>
    <a href="/feed/">Feed</a>
    <a href="https://github.com/mxcl/pkg.so">GitHub <span aria-hidden="true">↗</span></a>
  </nav>
</header>
"""


def footer(root: str, locale: dict[str, Any] | None = None) -> str:
    return f"""
<footer class="site-footer">
  <p>{html_escape(tx(locale, 'footer', 'Source-backed package intelligence, rebuilt daily.'))}</p>
  <div class="footer-links">
    <a href="https://mxcl.dev">a mxcl project</a>
    <a href="{root}sitemap.xml">Sitemap</a>
    <a href="{root}robots.txt">Robots</a>
    <a href="/privacy/">Privacy</a>
    <a href="https://github.com/mxcl/pkg.so">Source</a>
  </div>
</footer>
"""


def html_hreflang_links(path: str) -> str:
    lines = [f'  <link rel="alternate" hreflang="en" href="{SITE_ORIGIN}{path}">']
    for locale in non_default_i18n_locales():
        lines.append(f'  <link rel="alternate" hreflang="{locale.get("hreflang")}" href="{locale_url(path, locale)}">')
    lines.append(f'  <link rel="alternate" hreflang="x-default" href="{SITE_ORIGIN}{path}">')
    return "\n".join(lines)


def replace_schema_url(value: Any, source_url: str, target_url: str) -> Any:
    if isinstance(value, str):
        return value.replace(source_url, target_url)
    if isinstance(value, list):
        return [replace_schema_url(item, source_url, target_url) for item in value]
    if isinstance(value, dict):
        return {key: replace_schema_url(item, source_url, target_url) for key, item in value.items()}
    return value


def html_doc(
    title: str,
    description: str,
    canonical: str,
    body: str,
    stylesheet_href: str,
    favicon_href: str,
    schema: dict[str, Any],
    robots: str = "index,follow",
    extra_head: str = "",
    extra_body: str = "",
    alternates_path: str | None = None,
    locale: dict[str, Any] | None = None,
) -> str:
    if alternates_path:
        schema = replace_schema_url(copy.deepcopy(schema), f"{SITE_ORIGIN}{alternates_path}", canonical)
    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)
    hreflang_head = html_hreflang_links(alternates_path) if alternates_path else ""
    html_lang = str((locale or {}).get("htmlLang") or "en")
    return f"""<!DOCTYPE html>
<html lang="{attr(html_lang)}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html_escape(title)}</title>
  <meta name="description" content="{attr(description)}">
  <meta name="robots" content="{attr(robots)}">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="{SITE_NAME}">
  <meta property="og:title" content="{attr(title)}">
  <meta property="og:description" content="{attr(description)}">
  <meta property="og:url" content="{attr(canonical)}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{attr(title)}">
  <meta name="twitter:description" content="{attr(description)}">
  <link rel="canonical" href="{attr(canonical)}">
{hreflang_head}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&amp;family=Space+Grotesk:wght@400;500;600;700&amp;display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{stylesheet_href}">
{GOOGLE_TAG}
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-5218328608281545" crossorigin="anonymous"></script>
{extra_head}
  <script type="application/ld+json">
{schema_json}
  </script>
</head>
<body>
  <div class="site-shell">
{textwrap.indent(body.strip(), '    ')}
  </div>
{extra_body}
</body>
</html>
"""


def render_css() -> str:
    return """:root {
  --bg: #e7e1d2;
  --surface: #f7f3e8;
  --surface-2: #eee9dc;
  --ink: #121416;
  --muted: #555b5d;
  --dim: #747878;
  --line: #c9c3b5;
  --line-strong: #17191b;
  --hot: #2455ff;
  --blue: #2455ff;
  --green: #4d7200;
  --gold: #879f00;
  --signal: #caff42;
  --font-ui: "Space Grotesk", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
  --max: 1540px;
  --shell-gutter-y: 44px;
  --shell-gutter-x: 48px;
}

* { box-sizing: border-box; }

html {
  background: var(--bg);
  color: var(--ink);
  scroll-behavior: smooth;
}

body {
  position: relative;
  margin: 0;
  min-height: 100vh;
  background:
    linear-gradient(90deg, rgba(36, 85, 255, 0.08) 0 1px, transparent 1px 100%),
    linear-gradient(180deg, rgba(18, 20, 22, 0.045) 0 1px, transparent 1px 100%),
    var(--bg);
  background-size: 96px 100%, 100% 32px, auto;
  font-family: var(--font-ui);
  letter-spacing: 0;
}

body::after {
  content: "";
  position: absolute;
  top: 0;
  left: 50%;
  z-index: 0;
  width: min(calc(100% - var(--shell-gutter-x)), var(--max));
  height: var(--shell-gutter-y);
  border-inline: 1px solid var(--line-strong);
  background: var(--surface);
  transform: translateX(-50%);
  pointer-events: none;
}

body::before {
  content: "";
  position: fixed;
  inset: 0;
  z-index: 50;
  pointer-events: none;
  background: repeating-linear-gradient(115deg, transparent 0 11px, rgba(18, 20, 22, 0.016) 11px 12px);
  opacity: 0.7;
}

a { color: inherit; text-decoration: none; }
a:focus-visible { outline: 1px solid var(--gold); outline-offset: 4px; }
h1, h2, h3, h4, p, ul, ol, pre { margin: 0; }
code { font-family: var(--font-mono); }

.site-shell {
  position: relative;
  z-index: 1;
  width: min(calc(100% - var(--shell-gutter-x)), var(--max));
  margin: var(--shell-gutter-y) auto;
  overflow: clip;
  border: 1px solid var(--line-strong);
  border-radius: 0;
  background: var(--surface);
  box-shadow:
    12px 12px 0 rgba(18, 20, 22, 0.12),
    0 34px 90px rgba(18, 20, 22, 0.12);
}

.masthead {
  position: sticky;
  top: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  min-height: 82px;
  padding: 18px clamp(20px, 3vw, 44px);
  border-bottom: 1px solid var(--line);
  background: rgba(247, 243, 232, 0.94);
  backdrop-filter: blur(16px);
}

.brand { display: inline-flex; align-items: center; gap: 10px; min-width: 0; }
.brand-mark {
  display: inline-grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid var(--ink);
  border-radius: 50%;
  background: var(--signal);
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 1rem;
  font-weight: 700;
}
.brand-type {
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 1.08rem;
  font-weight: 700;
  text-transform: uppercase;
}
.brand-tagline {
  padding-left: 10px;
  border-left: 1px solid var(--line);
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.66rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.nav {
  display: flex;
  align-items: center;
  gap: clamp(14px, 2.4vw, 32px);
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.79rem;
  font-weight: 600;
  text-transform: uppercase;
}
.nav a { padding: 8px 0; transition: color 160ms ease, transform 160ms ease; }
.nav a:hover { color: var(--ink); transform: translateY(-1px); }

.breadcrumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  padding: 22px clamp(20px, 3vw, 44px);
  border-bottom: 1px solid var(--line);
  background: var(--surface-2);
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.76rem;
  font-weight: 700;
  text-transform: uppercase;
}
.breadcrumbs a:hover { color: var(--ink); }

.pkg-hero {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(310px, 0.36fr);
  gap: clamp(34px, 6vw, 84px);
  align-items: end;
  padding: clamp(42px, 6vw, 88px) clamp(20px, 3vw, 44px) clamp(32px, 5vw, 68px);
  border-bottom: 1px solid var(--line);
  overflow: hidden;
  background:
    linear-gradient(135deg, rgba(36, 85, 255, 0.11), transparent 34%),
    linear-gradient(180deg, rgba(202, 255, 66, 0.12), transparent 68%);
}
.pkg-hero::before {
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    linear-gradient(90deg, rgba(36, 85, 255, 0.3), transparent 1px) 0 0 / 16.666% 100%,
    linear-gradient(rgba(18, 20, 22, 0.12), transparent 1px) 0 0 / 100% 72px;
  opacity: 0.24;
}
.pkg-hero::after {
  content: "";
  position: absolute;
  right: clamp(18px, 4vw, 68px);
  bottom: 0;
  width: min(38vw, 520px);
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--hot), var(--gold));
  opacity: 0.72;
}
.pkg-hero-index { align-items: center; }
.hero-copy, .hero-panel { position: relative; z-index: 1; min-width: 0; }
.eyebrow, .section-kicker {
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.79rem;
  font-weight: 700;
  line-height: 1.45;
  text-transform: uppercase;
}
h1 {
  max-width: 14ch;
  margin-top: 14px;
  color: var(--ink);
  font-size: clamp(3.05rem, 7vw, 6.9rem);
  font-weight: 800;
  line-height: 0.92;
  overflow-wrap: anywhere;
  text-transform: uppercase;
}
.lede {
  width: min(100%, 820px);
  margin-top: clamp(20px, 3vw, 34px);
  color: var(--ink);
  font-size: clamp(1.45rem, 2.4vw, 2.15rem);
  font-weight: 600;
  line-height: 1.12;
}
.summary-card {
  width: min(100%, 820px);
  margin-top: clamp(20px, 3vw, 34px);
  padding: 18px 20px;
  border: 1px solid rgba(36, 85, 255, 0.38);
  border-radius: 0;
  background: rgba(36, 85, 255, 0.055);
}
.summary-card p:last-child {
  margin-top: 8px;
  color: var(--ink);
  font-size: clamp(1.18rem, 1.9vw, 1.72rem);
  font-weight: 650;
  line-height: 1.22;
}
.hero-actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: clamp(28px, 4vw, 48px); }
.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 46px;
  padding: 12px 18px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 0.83rem;
  font-weight: 700;
  text-transform: uppercase;
  transition: border-color 160ms ease, background 160ms ease, color 160ms ease, transform 160ms ease;
}
.button:hover { transform: translateY(-1px); }
.button:active { transform: translateY(1px); }
.button.primary { border-color: var(--hot); background: var(--hot); color: #fff; }
.button.secondary { background: rgba(255, 255, 255, 0.035); }
.copy-button {
  min-height: 36px;
  padding: 8px 12px;
  border: 1px solid var(--line-strong);
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--ink);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: 0.74rem;
  font-weight: 700;
  text-transform: uppercase;
  transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1), border-color 180ms cubic-bezier(0.16, 1, 0.3, 1), background 180ms cubic-bezier(0.16, 1, 0.3, 1);
}
.copy-button:hover { border-color: var(--hot); }
.copy-button:active { transform: translateY(1px) scale(0.98); }
.copy-button[data-state="copied"] { border-color: rgba(114, 182, 97, 0.72); color: var(--green); }
.copy-button[data-state="error"] { border-color: rgba(242, 109, 61, 0.72); color: var(--hot); }
.hero-panel {
  display: grid;
  align-self: stretch;
  padding: 4px 18px;
  border: 1px solid var(--line-strong);
  border-radius: 0;
  background:
    linear-gradient(180deg, rgba(202, 255, 66, 0.22), transparent),
    var(--surface-2);
  box-shadow:
    6px 6px 0 rgba(18, 20, 22, 0.12);
}
.at-a-glance {
  display: grid;
  align-content: center;
}
.at-a-glance .section-kicker {
  padding: 14px 0 6px;
  border-bottom: 1px solid var(--line);
}
.metric {
  display: grid;
  grid-template-columns: minmax(0, 0.45fr) minmax(0, 1fr);
  gap: 14px;
  padding: 15px 0;
  border-bottom: 1px solid var(--line);
}
.metric:last-child { border-bottom: 0; }
.metric span {
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.74rem;
  font-weight: 700;
  text-transform: uppercase;
}
.metric strong {
  min-width: 0;
  color: var(--ink);
  font-size: 1rem;
  line-height: 1.2;
  overflow-wrap: anywhere;
}

.pkg-section {
  padding: clamp(30px, 4.5vw, 58px) clamp(20px, 3vw, 44px);
  border-bottom: 1px solid var(--line);
}
.pkg-section:nth-of-type(odd) {
  background: rgba(255, 255, 255, 0.008);
}
.split-section {
  display: grid;
  grid-template-columns: minmax(260px, 0.66fr) minmax(340px, 1.18fr);
  gap: clamp(30px, 5vw, 70px);
  align-items: start;
}
.security-section {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(300px, 0.42fr);
  gap: clamp(30px, 5vw, 70px);
  background: linear-gradient(90deg, rgba(36, 85, 255, 0.07), transparent 34%);
}
.support-section {
  display: grid;
  grid-template-columns: minmax(260px, 0.44fr) minmax(0, 1fr);
  gap: clamp(24px, 4vw, 54px);
  align-items: start;
  background:
    linear-gradient(90deg, rgba(45, 139, 216, 0.07), transparent 42%),
    rgba(255, 255, 255, 0.012);
}
.support-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 10px;
  list-style: none;
}
.support-list li {
  min-height: 76px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.032);
}
.support-list strong,
.support-list span {
  display: block;
}
.support-list strong {
  color: var(--ink);
  font-size: 0.96rem;
}
.support-list span {
  margin-top: 6px;
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.78rem;
  font-weight: 700;
}
.agent-risk-section .detail-stack article:first-child {
  border-color: rgba(242, 109, 61, 0.34);
}
.faq-section {
  background: rgba(255, 255, 255, 0.012);
}
.faq-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 14px;
  margin-top: 20px;
}
.faq-grid article {
  padding: 18px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.028);
}
.faq-grid h3 {
  margin-top: 0;
  font-size: 1rem;
}
.faq-grid p {
  margin-top: 10px;
  color: var(--dim);
}
.gate-section { background: rgba(45, 139, 216, 0.035); }
.sources-section { background: rgba(255, 255, 255, 0.018); }
.pkg-search-section {
  display: grid;
  grid-template-columns: minmax(240px, 0.38fr) minmax(0, 1fr);
  gap: clamp(22px, 4vw, 58px);
  align-items: center;
  padding-top: clamp(22px, 3.2vw, 38px);
  padding-bottom: clamp(22px, 3.2vw, 38px);
  background:
    linear-gradient(90deg, rgba(114, 182, 97, 0.052), transparent 44%),
    rgba(255, 255, 255, 0.012);
}
.search-copy p {
  max-width: 520px;
  margin-top: 10px;
  font-size: 0.96rem;
  line-height: 1.45;
}
.pkg-section h2 {
  max-width: 780px;
  margin-top: 8px;
  color: var(--ink);
  font-size: clamp(2rem, 3.4vw, 4rem);
  line-height: 0.95;
  text-transform: uppercase;
  overflow-wrap: anywhere;
}
.pkg-section p {
  max-width: 820px;
  margin-top: 18px;
  color: var(--muted);
  font-size: 1.05rem;
  line-height: 1.58;
}
.install-section {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 24px;
  align-items: start;
  background: rgba(255, 255, 255, 0.014);
}
.install-command-panel {
  display: grid;
  gap: 22px;
}
.platform-install-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.platform-install-grid article {
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--line-strong);
  background: var(--surface-2);
}
.platform-install-grid h3 {
  color: var(--ink);
  font-size: 0.92rem;
  line-height: 1.2;
  text-transform: uppercase;
}
.install-command-list {
  display: grid;
  gap: 12px;
  margin-top: 14px;
}
.install-command-row {
  display: block;
  min-width: 0;
  padding-top: 14px;
  border-top: 1px solid var(--line);
}
.install-command-row:first-child { border-top: 0; padding-top: 0; }
.install-command-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  margin-bottom: 6px;
}
.install-command-eyebrow {
  color: var(--ink);
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.04em;
  line-height: 1.1;
  text-transform: uppercase;
}
.install-command-head span {
  flex: 0 0 auto;
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.68rem;
  font-weight: 700;
  text-transform: uppercase;
}
.install-command-shell {
  position: relative;
  min-width: 0;
  min-height: 48px;
  padding: 13px 88px 13px 14px;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.026);
}
.install-command-shell code {
  display: block;
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 0.9rem;
  line-height: 1.45;
}
.install-command-shell .copy-button {
  position: absolute;
  top: 8px;
  right: 8px;
  min-height: 32px;
}
.install-command-source {
  margin: 8px 0 0;
  color: var(--dim);
  font-size: 0.82rem;
  line-height: 1.35;
}
.install-command-source a { color: var(--muted); }
.install-command-source a:hover { color: var(--ink); }
.signal-grid,
.related-columns {
  display: grid;
  align-items: start;
  gap: 12px;
}
.signal-grid article,
.related-columns article {
  min-width: 0;
  padding: 18px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.028), transparent),
    rgba(255, 255, 255, 0.018);
}
.signal-grid h3,
.related-columns h3 {
  color: var(--ink);
  font-size: 1.02rem;
  line-height: 1.2;
}
.signal-grid p,
.signal-grid ul,
.related-columns p,
.related-columns ul {
  margin-top: 10px;
  color: var(--muted);
  line-height: 1.5;
}
.signal-grid ul,
.related-columns ul {
  padding-left: 1.1rem;
}
.signal-grid li + li,
.related-columns li + li {
  margin-top: 8px;
}
.signal-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-top: 28px;
}
.signal-grid strong {
  color: var(--ink);
  font-family: var(--font-mono);
}
.related-columns {
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}
.related-section {
  grid-template-columns: minmax(260px, 0.35fr) minmax(0, 1fr);
}
.related-columns a {
  color: var(--ink);
  text-decoration: underline;
  text-decoration-color: var(--hot);
  text-underline-offset: 0.22em;
}
.related-columns span {
  display: block;
  margin-top: 4px;
  color: var(--muted);
  font-size: 0.92rem;
}
.registry-insights-section {
  background: rgba(255, 255, 255, 0.01);
}
.registry-insights-table td {
  min-width: 280px;
}
.compact-chip-list {
  margin-top: 0;
}
.compact-chip-list li {
  font-size: 0.68rem;
}
.metadata-pair-list {
  display: grid;
  gap: 7px;
  margin: 0;
}
.metadata-pair-list div {
  display: grid;
  grid-template-columns: minmax(92px, 0.35fr) minmax(0, 1fr);
  gap: 10px;
}
.metadata-pair-list dt {
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.7rem;
  font-weight: 800;
  text-transform: uppercase;
}
.metadata-pair-list dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}
.manager-match-section {
  grid-template-columns: minmax(260px, 0.36fr) minmax(0, 1fr);
  background: rgba(114, 182, 97, 0.026);
}
.manager-match-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
}
.manager-match-card {
  position: relative;
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background:
    linear-gradient(180deg, rgba(114, 182, 97, 0.042), transparent),
    rgba(255, 255, 255, 0.018);
  overflow: hidden;
}
.manager-match-card::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 2px;
  background: var(--green);
  opacity: 0.44;
}
.manager-match-head {
  position: relative;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.manager-match-head strong {
  color: var(--ink);
  font-size: 0.82rem;
  font-weight: 800;
  letter-spacing: 0.04em;
  line-height: 1.2;
  text-transform: uppercase;
}
.manager-match-head span {
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  font-weight: 800;
}
.manager-match-card p {
  margin-top: 10px;
  color: var(--muted);
  font-size: 0.9rem;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.manager-match-card p:first-of-type {
  color: var(--ink);
}
.manager-match-card code {
  font-family: var(--font-mono);
}
.manager-match-card pre {
  margin-top: 10px;
  padding: 10px;
  overflow-x: auto;
  border: 1px solid var(--line);
  background: rgba(0, 0, 0, 0.18);
}
.manager-match-card ul {
  margin-top: 10px;
  padding-left: 1rem;
  color: var(--muted);
  font-size: 0.86rem;
  line-height: 1.45;
}
.manager-match-card small {
  display: block;
  margin-top: 10px;
  color: var(--dim);
  font-size: 0.75rem;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.manager-match-card a {
  color: var(--ink);
  text-decoration: underline;
  text-decoration-color: var(--hot);
  text-underline-offset: 0.22em;
}
.file-surface-section {
  grid-template-columns: minmax(260px, 0.38fr) minmax(0, 1fr);
  background:
    linear-gradient(90deg, rgba(208, 162, 72, 0.056), transparent 42%),
    rgba(255, 255, 255, 0.012);
}
.file-surface-section > div {
  min-width: 0;
}
.file-location-board {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  min-width: 0;
  border: 1px solid var(--line-strong);
  background: var(--line-strong);
}
.file-location-card {
  min-width: 0;
  padding: clamp(16px, 2vw, 22px);
  background:
    linear-gradient(180deg, rgba(208, 162, 72, 0.056), transparent),
    var(--surface-2);
}
.file-location-card h3 {
  color: var(--ink);
  font-size: clamp(1.05rem, 1.5vw, 1.38rem);
  line-height: 1.05;
  text-transform: uppercase;
}
.file-location-card p {
  margin-top: 10px;
  color: var(--muted);
  font-size: 0.92rem;
  line-height: 1.45;
}
.file-location-list {
  display: grid;
  gap: 12px;
  margin: 18px 0 0;
}
.file-location-list div {
  display: grid;
  gap: 8px;
  min-width: 0;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.file-location-list dt {
  color: var(--gold);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  font-weight: 800;
  text-transform: uppercase;
}
.file-location-list dd {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  min-width: 0;
  margin: 0;
}
.file-location-list code {
  display: block;
  max-width: 100%;
  padding: 5px 7px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.2);
  color: var(--ink);
  font-size: 0.78rem;
  line-height: 1.35;
  white-space: normal;
  overflow-wrap: anywhere;
}
.executable-table td:first-child {
  color: var(--ink);
  font-family: var(--font-mono);
  font-weight: 700;
}
.freshness-section {
  grid-template-columns: minmax(260px, 0.35fr) minmax(0, 1fr);
}
.freshness-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  border: 1px solid var(--line-strong);
  background: var(--line-strong);
}
.freshness-metrics div {
  min-width: 0;
  padding: 14px;
  background: var(--surface-2);
}
.freshness-metrics span {
  display: block;
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
}
.freshness-metrics strong {
  display: block;
  margin-top: 8px;
  color: var(--ink);
  font-family: var(--font-mono);
  overflow-wrap: anywhere;
}
.freshness-repo {
  margin-top: 14px;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.freshness-repo a {
  color: var(--ink);
  text-decoration: underline;
  text-decoration-color: var(--hot);
  text-underline-offset: 0.22em;
}
.freshness-list {
  display: grid;
  gap: 10px;
  margin-top: 16px;
  padding: 0;
  list-style: none;
}
.freshness-item {
  display: grid;
  grid-template-columns: minmax(72px, max-content) minmax(0, 1fr);
  gap: 6px 12px;
  padding: 14px;
  border: 1px solid var(--line-strong);
  background: var(--surface-2);
}
.freshness-item strong {
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: 0.76rem;
  text-transform: uppercase;
}
.freshness-item span {
  color: var(--muted);
  line-height: 1.45;
}
.freshness-item small,
.freshness-item em {
  grid-column: 2;
  color: var(--muted);
  font-size: 0.86rem;
  font-style: normal;
  overflow-wrap: anywhere;
}
.freshness-item small a {
  color: var(--ink);
  text-decoration: underline;
  text-decoration-color: var(--hot);
  text-underline-offset: 0.22em;
}
.freshness-warning {
  border-color: rgba(242, 178, 61, 0.58);
  background: rgba(242, 178, 61, 0.08);
}
.freshness-notice {
  border-color: rgba(242, 178, 61, 0.38);
}
.detail-stack { display: grid; gap: 12px; }
.detail-stack article {
  padding: 18px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.028), transparent),
    var(--surface-2);
}
.detail-stack h3 {
  color: var(--ink);
  font-size: 1.08rem;
  line-height: 1.2;
}
.detail-stack p, .detail-stack ul { margin-top: 10px; color: var(--muted); line-height: 1.5; }
.detail-stack ul { padding-left: 1.1rem; }
.detail-stack li + li { margin-top: 8px; }
.detail-stack a, table a { color: var(--ink); text-decoration: underline; text-decoration-color: var(--hot); text-underline-offset: 0.22em; overflow-wrap: anywhere; }
.pkg-search {
  --pagefind-ui-primary: var(--ink);
  --pagefind-ui-text: var(--ink);
  --pagefind-ui-background: transparent;
  --pagefind-ui-border: var(--line-strong);
  --pagefind-ui-tag: var(--surface-2);
  --pagefind-ui-border-width: 1px;
  --pagefind-ui-border-radius: 8px;
  --pagefind-ui-image-border-radius: 6px;
  --pagefind-ui-font: var(--font-ui);
  min-width: 0;
}
.pkg-search .pagefind-ui__form {
  margin: 0;
}
.pkg-search .pagefind-ui__form::before { display: none; }
.pkg-search .pagefind-ui__search-input {
  height: 54px;
  padding: 0 18px 0 20px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--surface-2);
  color: var(--ink);
  font-family: var(--font-mono);
  font-size: clamp(0.82rem, 1.5vw, 0.95rem);
  font-weight: 700;
  transition: border-color 160ms ease, background 160ms ease, transform 160ms ease;
}
.pkg-search .pagefind-ui__search-input:focus {
  border-color: rgba(114, 182, 97, 0.72);
  background: #fffdf6;
  outline: none;
  transform: translateY(-1px);
}
.pkg-search .pagefind-ui__search-input::placeholder {
  color: #8d887f;
  opacity: 1;
}
.pkg-search .pagefind-ui__drawer {
  margin-top: 14px;
}
.pkg-search .pagefind-ui__message,
.pkg-search .pagefind-ui__result-excerpt,
.pkg-search .pagefind-ui__result-nested {
  color: var(--muted);
}
.pkg-search .pagefind-ui__result {
  padding: 18px 0;
  border-top: 1px solid var(--line);
}
.pkg-search .pagefind-ui__result-title {
  color: var(--ink);
  font-size: 1.08rem;
  line-height: 1.25;
}
.pkg-search .pagefind-ui__result-title a {
  text-decoration: underline;
  text-decoration-color: var(--hot);
  text-underline-offset: 0.22em;
}
.pkg-search mark {
  background: rgba(242, 109, 61, 0.18);
  color: var(--ink);
}
.readme-excerpt {
  max-width: 860px;
  margin-top: 28px;
  padding-top: 22px;
  border-top: 1px solid var(--line-strong);
}
.readme-excerpt h3 {
  color: var(--ink);
  font-size: clamp(1.45rem, 2.2vw, 2.35rem);
  line-height: 1.05;
  text-transform: uppercase;
}
.readme-excerpt h4 {
  margin-top: 24px;
  color: var(--ink);
  font-size: 1.15rem;
  line-height: 1.25;
}
.readme-excerpt p,
.readme-excerpt ul,
.readme-excerpt ol,
.readme-excerpt pre {
  margin-top: 14px;
}
.readme-excerpt ul,
.readme-excerpt ol {
  padding-left: 1.25rem;
  color: var(--muted);
  line-height: 1.55;
}
.readme-excerpt li + li { margin-top: 8px; }
.readme-excerpt code {
  color: var(--ink);
  font-size: 0.92em;
}
.readme-excerpt pre {
  overflow-x: auto;
  padding: 14px;
  border: 1px solid var(--line);
  background: rgba(0, 0, 0, 0.24);
}
.readme-excerpt a {
  color: var(--ink);
  text-decoration: underline;
  text-decoration-color: var(--hot);
  text-underline-offset: 0.22em;
}
.readme-label,
.readme-source {
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.74rem;
  font-weight: 700;
  text-transform: uppercase;
}
.chip-list {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 8px;
  padding: 0;
  list-style: none;
}
.detail-stack .chip-list {
  padding-left: 0;
}
.chip-list li {
  display: inline-flex;
  align-items: center;
  padding: 5px 8px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.76rem;
  font-weight: 700;
  line-height: 1.2;
}
.detail-stack .chip-list li + li {
  margin-top: 0;
}
.table-wrap { margin-top: 22px; overflow-x: auto; border-top: 1px solid var(--line-strong); }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 16px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th {
  width: 230px;
  color: var(--dim);
  font-family: var(--font-mono);
  font-size: 0.76rem;
  text-transform: uppercase;
}
td { color: var(--ink); overflow-wrap: anywhere; }
.package-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
}
.package-row {
  position: relative;
  display: grid;
  gap: 8px;
  min-height: 82px;
  padding: 14px;
  background: var(--surface-2);
  overflow: hidden;
  transition: background 180ms cubic-bezier(0.16, 1, 0.3, 1), transform 180ms cubic-bezier(0.16, 1, 0.3, 1);
}
.package-row::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 2px;
  background: var(--hot);
  opacity: 0;
  transition: opacity 180ms cubic-bezier(0.16, 1, 0.3, 1);
}
.package-row:hover { background: #e2e8ff; transform: translateY(-1px); }
.package-row:hover::before { opacity: 0.76; }
.package-row:active { transform: translateY(1px) scale(0.995); }
.package-row span { position: relative; color: var(--ink); font-weight: 700; overflow-wrap: anywhere; }
.package-row small {
  position: relative;
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
}
.hub-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 1px;
  margin-top: 24px;
  border: 1px solid var(--line);
  background: var(--line);
}
.hub-groups {
  display: grid;
  gap: 28px;
  margin-top: 24px;
}
.hub-group {
  display: grid;
  gap: 12px;
}
.hub-group h3,
.hub-cluster h2 {
  color: var(--ink);
  font-size: clamp(1.15rem, 1.8vw, 1.7rem);
  line-height: 1;
  text-transform: uppercase;
}
.hub-group .hub-grid {
  margin-top: 0;
}
.hub-card {
  position: relative;
  display: grid;
  min-height: 160px;
  gap: 12px;
  align-content: space-between;
  padding: 18px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.026), transparent),
    var(--surface-2);
  overflow: hidden;
  transition: background 180ms cubic-bezier(0.16, 1, 0.3, 1), transform 180ms cubic-bezier(0.16, 1, 0.3, 1);
}
.hub-card::after {
  content: "";
  position: absolute;
  inset: auto 18px 0;
  height: 2px;
  background: linear-gradient(90deg, var(--hot), var(--gold), transparent);
  opacity: 0;
  transition: opacity 180ms cubic-bezier(0.16, 1, 0.3, 1);
}
.hub-card:hover { background: #e2e8ff; transform: translateY(-1px); }
.hub-card:hover::after { opacity: 0.82; }
.hub-card:active { transform: translateY(1px) scale(0.995); }
.hub-card span {
  position: relative;
  color: var(--ink);
  font-size: 1.05rem;
  font-weight: 800;
  line-height: 1.05;
  text-transform: uppercase;
  overflow-wrap: anywhere;
}
.hub-card strong {
  position: relative;
  color: var(--hot);
  font-family: var(--font-mono);
  font-size: 2rem;
  line-height: 1;
}
.hub-card small {
  position: relative;
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
}
.hub-table td:first-child { min-width: 160px; font-weight: 700; }
.hub-cluster {
  display: grid;
  gap: 18px;
}
.hub-spoke-list .package-row small {
  text-transform: none;
}
.hub-related-list {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  border: 1px solid var(--line);
  background: var(--line);
}
.hub-related-card {
  position: relative;
  display: grid;
  gap: 10px;
  min-height: 120px;
  padding: 16px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.026), transparent),
    var(--surface-2);
  overflow: hidden;
  transition: background 180ms cubic-bezier(0.16, 1, 0.3, 1), transform 180ms cubic-bezier(0.16, 1, 0.3, 1);
}
.hub-related-card:hover { background: #e2e8ff; transform: translateY(-1px); }
.hub-related-card:active { transform: translateY(1px) scale(0.995); }
.hub-related-card span {
  color: var(--ink);
  font-weight: 800;
  text-transform: uppercase;
}
.hub-related-card small {
  color: var(--muted);
}
.hub-related-card strong {
  color: var(--hot);
  font-family: var(--font-mono);
}
.site-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 22px;
  padding: 24px clamp(20px, 3vw, 44px);
  color: var(--dim);
  font-size: 0.9rem;
}
.site-footer p { max-width: 620px; }
.footer-links { display: flex; flex-wrap: wrap; gap: 16px; font-family: var(--font-mono); font-size: 0.74rem; font-weight: 700; text-transform: uppercase; }
.footer-links a:hover { color: var(--ink); }

@media (max-width: 960px) {
  .platform-install-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 860px) {
  :root {
    --shell-gutter-y: 12px;
    --shell-gutter-x: 24px;
  }
  .masthead, .site-footer { align-items: flex-start; flex-direction: column; }
  .brand-tagline { display: none; }
  .nav { width: 100%; flex-wrap: wrap; gap: 12px 18px; }
  .pkg-hero, .split-section, .security-section, .support-section, .pkg-search-section, .install-section, .signal-grid, .related-columns, .platform-install-grid, .install-command-row, .freshness-metrics, .file-location-board { grid-template-columns: 1fr; }
  .pkg-hero { padding-top: 38px; }
  h1 { font-size: clamp(2.8rem, 15vw, 4.8rem); }
  .lede { font-size: 1.32rem; }
  .package-list { grid-template-columns: 1fr; }
  .hub-grid,
  .hub-related-list { grid-template-columns: 1fr; }
  .metric { grid-template-columns: 1fr; gap: 6px; }
  th { width: 150px; }
}

/* 2026 visual system: a quiet, high-signal technical atlas. */
:root {
  --bg: #070a0f;
  --surface: #0c1017;
  --surface-2: #111722;
  --ink: #f2f5f7;
  --muted: #a6b0bd;
  --dim: #758191;
  --line: #222b38;
  --line-strong: #344154;
  --hot: #8fffc1;
  --blue: #7aa7ff;
  --green: #8fffc1;
  --gold: #ffd479;
  --signal: #8fffc1;
  --max: 1440px;
  --shell-gutter-y: 0px;
  --shell-gutter-x: 0px;
}

html { background: var(--bg); }
body {
  background:
    radial-gradient(circle at 10% -10%, rgba(122, 167, 255, .14), transparent 32rem),
    radial-gradient(circle at 90% 8%, rgba(143, 255, 193, .08), transparent 30rem),
    var(--bg);
  color: var(--ink);
}
body::before {
  background-image: linear-gradient(rgba(255,255,255,.018) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.018) 1px, transparent 1px);
  background-size: 32px 32px;
  opacity: 1;
}
body::after { display: none; }

::selection { background: var(--hot); color: #07110d; }
a:focus-visible { outline: 2px solid var(--hot); outline-offset: 4px; }

.site-shell {
  width: min(100%, var(--max));
  margin: 0 auto;
  border: 0;
  border-inline: 1px solid rgba(255,255,255,.07);
  background: rgba(10, 14, 20, .92);
  box-shadow: 0 0 100px rgba(0,0,0,.5);
}

.masthead {
  min-height: 76px;
  padding: 14px clamp(20px, 4vw, 64px);
  border-color: rgba(255,255,255,.09);
  background: rgba(7, 10, 15, .82);
  backdrop-filter: blur(20px) saturate(140%);
}
.brand { gap: 12px; }
.brand-mark {
  width: 44px;
  height: 44px;
  border: 0;
  border-radius: 10px;
  background: var(--hot);
  color: #07110d;
  font-size: .74rem;
  letter-spacing: -.04em;
  box-shadow: 0 0 0 1px rgba(143,255,193,.25), 0 0 28px rgba(143,255,193,.12);
}
.brand-lockup { display: grid; gap: 1px; }
.brand-type { font-size: 1rem; letter-spacing: -.03em; text-transform: none; }
.brand-tagline { padding: 0; border: 0; font-size: .58rem; letter-spacing: .13em; }
.nav { gap: 6px; font-size: .69rem; letter-spacing: .06em; }
.nav a { padding: 9px 12px; border-radius: 8px; }
.nav a:hover { background: rgba(255,255,255,.05); color: var(--ink); transform: none; }
.nav .nav-primary { border: 1px solid var(--line); color: var(--ink); }

.breadcrumbs {
  padding: 16px clamp(20px, 4vw, 64px);
  border-color: var(--line);
  background: rgba(17,23,34,.65);
  font-size: .66rem;
  letter-spacing: .06em;
}

.pkg-hero {
  grid-template-columns: minmax(0, 1fr) minmax(290px, .34fr);
  align-items: center;
  padding: clamp(64px, 9vw, 132px) clamp(20px, 4vw, 64px) clamp(56px, 8vw, 112px);
  border-color: var(--line);
  background: radial-gradient(circle at 85% 15%, rgba(143,255,193,.08), transparent 26rem);
}
.pkg-hero::before {
  background: linear-gradient(90deg, transparent 49.9%, rgba(255,255,255,.035) 50%, transparent 50.1%);
  opacity: 1;
}
.pkg-hero::after { right: 0; width: 40%; background: linear-gradient(90deg, transparent, var(--hot)); opacity: .55; }
.eyebrow, .section-kicker {
  color: var(--hot);
  font-size: .68rem;
  letter-spacing: .13em;
}
.eyebrow::before, .section-kicker::before { content: "// "; color: var(--dim); }
h1 {
  max-width: 12ch;
  margin-top: 18px;
  font-size: clamp(3.4rem, 7.4vw, 7.25rem);
  font-weight: 600;
  letter-spacing: -.07em;
  line-height: .88;
  text-transform: none;
}
.lede {
  max-width: 700px;
  color: var(--muted);
  font-size: clamp(1.16rem, 1.8vw, 1.55rem);
  font-weight: 400;
  line-height: 1.45;
}
.summary-card {
  max-width: 760px;
  padding: 18px 0 0;
  border: 0;
  border-top: 1px solid var(--line-strong);
  background: transparent;
}
.summary-card p:last-child { color: var(--muted); font-size: clamp(1.08rem, 1.6vw, 1.35rem); font-weight: 400; line-height: 1.45; }
.hero-actions { gap: 10px; }
.button { min-height: 44px; border-color: var(--line-strong); border-radius: 8px; font-size: .71rem; letter-spacing: .06em; }
.button.primary { border-color: var(--hot); background: var(--hot); color: #07110d; }
.button.secondary { background: transparent; }
.button.secondary:hover { border-color: var(--muted); background: rgba(255,255,255,.04); }

.hero-panel {
  padding: 10px 22px;
  border-color: var(--line);
  border-radius: 14px;
  background: linear-gradient(145deg, rgba(143,255,193,.07), rgba(122,167,255,.03)), var(--surface-2);
  box-shadow: 0 24px 70px rgba(0,0,0,.28);
}
.at-a-glance .section-kicker, .metric { border-color: var(--line); }
.metric { grid-template-columns: minmax(0,.55fr) minmax(0,1fr); padding: 18px 0; }
.metric span { font-size: .66rem; letter-spacing: .06em; }
.metric strong { font-size: .95rem; font-weight: 600; }

.pkg-section { padding: clamp(48px, 6vw, 84px) clamp(20px, 4vw, 64px); border-color: var(--line); }
.pkg-section:nth-of-type(odd) { background: rgba(255,255,255,.006); }
.pkg-section h2 {
  margin-top: 12px;
  font-size: clamp(2rem, 3.6vw, 3.75rem);
  font-weight: 550;
  letter-spacing: -.055em;
  line-height: .98;
  text-transform: none;
}
.pkg-section p { color: var(--muted); }
.split-section { grid-template-columns: minmax(250px,.55fr) minmax(360px,1.15fr); }
.security-section, .support-section, .gate-section, .sources-section, .manager-match-section, .file-surface-section, .pkg-search-section, .install-section, .faq-section, .registry-insights-section { background: transparent; }

.pkg-search-section { grid-template-columns: minmax(240px,.42fr) minmax(0,1fr); }
.pkg-search .pagefind-ui__search-input {
  height: 68px;
  padding-inline: 22px;
  border-color: var(--line-strong);
  border-radius: 12px;
  background: #0a0f16;
  box-shadow: inset 0 1px rgba(255,255,255,.03), 0 14px 38px rgba(0,0,0,.18);
}
.pkg-search .pagefind-ui__search-input:focus { border-color: var(--hot); background: #0d141c; box-shadow: 0 0 0 3px rgba(143,255,193,.08); transform: none; }
.pkg-search .pagefind-ui__search-input::placeholder { color: var(--dim); }
.pkg-search mark { background: rgba(143,255,193,.16); color: var(--ink); }

.hub-groups { gap: 40px; }
.hub-grid { grid-template-columns: repeat(4,minmax(0,1fr)); gap: 10px; border: 0; background: transparent; }
.hub-card {
  min-height: 170px;
  padding: 20px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-2);
}
.hub-card:hover { border-color: #405064; background: #151d29; transform: translateY(-3px); }
.hub-card::after { inset: auto 20px 0; background: var(--hot); }
.hub-card span { font-size: 1rem; font-weight: 600; text-transform: none; }
.hub-card strong { color: var(--hot); font-size: 1.65rem; font-weight: 500; }
.hub-card small { color: var(--dim); font-size: .65rem; }

.package-list { gap: 8px; border: 0; background: transparent; }
.package-row { min-height: 78px; padding: 16px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-2); }
.package-row::before { width: 3px; background: var(--hot); }
.package-row:hover { border-color: #405064; background: #151d29; transform: translateX(3px); }
.package-row span { font-weight: 600; }
.package-row small { color: var(--dim); font-size: .65rem; }

.detail-stack article, .signal-grid article, .related-columns article, .faq-grid article, .manager-match-card {
  border-color: var(--line);
  border-radius: 12px;
  background: var(--surface-2);
}
.detail-stack article:hover, .signal-grid article:hover, .related-columns article:hover { border-color: var(--line-strong); }
.platform-install-grid article, .file-location-card, .freshness-metrics div, .freshness-item { border-color: var(--line); background: var(--surface-2); }
.install-command-shell { border-color: var(--line); background: #080c12; }
.copy-button { border-color: var(--line-strong); background: var(--surface-2); color: var(--muted); }
.copy-button:hover { border-color: var(--hot); color: var(--hot); }
.chip-list li { border-color: var(--line-strong); color: var(--muted); }
.table-wrap, th, td { border-color: var(--line); }
.readme-excerpt pre, .manager-match-card pre { background: #070a0f; }

.site-footer { min-height: 110px; padding: 28px clamp(20px,4vw,64px); border-top: 1px solid var(--line); }
.footer-links a { padding: 7px 0; }

@media (max-width: 1040px) {
  .hub-grid { grid-template-columns: repeat(3,minmax(0,1fr)); }
}
@media (max-width: 860px) {
  :root { --shell-gutter-y: 0px; --shell-gutter-x: 0px; }
  .site-shell { border-inline: 0; }
  .masthead { align-items: center; flex-direction: row; }
  .nav { width: auto; }
  .nav a:not(.nav-primary) { display: none; }
  .brand-tagline { display: block; }
  .pkg-hero, .split-section, .security-section, .support-section, .pkg-search-section, .install-section, .signal-grid, .related-columns, .platform-install-grid, .freshness-metrics, .file-location-board { grid-template-columns: 1fr; }
  .pkg-hero { gap: 48px; padding-top: 64px; }
  .pkg-hero::before { display: none; }
  h1 { font-size: clamp(3rem,15vw,5rem); }
  .hub-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
}
@media (max-width: 540px) {
  .brand-tagline { display: none; }
  .nav .nav-primary { padding-inline: 10px; }
  .hub-grid, .package-list { grid-template-columns: 1fr; }
  .hub-card { min-height: 140px; }
  .site-footer { align-items: flex-start; }
}
"""


def check_current(output_dir: Path, terminal: Terminal) -> int:
    manifest_path = output_dir / MANIFEST_NAME
    if not manifest_path.exists():
        terminal.error_log(f"Missing {manifest_path}. Render package catalog diagnostics before checking this output.")
        return 1
    try:
        manifest = read_json(manifest_path)
    except json.JSONDecodeError as err:
        terminal.error_log(f"Invalid {manifest_path}: {err}")
        return 1
    files = source_files()
    expected_hash, _latest = source_digest(files)
    failures = validate_i18n_pkg_templates()
    if manifest.get("schema") != SCHEMA_VERSION:
        failures.append(f"schema is {manifest.get('schema')!r}, expected {SCHEMA_VERSION}")
    if manifest.get("source_hash") != expected_hash:
        failures.append("source hash does not match current data files")
    page_count = int(manifest.get("page_count") or 0)
    actual_pages = sum(1 for path in output_dir.glob("*/*/index.html"))
    if actual_pages != page_count:
        failures.append(f"manifest page count is {page_count}, but found {actual_pages} pages")
    radioisotope_count = local_radioisotope_manifest_count()
    full_isotope_count = local_full_isotope_manifest_count()
    if int(manifest.get("radioisotope_manifest_count") or 0) != radioisotope_count:
        failures.append(
            f"manifest radioisotope count is {manifest.get('radioisotope_manifest_count')}, but current data yields {radioisotope_count}"
        )
    if int(manifest.get("full_isotope_manifest_count") or 0) != full_isotope_count:
        failures.append(
            f"manifest full isotope count is {manifest.get('full_isotope_manifest_count')}, but current data yields {full_isotope_count}"
        )
    if int(manifest.get("isotope_manifest_count") or 0) != radioisotope_count + full_isotope_count:
        failures.append(
            f"manifest isotope count is {manifest.get('isotope_manifest_count')}, but current data yields {radioisotope_count + full_isotope_count}"
        )
    pages = sorted(package_pages_from_sources(load_sources()).values(), key=lambda page: (page.provider, page.slug, page.name))
    hubs = package_hub_pages(pages)
    indexable_pages = [page for page in pages if is_indexable_package_page(page)]
    noindex_pages = [page for page in pages if not is_indexable_package_page(page)]
    hub_count = int(manifest.get("hub_count") or 0)
    if hub_count != len(hubs):
        failures.append(f"manifest hub count is {hub_count}, but current data yields {len(hubs)} hubs")
    indexable_page_count = int(manifest.get("indexable_page_count") or 0)
    if indexable_page_count != len(indexable_pages):
        failures.append(f"manifest indexable page count is {indexable_page_count}, but current data yields {len(indexable_pages)}")
    isolated_pages = [page.key for page in indexable_pages if not has_internal_package_navigation(page)]
    if isolated_pages:
        failures.append(
            f"{len(isolated_pages):,} indexable package pages have no internal package graph links: {', '.join(isolated_pages[:12])}"
        )
    page_keys = {page.key for page in pages}
    hub_slugs = {hub.slug for hub, _hub_pages in hubs}
    weak_pages = [page.key for page in indexable_pages if internal_package_link_count(page) < 3]
    if weak_pages:
        failures.append(
            f"{len(weak_pages):,} indexable package pages have fewer than 3 internal package/hub/guide links: {', '.join(weak_pages[:12])}"
        )
    no_hub_pages = [page.key for page in indexable_pages if not page.package_hubs]
    if no_hub_pages:
        failures.append(
            f"{len(no_hub_pages):,} indexable package pages have no package hub link: {', '.join(no_hub_pages[:12])}"
        )
    for page in indexable_pages:
        for item in list(page.related_packages) + list(page.also_available_via):
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "")
            name = str(item.get("name") or "")
            target_key = f"{provider}:{name}"
            if target_key == page.key:
                failures.append(f"package page links to itself: {page.key}")
                break
            if target_key not in page_keys:
                failures.append(f"package page links to missing package: {page.key} -> {target_key}")
                break
        for item in page.package_hubs:
            if isinstance(item, dict) and str(item.get("slug") or "") not in hub_slugs:
                failures.append(f"package page links to missing hub: {page.key} -> {item.get('slug')}")
                break
    noindex_page_count = int(manifest.get("noindex_page_count") or 0)
    if noindex_page_count != len(noindex_pages):
        failures.append(f"manifest noindex page count is {noindex_page_count}, but current data yields {len(noindex_pages)}")
    markdown_page_count = int(manifest.get("markdown_page_count") or 0)
    if markdown_page_count != len(indexable_pages):
        failures.append(f"manifest markdown page count is {markdown_page_count}, but current data yields {len(indexable_pages)}")
    hub_markdown_page_count = int(manifest.get("hub_markdown_page_count") or 0)
    if hub_markdown_page_count != len(hubs):
        failures.append(f"manifest hub markdown page count is {hub_markdown_page_count}, but current data yields {len(hubs)}")
    for page in indexable_pages:
        if not (output_dir / page.provider / page.slug / "index.md").exists():
            failures.append(f"missing package markdown alternate: {output_dir / page.provider / page.slug / 'index.md'}")
            break
    for page in noindex_pages:
        if (output_dir / page.provider / page.slug / "index.md").exists():
            failures.append(f"noindex package page has markdown alternate: {page.key}")
            break
    for locale in non_default_i18n_locales():
        localized_output_dir = output_dir.parent / locale_slug(locale) / "pkg"
        if not (localized_output_dir / "index.html").exists():
            failures.append(f"missing localized package index: {localized_output_dir / 'index.html'}")
            continue
        if not (localized_output_dir / "styles.css").exists():
            failures.append(f"missing localized package stylesheet: {localized_output_dir / 'styles.css'}")
        for page in indexable_pages[:12]:
            localized_page = localized_output_dir / page.provider / page.slug / "index.html"
            localized_markdown = localized_output_dir / page.provider / page.slug / "index.md"
            if not localized_page.exists():
                failures.append(f"missing localized package page: {localized_page}")
                break
            page_text = localized_page.read_text(encoding="utf-8")
            expected_canonical = locale_url(page.path, locale)
            if f'<html lang="{locale.get("htmlLang")}"' not in page_text:
                failures.append(f"localized page has wrong html lang: {localized_page}")
                break
            if f'<link rel="canonical" href="{expected_canonical}">' not in page_text:
                failures.append(f"localized page has wrong canonical: {localized_page}")
                break
            leaks = localized_package_ui_leaks(page_text)
            if leaks:
                failures.append(f"localized page has English package UI copy: {localized_page} ({', '.join(leaks[:4])})")
                break
            if not localized_markdown.exists():
                failures.append(f"missing localized package markdown alternate: {localized_markdown}")
                break
    sitemap_path = output_dir / "sitemap.xml"
    expected_sitemap_names = ["sitemap-hubs.xml"] + [
        f"sitemap-{provider}.xml"
        for provider in PACKAGE_PROVIDERS
        if any(page.provider == provider for page in indexable_pages)
    ]
    if int(manifest.get("sitemap_count") or 0) != len(expected_sitemap_names):
        failures.append(f"manifest sitemap count is {manifest.get('sitemap_count')}, but current data yields {len(expected_sitemap_names)}")
    sitemap_page_counts = manifest.get("sitemap_page_counts") or {}
    if not isinstance(sitemap_page_counts, dict):
        failures.append("manifest sitemap page counts are missing or invalid")
        sitemap_page_counts = {}
    for provider in PACKAGE_PROVIDERS:
        expected_provider_count = sum(1 for page in indexable_pages if page.provider == provider)
        if expected_provider_count and int(sitemap_page_counts.get(provider) or 0) != expected_provider_count:
            failures.append(
                f"manifest {provider} sitemap count is {sitemap_page_counts.get(provider)}, but current data yields {expected_provider_count}"
            )
    if sitemap_path.exists():
        sitemap = sitemap_path.read_text(encoding="utf-8")
        if "<sitemapindex" not in sitemap:
            failures.append(f"package sitemap root is not a sitemap index: {sitemap_path}")
        for name in expected_sitemap_names:
            provider_sitemap = output_dir / name
            if not provider_sitemap.exists():
                failures.append(f"missing package sitemap: {provider_sitemap}")
                continue
            if f"{SITE_ORIGIN}/pkg/{name}" not in sitemap:
                failures.append(f"package sitemap index does not reference {name}")
            provider_sitemap_text = provider_sitemap.read_text(encoding="utf-8")
            if 'xmlns:xhtml="http://www.w3.org/1999/xhtml"' not in provider_sitemap_text:
                failures.append(f"package sitemap lacks xhtml namespace: {provider_sitemap}")
            for locale in non_default_i18n_locales():
                sample = next((page for page in indexable_pages if page.provider in name), None)
                if sample and locale_url(sample.path, locale) not in provider_sitemap_text:
                    failures.append(f"package sitemap lacks {locale_code(locale)} alternate: {provider_sitemap}")
                    break
            for page in noindex_pages:
                if f"{SITE_ORIGIN}{page.path}" in provider_sitemap_text:
                    failures.append(f"noindex package page is present in sitemap: {page.key}")
                    break
    else:
        failures.append(f"missing package sitemap: {sitemap_path}")
    for hub, _hub_pages in hubs:
        if not (output_dir / hub.slug / "index.html").exists():
            failures.append(f"missing package hub page: {output_dir / hub.slug / 'index.html'}")
            break
        if not (output_dir / hub.slug / "index.md").exists():
            failures.append(f"missing package hub markdown alternate: {output_dir / hub.slug / 'index.md'}")
            break
    for locale in non_default_i18n_locales():
        localized_output_dir = output_dir.parent / locale_slug(locale) / "pkg"
        for hub, _hub_pages in hubs[:12]:
            localized_hub_markdown = localized_output_dir / hub.slug / "index.md"
            if not localized_hub_markdown.exists():
                failures.append(f"missing localized package hub markdown alternate: {localized_hub_markdown}")
                break
    if failures:
        terminal.error_log("Rendered package catalog pages are stale.")
        for failure in failures:
            terminal.log(f"  - {failure}")
        terminal.log(f"{terminal.dim}Render package catalog diagnostics again, then rebuild the package-origin SQLite artifact.{terminal.reset}")
        return 1
    terminal.ok_log(f"Rendered package catalog pages are current ({fmt_int(page_count)} pages, {fmt_int(len(noindex_pages))} noindex)")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render package catalog pages for internal tooling.")
    parser.add_argument("--check", action="store_true", help="Validate rendered package pages in an explicitly provided output directory.")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help=f"Output directory. Defaults to {OUTPUT_DIR}.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable status and disable terminal styling.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_cwd()
    terminal = Terminal(json_mode=args.json)
    output_dir = Path(args.output)

    if os.environ.get(STATIC_PACKAGE_PAGE_OPT_IN) != "1":
        terminal.error_log(
            f"Direct package-page file generation is disabled. Run scripts/generate-pkg-sqlite.py for the Atlas package origin, "
            f"or set {STATIC_PACKAGE_PAGE_OPT_IN}=1 with an explicit --output for renderer diagnostics."
        )
        return 1

    if output_dir == OUTPUT_DIR:
        terminal.error_log(
            f"Refusing to write {OUTPUT_DIR}. Use scripts/generate-pkg-sqlite.py for the Atlas package origin, "
            "or pass an explicit scratch --output for renderer diagnostics."
        )
        return 1

    if args.check:
        return check_current(output_dir, terminal)

    terminal.header("Rendering package SEO pages", f"Local package metadata -> {output_dir}")
    terminal.step_log("Reading package sources")
    sources = load_sources()
    pages = package_pages_from_sources(sources)
    if not pages:
        terminal.error_log("No package metadata found in data/.")
        return 1
    files = source_files()
    previous_manifest = read_existing_manifest(output_dir)
    manifest = build_manifest(len(pages), files, previous_manifest)
    terminal.ok_log(f"Loaded {fmt_int(len(pages))} packages from {fmt_int(len(files))} source files")
    terminal.step_log("Rendering HTML, CSS, sitemap, and freshness manifest")
    stats = render_all(pages, manifest, output_dir)
    terminal.ok_log(
        f"Rendered {fmt_int(len(pages))} package pages to {output_dir} "
        f"({fmt_int(manifest['noindex_page_count'])} noindex, "
        f"{fmt_int(stats.written)} changed, {fmt_int(stats.unchanged)} unchanged, "
        f"{fmt_int(stats.deleted)} stale files removed)"
    )
    if args.json:
        print(json.dumps({
            "ok": True,
            "output": str(output_dir),
            "page_count": len(pages),
            "source_file_count": len(files),
            "written": stats.written,
            "unchanged": stats.unchanged,
            "deleted": stats.deleted,
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
