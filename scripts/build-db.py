#!/usr/bin/env -S uv run --python 3.10
import base64
import datetime
import email.utils
import hashlib
import http.client
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from avdb_paths import PACKAGE_INDEX_PATH

FORMULA_URL = "https://formulae.brew.sh/api/formula.json"
ANALYTICS_URL = "https://formulae.brew.sh/api/analytics/install/365d.json"
CASKS_URL = "https://formulae.brew.sh/api/cask.json"
CASK_ANALYTICS_URL = "https://formulae.brew.sh/api/analytics/cask-install/365d.json"
CACHE_DIR = "cache"
ECOSYSTEM = "brew.sh"
NPM_ECOSYSTEM = "npmjs"
DB_PATH = os.fspath(PACKAGE_INDEX_PATH)
SCHEMA_VERSION = 8
AUTHORITY_DB_PATH = os.environ.get(
    "AV_DB_AUTHORITY_PATH",
    DB_PATH,
)
AV_DB_FORMULAE_PATH = os.environ.get(
    "AV_DB_FORMULAE_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cache", "brew", "formulae.json")),
)
AV_DB_CASKS_PATH = os.environ.get(
    "AV_DB_CASKS_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "cache", "brew", "casks.json")),
)
HOMEWBREW_CORE_REPO = "Homebrew/homebrew-core"
HOMEWBREW_CASK_REPO = "Homebrew/homebrew-cask"
META_KEY = "__pkgdb_meta__"
PAYLOAD_KEY = "__pkgdb_payload__"
USER_AGENT = "pkgdb/0.1"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT = 60
MANIFEST_ACCEPT = "application/vnd.oci.image.index.v1+json"
TOKEN_SERVICE = "https://ghcr.io/token"
FORCE_REFRESH = False
NPM_REGISTRY_ROOT = "https://registry.npmjs.org"
NPM_REPLICATE_ROOT = "https://replicate.npmjs.com/registry"
NPM_REPLICATE_CHANGES_URL = f"{NPM_REPLICATE_ROOT}/_changes"
NPM_REPLICATE_ALL_DOCS_URL = f"{NPM_REPLICATE_ROOT}/_all_docs"
NPM_DOWNLOADS_POINT_ROOT = "https://api.npmjs.org/downloads/point/last-month"
NPM_MIN_MONTHLY_DOWNLOADS = 50_000
NPM_FULL_SCAN_PAGE_SIZE = 5_000
NPM_DOWNLOADS_BATCH_SIZE = 128
NPM_DOWNLOADS_BATCH_URL_MAX_LENGTH = 7_500
PULSE_NEW_WINDOW_DAYS = 7
PULSE_HISTORY_WINDOW_DAYS = 90
NPM_CHANGES_LIMIT = 5000
NPM_CHANGE_REFRESH_LIMIT = 500
NPM_INDEX_STATE_PATH = os.path.join(CACHE_DIR, NPM_ECOSYSTEM, "index.json")
NPM_AUTH_HOSTS = {"registry.npmjs.org", "replicate.npmjs.com", "api.npmjs.org"}
NPM_DOWNLOADS_HOSTS = {"api.npmjs.org"}
NPM_FULL_SCAN = False
NPM_FULL_SCAN_PARTS = 1

_GHCR_TOKENS = {}


class NpmFetchError(Exception):
    pass


class NpmRateLimitExceeded(NpmFetchError):
    pass


class NpmTransientFetchError(NpmFetchError):
    pass


class _TokenBucket:
    def __init__(self, rps):
        self.interval = 1.0 / rps if rps > 0 else 0
        self.next_available = 0.0
        self.lock = threading.Lock()

    def wait(self):
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            sleep_for = max(0.0, self.next_available - now)
            self.next_available = max(now, self.next_available) + self.interval
        if sleep_for > 0:
            time.sleep(sleep_for)


def _env_int(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError:
        print(f"Ignoring invalid {name}={value!r}; using {default}", file=sys.stderr)
        return default
    return parsed if parsed > 0 else default


def _env_float(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError:
        print(f"Ignoring invalid {name}={value!r}; using {default}", file=sys.stderr)
        return default
    return parsed if parsed > 0 else default


NPM_REGISTRY_RPS = _env_float("NPM_REGISTRY_RPS", 2.0)
NPM_DOWNLOADS_RPS = _env_float("NPM_DOWNLOADS_RPS", 0.5)
NPM_MAX_RETRIES = _env_int("NPM_MAX_RETRIES", 4)
NPM_RATE_LIMIT_BUDGET_SECONDS = _env_float("NPM_RATE_LIMIT_BUDGET_SECONDS", 600.0)
NPM_MAX_WORKERS = _env_int("NPM_MAX_WORKERS", 4)
NPM_FULL_SCAN_PAGE_SIZE = _env_int("NPM_FULL_SCAN_PAGE_SIZE", NPM_FULL_SCAN_PAGE_SIZE)
NPM_DOWNLOADS_BATCH_SIZE = _env_int("NPM_DOWNLOADS_BATCH_SIZE", NPM_DOWNLOADS_BATCH_SIZE)
NPM_DOWNLOADS_BATCH_URL_MAX_LENGTH = _env_int(
    "NPM_DOWNLOADS_BATCH_URL_MAX_LENGTH",
    NPM_DOWNLOADS_BATCH_URL_MAX_LENGTH,
)
NPM_CHANGE_REFRESH_LIMIT = _env_int(
    "NPM_CHANGE_REFRESH_LIMIT",
    NPM_CHANGE_REFRESH_LIMIT,
)

_NPM_BUCKETS = {
    "registry": _TokenBucket(NPM_REGISTRY_RPS),
    "downloads": _TokenBucket(NPM_DOWNLOADS_RPS),
}
_NPM_STATS = {
    "requests": 0,
    "cache_hits": 0,
    "rate_limits": 0,
    "retries": 0,
    "stale_uses": 0,
}


def _ensure_cwd():
    scripts_dir = os.path.abspath(os.path.dirname(__file__))
    root = os.path.dirname(scripts_dir)
    os.chdir(root)


def _cache_path_for(url, ecosystem):
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, ecosystem, f"{digest}.json")


def _cache_path(url):
    return _cache_path_for(url, ECOSYSTEM)


def _read_cached_json(url, ecosystem=ECOSYSTEM):
    path = _cache_path_for(url, ecosystem)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as handle:
        data = json.load(handle)
    if isinstance(data, dict) and META_KEY in data and PAYLOAD_KEY in data:
        meta = data.get(META_KEY) or {}
        return data.get(PAYLOAD_KEY), meta
    return data, {}


def _write_cache(path, payload, etag, checked_at):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wrapper = {
        META_KEY: {"etag": etag, "checked_at": checked_at},
        PAYLOAD_KEY: payload,
    }
    _write_json_atomic(path, wrapper)


def _write_json_atomic(path, payload, *, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=os.path.dirname(path),
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=indent, sort_keys=indent is not None)
            if indent is not None:
                handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _github_api_endpoint(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != "api.github.com":
        return None
    endpoint = parsed.path or "/"
    if parsed.query:
        endpoint = f"{endpoint}?{parsed.query}"
    return endpoint


def _gh_api_error_status(stderr):
    marker = "HTTP "
    index = stderr.rfind(marker)
    if index == -1:
        return None
    status = stderr[index + len(marker) : index + len(marker) + 3]
    return int(status) if status.isdigit() else None


def _fetch_github_api_json(url):
    endpoint = _github_api_endpoint(url)
    if endpoint is None:
        raise ValueError(f"not a GitHub API URL: {url}")
    command = [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        endpoint,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=DEFAULT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise urllib.error.URLError(err) from err
    if result.returncode != 0:
        stderr = result.stderr.strip()
        status = _gh_api_error_status(stderr)
        if status is not None:
            raise urllib.error.HTTPError(
                url,
                status,
                stderr or f"HTTP {status}",
                hdrs=None,
                fp=None,
            )
        raise urllib.error.URLError(stderr or f"gh api exited with {result.returncode}")
    return json.loads(result.stdout)


def _fetch_json(url, github_token=None, ecosystem=ECOSYSTEM, accept="application/json"):
    path = _cache_path_for(url, ecosystem)
    payload = None
    meta = {}
    if os.path.exists(path):
        payload, meta = _read_cached_json(url, ecosystem)

    checked_at = meta.get("checked_at")
    now = int(time.time())
    if (
        not FORCE_REFRESH
        and isinstance(checked_at, int)
        and now - checked_at < CHECK_INTERVAL_SECONDS
    ):
        return payload

    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname == "ghcr.io":
        headers["Accept"] = MANIFEST_ACCEPT
        repo = _ghcr_repo_from_url(parsed.path)
        if repo:
            token = _ghcr_bearer_token(repo, github_token)
            if token:
                headers["Authorization"] = f"Bearer {token}"
    elif parsed.hostname == "api.github.com":
        try:
            payload = _fetch_github_api_json(url)
            _write_cache(path, payload, None, now)
            return payload
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            if payload is not None:
                print(f"Using cached data for {url}: {err}", file=sys.stderr)
                return payload
        except (urllib.error.URLError, json.JSONDecodeError) as err:
            if payload is not None:
                print(f"Using cached data for {url}: {err}", file=sys.stderr)
                return payload
        headers["Accept"] = "application/vnd.github+json"
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
    etag = meta.get("etag")
    if etag:
        headers["If-None-Match"] = etag

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            data = response.read()
            etag = response.headers.get("etag")
            payload = json.loads(data)
            _write_cache(path, payload, etag, now)
            return payload
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        if err.code == 304 and payload is not None:
            _write_cache(path, payload, etag, now)
            return payload
        if payload is not None:
            print(f"Using cached data for {url}: {err}", file=sys.stderr)
            return payload
        raise
    except urllib.error.URLError as err:
        if payload is not None:
            print(f"Using cached data for {url}: {err}", file=sys.stderr)
            return payload
        raise


def _npm_token():
    for key in ("NPM_TOKEN", "NODE_AUTH_TOKEN", "NPM_REGISTRY_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value.strip()
    return None


def _npm_bucket_for_host(hostname):
    if hostname in NPM_DOWNLOADS_HOSTS:
        return _NPM_BUCKETS["downloads"]
    return _NPM_BUCKETS["registry"]


def _retry_after_seconds(headers):
    if not headers:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return max(0.0, float(value))
    try:
        retry_at = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (retry_at - now).total_seconds())


def _npm_backoff_seconds(attempt, err):
    retry_after = _retry_after_seconds(getattr(err, "headers", None))
    if retry_after is not None:
        return retry_after
    return min(60.0, (2**attempt) + random.uniform(0.0, 1.0))


def _npm_fetch_json(url, accept="application/json", use_cache=True):
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in NPM_AUTH_HOSTS:
        raise ValueError(f"Refusing npm fetch for non-npm host: {url}")

    path = _cache_path_for(url, NPM_ECOSYSTEM)
    payload = None
    meta = {}
    if use_cache and os.path.exists(path):
        payload, meta = _read_cached_json(url, NPM_ECOSYSTEM)

    checked_at = meta.get("checked_at")
    now = int(time.time())
    if (
        use_cache
        and not FORCE_REFRESH
        and isinstance(checked_at, int)
        and now - checked_at < CHECK_INTERVAL_SECONDS
    ):
        _NPM_STATS["cache_hits"] += 1
        return payload

    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    token = _npm_token()
    if token and parsed.hostname in NPM_AUTH_HOSTS:
        headers["Authorization"] = f"Bearer {token}"
    etag = meta.get("etag")
    if etag:
        headers["If-None-Match"] = etag

    started_at = time.monotonic()
    last_error = None
    for attempt in range(NPM_MAX_RETRIES + 1):
        _npm_bucket_for_host(parsed.hostname).wait()
        request = urllib.request.Request(url, headers=headers)
        _NPM_STATS["requests"] += 1
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
                data = response.read()
                etag = response.headers.get("etag")
                payload = json.loads(data)
                if use_cache:
                    _write_cache(path, payload, etag, int(time.time()))
                return payload
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            if err.code == 304 and payload is not None:
                if use_cache:
                    _write_cache(path, payload, etag, int(time.time()))
                return payload
            if err.code not in (429, 503):
                last_error = err
                break
            _NPM_STATS["rate_limits"] += 1
            delay = _npm_backoff_seconds(attempt, err)
            if (
                attempt >= NPM_MAX_RETRIES
                or time.monotonic() - started_at + delay > NPM_RATE_LIMIT_BUDGET_SECONDS
            ):
                if payload is not None:
                    _NPM_STATS["stale_uses"] += 1
                    print(
                        f"Using cached npm data for {url}: HTTP {err.code}",
                        file=sys.stderr,
                    )
                    return payload
                raise NpmRateLimitExceeded(f"npm fetch rate-limited for {url}") from err
            _NPM_STATS["retries"] += 1
            time.sleep(delay)
            continue
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, socket.timeout) as err:
            last_error = err
            delay = _npm_backoff_seconds(attempt, err)
            if (
                attempt >= NPM_MAX_RETRIES
                or time.monotonic() - started_at + delay > NPM_RATE_LIMIT_BUDGET_SECONDS
            ):
                if payload is not None:
                    _NPM_STATS["stale_uses"] += 1
                    print(f"Using cached npm data for {url}: {err}", file=sys.stderr)
                    return payload
                raise NpmTransientFetchError(f"npm fetch failed for {url}: {err}") from err
            _NPM_STATS["retries"] += 1
            time.sleep(delay)

    if payload is not None:
        _NPM_STATS["stale_uses"] += 1
        print(f"Using cached npm data for {url}: {last_error}", file=sys.stderr)
        return payload
    if isinstance(last_error, Exception):
        raise NpmTransientFetchError(f"npm fetch failed for {url}: {last_error}") from last_error
    raise NpmTransientFetchError(f"npm fetch failed for {url}")


def _github_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token.strip()
    return None


def _github_username():
    for key in ("GHCR_USERNAME", "GITHUB_ACTOR", "USER"):
        value = os.environ.get(key)
        if value:
            return value.strip()
    return None


def _ghcr_repo_from_url(path):
    parts = [part for part in path.split("/") if part]
    if len(parts) < 4 or parts[0] != "v2":
        return None
    return "/".join(parts[1:-2])


def _ghcr_bearer_token(repo, github_token):
    now = int(time.time())
    cache_key = (repo, bool(github_token))
    cached = _GHCR_TOKENS.get(cache_key)
    if cached and cached["expires_at"] > now:
        return cached["token"]

    username = _github_username() or "x-access-token"
    scope = f"repository:{repo}:pull"
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": scope})
    url = f"{TOKEN_SERVICE}?{query}"
    headers = {"User-Agent": USER_AGENT}
    if github_token:
        basic = base64.b64encode(
            f"{username}:{github_token}".encode("utf-8")
        ).decode("utf-8")
        headers["Authorization"] = f"Basic {basic}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as err:
        if github_token:
            return _ghcr_bearer_token(repo, None)
        print(f"Failed to get GHCR token for {repo}: {err}", file=sys.stderr)
        return None
    bearer = data.get("token")
    expires_in = data.get("expires_in", 300)
    if bearer:
        _GHCR_TOKENS[cache_key] = {
            "token": bearer,
            "expires_at": now + int(expires_in) - 10,
        }
        return bearer
    return None


def _stable_version(stable):
    if isinstance(stable, str):
        return stable
    if isinstance(stable, dict):
        for key in ("version", "tag"):
            value = stable.get(key)
            if value:
                return value
    return None


def _source_archive(formula):
    urls = formula.get("urls") or {}
    stable = urls.get("stable") if isinstance(urls, dict) else None
    if not isinstance(stable, dict):
        return ""
    url = stable.get("url")
    return url if isinstance(url, str) else ""


def _manifest_url(formula):
    name = formula.get("name")
    versions = formula.get("versions", {})
    stable = versions.get("stable")
    version = _stable_version(stable)
    if not name or not version:
        return None

    url = (
        "https://ghcr.io/v2/homebrew/core/"
        f"{name.replace('+', 'x')}/manifests/{version}"
    )

    revision = formula.get("revision")
    stable_revision = None
    if isinstance(stable, dict):
        stable_revision = stable.get("revision")
    revision_value = revision if revision is not None else stable_revision
    if revision_value not in (None, 0):
        url = f"{url}_{revision_value}"

    rebuild = formula.get("bottle", {}).get("stable", {}).get("rebuild")
    if rebuild:
        url = f"{url}-{rebuild}"

    return url


def _parse_count(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.replace(",", "")
        if value.isdigit():
            return int(value)
    return None


def _fetch_popularity(github_token):
    payload = _fetch_json(ANALYTICS_URL, github_token)
    items = payload.get("items") if isinstance(payload, dict) else None
    popularity = {}
    if not isinstance(items, list):
        return popularity
    for item in items:
        if not isinstance(item, dict):
            continue
        formula = item.get("formula")
        count = _parse_count(item.get("count"))
        rank = _parse_count(item.get("number"))
        if formula and count is not None and rank is not None:
            popularity[formula] = {
                "installs_per_365_days": count,
                "rank": rank,
            }
    return popularity


def _fetch_cask_popularity(github_token):
    payload = _fetch_json(CASK_ANALYTICS_URL, github_token)
    items = payload.get("items") if isinstance(payload, dict) else None
    popularity = {}
    if not isinstance(items, list):
        return popularity
    for item in items:
        if not isinstance(item, dict):
            continue
        cask = item.get("cask")
        count = _parse_count(item.get("count"))
        rank = _parse_count(item.get("number"))
        if cask and count is not None and rank is not None:
            popularity[cask] = {
                "installs_per_365_days": count,
                "rank": rank,
            }
    return popularity


def _overlay_homebrew_popularity_metadata(formulas, casks, github_token):
    formulas = {name: dict(metadata) for name, metadata in formulas.items()}
    casks = {token: dict(metadata) for token, metadata in casks.items()}

    try:
        popularity_by_formula = _fetch_popularity(github_token)
    except Exception as err:
        print(f"Failed to fetch formula analytics data: {err}", file=sys.stderr)
        popularity_by_formula = {}
    for name, popularity in popularity_by_formula.items():
        if name in formulas:
            formulas[name]["popularity"] = popularity

    try:
        popularity_by_cask = _fetch_cask_popularity(github_token)
    except Exception as err:
        print(f"Failed to fetch cask analytics data: {err}", file=sys.stderr)
        popularity_by_cask = {}
    for token, popularity in popularity_by_cask.items():
        if token in casks:
            casks[token]["popularity"] = popularity

    return formulas, casks


def _parse_exec_paths(paths):
    executables = set()
    for entry in paths:
        if not entry:
            continue
        entry = entry.strip()
        if not entry:
            continue
        name = entry.rsplit("/", 1)[-1]
        if name:
            executables.add(name)
    return executables


def _strip_repository_suffix(name):
    for suffix in (".git", ".tar.gz", ".zip"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _github_project_url(value):
    if not isinstance(value, str) or not value:
        return ""
    parsed = urllib.parse.urlparse(value)
    if parsed.netloc.lower() != "github.com":
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""
    owner = parts[0]
    repo = _strip_repository_suffix(parts[1])
    if not owner or not repo:
        return ""
    return f"https://github.com/{owner}/{repo}"


def _repository_from_formula(formula):
    repo = _github_project_url(formula.get("repository"))
    if repo:
        return repo
    urls = formula.get("urls")
    if not isinstance(urls, dict):
        return ""
    for key in ("head", "stable"):
        source = urls.get(key)
        if isinstance(source, dict):
            repo = _github_project_url(source.get("url"))
            if repo:
                return repo
    return ""


def _formula_metadata(formula):
    name = formula.get("name")
    if not name:
        return None
    homepage = formula.get("homepage") or ""
    repo = _repository_from_formula(formula)
    version = _stable_version((formula.get("versions") or {}).get("stable"))
    source_archive = _source_archive(formula)
    metadata = {
        "summary": formula.get("desc") or "",
        "homepage": homepage if isinstance(homepage, str) else "",
        "repository": repo if isinstance(repo, str) else "",
        "version": version or "",
        "sourceArchive": source_archive,
        "aliases": formula.get("aliases") or [],
        "oldnames": formula.get("oldnames") or [],
    }
    return {key: value for key, value in metadata.items() if value or key == "summary"}


def _formula_package_manager_metadata(formula):
    version = _stable_version((formula.get("versions") or {}).get("stable"))
    source_archive = _source_archive(formula)
    metadata = {
        "version": version or "",
        "sourceArchive": source_archive,
    }
    return {key: value for key, value in metadata.items() if value}


def _formulae_from_av_db_cache(path=None):
    path = path or AV_DB_FORMULAE_PATH
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        formulae = payload.get("formulae")
    else:
        formulae = payload
    return [formula for formula in formulae if isinstance(formula, dict)] if isinstance(formulae, list) else []


def _overlay_formula_package_manager_metadata(formulas, formulae=None):
    if formulae is None:
        formulae = _formulae_from_av_db_cache()
    by_name = {
        formula.get("name"): formula
        for formula in formulae
        if isinstance(formula.get("name"), str) and formula.get("name")
    }
    result = {name: dict(metadata) for name, metadata in formulas.items()}
    for name, metadata in result.items():
        formula = by_name.get(name)
        if not formula:
            continue
        metadata.update(_formula_package_manager_metadata(formula))
    return result


def _include_versioned_formula_metadata(formulas, formulae=None):
    if formulae is None:
        formulae = _formulae_from_av_db_cache()
    result = {name: dict(metadata) for name, metadata in formulas.items()}
    for formula in formulae:
        name = formula.get("name")
        if not isinstance(name, str) or not name or "@" not in name:
            continue
        if name in result:
            continue
        metadata = _formula_metadata(formula)
        if metadata is not None:
            result[name] = metadata
    return result


def _casks_from_av_db_cache(path=None):
    path = path or AV_DB_CASKS_PATH
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    casks = payload.get("casks") if isinstance(payload, dict) else payload
    return [cask for cask in casks if isinstance(cask, dict)] if isinstance(casks, list) else []


def _overlay_cask_package_manager_metadata(casks, cask_payloads=None):
    if cask_payloads is None:
        cask_payloads = _casks_from_av_db_cache()
    result = {token: dict(metadata) for token, metadata in casks.items()}
    for cask in cask_payloads:
        token = cask.get("token")
        if not isinstance(token, str) or token not in result:
            continue
        metadata = _cask_metadata(cask)
        if metadata is None:
            continue
        for key in ("url", "sha256", "version"):
            if metadata.get(key):
                result[token][key] = metadata[key]
    return result


def _cask_url(token):
    return f"https://formulae.brew.sh/api/cask/{token}.json"


def _formula_source_path(name):
    return f"Formula/{name[0]}/{name}.rb"


def _cask_source_path(token):
    return f"Casks/{token[0]}/{token}.rb"


def _git_repo_cache_path(repo):
    return os.path.join(CACHE_DIR, ECOSYSTEM, "git", repo.rsplit("/", 1)[-1])


def _ensure_git_repo(repo):
    path = _git_repo_cache_path(repo)
    url = f"https://github.com/{repo}.git"
    try:
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            subprocess.run(
                ["git", "clone", "--filter=blob:none", "--no-checkout", url, path],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            subprocess.run(
                ["git", "-C", path, "fetch", "--quiet", "--filter=blob:none", "origin"],
                check=True,
                capture_output=True,
                text=True,
            )
    except (OSError, subprocess.CalledProcessError) as err:
        if os.path.exists(path):
            print(f"Using stale Homebrew git cache for {repo}: {err}", file=sys.stderr)
            return path
        print(f"Skipping Homebrew pulse metadata for {repo}: {err}", file=sys.stderr)
        return None
    return path


def _git_default_revision(repo_path):
    candidates = [
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
        "refs/remotes/origin/master",
    ]
    for candidate in candidates:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--verify", candidate],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return candidate
    print(
        f"Skipping Homebrew pulse metadata for {repo_path}: no fetched revision",
        file=sys.stderr,
    )
    return None


def _git_pulse_events(repo, keyed_paths, scope):
    if not keyed_paths:
        return {}

    repo_path = _ensure_git_repo(repo)
    if repo_path is None:
        return {}
    revision = _git_default_revision(repo_path)
    if revision is None:
        return {}
    now = datetime.datetime.now(datetime.timezone.utc)
    new_cutoff = now - datetime.timedelta(days=PULSE_NEW_WINDOW_DAYS)
    history_cutoff = now - datetime.timedelta(days=PULSE_HISTORY_WINDOW_DAYS)
    pending_latest = set(keyed_paths.keys())
    pending_additions = set(keyed_paths.keys())
    events = {}
    current_date = None
    current_datetime = None
    recent_additions = set()
    command = [
        "git",
        "-C",
        repo_path,
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
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            if line.startswith("__DATE__"):
                current_date = line[len("__DATE__") :]
                current_datetime = _parse_git_timestamp(current_date)
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
            added_latest = False
            if path in pending_latest:
                events[key] = {
                    "last_updated_at": current_date,
                    "pulse_kind": "updated",
                }
                pending_latest.remove(path)
                added_latest = True
            if path in pending_additions:
                if status.startswith("A") and _is_recent_datetime(
                    current_datetime,
                    new_cutoff,
                ):
                    recent_additions.add(key)
                if status.startswith("A") or not _is_recent_datetime(
                    current_datetime,
                    new_cutoff,
                ):
                    pending_additions.remove(path)
            if added_latest and len(events) % 100 == 0:
                print(
                    f"Resolved {len(events)}/{len(keyed_paths)} git pulse events for {repo}",
                    file=sys.stderr,
                )
            if not pending_latest and not _is_recent_datetime(
                current_datetime,
                new_cutoff,
            ):
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


def _parse_git_timestamp(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_recent_datetime(value, cutoff):
    return value is not None and value >= cutoff


def _parse_binary_artifact(artifact):
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


def _supported_cask_artifacts(artifacts):
    binaries = []
    for artifact in artifacts:
        parsed = _parse_binary_artifact(artifact)
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


def _cask_metadata(cask):
    token = cask.get("token")
    if not token or cask.get("disabled") or cask.get("deprecated"):
        return None

    binaries = _supported_cask_artifacts(cask.get("artifacts") or [])
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
    formula_dependencies = depends_on.get("formula") or []
    if not all(isinstance(dep, str) and dep for dep in formula_dependencies):
        return None

    return {
        "summary": cask.get("desc") or "",
        "homepage": cask.get("homepage") or "",
        "aliases": cask.get("old_tokens") or [],
        "url": url,
        "sha256": sha256,
        "version": version,
        "dependencies": sorted(set(formula_dependencies)),
        "binaries": binaries,
    }


def _collect_formula_entries(formulae, popularity_by_formula, manifests, pulse_events_by_formula):
    entries = {}
    formulas = {}
    missing_manifests = 0
    for formula in formulae:
        if not isinstance(formula, dict):
            continue
        name = formula.get("name")
        if not name:
            continue

        metadata = _formula_metadata(formula)
        if metadata is not None:
            formulas[name] = metadata

        popularity = popularity_by_formula.get(name)
        pulse_event = pulse_events_by_formula.get(name)
        if name in formulas:
            if popularity is not None:
                formulas[name]["popularity"] = popularity
            if pulse_event is not None:
                formulas[name]["last_updated_at"] = pulse_event["last_updated_at"]
                formulas[name]["pulse_kind"] = pulse_event["pulse_kind"]

        if "@" in name:
            continue

        url = _manifest_url(formula)
        if not url:
            continue

        payload = manifests.get(url)
        if not payload:
            missing_manifests += 1
            continue

        manifest_list = payload.get("manifests", [])
        executables = set()
        for manifest in manifest_list:
            annotations = None
            if isinstance(manifest, dict):
                annotations = manifest.get("annotations")
            if not annotations:
                continue
            provides = annotations.get("sh.brew.path_exec_files")
            if not provides:
                continue
            paths = [item.strip() for item in provides.split(",") if item.strip()]
            executables.update(_parse_exec_paths(paths))
            if executables:
                break

        for executable in executables:
            entries.setdefault(executable, []).append(
                {
                    "provider": name,
                    "popularity": popularity["installs_per_365_days"]
                    if popularity is not None
                    else 0,
                }
            )

    return entries, formulas, missing_manifests


def _collect_cask_entries(casks, popularity_by_cask, pulse_events_by_cask):
    entries = {}
    metadata = {}
    for cask in casks:
        if not isinstance(cask, dict):
            continue
        token = cask.get("token")
        if not token:
            continue

        supported = _cask_metadata(cask)
        if supported is None:
            continue

        popularity = popularity_by_cask.get(token)
        if supported["binaries"] and popularity is not None:
            supported["popularity"] = popularity
        pulse_event = pulse_events_by_cask.get(token)
        if supported["binaries"] and pulse_event is not None:
            supported["last_updated_at"] = pulse_event["last_updated_at"]
            supported["pulse_kind"] = pulse_event["pulse_kind"]
        metadata[token] = supported
        for binary in supported["binaries"]:
            executable = binary.get("target") or os.path.basename(binary["source"])
            if executable:
                entries.setdefault(executable, []).append(
                    {
                        "provider": f"cask:{token}",
                        "popularity": popularity["installs_per_365_days"]
                        if popularity is not None
                        else 0,
                    }
                )

    return entries, metadata


def _fetch_uncached_json(url, accept="application/json"):
    request = urllib.request.Request(
        url,
        headers={"Accept": accept, "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
        return json.loads(response.read())


def _npm_package_url(package):
    return f"{NPM_REGISTRY_ROOT}/{urllib.parse.quote(package, safe='@')}"


def _npm_downloads_url(package):
    return f"{NPM_DOWNLOADS_POINT_ROOT}/{urllib.parse.quote(package, safe='@/')}"


def _npm_downloads_batch_url(packages):
    joined = ",".join(packages)
    return f"{NPM_DOWNLOADS_POINT_ROOT}/{urllib.parse.quote(joined, safe='@/,')}"


def _npm_package_supports_downloads_batch(package):
    return not package.startswith("@")


def _npm_download_batches(packages):
    batch = []
    for package in packages:
        candidate = [*batch, package]
        if batch and (
            len(candidate) > NPM_DOWNLOADS_BATCH_SIZE
            or len(_npm_downloads_batch_url(candidate)) > NPM_DOWNLOADS_BATCH_URL_MAX_LENGTH
        ):
            yield batch
            batch = [package]
        else:
            batch = candidate
    if batch:
        yield batch


def _npm_all_docs_url(startkey=None):
    params = {
        "limit": NPM_FULL_SCAN_PAGE_SIZE,
    }
    if startkey:
        params["startkey"] = json.dumps(startkey)
    return f"{NPM_REPLICATE_ALL_DOCS_URL}?{urllib.parse.urlencode(params)}"


def _npm_install_leaf_name(package):
    if package.startswith("@") and "/" in package:
        return package.rsplit("/", 1)[-1]
    return package


def _valid_npm_executable_name(value):
    if not isinstance(value, str) or not value:
        return False
    if value in (".", ".."):
        return False
    if "/" in value or "\\" in value:
        return False
    return not any(char.isspace() for char in value)


def _npm_matching_executable(package, bin_value):
    leaf = _npm_install_leaf_name(package)
    if isinstance(bin_value, str) and bin_value and _valid_npm_executable_name(leaf):
        return leaf
    if not isinstance(bin_value, dict):
        return None
    target = bin_value.get(leaf)
    if isinstance(target, str) and target and _valid_npm_executable_name(leaf):
        return leaf
    valid_bins = [
        name
        for name, target in bin_value.items()
        if _valid_npm_executable_name(name) and isinstance(target, str) and target
    ]
    if len(valid_bins) == 1:
        return valid_bins[0]
    return None


def _npm_latest_version_doc(packument):
    if not isinstance(packument, dict):
        return None, None
    latest = (packument.get("dist-tags") or {}).get("latest")
    versions = packument.get("versions") or {}
    if not isinstance(latest, str) or not isinstance(versions, dict):
        return None, None
    version_doc = versions.get(latest)
    if not isinstance(version_doc, dict):
        return None, None
    return latest, version_doc


def _npm_last_updated_at(packument, latest):
    times = packument.get("time") if isinstance(packument, dict) else None
    if not isinstance(times, dict):
        return None
    value = times.get(latest) or times.get("modified")
    return value if isinstance(value, str) and value else None


def _npm_created_at(packument):
    times = packument.get("time") if isinstance(packument, dict) else None
    if not isinstance(times, dict):
        return None
    value = times.get("created")
    return value if isinstance(value, str) and value else None


def _npm_pulse_kind(packument, last_updated_at):
    now = datetime.datetime.now(datetime.timezone.utc)
    new_cutoff = now - datetime.timedelta(days=PULSE_NEW_WINDOW_DAYS)
    history_cutoff = now - datetime.timedelta(days=PULSE_HISTORY_WINDOW_DAYS)
    created_at = _parse_git_timestamp(_npm_created_at(packument) or "")
    updated_at = _parse_git_timestamp(last_updated_at or "")
    if _is_recent_datetime(created_at, new_cutoff):
        return "new"
    if _is_recent_datetime(updated_at, history_cutoff):
        return "updated"
    return None


def _npm_metadata_from_packument(package, packument, monthly_downloads, stale_metadata=None):
    if monthly_downloads is None and isinstance(stale_metadata, dict):
        popularity = stale_metadata.get("popularity") or {}
        monthly_downloads = _parse_count(popularity.get("downloads_per_30_days"))
    if monthly_downloads is None or monthly_downloads < NPM_MIN_MONTHLY_DOWNLOADS:
        return None
    latest, version_doc = _npm_latest_version_doc(packument)
    if latest is None or version_doc is None:
        return None
    if version_doc.get("deprecated"):
        return None
    executable = _npm_matching_executable(package, version_doc.get("bin"))
    if executable is None:
        return None

    summary = version_doc.get("description") or packument.get("description") or ""
    homepage = version_doc.get("homepage") or packument.get("homepage") or ""
    last_updated_at = _npm_last_updated_at(packument, latest)
    metadata = {
        "summary": summary if isinstance(summary, str) else "",
        "homepage": homepage if isinstance(homepage, str) else "",
        "version": latest,
        "executable": executable,
        "popularity": {
            "downloads_per_30_days": monthly_downloads,
            "rank": 0,
        },
    }
    if last_updated_at:
        metadata["last_updated_at"] = last_updated_at
    pulse_kind = _npm_pulse_kind(packument, last_updated_at)
    if pulse_kind:
        metadata["pulse_kind"] = pulse_kind
    return metadata


def _fetch_npm_packument(package, use_cache=True):
    return _npm_fetch_json(
        _npm_package_url(package),
        accept="application/json",
        use_cache=use_cache,
    )


def _fetch_npm_packuments_for_packages(packages, progress_label=None, use_cache=True):
    if not packages:
        return {}
    completed = 0
    refreshed = {}
    max_workers = max(1, min(NPM_MAX_WORKERS, len(packages)))

    def fetch(package):
        try:
            return package, _fetch_npm_packument(package, use_cache=use_cache)
        except NpmFetchError as err:
            print(f"Keeping stale npm metadata for {package}: {err}", file=sys.stderr)
            return package, None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(fetch, package): package
            for package in sorted(packages)
        }
        for future in as_completed(future_map):
            package, packument = future.result()
            if packument is not None:
                refreshed[package] = packument
            completed += 1
            if progress_label and completed % 100 == 0:
                print(
                    f"Fetched {completed}/{len(packages)} {progress_label}...",
                    file=sys.stderr,
                )
    return refreshed


def _fetch_npm_monthly_downloads(package):
    payload = _npm_fetch_json(_npm_downloads_url(package))
    if not isinstance(payload, dict):
        return 0
    return _parse_count(payload.get("downloads")) or 0


def _default_npm_index_state():
    return {
        "last_seq": None,
        "packages": {},
        "full_scan_cursor": None,
        "full_scan_started_at": None,
        "last_full_scan_at": None,
        "full_scan_seen_count": 0,
        "full_scan_download_qualified_count": 0,
        "full_scan_packument_qualified_count": 0,
        "full_scan_page_count": 0,
        "full_scan_total_rows": None,
        "full_scan_last_shard_at": None,
    }


def _read_npm_index_state():
    if not os.path.exists(NPM_INDEX_STATE_PATH):
        return _default_npm_index_state()
    with open(NPM_INDEX_STATE_PATH, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        return _default_npm_index_state()
    state = _default_npm_index_state()
    state.update(payload)
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        packages = {}
    state["packages"] = packages
    if state.get("last_full_scan_at") and not state.get("full_scan_cursor"):
        state["full_scan_started_at"] = None
    return state


def _write_npm_index_state(state):
    _write_json_atomic(NPM_INDEX_STATE_PATH, state, indent=2)


def _current_npm_changes_sequence():
    params = urllib.parse.urlencode({"descending": "true", "limit": 1})
    payload = _npm_fetch_json(f"{NPM_REPLICATE_CHANGES_URL}?{params}", use_cache=False)
    return payload.get("last_seq") if isinstance(payload, dict) else None


def _fetch_npm_changes_since(last_seq, max_changes=NPM_CHANGE_REFRESH_LIMIT):
    if last_seq is None:
        return set(), set(), _current_npm_changes_sequence(), False

    changed = set()
    deleted = set()
    next_seq = last_seq
    last_processed_seq = last_seq
    processed_events = 0
    while True:
        params = urllib.parse.urlencode({"since": next_seq, "limit": NPM_CHANGES_LIMIT})
        payload = _npm_fetch_json(f"{NPM_REPLICATE_CHANGES_URL}?{params}", use_cache=False)
        if not isinstance(payload, dict):
            break
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            next_seq = payload.get("last_seq", next_seq)
            break
        for item in results:
            if not isinstance(item, dict):
                continue
            package = item.get("id")
            if not isinstance(package, str) or not package:
                continue
            item_seq = item.get("seq", last_processed_seq)
            if item.get("deleted"):
                deleted.add(package)
                changed.discard(package)
            else:
                changed.add(package)
            last_processed_seq = item_seq
            processed_events += 1
            if processed_events >= max_changes:
                return changed, deleted, last_processed_seq, True
        next_seq = payload.get("last_seq", next_seq)
        last_processed_seq = next_seq
        if len(results) < NPM_CHANGES_LIMIT:
            break
    return changed, deleted, next_seq, False


def _npm_monthly_downloads_batch(
    packages,
    existing_packages,
    allow_single_fallback=True,
):
    downloads = {}
    missing_packages = []
    for package in packages:
        stale = existing_packages.get(package) or {}
        popularity = stale.get("popularity") or {}
        stale_downloads = _parse_count(popularity.get("downloads_per_30_days"))
        if stale_downloads is not None:
            downloads[package] = stale_downloads
        else:
            missing_packages.append(package)

    batchable_packages = [
        package
        for package in missing_packages
        if _npm_package_supports_downloads_batch(package)
    ]
    single_packages = [
        package
        for package in missing_packages
        if not _npm_package_supports_downloads_batch(package)
    ]

    for batch in _npm_download_batches(batchable_packages):
        try:
            payload = _npm_fetch_json(_npm_downloads_batch_url(batch))
        except NpmFetchError as err:
            if allow_single_fallback and not isinstance(err, NpmRateLimitExceeded):
                for package in batch:
                    try:
                        payload = _npm_fetch_json(_npm_downloads_url(package))
                    except NpmFetchError:
                        continue
                    if isinstance(payload, dict):
                        count = _parse_count(payload.get("downloads"))
                        if count is not None:
                            downloads[package] = count
                continue
            print(
                "Using stale npm download counts for "
                f"{len(batch)} packages: {err}",
                file=sys.stderr,
            )
            continue
        if not isinstance(payload, dict):
            continue
        if len(batch) == 1 and "downloads" in payload:
            count = _parse_count(payload.get("downloads"))
            if count is not None:
                downloads[batch[0]] = count
            continue
        for package in batch:
            item = payload.get(package)
            if not isinstance(item, dict):
                continue
            count = _parse_count(item.get("downloads"))
            if count is not None:
                downloads[package] = count
    if allow_single_fallback:
        for package in single_packages:
            try:
                payload = _npm_fetch_json(_npm_downloads_url(package))
            except NpmFetchError:
                continue
            if isinstance(payload, dict):
                count = _parse_count(payload.get("downloads"))
                if count is not None:
                    downloads[package] = count
    return downloads


def _npm_reset_full_scan_stats(state):
    state["full_scan_seen_count"] = 0
    state["full_scan_download_qualified_count"] = 0
    state["full_scan_packument_qualified_count"] = 0
    state["full_scan_page_count"] = 0
    state["full_scan_total_rows"] = None


def _npm_full_scan_progress(state, indexed_count):
    seen = _parse_count(state.get("full_scan_seen_count")) or 0
    total = _parse_count(state.get("full_scan_total_rows"))
    pages = _parse_count(state.get("full_scan_page_count")) or 0
    download_qualified = (
        _parse_count(state.get("full_scan_download_qualified_count")) or 0
    )
    packument_qualified = (
        _parse_count(state.get("full_scan_packument_qualified_count")) or 0
    )
    remaining = "unknown"
    if total is not None:
        remaining_count = max(0, total - seen)
        remaining_pages = (
            remaining_count + NPM_FULL_SCAN_PAGE_SIZE - 1
        ) // NPM_FULL_SCAN_PAGE_SIZE
        remaining = f"{remaining_count} names, ~{remaining_pages} pages"
    total_label = total if total is not None else "unknown"
    print(
        "npm full scan progress: "
        f"seen={seen}/{total_label} "
        f"pages={pages} "
        f"download_qualified={download_qualified} "
        f"packument_qualified={packument_qualified} "
        f"indexed={indexed_count} "
        f"remaining={remaining} "
        f"requests={_NPM_STATS['requests']} "
        f"cache_hits={_NPM_STATS['cache_hits']} "
        f"rate_limits={_NPM_STATS['rate_limits']}",
        file=sys.stderr,
    )


def _npm_packument_name(fallback, packument):
    name = packument.get("name") if isinstance(packument, dict) else None
    return name if isinstance(name, str) and name else fallback


def _npm_packument_has_installable_cli(package, packument):
    latest, version_doc = _npm_latest_version_doc(packument)
    if latest is None or version_doc is None or version_doc.get("deprecated"):
        return False
    return _npm_matching_executable(package, version_doc.get("bin")) is not None


def _refresh_npm_packuments(packages, packuments, existing_packages, downloads=None):
    candidates = {}
    for fallback, packument in packuments.items():
        if not isinstance(packument, dict):
            continue
        package = _npm_packument_name(fallback, packument)
        if not _npm_packument_has_installable_cli(package, packument):
            packages.pop(package, None)
            continue
        candidates[package] = packument

    if downloads is None:
        downloads = _npm_monthly_downloads_batch(sorted(candidates), existing_packages)
    accepted = 0
    for package, packument in candidates.items():
        metadata = _npm_metadata_from_packument(
            package,
            packument,
            downloads.get(package),
            existing_packages.get(package),
        )
        if metadata is None:
            packages.pop(package, None)
        else:
            packages[package] = metadata
            accepted += 1
    return accepted


def _npm_full_scan_page_budget(total_rows):
    if NPM_FULL_SCAN_PARTS <= 1 or total_rows is None:
        return None
    total_pages = (
        total_rows + NPM_FULL_SCAN_PAGE_SIZE - 1
    ) // NPM_FULL_SCAN_PAGE_SIZE
    return max(1, (total_pages + NPM_FULL_SCAN_PARTS - 1) // NPM_FULL_SCAN_PARTS)


def _npm_full_scan_shard_complete(page_count, total_rows):
    page_budget = _npm_full_scan_page_budget(total_rows)
    if page_budget is None:
        return False
    parsed_page_count = _parse_count(page_count) or 0
    return parsed_page_count > 0 and parsed_page_count % page_budget == 0


def _npm_full_scan_shard_completed_today(state, now=None):
    if NPM_FULL_SCAN_PARTS <= 1:
        return False
    value = state.get("full_scan_last_shard_at")
    if not value and _npm_full_scan_shard_complete(
        state.get("full_scan_page_count"),
        state.get("full_scan_total_rows"),
    ):
        # Backward-compatible recovery for a shard checkpoint written before
        # full_scan_last_shard_at existed.  Reaching the durable boundary and
        # atomically writing the state file is sufficient proof of completion.
        try:
            value = datetime.datetime.fromtimestamp(
                os.path.getmtime(NPM_INDEX_STATE_PATH),
                datetime.timezone.utc,
            ).isoformat()
        except OSError:
            value = None
    if not isinstance(value, str) or not value:
        return False
    try:
        completed_at = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=datetime.timezone.utc)
    current = now or datetime.datetime.now(datetime.timezone.utc)
    return completed_at.astimezone(datetime.timezone.utc).date() == current.astimezone(
        datetime.timezone.utc
    ).date()


def _run_npm_full_scan(state):
    if _npm_full_scan_shard_completed_today(state):
        print(
            "Skipping npm full scan; today's durable shard already completed",
            file=sys.stderr,
        )
        return
    packages = state["packages"]
    cursor = state.get("full_scan_cursor")
    if not state.get("full_scan_started_at"):
        state["full_scan_started_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        _npm_reset_full_scan_stats(state)
    while True:
        payload = _npm_fetch_json(_npm_all_docs_url(cursor), use_cache=False)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        total_rows = payload.get("total_rows") if isinstance(payload, dict) else None
        parsed_total_rows = _parse_count(total_rows)
        if parsed_total_rows is not None:
            state["full_scan_total_rows"] = parsed_total_rows
        if not isinstance(rows, list) or not rows:
            state["full_scan_cursor"] = None
            state["full_scan_started_at"] = None
            state["last_full_scan_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            _write_npm_index_state(state)
            break

        page_packages = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            package = row.get("id") or row.get("key")
            if package == cursor:
                continue
            if isinstance(package, str) and package:
                page_packages.append(package)
        downloads = _npm_monthly_downloads_batch(
            page_packages,
            packages,
            allow_single_fallback=False,
        )
        popular_packages = [
            package
            for package in page_packages
            if (downloads.get(package) or 0) >= NPM_MIN_MONTHLY_DOWNLOADS
        ]
        packuments = _fetch_npm_packuments_for_packages(
            popular_packages,
            use_cache=False,
        )
        accepted_count = _refresh_npm_packuments(
            packages,
            packuments,
            packages,
            downloads,
        )
        next_cursor = rows[-1].get("id") or rows[-1].get("key")
        state["full_scan_cursor"] = next_cursor
        state["full_scan_seen_count"] = (
            (_parse_count(state.get("full_scan_seen_count")) or 0)
            + len(page_packages)
        )
        state["full_scan_download_qualified_count"] = (
            (_parse_count(state.get("full_scan_download_qualified_count")) or 0)
            + len(popular_packages)
        )
        state["full_scan_packument_qualified_count"] = (
            (_parse_count(state.get("full_scan_packument_qualified_count")) or 0)
            + accepted_count
        )
        state["full_scan_page_count"] = (
            (_parse_count(state.get("full_scan_page_count")) or 0) + 1
        )
        _write_npm_index_state(state)
        _npm_full_scan_progress(state, len(packages))

        if len(rows) < NPM_FULL_SCAN_PAGE_SIZE or next_cursor == cursor:
            state["full_scan_cursor"] = None
            state["full_scan_started_at"] = None
            state["last_full_scan_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            _write_npm_index_state(state)
            break
        if _npm_full_scan_shard_complete(
            state.get("full_scan_page_count"),
            state.get("full_scan_total_rows"),
        ):
            state["full_scan_last_shard_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            _write_npm_index_state(state)
            print(
                f"Paused npm full scan at cumulative page "
                f"{state['full_scan_page_count']}; "
                f"next run resumes at {next_cursor}",
                file=sys.stderr,
            )
            break
        cursor = next_cursor


def _refresh_changed_npm_packages(packages, changed):
    if not changed:
        return
    refreshed = _fetch_npm_packuments_for_packages(changed, "changed npm packages")
    _refresh_npm_packuments(packages, refreshed, packages)


def _collect_npm_metadata():
    state = _read_npm_index_state()
    packages = {
        name: metadata
        for name, metadata in state["packages"].items()
        if isinstance(name, str) and isinstance(metadata, dict)
    }
    state["packages"] = packages

    auth_status = "enabled" if _npm_token() else "disabled"
    print(
        "npm registry auth "
        f"{auth_status}; registry_rps={NPM_REGISTRY_RPS:g}, "
        f"downloads_rps={NPM_DOWNLOADS_RPS:g}, workers={NPM_MAX_WORKERS}",
        file=sys.stderr,
    )

    try:
        changes_since = state.get("last_seq")
        if NPM_FULL_SCAN:
            if NPM_FULL_SCAN_PARTS <= 1:
                changes_since = _current_npm_changes_sequence()
            print("Starting npm full metadata scan...", file=sys.stderr)
            _run_npm_full_scan(state)
        elif state.get("full_scan_cursor") or not state.get("last_full_scan_at"):
            print(
                "Skipping npm full metadata scan; run "
                "scripts/build-db.py --npm-full-scan to refresh the full npm index",
                file=sys.stderr,
            )

        changed, deleted, next_seq, has_more_changes = _fetch_npm_changes_since(
            changes_since
        )
        for package in deleted:
            packages.pop(package, None)

        if changed:
            print(
                f"Processing {len(changed)} npm registry changes...",
                file=sys.stderr,
            )
        if has_more_changes:
            print(
                "Deferring remaining npm registry changes to a later update cycle",
                file=sys.stderr,
            )
        _refresh_changed_npm_packages(packages, changed)
        if next_seq is not None:
            state["last_seq"] = next_seq
    except NpmFetchError as err:
        if packages:
            _NPM_STATS["stale_uses"] += 1
            print(
                f"Using stale npm package cache after npm fetch failure: {err}",
                file=sys.stderr,
            )
        else:
            raise

    ranked = {}
    sorted_packages = sorted(
        packages.items(),
        key=lambda item: (
            -((item[1].get("popularity") or {}).get("downloads_per_30_days") or 0),
            item[0],
        ),
    )
    for rank, (package, metadata) in enumerate(sorted_packages, start=1):
        popularity = metadata.setdefault("popularity", {})
        popularity["rank"] = rank
        ranked[package] = metadata

    state["packages"] = ranked
    _write_npm_index_state(state)
    print(
        "npm fetch stats: "
        f"requests={_NPM_STATS['requests']} "
        f"cache_hits={_NPM_STATS['cache_hits']} "
        f"retries={_NPM_STATS['retries']} "
        f"rate_limits={_NPM_STATS['rate_limits']} "
        f"stale_uses={_NPM_STATS['stale_uses']}",
        file=sys.stderr,
    )
    return ranked


def _apply_npm_entries(ordered_entries, npm_metadata):
    candidates = sorted(
        npm_metadata.items(),
        key=lambda item: (
            -((item[1].get("popularity") or {}).get("downloads_per_30_days") or 0),
            item[0],
        ),
    )
    for package, metadata in candidates:
        executable = metadata.get("executable")
        if isinstance(executable, str) and executable:
            ordered_entries.setdefault(executable, f"npm:{package}")
    return ordered_entries


def _sorted_entries(entries):
    ordered = {}
    for executable in sorted(entries.keys()):
        items = entries[executable]
        items.sort(
            key=lambda item: (
                -(item.get("popularity") or 0),
                item.get("provider", ""),
            )
        )
        top = items[0]["provider"] if items else None
        if top:
            ordered[executable] = top
    return ordered


def _merge_entries(*groups):
    merged = {}
    for group in groups:
        for executable, items in group.items():
            merged.setdefault(executable, []).extend(items)
    return merged


def _validate_authority_db(db):
    if not isinstance(db, dict):
        raise ValueError("authority db must contain an object")
    schema = db.get("schema")
    if not isinstance(schema, int) or schema > SCHEMA_VERSION:
        raise ValueError(
            f"authority db schema {schema!r} is unsupported (maximum {SCHEMA_VERSION})"
        )
    entries = db.get("entries")
    formulas = db.get("formulas")
    casks = db.get("casks")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("authority db must contain executable entries")
    if not isinstance(formulas, dict) or not formulas:
        raise ValueError("authority db must contain formula metadata")
    if not isinstance(casks, dict) or not casks:
        raise ValueError("authority db must contain supported cask metadata")
    for executable, provider in entries.items():
        if not isinstance(executable, str) or not isinstance(provider, str):
            raise ValueError("authority db entries must map strings to strings")
        if provider.startswith("cask:") and provider[len("cask:") :] not in casks:
            raise ValueError(
                f"authority db entry {executable!r} points at missing cask {provider!r}"
            )
        if provider.startswith("cargo:"):
            crates = db.get("crates")
            if not isinstance(crates, dict) or provider[len("cargo:") :] not in crates:
                raise ValueError(
                    f"authority db entry {executable!r} points at missing crate {provider!r}"
                )
            continue
        if not provider.startswith(("cask:", "npm:")) and provider not in formulas:
            raise ValueError(
                f"authority db entry {executable!r} points at missing formula {provider!r}"
            )


def _load_authority_db(path=None):
    if path is None:
        path = AUTHORITY_DB_PATH
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        db = json.load(handle)
    _validate_authority_db(db)
    return db


def _collect_homebrew_authority_from_av_db(path=None):
    if path is None:
        path = AUTHORITY_DB_PATH
    db = _load_authority_db(path)
    if db is None:
        return None
    print(f"Using Homebrew authority from {path}", file=sys.stderr)
    return (
        dict(sorted(db["entries"].items())),
        dict(sorted(db["formulas"].items())),
        dict(sorted(db["casks"].items())),
        0,
    )


def _overlay_homebrew_pulse_metadata(formulas, casks):
    formula_paths = {
        _formula_source_path(name): name
        for name in formulas.keys()
        if isinstance(name, str) and name and "/" not in name
    }
    cask_paths = {
        _cask_source_path(token): token
        for token in casks.keys()
        if isinstance(token, str) and token and "/" not in token
    }
    formula_pulse_events = _git_pulse_events(
        HOMEWBREW_CORE_REPO,
        formula_paths,
        "Formula",
    )
    cask_pulse_events = _git_pulse_events(
        HOMEWBREW_CASK_REPO,
        cask_paths,
        "Casks",
    )

    formulas = {name: dict(metadata) for name, metadata in formulas.items()}
    for name, pulse_event in formula_pulse_events.items():
        if name not in formulas:
            continue
        formulas[name]["last_updated_at"] = pulse_event["last_updated_at"]
        formulas[name]["pulse_kind"] = pulse_event["pulse_kind"]

    casks = {token: dict(metadata) for token, metadata in casks.items()}
    for token, pulse_event in cask_pulse_events.items():
        if token not in casks:
            continue
        casks[token]["last_updated_at"] = pulse_event["last_updated_at"]
        casks[token]["pulse_kind"] = pulse_event["pulse_kind"]

    return formulas, casks


def _collect_homebrew_authority_legacy(github_token):
    formulae = _fetch_json(FORMULA_URL, github_token)
    if not isinstance(formulae, list):
        print("Formula list was not a list.", file=sys.stderr)
        sys.exit(2)
    cask_index = _fetch_json(CASKS_URL, github_token)
    if not isinstance(cask_index, list):
        print("Cask list was not a list.", file=sys.stderr)
        sys.exit(2)

    popularity_by_formula = {}
    popularity_by_cask = {}
    try:
        popularity_by_formula = _fetch_popularity(github_token)
    except Exception as err:
        print(f"Failed to fetch analytics data: {err}", file=sys.stderr)
    try:
        popularity_by_cask = _fetch_cask_popularity(github_token)
    except Exception as err:
        print(f"Failed to fetch cask analytics data: {err}", file=sys.stderr)

    manifest_urls = []
    for formula in formulae:
        if not isinstance(formula, dict):
            continue
        name = formula.get("name")
        if not name or "@" in name:
            continue
        url = _manifest_url(formula)
        if url:
            manifest_urls.append(url)

    cask_urls = []
    for entry in cask_index:
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        if token and _cask_metadata(entry) is not None:
            cask_urls.append(_cask_url(token))

    formula_paths = {}
    for formula in formulae:
        if not isinstance(formula, dict):
            continue
        name = formula.get("name")
        if not name:
            continue
        formula_paths[_formula_source_path(name)] = name

    cask_paths = {}
    for entry in cask_index:
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        if token and _cask_metadata(entry) is not None:
            cask_paths[_cask_source_path(token)] = token

    manifests = {}
    casks = {}
    completed = 0
    max_workers = min(32, (os.cpu_count() or 4) * 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        for url in manifest_urls:
            future_map[executor.submit(_fetch_json, url, github_token)] = (
                "manifest",
                url,
            )
        for url in cask_urls:
            future_map[executor.submit(_fetch_json, url, github_token)] = (
                "cask",
                url,
            )
        for future in as_completed(future_map):
            kind, key = future_map[future]
            try:
                payload = future.result()
            except Exception as err:
                print(f"Failed to fetch {key}: {err}", file=sys.stderr)
                continue
            if payload:
                if kind == "manifest":
                    manifests[key] = payload
                elif kind == "cask":
                    casks[key] = payload
            completed += 1
            if completed % 20 == 0:
                print(
                    "Fetched "
                    f"{completed}/"
                    f"{len(manifest_urls) + len(cask_urls)} "
                    "api payloads...",
                    file=sys.stderr,
                )

    formula_pulse_events = _git_pulse_events(
        HOMEWBREW_CORE_REPO,
        formula_paths,
        "Formula",
    )
    cask_pulse_events = _git_pulse_events(
        HOMEWBREW_CASK_REPO,
        cask_paths,
        "Casks",
    )

    formula_entries, formulas, missing_manifests = _collect_formula_entries(
        formulae,
        popularity_by_formula,
        manifests,
        formula_pulse_events,
    )
    cask_entries, cask_metadata = _collect_cask_entries(
        casks.values(),
        popularity_by_cask,
        cask_pulse_events,
    )
    if cask_urls and not cask_metadata:
        print(
            "No supported cask metadata was collected; refusing to write a "
            "database without casks.",
            file=sys.stderr,
        )
        sys.exit(2)
    return (
        _sorted_entries(_merge_entries(formula_entries, cask_entries)),
        formulas,
        cask_metadata,
        missing_manifests,
    )


def main():
    global FORCE_REFRESH, NPM_FULL_SCAN, NPM_FULL_SCAN_PARTS

    _ensure_cwd()

    for arg in sys.argv[1:]:
        if arg == "--refresh":
            FORCE_REFRESH = True
        elif arg == "--npm-full-scan":
            NPM_FULL_SCAN = True
        elif arg.startswith("--npm-full-scan-parts="):
            try:
                NPM_FULL_SCAN_PARTS = int(arg.split("=", 1)[1])
            except ValueError:
                NPM_FULL_SCAN_PARTS = 0
            if NPM_FULL_SCAN_PARTS < 1:
                print("--npm-full-scan-parts must be a positive integer", file=sys.stderr)
                sys.exit(2)
            NPM_FULL_SCAN = True
        elif arg in ("--help", "-h"):
            print(
                "Usage: scripts/build-db.py [--refresh] [--npm-full-scan] "
                "[--npm-full-scan-parts=N]"
            )
            print()
            print("Environment knobs:")
            print("  NPM_TOKEN, NODE_AUTH_TOKEN, NPM_REGISTRY_TOKEN")
            print("  NPM_REGISTRY_RPS, NPM_DOWNLOADS_RPS, NPM_MAX_RETRIES")
            print("  NPM_RATE_LIMIT_BUDGET_SECONDS, NPM_MAX_WORKERS")
            print("  NPM_FULL_SCAN_PAGE_SIZE, NPM_DOWNLOADS_BATCH_SIZE")
            print("  NPM_DOWNLOADS_BATCH_URL_MAX_LENGTH, NPM_CHANGE_REFRESH_LIMIT")
            print("  AV_DB_AUTHORITY_PATH")
            return
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            print(
                "Usage: scripts/build-db.py [--refresh] [--npm-full-scan] "
                "[--npm-full-scan-parts=N]",
                file=sys.stderr,
            )
            sys.exit(2)

    os.makedirs(os.path.join(CACHE_DIR, ECOSYSTEM), exist_ok=True)
    os.makedirs(os.path.join(CACHE_DIR, NPM_ECOSYSTEM), exist_ok=True)

    homebrew_authority = _collect_homebrew_authority_from_av_db()
    if homebrew_authority is None:
        homebrew_authority = _collect_homebrew_authority_legacy(_github_token())
    ordered_entries, formulas, cask_metadata, missing_manifests = homebrew_authority
    formulas = _include_versioned_formula_metadata(formulas)
    formulas, cask_metadata = _overlay_homebrew_popularity_metadata(
        formulas,
        cask_metadata,
        _github_token(),
    )
    formulas, cask_metadata = _overlay_homebrew_pulse_metadata(formulas, cask_metadata)
    formulas = _overlay_formula_package_manager_metadata(formulas)
    cask_metadata = _overlay_cask_package_manager_metadata(cask_metadata)
    try:
        npm_metadata = _collect_npm_metadata()
    except NpmFetchError as err:
        print(f"Failed to collect npm metadata: {err}", file=sys.stderr)
        sys.exit(2)
    ordered_entries = _apply_npm_entries(ordered_entries, npm_metadata)

    db = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "entries": ordered_entries,
        "formulas": formulas,
        "casks": cask_metadata,
        "npms": npm_metadata,
    }

    from bootstrap.lib.discovery import overlay
    overlay(db)
    _write_json_atomic(DB_PATH, db, indent=2)

    print(
        f"Wrote {DB_PATH} with {len(ordered_entries)} executables "
        f"and {len(formulas)} formulas, {len(cask_metadata)} casks, "
        f"{len(npm_metadata)} npm packages"
    )
    if missing_manifests:
        print(
            f"Skipped {missing_manifests} formulas missing cached manifests",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
