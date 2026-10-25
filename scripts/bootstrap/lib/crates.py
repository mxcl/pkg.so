from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import shlex
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .common import CACHE_DIR, CHECK_INTERVAL_SECONDS, DEFAULT_TIMEOUT, USER_AGENT, replace_if_changed, stable_hash, write_json


SCHEMA_VERSION = 3
CRATES_IO_DUMP_URL = "https://static.crates.io/db-dump.tar.gz"
CRATES_IO_CACHE_DIR = CACHE_DIR / "cratesio"
CRATES_IO_DUMP_PATH = CRATES_IO_CACHE_DIR / "db-dump.tar.gz"
CRATES_IO_DUMP_META_PATH = CRATES_IO_CACHE_DIR / "db-dump.meta.json"
CRATES_IO_INDEX_PATH = CRATES_IO_CACHE_DIR / "index.json"
CRATES_IO_SOURCE_INSPECTION_CACHE_PATH = CRATES_IO_CACHE_DIR / "source-inspection-cache.json"
CRATES_IO_DOWNLOAD_TIMEOUT = int(os.environ.get("CRATES_IO_DOWNLOAD_TIMEOUT", "3600"))
CRATES_IO_SOURCE_DOWNLOAD_TIMEOUT = int(os.environ.get("CRATES_IO_SOURCE_DOWNLOAD_TIMEOUT", str(DEFAULT_TIMEOUT)))
CRATES_IO_SOURCE_MAX_INSPECT_BYTES = int(os.environ.get("CRATES_IO_SOURCE_MAX_INSPECT_BYTES", str(10 * 1024 * 1024)))
CRATES_IO_RECENT_WINDOW_DAYS = int(os.environ.get("CRATES_IO_RECENT_WINDOW_DAYS", "90"))
CRATES_IO_MIN_RECENT_DOWNLOADS = int(os.environ.get("CRATES_IO_MIN_RECENT_DOWNLOADS", "10000"))

INTERNAL_BINARY_NAME_RE = re.compile(
    r"(^|[-_])("
    r"test|tests|testing|fixture|fixtures|bench|benchmark|benches|example|examples|demo|"
    r"fuzz|afl|coverage|accuracy|schema|generate|generator|helper"
    r")([-_]|$)|^run-.*-test-suite$|_test($|[-_])|^test[-_]",
    re.IGNORECASE,
)
GENERIC_TEST_BINARY_NAMES = {
    "cat",
    "cat-both",
    "child-grandchild",
    "echo",
    "exit",
    "exe-in-dir",
    "print-env",
    "print-env-name",
    "pwd",
    "reader",
    "sleep",
    "status",
    "swap",
    "x-to-y",
}
CLI_README_RE = re.compile(
    r"\b(cargo\s+install|usage:|command[- ]line tool|cli|executable|binary)\b",
    re.IGNORECASE,
)
LIBRARY_SUMMARY_RE = re.compile(
    r"\b(crate|library|lib|bindings?|wrapper|parser|implementation|api|framework|"
    r"generator logic|serialization|transpiled)\b",
    re.IGNORECASE,
)

SELECTED_DUMP_FILES = {
    "crates.csv",
    "crate_downloads.csv",
    "default_versions.csv",
    "version_downloads.csv",
    "versions.csv",
}


class CratesIndexError(Exception):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def valid_executable_name(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value in {".", ".."}:
        return False
    if "/" in value or "\\" in value:
        return False
    return not any(char.isspace() for char in value)


def parse_pg_text_array(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text or text == "{}":
        return []
    if not (text.startswith("{") and text.endswith("}")):
        return [text] if text else []
    inner = text[1:-1]
    if not inner:
        return []
    return [
        item
        for row in csv.reader([inner], quotechar='"', escapechar="\\")
        for item in row
        if item
    ]


def read_json_file(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_db_dump_meta(meta: dict[str, Any]) -> None:
    CRATES_IO_DUMP_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRATES_IO_DUMP_META_PATH.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_source_inspection_cache(definition_hash: str) -> dict[str, dict[str, Any]]:
    payload = read_json_file(CRATES_IO_SOURCE_INSPECTION_CACHE_PATH, {}) or {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("definition_hash") != definition_hash:
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {
        str(key): value
        for key, value in entries.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def write_source_inspection_cache(definition_hash: str, entries: dict[str, dict[str, Any]]) -> None:
    CRATES_IO_SOURCE_INSPECTION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "definition_hash": definition_hash,
        "entries": entries,
    }
    CRATES_IO_SOURCE_INSPECTION_CACHE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def head_db_dump(url: str, *, now: int) -> dict[str, Any]:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=CRATES_IO_DOWNLOAD_TIMEOUT) as response:
        return {
            "checked_at": now,
            "content_length": response.headers.get("content-length"),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "source_url": url,
            "resolved_url": response.url,
        }


def db_dump_metadata_matches(local_meta: dict[str, Any], remote_meta: dict[str, Any]) -> bool:
    local_etag = str(local_meta.get("etag") or "").strip()
    remote_etag = str(remote_meta.get("etag") or "").strip()
    if local_etag and remote_etag:
        return local_etag == remote_etag

    local_last_modified = str(local_meta.get("last_modified") or "").strip()
    remote_last_modified = str(remote_meta.get("last_modified") or "").strip()
    local_content_length = str(local_meta.get("content_length") or "").strip()
    remote_content_length = str(remote_meta.get("content_length") or "").strip()
    return (
        bool(local_last_modified)
        and local_last_modified == remote_last_modified
        and bool(local_content_length)
        and local_content_length == remote_content_length
    )


def source_definition_hash(*, min_recent_downloads: int, recent_window_days: int) -> str:
    return stable_hash({
        "schema": SCHEMA_VERSION,
        "selected_files": sorted(SELECTED_DUMP_FILES),
        "min_recent_downloads": min_recent_downloads,
        "recent_window_days": recent_window_days,
        "source_inspection": {
            "internal_binary_name_re": INTERNAL_BINARY_NAME_RE.pattern,
            "generic_test_binary_names": sorted(GENERIC_TEST_BINARY_NAMES),
            "max_archive_bytes": CRATES_IO_SOURCE_MAX_INSPECT_BYTES,
        },
    })


def download_db_dump(*, refresh: bool = False, url: str = CRATES_IO_DUMP_URL, output_path: Path = CRATES_IO_DUMP_PATH) -> tuple[Path, dict[str, Any]]:
    meta = read_json_file(CRATES_IO_DUMP_META_PATH, {}) or {}
    checked_at = parse_int(meta.get("checked_at"))
    now = int(time.time())
    if output_path.exists() and not refresh and checked_at and now - checked_at < CHECK_INTERVAL_SECONDS:
        return output_path, meta

    if output_path.exists():
        try:
            remote_meta = head_db_dump(url, now=now)
        except urllib.error.HTTPError:
            remote_meta = {}
        except OSError:
            remote_meta = {}
        if remote_meta and db_dump_metadata_matches(meta, remote_meta):
            next_meta = dict(meta)
            next_meta.update(remote_meta)
            write_db_dump_meta(next_meta)
            return output_path, next_meta

    headers = {
        "Accept": "application/gzip, application/octet-stream;q=0.9, */*;q=0.1",
        "User-Agent": USER_AGENT,
    }
    if output_path.exists():
        if meta.get("etag"):
            headers["If-None-Match"] = str(meta["etag"])
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = str(meta["last_modified"])

    request = urllib.request.Request(url, headers=headers)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, prefix=".db-dump.", suffix=".tmp")
    os.close(fd)
    Path(tmp_name).unlink(missing_ok=True)
    try:
        try:
            with urllib.request.urlopen(request, timeout=CRATES_IO_DOWNLOAD_TIMEOUT) as response:
                with open(tmp_name, "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                next_meta = {
                    "checked_at": now,
                    "content_length": response.headers.get("content-length"),
                    "etag": response.headers.get("etag"),
                    "last_modified": response.headers.get("last-modified"),
                    "source_url": url,
                    "resolved_url": response.url,
                }
        except urllib.error.HTTPError as err:
            if err.code == 304 and output_path.exists():
                meta["checked_at"] = now
                write_db_dump_meta(meta)
                return output_path, meta
            if output_path.exists():
                meta["checked_at"] = now
                meta["last_error"] = f"HTTP {err.code}"
                write_db_dump_meta(meta)
                return output_path, meta
            raise
        except OSError as err:
            if output_path.exists():
                meta["checked_at"] = now
                meta["last_error"] = str(err)
                write_db_dump_meta(meta)
                return output_path, meta
            raise
        replace_if_changed(Path(tmp_name), output_path)
        write_db_dump_meta(next_meta)
        return output_path, next_meta
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def csv_rows(source: Path | BinaryIO):
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            break
        except OverflowError:
            limit //= 10
    if isinstance(source, Path):
        handle = source.open("r", encoding="utf-8", newline="")
        with handle:
            yield from csv.DictReader(handle)
        return

    # Streaming tar readers deliberately do not implement seekability, which
    # TextIOWrapper probes during construction.  CSV only needs an iterable of
    # decoded lines, so keep the archive member forward-only and bounded.
    lines = (line.decode("utf-8") for line in source)
    yield from csv.DictReader(lines)


def crate_downloads(source: Path | BinaryIO, crate_ids: set[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in csv_rows(source):
        crate_id = str(row.get("crate_id") or "").strip()
        if crate_id in crate_ids:
            result[crate_id] = parse_int(row.get("downloads"))
    return result


def default_versions(source: Path | BinaryIO) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in csv_rows(source):
        crate_id = str(row.get("crate_id") or "").strip()
        version_id = str(row.get("version_id") or "").strip()
        if crate_id and version_id:
            result[crate_id] = {
                "version_id": version_id,
                "num_versions": parse_int(row.get("num_versions")),
            }
    return result


def recent_version_downloads(
    source: Path | BinaryIO,
    version_ids: set[str],
    *,
    window_days: int,
) -> dict[str, int]:
    cutoff = dt.date.today() - dt.timedelta(days=window_days)
    result: dict[str, int] = {}
    for row in csv_rows(source):
        version_id = str(row.get("version_id") or "").strip()
        if version_id not in version_ids:
            continue
        downloaded_at = parse_date(row.get("date"))
        if downloaded_at is not None and downloaded_at < cutoff:
            continue
        result[version_id] = result.get(version_id, 0) + parse_int(row.get("downloads"))
    return result


def executable_version_rows(source: Path | BinaryIO, version_ids: set[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in csv_rows(source):
        version_id = str(row.get("id") or "").strip()
        executables = parse_pg_text_array(row.get("bin_names"))
        if version_id in version_ids and any(valid_executable_name(item) for item in executables):
            result[version_id] = row
    return result


class DefaultVersionStore:
    """Disk-backed default-version lookup for the crates.io dump.

    The production dump contains enough rows that retaining each CSV row as a
    Python dictionary can consume gigabytes.  Keep that high-cardinality
    relation in SQLite and expose compact ID sets/dicts only after popularity
    and executable filters have reduced it.
    """

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix="default-versions-",
            suffix=".sqlite",
            dir=directory,
            delete=False,
        )
        handle.close()
        self.path = Path(handle.name)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute(
            "CREATE TABLE defaults ("
            "crate_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, num_versions INTEGER NOT NULL"
            ") WITHOUT ROWID"
        )
        self.connection.execute("CREATE INDEX defaults_version_id ON defaults(version_id)")

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            self.path.unlink(missing_ok=True)

    def load(self, source: Path | BinaryIO) -> None:
        rows: list[tuple[str, str, int]] = []
        for row in csv_rows(source):
            crate_id = str(row.get("crate_id") or "").strip()
            version_id = str(row.get("version_id") or "").strip()
            if not crate_id or not version_id:
                continue
            rows.append((crate_id, version_id, parse_int(row.get("num_versions"))))
            if len(rows) >= 10_000:
                self.connection.executemany("INSERT OR REPLACE INTO defaults VALUES (?, ?, ?)", rows)
                rows.clear()
        if rows:
            self.connection.executemany("INSERT OR REPLACE INTO defaults VALUES (?, ?, ?)", rows)
        self.connection.commit()

    def version_ids(self) -> set[str]:
        return {row[0] for row in self.connection.execute("SELECT version_id FROM defaults")}

    def candidates_for_versions(self, version_ids: set[str]) -> dict[str, dict[str, Any]]:
        if not version_ids:
            return {}
        self.connection.execute("CREATE TEMP TABLE selected_versions (version_id TEXT PRIMARY KEY) WITHOUT ROWID")
        self.connection.executemany(
            "INSERT INTO selected_versions VALUES (?)",
            ((version_id,) for version_id in version_ids),
        )
        result = {
            str(crate_id): {"version_id": str(version_id), "num_versions": int(num_versions)}
            for crate_id, version_id, num_versions in self.connection.execute(
                "SELECT crate_id, version_id, num_versions FROM defaults "
                "JOIN selected_versions USING (version_id)"
            )
        }
        self.connection.execute("DROP TABLE selected_versions")
        return result


def clean_summary(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text


def source_archive_url(name: str, version: str) -> str:
    quoted_name = urllib.request.pathname2url(name)
    quoted_file = urllib.request.pathname2url(f"{name}-{version}.crate")
    return f"https://static.crates.io/crates/{quoted_name}/{quoted_file}"


def normalized_cargo_name(value: str) -> str:
    return re.sub(r"[-_]+", "-", value.strip().lower())


def manifest_declares_binary(manifest: str) -> bool:
    return bool(re.search(r"(?m)^\s*\[\[bin\]\]\s*$|^\s*\[bin\]\s*$", manifest))


def read_archive_text(archive: tarfile.TarFile, name: str, *, limit: int = 256_000) -> str:
    handle = archive.extractfile(name)
    if handle is None:
        return ""
    return handle.read(limit).decode("utf-8", errors="replace")


def default_source_archive_fetcher(name: str, version: str) -> bytes:
    url = source_archive_url(name, version)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=CRATES_IO_SOURCE_DOWNLOAD_TIMEOUT) as response:
        content_length = parse_int(response.headers.get("content-length"))
        if content_length > CRATES_IO_SOURCE_MAX_INSPECT_BYTES:
            raise CratesIndexError(f"source archive too large for inspection: {content_length} bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > CRATES_IO_SOURCE_MAX_INSPECT_BYTES:
                raise CratesIndexError(f"source archive exceeded inspection limit: {total} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def inspect_source_archive(data: bytes) -> dict[str, Any]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        names = [member.name for member in archive if member.isfile()]
        normalized_name = next((name for name in names if name.endswith("/Cargo.toml")), "")
        manifest_name = next((name for name in names if name.endswith("/Cargo.toml.orig")), "") or normalized_name
        manifest = read_archive_text(archive, manifest_name) if manifest_name else ""
        # Cargo's published manifest resolves targets, but only the original tells
        # us whether the author explicitly advertised a binary.
        normalized_manifest = read_archive_text(archive, normalized_name) if normalized_name else manifest
        readme_name = next(
            (
                name
                for name in names
                if re.search(r"/readme(?:\.(?:md|rst|txt))?$", name, flags=re.IGNORECASE)
            ),
            "",
        )
        readme = read_archive_text(archive, readme_name) if readme_name else ""
        binary_source_paths = sorted(
            name
            for name in names
            if "/src/bin/" in name or name.endswith("/src/main.rs")
        )
        test_like_paths = [
            name
            for name in names
            if re.search(r"/(tests?|benches|fixtures?|testdata|data)/", name, flags=re.IGNORECASE)
        ]
    import tomllib

    parsed = tomllib.loads(normalized_manifest)
    required_features = {
        target["name"]: target.get("required-features", [])
        for target in parsed.get("bin", [])
        if isinstance(target, dict) and isinstance(target.get("name"), str)
    }
    if any(not isinstance(features, list) or any(not isinstance(f, str) for f in features)
           for features in required_features.values()):
        raise CratesIndexError("invalid binary required-features")
    return {
        "required_features": required_features,
        "manifest": manifest,
        "manifest_name": manifest_name,
        "readme": readme,
        "readme_name": readme_name,
        "binary_source_paths": binary_source_paths,
        "has_src_main": any(path.endswith("/src/main.rs") for path in binary_source_paths),
        "has_test_like_paths": bool(test_like_paths),
        "declares_binary": manifest_declares_binary(manifest),
    }


def executable_matches_crate(name: str, executable: str) -> bool:
    crate_name = normalized_cargo_name(name)
    binary_name = normalized_cargo_name(executable)
    return binary_name == crate_name


def executable_is_cargo_subcommand(crate_name: str, executable: str, summary: str) -> bool:
    binary_name = normalized_cargo_name(executable)
    return binary_name.startswith("cargo-") and (
        normalized_cargo_name(crate_name).startswith("cargo-")
        or re.search(r"\bcargo\b", summary, flags=re.IGNORECASE)
    )


def executable_name_looks_internal(executable: str) -> bool:
    normalized = normalized_cargo_name(executable)
    return normalized in GENERIC_TEST_BINARY_NAMES or bool(INTERNAL_BINARY_NAME_RE.search(normalized))


def cargo_executable_intent(
    name: str,
    crate: dict[str, Any],
    version: dict[str, Any],
    executables: list[str],
    *,
    source_archive_fetcher: Callable[[str, str], bytes] | None = None,
) -> dict[str, Any]:
    summary = clean_summary(version.get("description") or crate.get("description"))
    has_lib = str(version.get("has_lib") or "").lower() == "t"

    version_num = str(version.get("num") or "").strip()
    fetcher = source_archive_fetcher or default_source_archive_fetcher
    try:
        archive = inspect_source_archive(fetcher(name, version_num))
    except Exception as err:
        return {
            "intent": "unknown",
            "signals": ["source archive inspection failed"],
            "error": str(err),
        }

    requirements = {binary: archive["required_features"].get(binary, []) for binary in executables}
    feature_metadata = {"cargoRequiredFeatures": requirements} if any(requirements.values()) else {}
    strong_public_signals: list[str] = []
    if not has_lib:
        strong_public_signals.append("crate has no library target")
    if archive.get("declares_binary"):
        strong_public_signals.append("manifest declares a binary target")
    if archive.get("has_src_main"):
        strong_public_signals.append("archive contains src/main.rs")
    if any(executable_matches_crate(name, executable) for executable in executables):
        strong_public_signals.append("binary name matches crate name")
    if any(executable_is_cargo_subcommand(name, executable, summary) for executable in executables):
        strong_public_signals.append("binary is a Cargo subcommand")
    if strong_public_signals:
        return {"intent": "public_cli", "signals": strong_public_signals, **feature_metadata}

    internal_signals: list[str] = []
    if all(executable_name_looks_internal(executable) for executable in executables):
        internal_signals.append("all binary names look like test, fixture, benchmark, or helper names")
    if not archive.get("declares_binary") and not archive.get("has_src_main"):
        internal_signals.append("manifest does not advertise a binary target")
    if archive.get("has_test_like_paths"):
        internal_signals.append("archive contains tests or fixtures")
    if not CLI_README_RE.search(str(archive.get("readme") or "")):
        internal_signals.append("README does not describe CLI installation or usage")
    if LIBRARY_SUMMARY_RE.search(summary):
        internal_signals.append("crate summary reads like a library")

    if len(internal_signals) >= 2 and (
        all(executable_name_looks_internal(executable) for executable in executables)
        or (
            "README does not describe CLI installation or usage" in internal_signals
            and "crate summary reads like a library" in internal_signals
        )
    ):
        return {"intent": "internal_only", "signals": internal_signals}

    return {"intent": "unknown", "signals": internal_signals or ["no decisive executable intent signals"]}


def crate_record(
    crate: dict[str, Any],
    version: dict[str, Any],
    *,
    all_time_downloads: int,
    recent_downloads: int,
    num_versions: int,
    rank: int,
    recent_window_days: int,
) -> dict[str, Any]:
    name = str(crate.get("name") or "").strip()
    version_num = str(version.get("num") or "").strip()
    executables = [
        {
            "name": executable,
            "kind": "binary",
            "exposure": "cargo-installed executable",
            "note": "Declared by crates.io version metadata.",
        }
        for executable in parse_pg_text_array(version.get("bin_names"))
        if valid_executable_name(executable)
    ]
    metadata: dict[str, Any] = {
        "summary": clean_summary(version.get("description") or crate.get("description")),
        "homepage": str(version.get("homepage") or crate.get("homepage") or ""),
        "repository": str(version.get("repository") or crate.get("repository") or ""),
        "upstreamDocs": str(version.get("documentation") or crate.get("documentation") or ""),
        "version": version_num,
        "license": str(version.get("license") or ""),
        "sourceArchive": source_archive_url(name, version_num) if name and version_num else "",
        "sha256": str(version.get("checksum") or ""),
        "publishedAt": str(version.get("created_at") or ""),
        "last_updated_at": str(version.get("updated_at") or crate.get("updated_at") or ""),
        "created_at": str(crate.get("created_at") or ""),
        "executables": executables,
        "packageManager": "Cargo",
        "packageManagerUrl": f"https://crates.io/crates/{urllib.request.pathname2url(name)}",
        "registryInsights": {
            "sourceDatabase": "crates.io database dump",
            "crateId": parse_int(crate.get("id")),
            "versionId": parse_int(version.get("id")),
            "numVersions": num_versions,
            "hasLib": str(version.get("has_lib") or "").lower() == "t",
            "rustVersion": str(version.get("rust_version") or ""),
            "edition": str(version.get("edition") or ""),
            "crateSize": parse_int(version.get("crate_size")),
            "recentDownloadWindowDays": recent_window_days,
        },
        "popularity": {
            "downloads": all_time_downloads,
            "recent_downloads": recent_downloads,
            "recent_download_window_days": recent_window_days,
            "rank": rank,
        },
    }
    return {key: value for key, value in metadata.items() if value not in ("", [], {}, None)}


def build_index_from_dump(
    dump_path: Path,
    *,
    min_recent_downloads: int = CRATES_IO_MIN_RECENT_DOWNLOADS,
    recent_window_days: int = CRATES_IO_RECENT_WINDOW_DAYS,
    dump_meta: dict[str, Any] | None = None,
    source_archive_fetcher: Callable[[str, str], bytes] | None = None,
) -> dict[str, Any]:
    definition_hash = source_definition_hash(
        min_recent_downloads=min_recent_downloads,
        recent_window_days=recent_window_days,
    )
    source_inspection_cache = read_source_inspection_cache(definition_hash)
    cache_hits = 0
    cache_misses = 0
    found: set[str] = set()
    default_store = DefaultVersionStore(dump_path.parent)
    try:
        with tarfile.open(dump_path, mode="r|gz") as archive:
            for member in archive:
                basename = Path(member.name).name
                if not member.isfile() or basename != "default_versions.csv":
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                found.add(basename)
                default_store.load(handle)
        default_version_ids = default_store.version_ids()
        recent_by_version: dict[str, int] = {}
        with tarfile.open(dump_path, mode="r|gz") as archive:
            for member in archive:
                basename = Path(member.name).name
                if not member.isfile() or basename != "version_downloads.csv":
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                found.add(basename)
                recent_by_version = {
                    version_id: downloads
                    for version_id, downloads in recent_version_downloads(
                        handle,
                        default_version_ids,
                        window_days=recent_window_days,
                    ).items()
                    if downloads >= min_recent_downloads
                }
        del default_version_ids
        versions_by_id: dict[str, dict[str, Any]] = {}
        with tarfile.open(dump_path, mode="r|gz") as archive:
            for member in archive:
                basename = Path(member.name).name
                if not member.isfile() or basename != "versions.csv":
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                found.add(basename)
                versions_by_id = executable_version_rows(handle, set(recent_by_version))
        defaults_by_crate = default_store.candidates_for_versions(set(versions_by_id))
    finally:
        default_store.close()
    candidate_crate_ids = set(defaults_by_crate)
    crates_by_id: dict[str, dict[str, Any]] = {}
    downloads_by_id: dict[str, int] = {}
    with tarfile.open(dump_path, mode="r|gz") as archive:
        for member in archive:
            basename = Path(member.name).name
            if not member.isfile() or basename not in {"crates.csv", "crate_downloads.csv"}:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            found.add(basename)
            if basename == "crates.csv":
                crates_by_id = {
                    crate_id: row
                    for row in csv_rows(handle)
                    if (crate_id := str(row.get("id") or "").strip()) in candidate_crate_ids
                    and str(row.get("name") or "").strip()
                }
            elif basename == "crate_downloads.csv":
                downloads_by_id = crate_downloads(handle, candidate_crate_ids)
    missing = sorted(SELECTED_DUMP_FILES - found)
    if missing:
        raise CratesIndexError(f"crates.io dump missing expected files: {', '.join(missing)}")

    candidates: list[tuple[str, int, int, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    excluded_internal_binary_crates: list[dict[str, Any]] = []
    source_inspection_failures: list[dict[str, Any]] = []
    for crate_id, crate in crates_by_id.items():
        default = defaults_by_crate.get(crate_id)
        if not default:
            continue
        version = versions_by_id.get(str(default["version_id"]))
        if not version or str(version.get("yanked") or "").lower() == "t":
            continue
        executables = [
            item
            for item in parse_pg_text_array(version.get("bin_names"))
            if valid_executable_name(item)
        ]
        if not executables:
            continue
        recent_downloads = recent_by_version.get(str(default["version_id"]), 0)
        if recent_downloads < min_recent_downloads:
            continue
        all_time_downloads = downloads_by_id.get(crate_id, parse_int(version.get("downloads")))
        name = str(crate.get("name") or "").strip()
        cache_key = f"{name}@{str(version.get('num') or '').strip()}"
        cached_intent = source_inspection_cache.get(cache_key)
        if cached_intent is not None:
            intent = cached_intent
            cache_hits += 1
        else:
            intent = cargo_executable_intent(
                name,
                crate,
                version,
                executables,
                source_archive_fetcher=source_archive_fetcher,
            )
            source_inspection_cache[cache_key] = intent
            cache_misses += 1
        if intent.get("intent") == "internal_only":
            excluded_internal_binary_crates.append({
                "name": name,
                "executables": executables,
                "signals": intent.get("signals") or [],
            })
            continue
        if intent.get("intent") == "unknown" and intent.get("error"):
            source_inspection_failures.append({
                "name": name,
                "version": str(version.get("num") or ""),
                "error": str(intent.get("error") or ""),
            })
        candidates.append((name, recent_downloads, all_time_downloads, crate, version, default, intent))

    candidates.sort(key=lambda item: (-item[1], -item[2], item[0].lower()))
    crates: dict[str, Any] = {}
    for rank, (name, recent_downloads, all_time_downloads, crate, version, default, intent) in enumerate(candidates, start=1):
        record = crate_record(
            crate,
            version,
            all_time_downloads=all_time_downloads,
            recent_downloads=recent_downloads,
            num_versions=parse_int(default.get("num_versions")),
            rank=rank,
            recent_window_days=recent_window_days,
        )
        if intent:
            registry_insights = record.setdefault("registryInsights", {})
            if isinstance(registry_insights, dict):
                registry_insights["executableIntent"] = {
                    key: value
                    for key, value in intent.items()
                    if key in {"intent", "signals"}
                }
        if intent.get("cargoRequiredFeatures"):
            record["cargoRequiredFeatures"] = intent["cargoRequiredFeatures"]
        crates[name] = record

    inspection_summary = {
        "excludedInternalBinaryCrates": len(excluded_internal_binary_crates),
        "sourceInspectionFailures": len(source_inspection_failures),
    }
    if excluded_internal_binary_crates:
        inspection_summary["excludedInternalBinaryCrateSamples"] = excluded_internal_binary_crates[:25]
    if source_inspection_failures:
        inspection_summary["sourceInspectionFailureSamples"] = source_inspection_failures[:25]
    inspection_summary["cacheHits"] = cache_hits
    inspection_summary["cacheMisses"] = cache_misses

    write_source_inspection_cache(definition_hash, source_inspection_cache)

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "description": "crates.io CLI package index generated from the daily database dump.",
        "source": {
            "url": CRATES_IO_DUMP_URL,
            "dump": dump_meta or {},
            "selected_files": sorted(SELECTED_DUMP_FILES),
            "min_recent_downloads": min_recent_downloads,
            "recent_window_days": recent_window_days,
            "source_inspection": {
                "max_archive_bytes": CRATES_IO_SOURCE_MAX_INSPECT_BYTES,
                "mode": "conservative internal-binary exclusion",
                **inspection_summary,
            },
            "definition_hash": definition_hash,
        },
        "crates": crates,
    }


def build_crates_index(
    *,
    refresh: bool = False,
    dump_path: Path | None = None,
    output_path: Path = CRATES_IO_INDEX_PATH,
    min_recent_downloads: int = CRATES_IO_MIN_RECENT_DOWNLOADS,
    recent_window_days: int = CRATES_IO_RECENT_WINDOW_DAYS,
) -> dict[str, Any]:
    if dump_path is None:
        dump_path, dump_meta = download_db_dump(refresh=refresh)
    else:
        dump_meta = {"source_url": dump_path.as_posix(), "checked_at": int(time.time())}
    definition_hash = source_definition_hash(
        min_recent_downloads=min_recent_downloads,
        recent_window_days=recent_window_days,
    )
    existing_index = read_json_file(output_path, {}) or {}
    existing_source = existing_index.get("source") if isinstance(existing_index, dict) else {}
    if (
        isinstance(existing_source, dict)
        and db_dump_metadata_matches(existing_source.get("dump") or {}, dump_meta)
        and existing_source.get("definition_hash") == definition_hash
    ):
        return existing_index
    index = build_index_from_dump(
        dump_path,
        min_recent_downloads=min_recent_downloads,
        recent_window_days=recent_window_days,
        dump_meta=dump_meta,
    )
    write_json(output_path, index)
    return index


def cargo_install_commands(name: str, required_features: dict[str, list[str]] | None = None) -> list[str]:
    base = f"cargo install {shlex.quote(name)}"
    if not required_features or not any(required_features.values()):
        return [base]
    # Keep feature sets separate: enabling every binary's features can conflict.
    commands = []
    for binary, features in sorted(required_features.items()):
        command = f"{base} --bin={shlex.quote(binary)}"
        if features:
            command += f" --features={shlex.quote(','.join(sorted(set(features))))}"
        commands.append(command)
    return commands
