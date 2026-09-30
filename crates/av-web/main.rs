#![cfg_attr(test, recursion_limit = "256")]

mod project_history;

use rusqlite::{Connection, OpenFlags, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, OnceLock};
use std::thread;

const DEFAULT_BIND_ADDR: &str = "127.0.0.1:3004";
const DEFAULT_DB_PATH: &str = "/var/lib/automic-vault-web/pkg.sqlite";
const DEFAULT_PUBLIC_DB_PATH: &str = "/var/lib/automic-vault-web/db.json";
const DEFAULT_FEED_ROOT: &str = "/var/lib/automic-vault-web/feed/current";
const DEFAULT_ORIGIN_HEADER: &str = "x-automic-vault-origin";
const HTML_CACHE_CONTROL: &str = "public, max-age=300, s-maxage=300";
const FALLBACK_LAST_MODIFIED: &str = "Thu, 01 Jan 1970 00:00:00 GMT";
const DEFAULT_SEARCH_LIMIT: usize = 8;
const MAX_SEARCH_LIMIT: usize = 50;
const PKG_CSS: &str = include_str!("pkg.css");
const SEARCH_JS: &str = include_str!("search.js");
const I18N_PKG_TEMPLATES_JSON: &str = include_str!("../../data/pkg-i18n/templates.json");
static I18N_PKG_TEMPLATES: OnceLock<Value> = OnceLock::new();

fn main() {
    if let Err(err) = run() {
        eprintln!("av-web: {err}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), String> {
    let state = Arc::new(AppState::from_env());
    assert_database_ready(&state.db_path)?;
    let listener = TcpListener::bind(&state.bind_addr)
        .map_err(|err| format!("failed to bind {}: {err}", state.bind_addr))?;
    eprintln!("av-web listening on {}", state.bind_addr);

    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let state = Arc::clone(&state);
                thread::spawn(move || {
                    if let Err(err) = handle_connection(stream, &state) {
                        eprintln!("request failed: {err}");
                    }
                });
            }
            Err(err) => eprintln!("accept failed: {err}"),
        }
    }
    Ok(())
}

fn assert_database_ready(path: &Path) -> Result<(), String> {
    let connection = open_database(path)?;
    connection
        .query_row(
            "SELECT value FROM metadata WHERE key = 'schema'",
            [],
            |_row| Ok(()),
        )
        .map_err(|err| format!("database {} is not ready: {err}", path.display()))
}

#[derive(Debug)]
struct AppState {
    bind_addr: String,
    db_path: PathBuf,
    public_db_path: PathBuf,
    feed_root: PathBuf,
    origin_header: String,
    origin_secret: Option<String>,
}

impl AppState {
    fn from_env() -> Self {
        Self {
            bind_addr: env::var("AV_WEB_BIND_ADDR")
                .unwrap_or_else(|_| DEFAULT_BIND_ADDR.to_string()),
            db_path: env::var_os("AV_WEB_DB_PATH")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(DEFAULT_DB_PATH)),
            public_db_path: env::var_os("AV_WEB_PUBLIC_DB_PATH")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(DEFAULT_PUBLIC_DB_PATH)),
            feed_root: env::var_os("AV_WEB_FEED_ROOT")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(DEFAULT_FEED_ROOT)),
            origin_header: env::var("AV_WEB_ORIGIN_HEADER")
                .unwrap_or_else(|_| DEFAULT_ORIGIN_HEADER.to_string())
                .to_ascii_lowercase(),
            origin_secret: env::var("AV_WEB_ORIGIN_SECRET")
                .ok()
                .filter(|value| !value.is_empty()),
        }
    }
}

fn handle_connection(mut stream: TcpStream, state: &AppState) -> Result<(), String> {
    let request = match read_request(&stream)? {
        Some(request) => request,
        None => return Ok(()),
    };

    if request.path == "/healthz" {
        return write_response(
            &mut stream,
            &request.method,
            200,
            "OK",
            vec![
                ("content-type", "text/plain; charset=utf-8".to_string()),
                ("cache-control", "no-store".to_string()),
            ],
            b"ok\n".to_vec(),
        );
    }

    if !origin_request_authorized(
        &request.path,
        &request.headers,
        &state.origin_header,
        state.origin_secret.as_deref(),
    ) {
        return write_response(
            &mut stream,
            &request.method,
            403,
            "Forbidden",
            vec![
                ("content-type", "text/plain; charset=utf-8".to_string()),
                ("cache-control", "no-store".to_string()),
            ],
            b"forbidden\n".to_vec(),
        );
    }

    if request.method != "GET" && request.method != "HEAD" {
        return write_response(
            &mut stream,
            &request.method,
            405,
            "Method Not Allowed",
            vec![
                ("content-type", "text/plain; charset=utf-8".to_string()),
                ("allow", "GET, HEAD".to_string()),
                ("cache-control", "no-store".to_string()),
            ],
            b"method not allowed\n".to_vec(),
        );
    }

    if let Some(location) = legacy_pkg_redirect_location(&request.path, request.query.as_deref()) {
        let response = StoredResponse {
            content_type: "text/plain; charset=utf-8".to_string(),
            body: Vec::new(),
            etag: etag_for_bytes(location.as_bytes()),
            last_modified: FALLBACK_LAST_MODIFIED.to_string(),
            cache_control: HTML_CACHE_CONTROL.to_string(),
        };
        return write_stored_response(
            &mut stream,
            &request,
            301,
            "Moved Permanently",
            response,
            vec![("location", location)],
        );
    }

    if is_search_path(&request.path) {
        let query = parse_query(request.query.as_deref().unwrap_or(""));
        let body = search_response_json(&state.db_path, &request.path, &query)?;
        let response = stored_response_for_body(
            &state.db_path,
            "application/json; charset=utf-8",
            body,
            HTML_CACHE_CONTROL,
        )?;
        return write_stored_response(&mut stream, &request, 200, "OK", response, Vec::new());
    }

    if request.path == "/db.json" {
        let body = fs::read(&state.public_db_path)
            .map_err(|err| format!("failed to read {}: {err}", state.public_db_path.display()))?;
        let response = StoredResponse {
            content_type: "application/json; charset=utf-8".to_string(),
            etag: etag_for_bytes(&body),
            body,
            last_modified: FALLBACK_LAST_MODIFIED.to_string(),
            cache_control: HTML_CACHE_CONTROL.to_string(),
        };
        return write_stored_response(&mut stream, &request, 200, "OK", response, Vec::new());
    }

    if let Some(response) = discover_feed_response(&state.feed_root, &request.path)? {
        return write_stored_response(&mut stream, &request, 200, "OK", response, Vec::new());
    }

    if let Some(response) = static_asset_response(&request.path) {
        return write_stored_response(&mut stream, &request, 200, "OK", response, Vec::new());
    }

    if let Some(response) = dynamic_response_for_path(&state.db_path, &request.path)? {
        return write_stored_response(&mut stream, &request, 200, "OK", response, Vec::new());
    }

    let response = stored_response_for_body(
        &state.db_path,
        "text/plain; charset=utf-8",
        b"not found\n".to_vec(),
        HTML_CACHE_CONTROL,
    )?;
    write_stored_response(
        &mut stream,
        &request,
        404,
        "Not Found",
        response,
        Vec::new(),
    )
}

#[derive(Debug, PartialEq, Eq)]
struct Request {
    method: String,
    path: String,
    query: Option<String>,
    headers: BTreeMap<String, String>,
}

fn read_request(stream: &TcpStream) -> Result<Option<Request>, String> {
    let mut reader = BufReader::new(
        stream
            .try_clone()
            .map_err(|err| format!("failed to clone stream: {err}"))?,
    );
    let mut line = String::new();
    if reader
        .read_line(&mut line)
        .map_err(|err| format!("failed to read request line: {err}"))?
        == 0
    {
        return Ok(None);
    }
    let parts = line.split_whitespace().collect::<Vec<_>>();
    if parts.len() < 2 {
        return Err("invalid request line".to_string());
    }
    let method = parts[0].to_ascii_uppercase();
    let (path, query) = split_target(parts[1])?;
    let mut headers = BTreeMap::new();
    loop {
        line.clear();
        let bytes = reader
            .read_line(&mut line)
            .map_err(|err| format!("failed to read header: {err}"))?;
        if bytes == 0 || line == "\n" || line == "\r\n" {
            break;
        }
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        headers.insert(
            name.trim().to_ascii_lowercase(),
            value.trim().trim_end_matches('\r').to_string(),
        );
    }
    Ok(Some(Request {
        method,
        path,
        query,
        headers,
    }))
}

fn split_target(target: &str) -> Result<(String, Option<String>), String> {
    let target = target.split('#').next().unwrap_or(target);
    let (path, query) = target
        .split_once('?')
        .map(|(path, query)| (path, Some(query.to_string())))
        .unwrap_or((target, None));
    let decoded = urlencoding::decode(path)
        .map_err(|err| format!("invalid request path encoding: {err}"))?
        .into_owned();
    let path = if decoded.starts_with('/') {
        decoded
    } else {
        format!("/{decoded}")
    };
    Ok((path, query))
}

fn write_response(
    stream: &mut TcpStream,
    method: &str,
    status: u16,
    reason: &str,
    mut headers: Vec<(&'static str, String)>,
    body: Vec<u8>,
) -> Result<(), String> {
    if status != 304 {
        headers.push(("content-length", body.len().to_string()));
    }
    let mut response = format!("HTTP/1.1 {status} {reason}\r\n");
    for (name, value) in headers {
        response.push_str(name);
        response.push_str(": ");
        response.push_str(&value);
        response.push_str("\r\n");
    }
    response.push_str("\r\n");
    stream
        .write_all(response.as_bytes())
        .map_err(|err| format!("failed to write response headers: {err}"))?;
    if method != "HEAD" {
        stream
            .write_all(&body)
            .map_err(|err| format!("failed to write response body: {err}"))?;
    }
    stream
        .flush()
        .map_err(|err| format!("failed to flush response: {err}"))
}

fn write_stored_response(
    stream: &mut TcpStream,
    request: &Request,
    status: u16,
    reason: &str,
    response: StoredResponse,
    mut extra_headers: Vec<(&'static str, String)>,
) -> Result<(), String> {
    let not_modified =
        request_is_not_modified(&request.headers, &response.etag, &response.last_modified);
    extra_headers.extend(response.headers());
    if not_modified {
        return write_response(
            stream,
            &request.method,
            304,
            "Not Modified",
            extra_headers,
            Vec::new(),
        );
    }
    write_response(
        stream,
        &request.method,
        status,
        reason,
        extra_headers,
        response.body,
    )
}

fn request_is_not_modified(
    headers: &BTreeMap<String, String>,
    etag: &str,
    last_modified: &str,
) -> bool {
    if let Some(if_none_match) = headers.get("if-none-match") {
        return if_none_match.split(',').any(|candidate| {
            let candidate = candidate.trim();
            candidate == "*" || weak_etag(candidate) == weak_etag(etag)
        });
    }
    let Some(if_modified_since) = headers.get("if-modified-since") else {
        return false;
    };
    let Ok(since) = httpdate::parse_http_date(if_modified_since) else {
        return false;
    };
    httpdate::parse_http_date(last_modified).is_ok_and(|modified| modified <= since)
}

fn weak_etag(value: &str) -> &str {
    value
        .strip_prefix("W/")
        .or_else(|| value.strip_prefix("w/"))
        .unwrap_or(value)
}

fn origin_request_authorized(
    path: &str,
    headers: &BTreeMap<String, String>,
    header_name: &str,
    expected_secret: Option<&str>,
) -> bool {
    if path == "/healthz" {
        return true;
    }
    let Some(expected_secret) = expected_secret else {
        return true;
    };
    headers
        .get(&header_name.to_ascii_lowercase())
        .is_some_and(|value| constant_time_eq(value.as_bytes(), expected_secret.as_bytes()))
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let mut diff = left.len() ^ right.len();
    for index in 0..left.len().max(right.len()) {
        let left_byte = left.get(index).copied().unwrap_or(0);
        let right_byte = right.get(index).copied().unwrap_or(0);
        diff |= usize::from(left_byte ^ right_byte);
    }
    diff == 0
}

fn legacy_pkg_redirect_location(path: &str, query: Option<&str>) -> Option<String> {
    let base = flatten_legacy_pkg_path(path);
    if base == path {
        return None;
    }
    Some(match query {
        Some(query) if !query.is_empty() => format!("{base}?{query}"),
        _ => base,
    })
}

fn is_search_path(path: &str) -> bool {
    path == "/search.json"
        || path == "/de/search.json"
        || path == "/fr/search.json"
        || path == "/ja/search.json"
        || path == "/zh-hans/search.json"
}

fn path_has_extension(path: &str) -> bool {
    path.rsplit('/')
        .next()
        .is_some_and(|segment| segment.contains('.'))
}

#[derive(Debug, PartialEq, Eq)]
struct StoredResponse {
    content_type: String,
    body: Vec<u8>,
    etag: String,
    last_modified: String,
    cache_control: String,
}

impl StoredResponse {
    fn headers(&self) -> Vec<(&'static str, String)> {
        vec![
            ("content-type", self.content_type.clone()),
            ("cache-control", self.cache_control.clone()),
            ("etag", self.etag.clone()),
            ("last-modified", self.last_modified.clone()),
        ]
    }
}

fn open_database(path: &Path) -> Result<Connection, String> {
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|err| format!("failed to open {}: {err}", path.display()))
}

fn content_version(content: &[u8]) -> String {
    let hash = content.iter().fold(0xcbf29ce484222325_u64, |hash, byte| {
        (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3)
    });
    format!("{hash:016x}")
}

fn asset_version(content: &str) -> String {
    content_version(content.as_bytes())
}

fn etag_for_bytes(content: &[u8]) -> String {
    format!("\"{}\"", content_version(content))
}

fn static_asset_response(path: &str) -> Option<StoredResponse> {
    let (_locale, canonical_path) = canonical_pkg_route(path);
    let (content_type, content) = match canonical_path.as_str() {
        "/pkg/ads.txt" => ("text/plain; charset=utf-8", "google.com, pub-5218328608281545, DIRECT, f08c47fec0942fa0\n"),
        "/pkg/styles.css" => ("text/css; charset=utf-8", PKG_CSS),
        "/pkg/search.js" => ("application/javascript; charset=utf-8", SEARCH_JS),
        _ => return None,
    };
    Some(StoredResponse {
        content_type: content_type.to_string(),
        body: content.as_bytes().to_vec(),
        etag: etag_for_bytes(content.as_bytes()),
        last_modified: FALLBACK_LAST_MODIFIED.to_string(),
        cache_control: HTML_CACHE_CONTROL.to_string(),
    })
}

/// Returns a response only for the published feed documents and artwork that a
/// document in the release explicitly references.  This keeps the release
/// directory from becoming a general-purpose static file server.
fn discover_feed_response(feed_root: &Path, request_path: &str) -> Result<Option<StoredResponse>, String> {
    let Some(relative) = request_path.strip_prefix("/discover/feed/") else {
        return Ok(None);
    };
    let Some(relative) = safe_discover_feed_path(relative) else {
        return Ok(None);
    };
    let root = match fs::canonicalize(feed_root) {
        Ok(root) => root,
        Err(_) => return Ok(None),
    };
    let candidate = match fs::canonicalize(root.join(&relative)) {
        Ok(candidate) if candidate.starts_with(&root) => candidate,
        _ => return Ok(None),
    };
    if !candidate.is_file() || (relative.starts_with("assets/") && !feed_artwork_is_referenced(&root, &relative)?) {
        return Ok(None);
    }
    let body = fs::read(&candidate)
        .map_err(|err| format!("failed to read published Discover feed {}: {err}", candidate.display()))?;
    let metadata = fs::metadata(&candidate)
        .map_err(|err| format!("failed to stat published Discover feed {}: {err}", candidate.display()))?;
    let content_type = discover_feed_content_type(&relative.to_string_lossy())
        .expect("safe path has a content type");
    let last_modified = metadata.modified().ok().map(httpdate::fmt_http_date)
        .unwrap_or_else(|| FALLBACK_LAST_MODIFIED.to_string());
    Ok(Some(StoredResponse {
        content_type: content_type.to_string(),
        etag: etag_for_bytes(&body),
        body,
        last_modified,
        cache_control: HTML_CACHE_CONTROL.to_string(),
    }))
}

fn safe_discover_feed_path(path: &str) -> Option<PathBuf> {
    if path == "v1.json" || path == "v2.json" {
        return Some(PathBuf::from(path));
    }
    if let Some(name) = path.strip_prefix("v2/pages/") {
        if is_safe_feed_filename(name) && name.ends_with(".json") {
            return Some(PathBuf::from("v2/pages").join(name));
        }
    }
    if let Some(name) = path.strip_prefix("assets/") {
        if is_safe_feed_filename(name) && discover_feed_content_type(name).is_some() {
            return Some(PathBuf::from("assets").join(name));
        }
    }
    None
}

fn is_safe_feed_filename(name: &str) -> bool {
    !name.is_empty() && !name.contains('/') && !name.contains('\\') && !name.contains("..")
}

fn discover_feed_content_type(path: &str) -> Option<&'static str> {
    if path.ends_with(".json") {
        Some("application/json; charset=utf-8")
    } else if path.ends_with(".png") {
        Some("image/png")
    } else if path.ends_with(".jpg") || path.ends_with(".jpeg") {
        Some("image/jpeg")
    } else if path.ends_with(".webp") {
        Some("image/webp")
    } else if path.ends_with(".avif") {
        Some("image/avif")
    } else {
        None
    }
}

fn feed_artwork_is_referenced(root: &Path, relative: &Path) -> Result<bool, String> {
    let expected = format!("feed/{}", relative.display());
    for document in feed_json_documents(root)? {
        let raw = fs::read_to_string(&document)
            .map_err(|err| format!("failed to read feed document {}: {err}", document.display()))?;
        let value: Value = serde_json::from_str(&raw)
            .map_err(|err| format!("invalid feed document {}: {err}", document.display()))?;
        if value_contains_string(&value, &expected) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn feed_json_documents(root: &Path) -> Result<Vec<PathBuf>, String> {
    let mut documents = vec![root.join("v1.json"), root.join("v2.json")];
    let pages = root.join("v2/pages");
    if let Ok(entries) = fs::read_dir(pages) {
        for entry in entries {
            let entry = entry.map_err(|err| format!("failed to enumerate feed pages: {err}"))?;
            let path = entry.path();
            if path.extension().is_some_and(|extension| extension == "json") && path.is_file() {
                documents.push(path);
            }
        }
    }
    Ok(documents)
}

fn value_contains_string(value: &Value, expected: &str) -> bool {
    match value {
        Value::String(value) => value == expected,
        Value::Array(values) => values.iter().any(|value| value_contains_string(value, expected)),
        Value::Object(values) => values.values().any(|value| value_contains_string(value, expected)),
        _ => false,
    }
}

#[derive(Debug, Clone)]
struct Locale {
    code: &'static str,
    prefix: &'static str,
    hreflang: &'static str,
}

const LOCALES: &[Locale] = &[
    Locale {
        code: "en",
        prefix: "",
        hreflang: "en",
    },
    Locale {
        code: "de",
        prefix: "/de",
        hreflang: "de",
    },
    Locale {
        code: "fr",
        prefix: "/fr",
        hreflang: "fr",
    },
    Locale {
        code: "ja",
        prefix: "/ja",
        hreflang: "ja",
    },
    Locale {
        code: "zh-Hans",
        prefix: "/zh-hans",
        hreflang: "zh-Hans",
    },
];

const SITE_ORIGIN: &str = "https://pkg.so";
const SITE_NAME: &str = "pkg.so";
const PROVIDERS: &[&str] = &["brew", "cask", "npm", "pip", "cargo"];

#[derive(Debug, Clone, Default, Deserialize)]
struct PackageData {
    #[serde(default)]
    full: Value,
}

#[derive(Debug, Clone)]
struct PackageRow {
    path: String,
    provider: String,
    slug: String,
    package_key: String,
    name: String,
    display_name: String,
    summary: String,
    provider_label: String,
    package_manager_url: String,
    install_command: String,
    native_install_command: String,
    version: String,
    category: String,
    license: String,
    homepage: String,
    repository: String,
    rank: Option<u32>,
    last_updated_at: String,
    indexable: bool,
    data: PackageData,
}

#[derive(Debug, Clone)]
struct HubRow {
    path: String,
    slug: String,
    title: String,
    description: String,
    group: String,
}

fn dynamic_response_for_path(db_path: &Path, path: &str) -> Result<Option<StoredResponse>, String> {
    if path == "/privacy/" {
        let connection = open_database(db_path)?;
        let locale = &LOCALES[0];
        let body = format!("{}{}{}", site_nav(locale), include_str!("privacy.html"), site_footer(locale));
        return Ok(Some(dynamic_stored_response(
            &connection, path, "text/html; charset=utf-8",
            html_doc(locale, "Privacy | pkg.so", "How pkg.so uses analytics, advertising, and cookies.",
                "https://pkg.so/privacy/", "index,follow", "", "{}", &body, ""),
        )?));
    }
    if path == "/robots.txt" {
        let connection = open_database(db_path)?;
        return Ok(Some(dynamic_stored_response(
            &connection,
            path,
            "text/plain; charset=utf-8",
            format!("User-agent: *\nAllow: /\nSitemap: {SITE_ORIGIN}/sitemap.xml\n"),
        )?));
    }
    if path == "/sitemap.xml" {
        let connection = open_database(db_path)?;
        let body = render_sitemap_index(&connection)?;
        return Ok(Some(dynamic_stored_response(
            &connection,
            path,
            "application/xml; charset=utf-8",
            body,
        )?));
    }
    // Only explicit frozen English history routes exist; no ecosystem/localized fan-out.
    if path.ends_with("/history/") || path == "/sitemap-history.xml" {
        let connection = open_database(db_path)?;
        if let Some((body, content_type)) = project_history::response(&connection, path)? {
            return Ok(Some(dynamic_stored_response(&connection, path, content_type, body)?));
        }
        return Ok(None);
    }
    if let Some(locale) = landing_locale(path) {
        let connection = open_database(db_path)?;
        return Ok(Some(dynamic_stored_response(
            &connection,
            path,
            "text/html; charset=utf-8",
            render_index_page(&connection, locale)?,
        )?));
    }
    if let Some(locale) = feed_locale(path) {
        let connection = open_database(db_path)?;
        return Ok(Some(dynamic_stored_response(
            &connection,
            path,
            "text/html; charset=utf-8",
            render_feed_page(locale),
        )?));
    }
    let (locale, canonical_path) = canonical_pkg_route(path);
    if !canonical_path.starts_with("/pkg/") && canonical_path != "/pkg/index.html" {
        return Ok(None);
    }
    let connection = open_database(db_path)?;
    let body_and_type = if canonical_path == "/pkg/index.html" {
        Some((
            render_index_page(&connection, locale)?,
            "text/html; charset=utf-8",
        ))
    } else if canonical_path == "/pkg/sitemap.xml" {
        Some((
            render_sitemap_index(&connection)?,
            "application/xml; charset=utf-8",
        ))
    } else if canonical_path == "/pkg/sitemap-hubs.xml" {
        Some((
            render_hub_sitemap(&connection)?,
            "application/xml; charset=utf-8",
        ))
    } else if let Some(provider) = sitemap_provider(&canonical_path) {
        Some((
            render_provider_sitemap(&connection, provider)?,
            "application/xml; charset=utf-8",
        ))
    } else if canonical_path == "/pkg/.manifest.json" {
        let manifest = metadata_json(&connection, "manifest")?.unwrap_or_else(|| json!({}));
        Some((
            serde_json::to_string_pretty(&manifest)
                .map_err(|err| format!("failed to encode manifest json: {err}"))?,
            "application/json; charset=utf-8",
        ))
    } else if canonical_path == "/pkg/new.json" {
        Some((
            render_new_packages_json(&connection)?,
            "application/json; charset=utf-8",
        ))
    } else if let Some((provider, slug)) = package_json_route(&canonical_path) {
        let Some(package) = package_by_provider_slug(&connection, provider, slug)? else {
            return Ok(None);
        };
        Some((
            render_package_json(&package)?,
            "application/json; charset=utf-8",
        ))
    } else if let Some((provider, slug, markdown)) = package_route(&canonical_path) {
        let Some(package) = package_by_provider_slug(&connection, provider, slug)? else {
            return Ok(None);
        };
        if markdown {
            if !package.indexable {
                return Ok(None);
            }
            let generated_at = metadata_string(&connection, "generated_at")?.unwrap_or_default();
            Some((
                render_package_markdown(&package, locale, &generated_at),
                "text/markdown; charset=utf-8",
            ))
        } else {
            let generated_at = metadata_string(&connection, "generated_at")?.unwrap_or_default();
            Some((
                render_package_page(&package, locale, &generated_at),
                "text/html; charset=utf-8",
            ))
        }
    } else if let Some((slug, markdown)) = hub_route(&canonical_path) {
        let Some(hub) = hub_by_slug(&connection, slug)? else {
            return Ok(None);
        };
        if markdown {
            Some((
                render_hub_markdown(&connection, &hub, locale)?,
                "text/markdown; charset=utf-8",
            ))
        } else {
            Some((
                render_hub_page(&connection, &hub, locale)?,
                "text/html; charset=utf-8",
            ))
        }
    } else {
        None
    };

    let Some((body, content_type)) = body_and_type else {
        return Ok(None);
    };
    Ok(Some(dynamic_stored_response(
        &connection,
        &canonical_path,
        content_type,
        body,
    )?))
}

fn landing_locale(path: &str) -> Option<&'static Locale> {
    LOCALES
        .iter()
        .find(|locale| path == locale_path("/", locale))
}

fn feed_locale(path: &str) -> Option<&'static Locale> {
    LOCALES
        .iter()
        .find(|locale| path == locale_path("/feed/", locale))
}

fn canonical_pkg_route(path: &str) -> (&'static Locale, String) {
    let mut locale = &LOCALES[0];
    let mut route = path.to_string();
    for candidate in LOCALES.iter().skip(1) {
        if path == candidate.prefix || path == format!("{}/", candidate.prefix) {
            locale = candidate;
            route = "/".to_string();
            break;
        }
        if let Some(rest) = path.strip_prefix(&format!("{}/", candidate.prefix)) {
            locale = candidate;
            route = format!("/{rest}");
            break;
        }
    }
    let mut canonical = if route == "/pkg" || route.starts_with("/pkg/") {
        route
    } else {
        format!("/pkg{route}")
    };
    if canonical == "/pkg" {
        canonical = "/pkg/index.html".to_string();
    } else if canonical.ends_with('/') {
        canonical.push_str("index.html");
    } else if !path_has_extension(&canonical) {
        canonical.push_str("/index.html");
    }
    (locale, canonical)
}

fn package_route(path: &str) -> Option<(&str, &str, bool)> {
    let rest = path.strip_prefix("/pkg/")?;
    let parts = rest.split('/').collect::<Vec<_>>();
    match parts.as_slice() {
        [provider, slug, "index.html"] if PROVIDERS.contains(provider) => {
            Some((provider, slug, false))
        }
        [provider, slug, "index.md"] if PROVIDERS.contains(provider) => {
            Some((provider, slug, true))
        }
        _ => None,
    }
}

fn package_json_route(path: &str) -> Option<(&str, &str)> {
    let rest = path.strip_prefix("/pkg/")?;
    let (provider, slug) = rest.split_once('/')?;
    let slug = slug.strip_suffix(".json")?;
    (PROVIDERS.contains(&provider) && !slug.contains('/')).then_some((provider, slug))
}

fn hub_route(path: &str) -> Option<(&str, bool)> {
    let rest = path.strip_prefix("/pkg/")?;
    let parts = rest.split('/').collect::<Vec<_>>();
    match parts.as_slice() {
        [slug, "index.html"] if !PROVIDERS.contains(slug) => Some((slug, false)),
        [slug, "index.md"] if !PROVIDERS.contains(slug) => Some((slug, true)),
        _ => None,
    }
}

fn sitemap_provider(path: &str) -> Option<&str> {
    let provider = path.strip_prefix("/pkg/sitemap-")?.strip_suffix(".xml")?;
    PROVIDERS.contains(&provider).then_some(provider)
}

fn dynamic_stored_response(
    connection: &Connection,
    _path: &str,
    content_type: &str,
    body: String,
) -> Result<StoredResponse, String> {
    let last_modified = metadata_string(connection, "last_modified")?
        .unwrap_or_else(|| FALLBACK_LAST_MODIFIED.to_string());
    let body = body.into_bytes();
    Ok(StoredResponse {
        content_type: content_type.to_string(),
        etag: etag_for_bytes(&body),
        body,
        last_modified,
        cache_control: HTML_CACHE_CONTROL.to_string(),
    })
}

fn stored_response_for_body(
    db_path: &Path,
    content_type: &str,
    body: Vec<u8>,
    cache_control: &str,
) -> Result<StoredResponse, String> {
    let connection = open_database(db_path)?;
    let last_modified = metadata_string(&connection, "last_modified")?
        .unwrap_or_else(|| FALLBACK_LAST_MODIFIED.to_string());
    Ok(StoredResponse {
        content_type: content_type.to_string(),
        etag: etag_for_bytes(&body),
        body,
        last_modified,
        cache_control: cache_control.to_string(),
    })
}

fn metadata_string(connection: &Connection, key: &str) -> Result<Option<String>, String> {
    let value = connection
        .query_row(
            "SELECT value FROM metadata WHERE key = ?1",
            params![key],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|err| format!("failed to read metadata {key}: {err}"))?;
    Ok(value.map(|value| serde_json::from_str::<String>(&value).unwrap_or(value)))
}

fn package_by_provider_slug(
    connection: &Connection,
    provider: &str,
    slug: &str,
) -> Result<Option<PackageRow>, String> {
    connection
        .query_row(
            "SELECT path, provider, slug, package_key, name, display_name, summary,
                    provider_label, package_manager_url, install_command, native_install_command,
                    version, category, license, homepage, repository, rank, last_updated_at,
                    indexable, data_json
             FROM packages
             WHERE provider = ?1 AND slug = ?2",
            params![provider, slug],
            package_from_row,
        )
        .optional()
        .map_err(|err| format!("failed to query package {provider}/{slug}: {err}"))
}

fn package_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<PackageRow> {
    let data_json: String = row.get(19)?;
    let data = serde_json::from_str(&data_json).unwrap_or_default();
    Ok(PackageRow {
        path: row.get(0)?,
        provider: row.get(1)?,
        slug: row.get(2)?,
        package_key: row.get(3)?,
        name: row.get(4)?,
        display_name: row.get(5)?,
        summary: row.get(6)?,
        provider_label: row.get(7)?,
        package_manager_url: row.get(8)?,
        install_command: row.get(9)?,
        native_install_command: row.get(10)?,
        version: row.get(11)?,
        category: row.get(12)?,
        license: row.get(13)?,
        homepage: row.get(14)?,
        repository: row.get(15)?,
        rank: row.get(16)?,
        last_updated_at: row.get(17)?,
        indexable: row.get::<_, i64>(18)? != 0,
        data,
    })
}

fn hub_by_slug(connection: &Connection, slug: &str) -> Result<Option<HubRow>, String> {
    connection
        .query_row(
            "SELECT path, slug, title, description, group_name FROM hubs WHERE slug = ?1",
            params![slug],
            |row| {
                Ok(HubRow {
                    path: row.get(0)?,
                    slug: row.get(1)?,
                    title: row.get(2)?,
                    description: row.get(3)?,
                    group: row.get(4)?,
                })
            },
        )
        .optional()
        .map_err(|err| format!("failed to query hub {slug}: {err}"))
}

fn hub_signal_condition(signal: &str) -> Option<&'static str> {
    match signal {
        "isotope" => {
            Some("p.data_json LIKE '%\"isotope\":{%' AND p.data_json NOT LIKE '%\"isotope\":null%'")
        }
        "approval_gate" => Some(
            "p.data_json LIKE '%\"approvalGate\":{%' AND p.data_json NOT LIKE '%\"approvalGate\":null%'",
        ),
        "non_low_geiger" => Some(
            "p.data_json LIKE '%\"geiger\":{%'
                AND p.data_json NOT LIKE '%\"level\":\"low\"%'
                AND p.data_json NOT LIKE '%\"level\":\"green\"%'
                AND p.data_json NOT LIKE '%\"level\":\"unknown\"%'",
        ),
        _ => None,
    }
}

fn hub_packages_query(
    connection: &Connection,
    slug: &str,
    limit: usize,
    signal: Option<&str>,
    order_by: &str,
) -> Result<Vec<(PackageRow, String)>, String> {
    let condition = signal
        .and_then(hub_signal_condition)
        .map(|condition| format!(" AND {condition}"))
        .unwrap_or_default();
    let order_by = match order_by {
        "rank" => "p.rank IS NULL, p.rank, p.display_name",
        _ => "hp.position",
    };
    let query = format!(
        "SELECT p.path, p.provider, p.slug, p.package_key, p.name, p.display_name, p.summary,
                p.provider_label, p.package_manager_url, p.install_command, p.native_install_command,
                p.version, p.category, p.license, p.homepage, p.repository, p.rank,
                p.last_updated_at, p.indexable, p.data_json, hp.reason
         FROM hub_packages hp
         JOIN packages p ON p.package_key = hp.package_key
         WHERE hp.hub_slug = ?1{condition}
         ORDER BY {order_by}
         LIMIT ?2"
    );
    let mut statement = connection
        .prepare(&query)
        .map_err(|err| format!("failed to prepare hub package query: {err}"))?;
    let rows = statement
        .query_map(params![slug, limit as i64], |row| {
            Ok((package_from_row(row)?, row.get::<_, String>(20)?))
        })
        .map_err(|err| format!("failed to query hub packages: {err}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("failed to read hub packages: {err}"))
}

#[derive(Debug, Clone, Copy)]
struct HubStats {
    package_count: i64,
    secured_count: i64,
    gated_count: i64,
    risked_count: i64,
}

fn hub_stats(connection: &Connection, slug: &str) -> Result<HubStats, String> {
    connection
        .query_row(
            "SELECT COUNT(*),
                    COALESCE(SUM(CASE WHEN p.data_json LIKE '%\"isotope\":{%'
                                         AND p.data_json NOT LIKE '%\"isotope\":null%'
                                      THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN p.data_json LIKE '%\"approvalGate\":{%'
                                         AND p.data_json NOT LIKE '%\"approvalGate\":null%'
                                      THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN p.data_json LIKE '%\"geiger\":{%'
                                         AND p.data_json NOT LIKE '%\"level\":\"low\"%'
                                         AND p.data_json NOT LIKE '%\"level\":\"green\"%'
                                         AND p.data_json NOT LIKE '%\"level\":\"unknown\"%'
                                      THEN 1 ELSE 0 END), 0)
             FROM hub_packages hp
             JOIN packages p ON p.package_key = hp.package_key
             WHERE hp.hub_slug = ?1",
            params![slug],
            |row| {
                Ok(HubStats {
                    package_count: row.get(0)?,
                    secured_count: row.get(1)?,
                    gated_count: row.get(2)?,
                    risked_count: row.get(3)?,
                })
            },
        )
        .map_err(|err| format!("failed to query hub stats: {err}"))
}

fn hubs(connection: &Connection) -> Result<Vec<HubRow>, String> {
    let mut statement = connection
        .prepare("SELECT path, slug, title, description, group_name FROM hubs ORDER BY group_name, title")
        .map_err(|err| format!("failed to prepare hubs query: {err}"))?;
    let rows = statement
        .query_map([], |row| {
            Ok(HubRow {
                path: row.get(0)?,
                slug: row.get(1)?,
                title: row.get(2)?,
                description: row.get(3)?,
                group: row.get(4)?,
            })
        })
        .map_err(|err| format!("failed to query hubs: {err}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("failed to read hubs: {err}"))
}

fn top_packages(connection: &Connection, limit: usize) -> Result<Vec<PackageRow>, String> {
    let mut statement = connection
        .prepare(
            "SELECT path, provider, slug, package_key, name, display_name, summary,
                    provider_label, package_manager_url, install_command, native_install_command,
                    version, category, license, homepage, repository, rank, last_updated_at,
                    indexable, data_json
             FROM packages
             ORDER BY rank IS NULL, rank, display_name
             LIMIT ?1",
        )
        .map_err(|err| format!("failed to prepare top packages query: {err}"))?;
    collect_packages(statement.query_map(params![limit as i64], package_from_row))
}

fn package_count(connection: &Connection) -> Result<i64, String> {
    connection
        .query_row("SELECT COUNT(*) FROM packages", [], |row| row.get(0))
        .map_err(|err| format!("failed to count packages: {err}"))
}

fn approval_gate_count(connection: &Connection) -> Result<i64, String> {
    connection
        .query_row(
            "SELECT COUNT(*)
             FROM packages
             WHERE data_json LIKE '%\"approvalGate\":%'
               AND data_json NOT LIKE '%\"approvalGate\":null%'",
            [],
            |row| row.get(0),
        )
        .map_err(|err| format!("failed to count approval-gate packages: {err}"))
}

#[derive(Debug, Clone)]
struct HubSummary {
    hub: HubRow,
    package_count: i64,
}

fn hub_summaries(connection: &Connection) -> Result<Vec<HubSummary>, String> {
    let mut statement = connection
        .prepare(
            "SELECT h.path, h.slug, h.title, h.description, h.group_name, COUNT(hp.package_key)
             FROM hubs h
             LEFT JOIN hub_packages hp ON hp.hub_slug = h.slug
             GROUP BY h.path, h.slug, h.title, h.description, h.group_name
             ORDER BY h.group_name, COUNT(hp.package_key) DESC, h.title",
        )
        .map_err(|err| format!("failed to prepare hub summaries query: {err}"))?;
    let rows = statement
        .query_map([], |row| {
            Ok(HubSummary {
                hub: HubRow {
                    path: row.get(0)?,
                    slug: row.get(1)?,
                    title: row.get(2)?,
                    description: row.get(3)?,
                    group: row.get(4)?,
                },
                package_count: row.get(5)?,
            })
        })
        .map_err(|err| format!("failed to query hub summaries: {err}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("failed to read hub summaries: {err}"))
}

fn metadata_json(connection: &Connection, key: &str) -> Result<Option<Value>, String> {
    let Some(value) = metadata_string(connection, key)? else {
        return Ok(None);
    };
    Ok(serde_json::from_str(&value).ok())
}

fn collect_packages<F>(
    rows: rusqlite::Result<rusqlite::MappedRows<'_, F>>,
) -> Result<Vec<PackageRow>, String>
where
    F: FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<PackageRow>,
{
    rows.map_err(|err| format!("failed to query packages: {err}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("failed to read packages: {err}"))
}

fn package_json_value(package: &PackageRow) -> Value {
    json!({
        "path": public_catalog_path(&package.path),
        "provider": package.provider,
        "slug": package.slug,
        "packageKey": package.package_key,
        "name": package.name,
        "displayName": package.display_name,
        "summary": package.summary,
        "providerLabel": package.provider_label,
        "packageManagerUrl": package.package_manager_url,
        "installCommand": package.install_command,
        "nativeInstallCommand": package.native_install_command,
        "version": package.version,
        "category": package.category,
        "license": package.license,
        "homepage": package.homepage,
        "repository": package.repository,
        "rank": package.rank,
        "lastUpdatedAt": package.last_updated_at,
        "indexable": package.indexable,
        "data": package.data.full,
    })
}

fn render_package_json(package: &PackageRow) -> Result<String, String> {
    serde_json::to_string(&package_json_value(package))
        .map_err(|err| format!("failed to encode package json: {err}"))
}

fn render_new_packages_json(connection: &Connection) -> Result<String, String> {
    let generated_at =
        metadata_string(connection, "generated_at")?.unwrap_or_else(|| "now".to_string());
    let mut statement = connection
        .prepare(
            "SELECT path, provider, slug, package_key, name, display_name, summary,
                    provider_label, package_manager_url, install_command, native_install_command,
                    version, category, license, homepage, repository, rank, last_updated_at,
                    indexable, data_json
             FROM packages
             WHERE date(last_updated_at) >= date(?1, '-7 days')
               AND date(last_updated_at) <= date(?1)
             ORDER BY date(last_updated_at) DESC, rank IS NULL, rank, display_name",
        )
        .map_err(|err| format!("failed to prepare new packages query: {err}"))?;
    let packages = collect_packages(statement.query_map(params![generated_at], package_from_row))?;
    let values = packages.iter().map(package_json_value).collect::<Vec<_>>();
    serde_json::to_string(&values)
        .map_err(|err| format!("failed to encode new packages json: {err}"))
}

fn render_feed_page(locale: &Locale) -> String {
    let title = tx(locale, "dataFeedTitle", "pkg.so JSON feeds");
    let new_feed = locale_path("/pkg/new.json", locale);
    let manifest = locale_path("/pkg/.manifest.json", locale);
    let search = format!("{}?q=awscli", locale_path("/pkg/search.json", locale));
    let package_example = locale_path("/pkg/brew/awscli.json", locale);
    let database = "/db.json";
    let mut body = String::new();
    body.push_str(&site_nav(locale));
    body.push_str("<main>");
    body.push_str(&format!(
        r#"<section class="pkg-hero pkg-hero-index" aria-labelledby="data-title"><div class="hero-copy"><p class="eyebrow">{}</p><h1 id="data-title">{}</h1><p class="lede">{}</p></div></section>"#,
        html_escape(&tx(locale, "dataFeedKicker", "free package data")),
        html_escape(&title),
        html_escape(&tx(locale, "dataFeedCopy", "All public pkg.so JSON endpoints are free for anyone to use. Browse the catalog feeds below, search programmatically, or add .json to a package URL for its complete record.")),
    ));
    body.push_str(&format!(
        r#"<section class="pkg-section split-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p></div><div class="detail-stack"><article><h3><a href="{}">{}</a></h3><p>{}</p><code>{}</code></article><article><h3><a href="{}">{}</a></h3><p>{}</p><code>{}</code></article><article><h3><a href="{}">{}</a></h3><p>{}</p><code>{}</code></article><article><h3><a href="{}">{}</a></h3><p>{}</p><code>{}</code></article></div></section>"#,
        html_escape(&tx(locale, "jsonEndpointsKicker", "JSON endpoints")),
        html_escape(&tx(locale, "catalogFeedsTitle", "Catalog-level feeds")),
        html_escape(&tx(locale, "catalogFeedsCopy", "These endpoints expose recent additions, build metadata, and searchable package records.")),
        html_escape(&new_feed), html_escape(&tx(locale, "newPackagesFeed", "New packages")),
        html_escape(&tx(locale, "newPackagesFeedCopy", "Packages added or updated during the latest seven-day window.")), html_escape(&new_feed),
        html_escape(&manifest), html_escape(&tx(locale, "manifestFeed", "Build manifest")),
        html_escape(&tx(locale, "manifestFeedCopy", "Catalog counts, generation timestamps, source hashes, and sitemap totals.")), html_escape(&manifest),
        html_escape(&search), html_escape(&tx(locale, "searchFeed", "Search API")),
        html_escape(&tx(locale, "searchFeedCopy", "Search package titles, keys, summaries, and indexed metadata. Use q, limit, and offset query parameters.")), html_escape(&search),
        html_escape(database), html_escape(&tx(locale, "databaseFeed", "Complete database export")),
        html_escape(&tx(locale, "databaseFeedCopy", "The complete canonical package metadata database as a single JSON document.")), html_escape(database),
    ));
    body.push_str(&format!(
        r#"<section class="pkg-section split-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p></div><div class="detail-stack"><article><h3>{}</h3><p>{}</p><code>{}</code><p><a href="{}">{}</a></p></article><article><h3>{}</h3><p>{}</p><code>application/json</code></article></div></section>"#,
        html_escape(&tx(locale, "packageJsonKicker", "per-package JSON")),
        html_escape(&tx(locale, "packageJsonTitle", "Add .json to a package URL")),
        html_escape(&tx(locale, "packageJsonCopy", "Every package page has a JSON representation containing its normalized fields and full source-backed data object.")),
        html_escape(&tx(locale, "packageJsonExampleTitle", "Example")),
        html_escape(&tx(locale, "packageJsonExampleCopy", "Replace the trailing slash with .json.")),
        html_escape(&package_example), html_escape(&package_example),
        html_escape(&tx(locale, "openExampleJson", "Open example JSON")),
        html_escape(&tx(locale, "contentType", "Content type")),
        html_escape(&tx(locale, "packageJsonContentTypeCopy", "JSON endpoints return UTF-8 JSON and use the same cache validators as the corresponding catalog data.")),
    ));
    body.push_str(&format!(
        r#"<section class="pkg-section split-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p></div><div class="detail-stack"><article><h3>{}</h3><p>{}</p></article><article><h3>{}</h3><p>{}</p></article></div></section></main>"#,
        html_escape(&tx(locale, "feedTermsKicker", "terms of use")),
        html_escape(&tx(locale, "feedTermsTitle", "Free to use, with abuse prevention")),
        html_escape(&tx(locale, "feedTermsCopy", "Anyone may use, copy, and redistribute data from these public JSON endpoints free of charge. Attribution is appreciated but not required.")),
        html_escape(&tx(locale, "feedAbuseTitle", "Abuse prevention")),
        html_escape(&tx(locale, "feedAbuseCopy", "We reserve the right to rate-limit, block, or take other action against traffic that disrupts the service, imposes unreasonable load, attempts unauthorized access, or otherwise abuses these endpoints.")),
        html_escape(&tx(locale, "feedAvailabilityTitle", "Availability")),
        html_escape(&tx(locale, "feedAvailabilityCopy", "The feeds are provided as-is without a service-level guarantee. Endpoint formats may evolve; clients should tolerate additional JSON fields.")),
    ));
    body.push_str(&site_footer(locale));
    let schema = json!({
        "@context": "https://schema.org", "@type": "TechArticle", "name": title,
        "url": locale_url("/feed/", locale), "inLanguage": locale.hreflang,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": format!("{SITE_ORIGIN}/")}
    });
    let schema_json = serde_json::to_string_pretty(&schema).unwrap_or_else(|_| "{}".to_string());
    html_doc(
        locale,
        &format!(
            "{} | {}",
            tx(locale, "dataFeedTitle", "Package data and JSON feeds"),
            SITE_NAME
        ),
        &tx(
            locale,
            "dataFeedDescription",
            "JSON feeds and per-package JSON records for the pkg.so package catalog.",
        ),
        &locale_url("/feed/", locale),
        "index,follow",
        "",
        &schema_json,
        &body,
        "",
    )
}

fn render_index_page(connection: &Connection, locale: &Locale) -> Result<String, String> {
    let hub_summaries = hub_summaries(connection)?;
    let top_packages = top_packages(connection, 72)?;
    let manifest = metadata_json(connection, "manifest")?;
    let package_total = if let Some(count) = manifest
        .as_ref()
        .and_then(|manifest| value_i64_key(manifest, "package_count"))
    {
        count
    } else {
        package_count(connection)?
    };
    let source_files = manifest
        .as_ref()
        .and_then(|manifest| value_i64_key(manifest, "source_file_count"))
        .unwrap_or_default();
    let search_endpoint = locale_path("/pkg/search.json", locale);
    let catalog_title = tx(locale, "packageCatalogTitle", "Package catalog");
    let mut body = String::new();
    body.push_str(&site_nav(locale));
    body.push_str("<main>");
    body.push_str(&format!(
        r#"<section class="pkg-hero pkg-hero-index" aria-labelledby="pkg-title"><div class="hero-copy"><p class="eyebrow">{}</p><h1 id="pkg-title">{}</h1><p class="lede">{}</p></div><aside class="hero-panel" aria-label="{}">{}{}{}</aside></section>"#,
        html_escape(&tx(locale, "catalogEyebrow", "pkg.so package intelligence")),
        html_escape(&catalog_title),
        html_escape(&tx(locale, "catalogPagesCopy", "Generated pages for executable packages in the pkg.so catalog, with install routes, popularity, executable aliases, and upstream package facts.")),
        html_escape(&tx(locale, "catalogCounts", "Catalog counts")),
        metric(&tx(locale, "packages", "packages"), &fmt_int(package_total)),
        metric(&tx(locale, "packageHubs", "package hubs"), &fmt_int(hub_summaries.len() as i64)),
        metric(&tx(locale, "sourceFiles", "source files"), &fmt_int(source_files)),
    ));
    body.push_str(&format!(
        r#"<section class="pkg-section pkg-search-section" aria-labelledby="pkg-search-title"><div class="search-copy"><p class="section-kicker">{}</p><h2 id="pkg-search-title">{}</h2><p>{}</p></div><div id="search" class="pkg-search" data-av-package-search data-locale="{}" data-search-endpoint="{}" data-placeholder="{}"></div></section>"#,
        html_escape(&tx(locale, "siteSearch", "site search")),
        html_escape(&tx(locale, "findPackageCoverage", "Find package coverage")),
        html_escape(&tx(locale, "catalogSearchCopy", "Search the package catalog, documentation, and source-backed metadata from one index.")),
        html_escape(locale.code),
        html_escape(&search_endpoint),
        html_escape(&tx(locale, "searchPlaceholder", "Search awscli, gh, .env, npm publish"))
    ));
    body.push_str(&format!(
        r#"<section class="pkg-section" aria-labelledby="pkg-hubs-title"><p class="section-kicker">{}</p><h2 id="pkg-hubs-title">{}</h2><p>{}</p><div class="hub-groups" aria-label="{}">{}</div></section>"#,
        html_escape(&tx(locale, "catalogHubsKicker", "package hubs")),
        html_escape(&tx(locale, "catalogHubsTitle", "Package groups by ecosystem and workflow")),
        html_escape(&tx(locale, "catalogHubsCopy", "These crawlable hubs group related package families, including cloud CLIs, source-control tools, package publishers, MCP tools, and language ecosystems.")),
        html_escape(&tx(locale, "catalogHubsAria", "Package category hubs")),
        hub_group_sections(&hub_summaries, locale)
    ));
    body.push_str(&format!(
        r#"<section class="pkg-section split-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p></div><div class="package-list" aria-label="{}">"#,
        html_escape(&tx(locale, "catalogPagesKicker", "crawlable catalog")),
        html_escape(&tx(locale, "catalogPagesTitle", "Package pages from local source data")),
        html_escape(&tx(locale, "crawlableCatalog", "pkg.so package metadata, generated package inventories, executable indexes, and package-manager facts are served by the Atlas package origin so search and answer engines can find specific tools.")),
        html_escape(&tx(locale, "popularPackages", "Popular packages")),
    ));
    for package in &top_packages {
        body.push_str(&index_package_row(package, locale));
    }
    body.push_str("</div></section>");
    body.push_str(&project_history::index_links(connection)?);
    body.push_str("</main>");
    body.push_str(&site_footer(locale));
    let schema = json!({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": catalog_title.clone(),
        "url": locale_url("/", locale),
        "inLanguage": locale.hreflang,
        "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": format!("{SITE_ORIGIN}/")},
        "about": tx(locale, "packageCatalogDescription", "Source-backed package intelligence for executable tools, install routes, and package metadata")
    });
    let schema_json = serde_json::to_string_pretty(&schema).unwrap_or_else(|_| "{}".to_string());
    Ok(html_doc(
        locale,
        &format!("{catalog_title} | {SITE_NAME}"),
        &tx(
            locale,
            "packageCatalogDescription",
            "Source-backed package intelligence for executable tools, including install metadata, aliases, popularity, and upstream facts.",
        ),
        &locale_url("/", locale),
        "index,follow",
        "",
        &schema_json,
        &body,
        &format!(
            r#"  <script src="{}?v={}"></script>"#,
            html_escape(&locale_path("/pkg/search.js", locale)),
            asset_version(SEARCH_JS)
        ),
    ))
}

fn render_hub_page(
    connection: &Connection,
    hub: &HubRow,
    locale: &Locale,
) -> Result<String, String> {
    let stats = hub_stats(connection, &hub.slug)?;
    let top = hub_packages_query(connection, &hub.slug, 72, None, "position")?;
    let high_signal_rows = hub_packages_query(connection, &hub.slug, 12, None, "position")?;
    let protected_rows = hub_packages_query(connection, &hub.slug, 8, Some("isotope"), "position")?;
    let approval_gated_rows =
        hub_packages_query(connection, &hub.slug, 8, Some("approval_gate"), "position")?;
    let spokes_rows = hub_packages_query(connection, &hub.slug, 16, None, "rank")?;
    let high_signal = high_signal_rows
        .iter()
        .map(|(package, _reason)| package)
        .collect::<Vec<_>>();
    let protected = protected_rows
        .iter()
        .map(|(package, _reason)| package)
        .collect::<Vec<_>>();
    let approval_gated = approval_gated_rows
        .iter()
        .map(|(package, _reason)| package)
        .collect::<Vec<_>>();
    let spokes = spokes_rows
        .iter()
        .map(|(package, _reason)| package)
        .collect::<Vec<_>>();
    let generated_at = metadata_string(connection, "generated_at")?.unwrap_or_default();
    let updated = fmt_date(&generated_at);
    let mut body = String::new();
    body.push_str(&site_nav(locale));
    body.push_str("<main>");
    body.push_str(&format!(
        r#"<nav class="breadcrumbs" aria-label="{}"><a href="{}">{}</a><span>/</span><a href="{}">{}</a><span>/</span><span>{}</span></nav><section class="pkg-hero pkg-hero-index" aria-labelledby="hub-title"><div class="hero-copy"><p class="eyebrow">{}</p><h1 id="hub-title">{}</h1><p class="lede">{}</p></div><aside class="hero-panel" aria-label="{}">{}{}{}{}</aside></section>"#,
        html_escape(&tx(locale, "breadcrumbs", "Breadcrumbs")),
        html_escape(&locale_path("/", locale)),
        html_escape(&tx(locale, "home", "Home")),
        html_escape(&locale_path("/pkg/", locale)),
        html_escape(&tx(locale, "packages", "Packages")),
        html_escape(&hub.title),
        html_escape(&hub.group),
        html_escape(&hub.title),
        html_escape(&hub.description),
        html_escape(&tx(locale, "hubCounts", "Hub counts")),
        metric(&tx(locale, "packages", "packages"), &fmt_int(stats.package_count)),
        metric(&tx(locale, "radioisotopes", "protected tools"), &fmt_int(stats.secured_count)),
        metric(&tx(locale, "approvalGates", "approval gates"), &fmt_int(stats.gated_count)),
        metric(&tx(locale, "updated", "updated"), &updated),
    ));
    let hub_description = txf(
        locale,
        "hubDescription",
        "{title} currently includes {count} package catalog entries. {secured} have protected-tool coverage, {gated} have approval-gate metadata, and {risked} have non-low Geiger classifier findings. The grouping comes from package metadata, so it can stay current as that metadata changes.",
        &[
            ("title", hub.title.clone()),
            ("count", stats.package_count.to_string()),
            ("secured", stats.secured_count.to_string()),
            ("gated", stats.gated_count.to_string()),
            ("risked", stats.risked_count.to_string()),
        ],
    );
    body.push_str(&format!(
        r#"<section class="pkg-section split-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p></div><div class="detail-stack"><article><h3>{}</h3><p>{}</p></article><article><h3>{}</h3><p>{}</p></article></div></section>"#,
        html_escape(&tx(locale, "packageSummary", "summary")),
        html_escape(&tx(locale, "hubSummaryTitle", "Why this package group is here")),
        html_escape(&hub_description),
        html_escape(&tx(locale, "generatedSource", "Generated source")),
        html_escape(&tx(locale, "generatedSourceCopy", "This hub uses the same local package data as individual package pages: pkg.so package metadata, Homebrew enrichment, Geiger classifier output, secret-handling manifests, and approval-gate seeds where available.")),
        html_escape(&tx(locale, "hubReviewModel", "Review model")),
        html_escape(&tx(locale, "hubReviewCopy", "Use the hub to find command families that need tighter secret injection, approval gates, or manual review before agents run them."))
    ));
    body.push_str(&hub_cluster_block(
        &tx(locale, "hubHighSignalTitle", "High-signal tools"),
        &high_signal,
        locale,
    ));
    body.push_str(&hub_cluster_block(
        &tx(locale, "hubProtectedToolsTitle", "Protected tools"),
        &protected,
        locale,
    ));
    body.push_str(&hub_cluster_block(
        &tx(locale, "hubApprovalGatedTitle", "Approval-gated tools"),
        &approval_gated,
        locale,
    ));
    body.push_str(&hub_related_block(
        &tx(locale, "hubRelatedHubsTitle", "Related hubs"),
        &related_hub_links(connection, hub, locale)?,
    ));
    body.push_str(&hub_cluster_block(
        &tx(
            locale,
            "hubRepresentativeSpokesTitle",
            "Representative package spokes",
        ),
        &spokes,
        locale,
    ));
    body.push_str(&format!(
        r#"<section class="pkg-section"><p class="section-kicker">{}</p><h2>{}</h2><div class="table-wrap hub-table"><table><thead><tr><th>{}</th><th>{}</th><th>{}</th><th>{}</th></tr></thead><tbody>"#,
        html_escape(&tx(locale, "packages", "packages")),
        html_escape(&tx(locale, "hubIndexedPagesTitle", "Indexed package pages")),
        html_escape(&tx(locale, "package", "Package")),
        html_escape(&tx(locale, "manager", "Manager")),
        html_escape(&tx(locale, "signals", "Signals")),
        html_escape(&tx(locale, "why", "Why it appears here")),
    ));
    for (package, reason) in &top {
        body.push_str(&hub_package_row(package, reason, locale));
    }
    body.push_str("</tbody></table></div></section></main>");
    body.push_str(&site_footer(locale));
    let description = short_text(
        &txf(
            locale,
            "hubSchemaDescription",
            "{description} Browse {count} package pages with install commands, metadata, and security notes.",
            &[
                ("description", hub.description.clone()),
                ("count", stats.package_count.to_string()),
            ],
        ),
        155,
    );
    let schema = schema_for_hub(
        hub,
        top.iter().map(|(package, _)| package).collect(),
        &description,
        &updated,
        locale,
    );
    let schema_json = serde_json::to_string_pretty(&schema).unwrap_or_else(|_| "{}".to_string());
    Ok(html_doc(
        locale,
        &format!(
            "{} | {}",
            hub.title,
            tx(locale, "packageCatalogTitle", "Package security catalog")
        ),
        &description,
        &locale_url(&hub.path, locale),
        "index,follow",
        &format!(
            r#"  <link rel="alternate" type="text/markdown" href="{}index.md">"#,
            html_escape(&locale_url(&hub.path, locale))
        ),
        &schema_json,
        &body,
        "",
    ))
}

fn render_hub_markdown(
    connection: &Connection,
    hub: &HubRow,
    locale: &Locale,
) -> Result<String, String> {
    let stats = hub_stats(connection, &hub.slug)?;
    let top = hub_packages_query(connection, &hub.slug, 72, None, "position")?;
    let generated_at = metadata_string(connection, "generated_at")?.unwrap_or_default();
    let updated = fmt_date(&generated_at);
    let description = txf(
        locale,
        "hubDescription",
        "{title} currently includes {count} package catalog entries. {secured} have protected-tool coverage, {gated} have approval-gate metadata, and {risked} have non-low Geiger classifier findings. The grouping comes from package metadata, so it can stay current as that metadata changes.",
        &[
            ("title", hub.title.clone()),
            ("count", stats.package_count.to_string()),
            ("secured", stats.secured_count.to_string()),
            ("gated", stats.gated_count.to_string()),
            ("risked", stats.risked_count.to_string()),
        ],
    );
    let mut text = format!(
        "# {}\n\n{}\n\n- **{}:** {}\n- **{}:** {}\n- **{}:** {}\n- **{}:** {}\n- **{}:** {}\n\n## {}\n\n{}\n\n## {}\n\n{}\n\n## {}\n\n{}\n\n## {}\n\n",
        hub.title,
        hub.description,
        tx(locale, "packages", "Packages"),
        fmt_int(stats.package_count),
        tx(locale, "radioisotopes", "Protected tools"),
        fmt_int(stats.secured_count),
        tx(locale, "approvalGates", "Approval gates"),
        fmt_int(stats.gated_count),
        tx(locale, "risk", "Non-low risk"),
        fmt_int(stats.risked_count),
        tx(locale, "updated", "Updated"),
        updated,
        tx(locale, "hubSummaryTitle", "Why this package group is here"),
        description,
        tx(locale, "generatedSource", "Generated source"),
        tx(
            locale,
            "generatedSourceCopy",
            "This hub uses the same local package data as individual package pages: pkg.so package metadata, Homebrew enrichment, Geiger classifier output, secret-handling manifests, and approval-gate seeds where available."
        ),
        tx(locale, "hubReviewModel", "Review model"),
        tx(
            locale,
            "hubReviewCopy",
            "Use the hub to find command families that need tighter secret injection, approval gates, or manual review before agents run them."
        ),
        tx(locale, "hubIndexedPagesTitle", "Indexed package pages"),
    );
    for (package, reason) in top {
        let reason = if reason.trim().is_empty() {
            hub_package_reason(&package, locale)
        } else {
            reason
        };
        text.push_str(&format!(
            "- [{}]({}) - {}\n",
            package.display_name,
            locale_url(&package.path, locale),
            markdown_value(&reason)
        ));
    }
    Ok(text)
}

fn render_package_page(package: &PackageRow, locale: &Locale, generated_at: &str) -> String {
    let install_heading = package_install_heading(package, locale);
    let title = format!("{install_heading} | {SITE_NAME}");
    let description = meta_description(package, locale);
    let updated = first_non_empty(&[
        display_last_verified(package),
        package.last_updated_at.clone(),
    ])
    .map(|value| fmt_date(&value))
    .filter(|value| !value.is_empty())
    .unwrap_or_else(|| "2026-06-03".to_string());
    let canonical = locale_url(&package.path, locale);
    let schema = schema_for_package(package, &description, &updated, locale);
    let schema_json = serde_json::to_string_pretty(&schema).unwrap_or_else(|_| "{}".to_string());
    let mut body = String::new();
    body.push_str(&site_nav(locale));
    body.push_str("<main>");
    body.push_str(&format!(
        r#"<nav class="breadcrumbs" aria-label="{}"><a href="{}">{}</a><span>/</span><a href="{}">{}</a><span>/</span><span>{}</span></nav>"#,
        html_escape(&tx(locale, "breadcrumbs", "Breadcrumbs")),
        html_escape(&locale_path("/", locale)),
        html_escape(&tx(locale, "home", "Home")),
        html_escape(&locale_path("/pkg/", locale)),
        html_escape(&tx(locale, "packages", "Packages")),
        html_escape(&package.display_name)
    ));
    body.push_str(&format!(
        r##"<section class="pkg-hero" aria-labelledby="pkg-title"><div class="hero-copy"><p class="eyebrow">{}</p><h1 id="pkg-title">{}</h1><p class="lede">{}</p><div class="hero-actions"><a class="button primary" href="#install">{}</a><a class="button secondary" href="#security">{}</a>{}</div></div><aside class="hero-panel" aria-label="{}">{}</aside></section>"##,
        html_escape(&label_for(package, locale)),
        html_escape(&install_heading),
        html_escape(&localized_hero_sentence(package, locale)),
        html_escape(&tx(locale, "installCommand", "Install command")),
        html_escape(&tx(locale, "securityNotes", "Security notes")),
        project_history::package_link(package),
        html_escape(&tx(locale, "heroPanelAria", "Package facts")),
        package_facts(package, locale)
    ));
    body.push_str(&render_agent_safety_answer(package, locale));
    body.push_str(&render_install(package, locale));
    body.push_str(&render_overview(package, locale));
    body.push_str(&render_history(package, locale));
    body.push_str(&render_security(package, locale));
    body.push_str(&render_file_locations(package, locale));
    body.push_str(&render_executables(package, locale));
    body.push_str(&render_freshness(package, generated_at, locale));
    body.push_str(&render_install_metadata(package, locale));
    body.push_str(&render_registry_insights(package, locale));
    body.push_str(&render_external_package_manager_matches(package, locale));
    body.push_str(&render_related(package, locale));
    body.push_str(&render_sources(package, locale));
    body.push_str("</main>");
    body.push_str(&site_footer(locale));
    html_doc(
        locale,
        &title,
        &description,
        &canonical,
        if package.indexable {
            "index,follow"
        } else {
            "noindex,follow"
        },
        &format!(
            r#"  <link rel="alternate" type="text/markdown" href="{}index.md">"#,
            html_escape(&canonical)
        ),
        &schema_json,
        &body,
        &copy_script(locale),
    )
}

fn render_package_markdown(package: &PackageRow, locale: &Locale, generated_at: &str) -> String {
    let mut text = format!(
        "# {}\n\n{}\n\n## {}\n\n```sh\n{}\n```\n\n",
        package_install_heading(package, locale),
        localized_hero_sentence(package, locale),
        tx(locale, "install", "Install"),
        package.install_command,
    );
    markdown_agent_safety_section(&mut text, package, locale);
    markdown_install_groups(&mut text, package, locale);
    text.push_str(&format!(
        "## {}\n\n",
        tx(locale, "packageFacts", "Package facts")
    ));
    for (label, value) in [
        (
            tx(locale, "packageKey", "Package key"),
            package.package_key.clone(),
        ),
        (
            tx(locale, "packageManager", "Package manager"),
            package.provider_label.clone(),
        ),
        (
            tx(locale, "packageManagerPage", "Package manager page"),
            package.package_manager_url.clone(),
        ),
        (
            tx(locale, "version", "Version"),
            authoritative_package_version(package),
        ),
        (
            tx(locale, "sourceSummary", "Source summary"),
            package.summary.clone(),
        ),
        (tx(locale, "homepage", "Homepage"), package.homepage.clone()),
        (
            tx(locale, "repository", "Repository"),
            package.repository.clone(),
        ),
        (
            tx(locale, "upstreamDocs", "Upstream docs"),
            full_str(package, "upstreamDocs"),
        ),
        (tx(locale, "license", "License"), package.license.clone()),
        (
            tx(locale, "sourceArchive", "Source archive"),
            full_str(package, "sourceArchive"),
        ),
        (
            tx(locale, "issueTracker", "Issue tracker"),
            full_str(package, "issueTracker"),
        ),
        (
            tx(locale, "published", "Published"),
            full_str(package, "publishedAt"),
        ),
        (
            tx(locale, "lastVerified", "Last verified"),
            display_last_verified(package),
        ),
        (
            tx(locale, "lastUpdated", "Last updated"),
            package.last_updated_at.clone(),
        ),
        (
            tx(locale, "generated", "Generated"),
            generated_at.to_string(),
        ),
    ] {
        if !value.trim().is_empty() {
            text.push_str(&format!("- **{}:** {}\n", label, markdown_value(&value)));
        }
    }
    markdown_value_list(
        &mut text,
        &tx(locale, "executables", "Executables"),
        &executable_markdown_items(package, locale),
    );
    markdown_value_list(
        &mut text,
        &tx(locale, "dependencies", "Dependencies"),
        &full_string_array(package, "dependencies"),
    );
    markdown_value_list(
        &mut text,
        &tx(locale, "buildDependencies", "Build dependencies"),
        &full_string_array(package, "buildDependencies"),
    );
    markdown_value_list(
        &mut text,
        &tx(locale, "usesFromMacos", "Uses from macOS"),
        &full_string_array(package, "usesFromMacos"),
    );
    markdown_value_list(
        &mut text,
        &tx(locale, "installBehaviorTitle", "Install behavior"),
        &markdown_install_behavior_items(package, locale),
    );
    markdown_value_list(
        &mut text,
        &tx(locale, "freshnessTitle", "Freshness"),
        &markdown_freshness_items(package, generated_at, locale),
    );
    markdown_history_section(&mut text, package, locale);
    markdown_security_section(&mut text, package, locale);
    markdown_file_locations(&mut text, package, locale);
    markdown_registry_insights(&mut text, package, locale);
    markdown_external_package_manager_matches(&mut text, package, locale);
    markdown_related(&mut text, package, locale);
    markdown_combined_yaml_source(&mut text, package, locale);
    markdown_value_list(
        &mut text,
        &tx(locale, "sources", "Sources"),
        &full_string_array(package, "sourceNotes"),
    );
    text
}

fn html_doc(
    locale: &Locale,
    title: &str,
    description: &str,
    canonical: &str,
    robots: &str,
    extra_head: &str,
    schema_json: &str,
    body: &str,
    extra_body: &str,
) -> String {
    format!(
        r#"<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <meta name="description" content="{description}">
  <meta name="robots" content="{robots}">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="{site_name}">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{description}">
  <meta property="og:url" content="{canonical}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{title}">
  <meta name="twitter:description" content="{description}">
  <link rel="canonical" href="{canonical}">
{hreflang}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&amp;family=Space+Grotesk:wght@400;500;600;700&amp;display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{stylesheet}">
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-5218328608281545" crossorigin="anonymous"></script>
  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-CMPY7XG4QH"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());
    gtag('config', 'G-CMPY7XG4QH');
  </script>
{extra_head}
  <script type="application/ld+json">
{schema_json}
  </script>
</head>
<body>
  <div class="site-shell">
    {body}
  </div>
{extra_body}
</body>
</html>
"#,
        lang = html_escape(locale.code),
        title = html_escape(title),
        description = html_escape(description),
        robots = html_escape(robots),
        canonical = html_escape(canonical),
        site_name = SITE_NAME,
        hreflang = if canonical == "https://pkg.so/privacy/" { String::new() } else { html_hreflang_links(canonical) },
        stylesheet = html_escape(&format!(
            "{}?v={}",
            locale_path("/pkg/styles.css", locale),
            asset_version(PKG_CSS)
        )),
        extra_head = extra_head,
        schema_json = schema_json,
        body = body,
        extra_body = extra_body,
    )
}

fn site_nav(locale: &Locale) -> String {
    format!(
        r#"<header class="masthead"><a class="brand" href="{}" aria-label="{}"><span class="brand-mark" aria-hidden="true">pkg</span><span class="brand-lockup"><span class="brand-type">pkg.so</span><span class="brand-tagline">open package index</span></span></a><nav class="nav" aria-label="{}"><a class="nav-primary" href="{}">{}</a><a href="{}">Feed</a><a href="https://github.com/mxcl/pkg.so">GitHub <span aria-hidden="true">↗</span></a></nav></header>"#,
        html_escape(&locale_path("/", locale)),
        html_escape(&tx(locale, "brandHomeAria", "pkg.so package catalog")),
        html_escape(&tx(locale, "mainNavigation", "Main navigation")),
        html_escape(&format!("{}#search", locale_path("/", locale))),
        html_escape(&tx(locale, "search", "Search")),
        html_escape(&locale_path("/feed/", locale)),
    )
}

fn site_footer(locale: &Locale) -> String {
    format!(
        r#"<footer class="site-footer"><p>{}</p><div class="footer-links"><a href="https://mxcl.dev">a mxcl project</a><a href="/sitemap.xml">Sitemap</a><a href="/robots.txt">Robots</a><a href="/privacy/">Privacy</a><a href="https://github.com/mxcl/pkg.so">Source</a></div></footer>"#,
        html_escape(&tx(
            locale,
            "footer",
            "Source-backed package intelligence, rebuilt daily."
        )),
    )
}

fn copy_script(locale: &Locale) -> String {
    format!(
        r#"  <script>
    document.addEventListener("click", async (event) => {{
      const button = event.target.closest("[data-copy]");
      if (!button) return;
      try {{
        await navigator.clipboard.writeText(button.getAttribute("data-copy"));
        const previous = button.textContent;
        button.textContent = "{}";
        button.setAttribute("data-state", "copied");
        window.setTimeout(() => {{
          button.textContent = previous;
          button.removeAttribute("data-state");
        }}, 1600);
      }} catch (_error) {{
        button.textContent = "{}";
        button.setAttribute("data-state", "error");
      }}
    }});
  </script>"#,
        js_string_escape(&tx(locale, "copied", "Copied")),
        js_string_escape(&tx(locale, "copyFailed", "Copy failed")),
    )
}

fn html_hreflang_links(canonical: &str) -> String {
    if canonical.strip_prefix(SITE_ORIGIN).is_some_and(project_history::is_history_path) {
        return String::new();
    }
    let Some(path) = canonical.strip_prefix(SITE_ORIGIN) else {
        return String::new();
    };
    let canonical_path;
    let path = if landing_locale(path).is_some() {
        "/"
    } else if feed_locale(path).is_some() {
        "/feed/"
    } else {
        let (_, path) = canonical_pkg_route(path);
        canonical_path = path;
        canonical_path
            .strip_suffix("index.html")
            .unwrap_or(&canonical_path)
    };
    let mut lines = Vec::new();
    for locale in LOCALES {
        lines.push(format!(
            r#"  <link rel="alternate" hreflang="{}" href="{}{}">"#,
            html_escape(locale.hreflang),
            SITE_ORIGIN,
            html_escape(&locale_path(path, locale))
        ));
    }
    lines.push(format!(
        r#"  <link rel="alternate" hreflang="x-default" href="{}{}">"#,
        SITE_ORIGIN,
        html_escape(path)
    ));
    lines.join("\n")
}

fn render_install(package: &PackageRow, locale: &Locale) -> String {
    let commands = package_manager_install_entries(package);
    let platform_html = render_platform_install_commands(&commands, locale);
    if platform_html.is_empty() {
        return String::new();
    }
    format!(
        r#"<section id="install" class="pkg-section install-section" aria-labelledby="install-title"><div class="install-command-panel"><div><p class="section-kicker">{}</p><h2 id="install-title">{}</h2></div>{}</div></section>"#,
        html_escape(&tx(locale, "install", "install")),
        html_escape(&tx(
            locale,
            "additionalInstallCommands",
            "Additional install commands"
        )),
        platform_html
    )
}

fn agent_safety_answer(package: &PackageRow) -> Option<&Value> {
    full_value(package, "agentSafetyAnswer").filter(|value| value.is_object())
}

fn render_agent_safety_answer(package: &PackageRow, locale: &Locale) -> String {
    let Some(answer) = agent_safety_answer(package) else {
        return String::new();
    };
    let Some(summary) = value_str_key(answer, "summary") else {
        return String::new();
    };
    let articles = [
        (
            tx(locale, "agentSafetyCredentialAccess", "Credential access"),
            "credentialAccess",
        ),
        (
            tx(locale, "agentSafetyRemoteMutation", "Remote mutation"),
            "remoteMutation",
        ),
        (
            tx(locale, "agentSafetyPublishRisk", "Publish/artifact risk"),
            "publishOrArtifactRisk",
        ),
        (
            tx(locale, "agentSafetyControl", "Recommended control"),
            "recommendedControl",
        ),
        (
            tx(locale, "agentSafetyGuidance", "Agent-use guidance"),
            "agentUseGuidance",
        ),
    ]
    .into_iter()
    .filter_map(|(label, field)| {
        value_str_key(answer, field).map(|value| {
            format!(
                "<article><h3>{}</h3><p>{}</p></article>",
                html_escape(&label),
                html_escape(&value)
            )
        })
    })
    .collect::<String>();
    format!(
        r#"<section class="pkg-section split-section agent-safety-section" aria-labelledby="agent-safety-title"><div><p class="section-kicker">{}</p><h2 id="agent-safety-title">{}</h2><p>{}</p></div><div class="detail-stack">{}</div></section>"#,
        html_escape(&tx(locale, "agentSafetyKicker", "agent safety")),
        html_escape(&tx(locale, "agentSafetyTitle", "Agent safety answer")),
        html_escape(&summary),
        articles
    )
}

fn render_platform_install_commands(commands: &[Value], locale: &Locale) -> String {
    let mut sections = Vec::new();
    for (platform, label) in [
        ("macos", tx(locale, "macos", "macOS")),
        ("linux", tx(locale, "linux", "Linux")),
        ("windows", tx(locale, "windows", "Windows")),
        (
            "portable",
            tx(
                locale,
                "platformPortableLanguageManagers",
                "Portable and language managers",
            ),
        ),
    ] {
        let rows = commands
            .iter()
            .filter(|item| {
                value_str_key(item, "platform").unwrap_or_else(|| "portable".to_string())
                    == platform
            })
            .map(|item| install_command_row(item, locale))
            .collect::<String>();
        if !rows.is_empty() {
            sections.push(format!(
                r#"<article><h3>{}</h3><div class="install-command-list">{}</div></article>"#,
                html_escape(&label),
                rows
            ));
        }
    }
    if sections.is_empty() {
        String::new()
    } else {
        format!(
            r#"<div class="platform-install-grid" aria-label="{}">{}</div>"#,
            html_escape(&tx(
                locale,
                "platformInstallCommands",
                "Platform install commands"
            )),
            sections.join("")
        )
    }
}

fn install_command_row(item: &Value, locale: &Locale) -> String {
    let command = value_str_key(item, "command").unwrap_or_default();
    let label = install_command_manager_label(item);
    let confidence = value_f64_key(item, "confidence").unwrap_or(0.0);
    let confidence_label = if confidence >= 0.9 {
        tx(locale, "verified", "verified")
    } else {
        tx(locale, "inferred", "inferred")
    };
    let source = install_command_source_html(item, locale);
    format!(
        r#"<div class="install-command-row"><div class="install-command-head"><strong class="install-command-eyebrow">{}</strong><span>{} · {:.0}%</span></div><div class="install-command-shell"><code>{}</code><button class="copy-button" type="button" data-copy="{}" aria-label="{}">{}</button></div>{}</div>"#,
        html_escape(&label),
        html_escape(&confidence_label),
        confidence * 100.0,
        html_escape(&command),
        html_escape(&command),
        html_escape(&txf(
            locale,
            "copyManagerInstallCommand",
            "Copy {manager} install command",
            &[("manager", label.clone())],
        )),
        html_escape(&tx(locale, "copy", "Copy")),
        source
    )
}

fn install_command_manager_label(item: &Value) -> String {
    let manager = value_str_key(item, "manager").unwrap_or_else(|| "shell".to_string());
    let source_manager = item
        .get("source")
        .and_then(|source| value_str_key(source, "manager"))
        .unwrap_or_default();
    match (if source_manager.is_empty() {
        manager.clone()
    } else {
        source_manager
    })
    .to_ascii_lowercase()
    .as_str()
    {
        "apk" => "Alpine Linux apk".to_string(),
        "apt" => "Debian apt".to_string(),
        "chocolatey" => "Chocolatey".to_string(),
        "dnf" => "Fedora dnf".to_string(),
        "macports" => "MacPorts".to_string(),
        "nix" => "Nix".to_string(),
        "pacman" => "Arch Linux pacman".to_string(),
        "scoop" => "Scoop".to_string(),
        "winget" => "Windows Package Manager".to_string(),
        "zypper" => "openSUSE zypper".to_string(),
        "pip" => "Python pip".to_string(),
        "npm" => "npm".to_string(),
        _ => manager,
    }
}

fn install_command_source_html(item: &Value, locale: &Locale) -> String {
    if let Some(source) = item.get("source").filter(|value| value.is_object()) {
        let label = value_str_key(source, "source_label").unwrap_or_default();
        let package_name = value_str_key(source, "package_name")
            .or_else(|| value_str_key(source, "package_id"))
            .unwrap_or_default();
        let source_url = value_str_key(source, "source_url").unwrap_or_default();
        let mut pieces = [label, package_name]
            .into_iter()
            .filter(|value| !value.is_empty())
            .map(|value| html_escape(&value))
            .collect::<Vec<_>>();
        if !source_url.is_empty() {
            pieces.push(format!(
                r#"<a href="{}">{}: {}</a>"#,
                html_escape(&source_url),
                html_escape(&tx(locale, "source", "source")),
                html_escape(source_host_label(&source_url))
            ));
        }
        return if pieces.is_empty() {
            String::new()
        } else {
            format!(
                r#"<p class="install-command-source">{}</p>"#,
                pieces.join(" · ")
            )
        };
    }
    value_str_key(item, "evidence")
        .filter(|value| !value.is_empty())
        .map(|value| {
            format!(
                r#"<p class="install-command-source">{}</p>"#,
                html_escape(&value)
            )
        })
        .unwrap_or_default()
}

fn render_overview(package: &PackageRow, locale: &Locale) -> String {
    let commands = overview_command_names(package);
    let command_block = if commands.is_empty() {
        format!(
            "<p>{}</p>",
            html_escape(&tx(
                locale,
                "noAliases",
                "No executable aliases were found in the local package database."
            ))
        )
    } else {
        format!(
            r#"<ul class="chip-list">{}</ul>"#,
            commands
                .iter()
                .take(32)
                .map(|command| format!("<li>{}</li>", html_escape(command)))
                .collect::<String>()
        )
    };
    let project_links = overview_project_links(package, locale);
    let project_block = if project_links.is_empty() {
        format!(
            "<p>{}</p>",
            html_escape(&tx(
                locale,
                "homepageMissing",
                "Not present in the local metadata.",
            ))
        )
    } else {
        format!(
            r#"<ul class="chip-list">{}</ul>"#,
            project_links
                .into_iter()
                .map(|(label, url)| {
                    format!(
                        r#"<li><a href="{}">{}</a></li>"#,
                        html_escape(&url),
                        html_escape(&label)
                    )
                })
                .collect::<String>()
        )
    };
    format!(
        r#"<section class="pkg-section split-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p></div><div class="detail-stack"><article><h3>{}</h3>{}</article><article><h3>{}</h3>{}</article></div></section>"#,
        html_escape(&tx(locale, "overview", "overview")),
        html_escape(&tx(locale, "packageSummary", "Package summary")),
        html_escape(&package.summary),
        html_escape(&tx(locale, "projectLinks", "Project links")),
        project_block,
        html_escape(&tx(locale, "commandsAndAliases", "Commands and aliases")),
        command_block
    )
}

fn render_history(package: &PackageRow, locale: &Locale) -> String {
    let Some(history) = full_value(package, "history").filter(|value| value.is_object()) else {
        return String::new();
    };
    let mut body = String::new();
    for paragraph in history_items(history, "summary") {
        body.push_str(&format!("<p>{}</p>", html_escape(&paragraph)));
    }
    let mut articles = String::new();
    for (key, title) in [
        (
            "project-history",
            tx(locale, "projectHistory", "Project history"),
        ),
        (
            "adoption-history",
            tx(locale, "adoptionHistory", "Adoption history"),
        ),
        ("usage", tx(locale, "usage", "How it is used")),
        (
            "package-nerd-significance",
            tx(locale, "packageNerdSignificance", "Why package nerds care"),
        ),
    ] {
        let paragraphs = history_items(history, key);
        if paragraphs.is_empty() {
            continue;
        }
        let content = paragraphs
            .into_iter()
            .map(|item| format!("<p>{}</p>", html_escape(&item)))
            .collect::<String>();
        articles.push_str(&format!(
            "<article><h3>{}</h3>{}</article>",
            html_escape(&title),
            content
        ));
    }
    for (key, title) in [
        ("timeline", tx(locale, "timeline", "Timeline")),
        (
            "related-projects",
            tx(locale, "relatedProjects", "Related projects"),
        ),
        ("sources", tx(locale, "sources", "Sources")),
    ] {
        let items = history_items(history, key);
        if items.is_empty() {
            continue;
        }
        let list = items
            .into_iter()
            .map(|item| history_list_item(&item))
            .collect::<String>();
        articles.push_str(&format!(
            r#"<article><h3>{}</h3><ul>{}</ul></article>"#,
            html_escape(&title),
            list
        ));
    }
    if articles.is_empty() && body.is_empty() {
        return String::new();
    }
    format!(
        r#"<section class="pkg-section split-section" id="history" aria-labelledby="history-title"><div><p class="section-kicker">{}</p><h2 id="history-title">{}</h2>{}</div><div class="detail-stack">{}</div></section>"#,
        html_escape(&tx(locale, "history", "history")),
        html_escape(&tx(locale, "historyTitle", "Project history and usage")),
        body,
        articles
    )
}

fn history_items(history: &Value, key: &str) -> Vec<String> {
    value_array(history, key)
        .into_iter()
        .filter_map(value_string)
        .collect()
}

fn history_list_item(value: &str) -> String {
    if value.starts_with("http://") || value.starts_with("https://") {
        format!(
            r#"<li><a href="{}">{}</a></li>"#,
            html_escape(value),
            html_escape(value)
        )
    } else {
        format!("<li>{}</li>", html_escape(value))
    }
}

fn overview_project_links(package: &PackageRow, locale: &Locale) -> Vec<(String, String)> {
    let mut links = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for (label, url) in [
        (
            tx(locale, "homepage", "Homepage"),
            first_non_empty(&[package.homepage.clone(), full_str(package, "homepage")])
                .unwrap_or_default(),
        ),
        (
            tx(locale, "repository", "Repository"),
            package.repository.clone(),
        ),
        (
            tx(locale, "upstreamDocs", "Upstream docs"),
            full_str(package, "upstreamDocs"),
        ),
        (
            tx(locale, "packageManagerPage", "Package manager page"),
            package.package_manager_url.clone(),
        ),
    ] {
        let url = url.trim();
        if !url.is_empty() && seen.insert(url.to_string()) {
            links.push((label, url.to_string()));
        }
    }
    links
}

fn overview_command_names(package: &PackageRow) -> Vec<String> {
    let mut names = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for item in full_string_array(package, "aliases")
        .into_iter()
        .chain(full_string_array(package, "executablesDetailed"))
        .chain(full_string_array(package, "binaries"))
    {
        let item = item.trim();
        if !item.is_empty() && seen.insert(item.to_string()) {
            names.push(item.to_string());
        }
    }
    names
}

fn render_security(package: &PackageRow, locale: &Locale) -> String {
    let geiger = render_geiger(package, locale);
    let install_signals = render_install_behavior_signals(package, locale);
    let gate = render_gate(package, locale);
    if let Some(isotope) = full_value(package, "isotope").filter(|value| value.is_object()) {
        let justification = isotope.get("justification").unwrap_or(&Value::Null);
        let title = value_str_key(justification, "title")
            .or_else(|| Some(tx(locale, "radioisotopeKicker", "Protected-tool coverage")))
            .unwrap();
        let detail = value_str_key(justification, "detail")
            .or_else(|| full_opt_str(package, "isotopeReadme"))
            .unwrap_or_else(|| {
                tx(
                    locale,
                    "radioisotopeManifestFallback",
                    "Automic Vault has a local secret-handling manifest for this package.",
                )
            });
        let caveats = isotope
            .get("caveats")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .take(8)
                    .filter_map(value_string)
                    .map(|item| format!("<li>{}</li>", html_escape(&item)))
                    .collect::<String>()
            })
            .unwrap_or_default();
        let readme = full_opt_str(package, "isotopeReadmeHtml")
            .filter(|value| !value.is_empty())
            .map(|html| {
                format!(
                    r#"<div class="readme-excerpt"><p class="readme-label">{}</p>{}<p class="readme-source">{}: <code>{}</code></p></div>"#,
                    html_escape(&tx(locale, "localReadmeExcerpt", "Local README excerpt")),
                    html,
                    html_escape(&tx(locale, "source", "Source")),
                    html_escape(&full_str(package, "isotopeReadmeSource"))
                )
            })
            .unwrap_or_default();
        return format!(
            r#"<section id="security" class="pkg-section security-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p>{}{}{}</div><div class="detail-stack"><article><h3>{}</h3><p>{}</p></article><article><h3>{}</h3><ul>{}</ul></article></div></section>{}"#,
            html_escape(&tx(locale, "radioisotopeKicker", "protected-tool coverage")),
            html_escape(&title),
            html_escape(&detail),
            geiger,
            install_signals,
            readme,
            html_escape(&tx(locale, "coverageSource", "Coverage source")),
            html_escape(&tx(
                locale,
                "sourceExcerpt",
                "Local secret-handling manifest"
            )),
            html_escape(&tx(locale, "caveats", "Caveats")),
            if caveats.is_empty() {
                format!(
                    "<li>{}</li>",
                    html_escape(&tx(
                        locale,
                        "noCaveats",
                        "No caveats were listed in the local manifest."
                    ))
                )
            } else {
                caveats
            },
            render_gate(package, locale)
        );
    }
    if !gate.is_empty() {
        return gate;
    }
    format!(
        r#"<section id="security" class="pkg-section security-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p>{}{}</div><div class="detail-stack"><article><h3>{}</h3><p>{}</p></article></div></section>"#,
        html_escape(&tx(locale, "securityPosture", "security posture")),
        html_escape(&security_heading(package, locale)),
        html_escape(&security_summary(package, locale)),
        geiger,
        install_signals,
        html_escape(&tx(locale, "recommendedReview", "Recommended review")),
        html_escape(&tx(
            locale,
            "recommendedReviewCopy",
            "Before unattended agent use, check whether the tool reads plaintext credentials, writes remote state, publishes artifacts, or shells out to plugins."
        ))
    )
}

fn render_file_locations(package: &PackageRow, locale: &Locale) -> String {
    let config = path_location_groups(package, "configFileLocations");
    let credentials = path_location_groups(package, "credentialsFileLocations");
    if config.is_empty() && credentials.is_empty() {
        return String::new();
    }
    let mut cards = String::new();
    if !config.is_empty() {
        cards.push_str(&file_location_card(
            &tx(locale, "configFileLocationsTitle", "Configuration files"),
            &tx(
                locale,
                "configFileLocationsCopy",
                "Config paths the tool may read or write during local use.",
            ),
            &config,
            locale,
        ));
    }
    if !credentials.is_empty() {
        cards.push_str(&file_location_card(
            &tx(locale, "credentialFileLocationsTitle", "Credential files"),
            &tx(
                locale,
                "credentialFileLocationsCopy",
                "Credential-bearing paths to review before unattended agent runs.",
            ),
            &credentials,
            locale,
        ));
    }
    format!(
        r#"<section class="pkg-section split-section file-surface-section" aria-labelledby="file-surface-title"><div><p class="section-kicker">{}</p><h2 id="file-surface-title">{}</h2><p>{}</p></div><div class="file-location-board">{}</div></section>"#,
        html_escape(&tx(locale, "localFilesKicker", "local files")),
        html_escape(&tx(
            locale,
            "localFilesTitle",
            "Configuration and credential file locations"
        )),
        html_escape(&tx(
            locale,
            "localFilesCopy",
            "These source-backed paths show where this package keeps local settings or durable credentials. Automic Vault can use them as review targets for secret scanning, migration, and command approval."
        )),
        cards
    )
}

fn file_location_card(
    title: &str,
    copy: &str,
    groups: &[(String, Vec<String>)],
    locale: &Locale,
) -> String {
    let rows = groups
        .iter()
        .map(|(platform, paths)| {
            let path_html = paths
                .iter()
                .map(|path| format!("<code>{}</code>", html_escape(path)))
                .collect::<String>();
            format!(
                "<div><dt>{}</dt><dd>{}</dd></div>",
                html_escape(&localized_platform_label(locale, platform)),
                path_html
            )
        })
        .collect::<String>();
    format!(
        r#"<article class="file-location-card"><h3>{}</h3><p>{}</p><dl class="file-location-list">{}</dl></article>"#,
        html_escape(title),
        html_escape(copy),
        rows
    )
}

fn path_location_groups(package: &PackageRow, key: &str) -> Vec<(String, Vec<String>)> {
    let Some(value) = full_value(package, key) else {
        return Vec::new();
    };
    let mut groups: BTreeMap<String, Vec<String>> = BTreeMap::new();
    match value {
        Value::Object(items) => {
            for (platform, value) in items {
                let paths = path_location_values(value);
                if !paths.is_empty() {
                    groups.insert(platform.to_string(), paths);
                }
            }
        }
        _ => {
            let paths = path_location_values(value);
            if !paths.is_empty() {
                groups.insert("portable".to_string(), paths);
            }
        }
    }
    groups
        .into_iter()
        .map(|(platform, paths)| {
            let mut seen = std::collections::BTreeSet::new();
            let paths = paths
                .into_iter()
                .map(|path| path.trim().to_string())
                .filter(|path| !path.is_empty() && seen.insert(path.clone()))
                .collect::<Vec<_>>();
            (platform, paths)
        })
        .filter(|(_platform, paths)| !paths.is_empty())
        .collect()
}

fn path_location_values(value: &Value) -> Vec<String> {
    match value {
        Value::String(text) => {
            let text = text.trim();
            if text.is_empty() {
                Vec::new()
            } else {
                vec![text.to_string()]
            }
        }
        Value::Array(items) => items.iter().filter_map(value_string).collect(),
        _ => Vec::new(),
    }
}

fn localized_platform_label(locale: &Locale, platform: &str) -> String {
    match platform {
        "macos" => tx(locale, "platformMacos", "macOS"),
        "unix" => tx(locale, "platformUnix", "Unix"),
        "linux" => tx(locale, "platformLinux", "Linux"),
        "windows" => tx(locale, "platformWindows", "Windows"),
        "portable" => tx(locale, "platformPortable", "Portable"),
        value => human_metadata_label(value),
    }
}

fn render_geiger(package: &PackageRow, locale: &Locale) -> String {
    let Some(geiger) = full_value(package, "geiger").filter(|value| value.is_object()) else {
        return String::new();
    };
    let reasons = value_array(geiger, "reasons")
        .into_iter()
        .take(5)
        .filter_map(value_string)
        .map(|item| format!("<li>{}</li>", html_escape(&item)))
        .collect::<String>();
    let signals = value_array(geiger, "signals")
        .into_iter()
        .take(5)
        .filter_map(value_string)
        .map(|item| format!("<li>{}</li>", html_escape(&item)))
        .collect::<String>();
    format!(
        r#"<div class="signal-grid" aria-label="{}"><article><h3>{}</h3><p>{}</p></article><article><h3>{}</h3><ul>{}</ul></article><article><h3>{}</h3><ul>{}</ul></article></div>"#,
        html_escape(&tx(
            locale,
            "geigerSignalsAria",
            "Geiger classifier signals"
        )),
        html_escape(&tx(locale, "riskClassifier", "Risk classifier")),
        html_escape(&txf(
            locale,
            "geigerRiskSummary",
            "{level} risk · {confidence} confidence · {category}",
            &[
                (
                    "level",
                    localized_standard_value(
                        locale,
                        &value_str_key(geiger, "level").unwrap_or_else(|| "unknown".to_string()),
                    ),
                ),
                (
                    "confidence",
                    localized_standard_value(
                        locale,
                        &value_str_key(geiger, "confidence")
                            .unwrap_or_else(|| "unknown".to_string()),
                    ),
                ),
                (
                    "category",
                    localized_standard_value(
                        locale,
                        &value_str_key(geiger, "category")
                            .unwrap_or_else(|| "uncategorized".to_string()),
                    ),
                ),
            ],
        )),
        html_escape(&tx(locale, "why", "Why")),
        if reasons.is_empty() {
            format!(
                "<li>{}</li>",
                html_escape(&tx(
                    locale,
                    "noClassifierReasons",
                    "No classifier reasons were present."
                ))
            )
        } else {
            reasons
        },
        html_escape(&tx(locale, "signals", "Signals")),
        if signals.is_empty() {
            format!(
                "<li>{}</li>",
                html_escape(&tx(
                    locale,
                    "noClassifierSignals",
                    "No classifier signals were present."
                ))
            )
        } else {
            signals
        },
    )
}

fn render_install_behavior_signals(package: &PackageRow, locale: &Locale) -> String {
    let Some(behavior) = full_value(package, "installBehavior").filter(|value| value.is_object())
    else {
        return String::new();
    };
    let mut signals = Vec::new();
    if let Some(items) = behavior
        .get("lifecycleScripts")
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
    {
        signals.push(txf(
            locale,
            "signalNpmLifecycle",
            "npm lifecycle scripts are declared: {scripts}.",
            &[(
                "scripts",
                items
                    .iter()
                    .take(5)
                    .filter_map(value_string)
                    .collect::<Vec<_>>()
                    .join(", "),
            )],
        ));
    }
    if let Some(value) = behavior.get("postInstallDefined").and_then(Value::as_bool) {
        signals.push(if value {
            if package.provider == "npm" {
                tx(
                    locale,
                    "signalNpmPostinstall",
                    "npm package metadata declares a postinstall script.",
                )
            } else {
                tx(
                    locale,
                    "signalHomebrewPostinstall",
                    "Homebrew declares a post-install hook for this formula.",
                )
            }
        } else if package.provider == "npm" {
            tx(
                locale,
                "signalNoNpmPostinstall",
                "No npm postinstall script is recorded in package metadata.",
            )
        } else {
            tx(
                locale,
                "signalNoHomebrewPostinstall",
                "No Homebrew post-install hook is recorded in formula metadata.",
            )
        });
    }
    if behavior.get("prepareDefined").and_then(Value::as_bool) == Some(true) {
        signals.push(tx(
            locale,
            "signalNpmPrepare",
            "npm package metadata declares a prepare script.",
        ));
    }
    if let Some(value) = value_str_key(behavior, "pythonRequires").filter(|value| !value.is_empty())
    {
        signals.push(txf(
            locale,
            "signalPythonRequires",
            "PyPI metadata requires Python {version}.",
            &[("version", value)],
        ));
    }
    if let Some(value) = value_i64_key(behavior, "requiresDistCount").filter(|value| *value > 0) {
        signals.push(txf(
            locale,
            "signalRequiresDistCount",
            "PyPI metadata lists {count} dependency specifications.",
            &[("count", fmt_int(value))],
        ));
    }
    if value_str_key(behavior, "service")
        .filter(|value| !value.is_empty())
        .is_some()
    {
        signals.push(tx(
            locale,
            "signalService",
            "Formula metadata declares a service or daemon block.",
        ));
    }
    if let Some(bottle) = full_value(package, "bottle").filter(|value| value.is_object()) {
        if bottle.get("available").and_then(Value::as_bool) == Some(true) {
            let platforms = value_array(bottle, "platforms");
            if platforms.is_empty() {
                signals.push(tx(
                    locale,
                    "signalBottleAvailable",
                    "Homebrew bottle metadata is available.",
                ));
            } else {
                signals.push(txf(
                    locale,
                    "signalBottlePlatforms",
                    "Homebrew bottle metadata is available for {count} platform targets.",
                    &[("count", fmt_int(platforms.len() as i64))],
                ));
            }
        } else {
            signals.push(tx(
                locale,
                "signalNoBottle",
                "No Homebrew bottle metadata was recorded.",
            ));
        }
    }
    let dependencies = full_string_array(package, "dependencies");
    if !dependencies.is_empty() {
        signals.push(txf(
            locale,
            "signalDependencies",
            "Installs with {count} runtime dependencies.",
            &[("count", fmt_int(dependencies.len() as i64))],
        ));
    }
    let build_dependencies = full_string_array(package, "buildDependencies");
    if !build_dependencies.is_empty() {
        signals.push(txf(
            locale,
            "signalBuildDependencies",
            "Build metadata lists {count} build dependencies.",
            &[("count", fmt_int(build_dependencies.len() as i64))],
        ));
    }
    if signals.is_empty() {
        String::new()
    } else {
        format!(
            r#"<div class="signal-grid install-signal-grid" aria-label="{}"><article><h3>{}</h3><ul>{}</ul></article></div>"#,
            html_escape(&tx(
                locale,
                "installBehaviorSignals",
                "Install behavior signals"
            )),
            html_escape(&tx(locale, "installBehaviorTitle", "Install behavior")),
            signals
                .into_iter()
                .take(6)
                .map(|item| format!("<li>{}</li>", html_escape(&item)))
                .collect::<String>()
        )
    }
}

fn render_gate(package: &PackageRow, locale: &Locale) -> String {
    let Some(gate) = full_value(package, "approvalGate").filter(|value| value.is_object()) else {
        return String::new();
    };
    let rules = value_array(gate, "rules")
        .into_iter()
        .filter_map(value_string)
        .map(|item| format!("<li>{}</li>", html_escape(&item)))
        .collect::<String>();
    let severities = full_value(package, "approvalGate")
        .map(|gate| value_array(gate, "severities"))
        .unwrap_or_default()
        .into_iter()
        .filter_map(value_string)
        .collect::<Vec<_>>()
        .join(", ");
    let entrypoints = value_array(gate, "entrypoints")
        .into_iter()
        .filter_map(value_string)
        .collect::<Vec<_>>()
        .join(", ");
    let reviewed = value_str_key(gate, "reviewed_at").unwrap_or_default();
    let reviewed_copy = if reviewed.is_empty() {
        String::new()
    } else {
        txf(
            locale,
            "approvalGateReviewedSuffix",
            ", reviewed {date}",
            &[("date", reviewed)],
        )
    };
    let severities = if severities.is_empty() {
        tx(locale, "notSpecified", "not specified")
    } else {
        severities
            .split(',')
            .map(|value| localized_standard_value(locale, value.trim()))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let coverage = value_str_key(gate, "coverage_status")
        .map(|value| localized_standard_value(locale, &value))
        .unwrap_or_else(|| tx(locale, "unknown", "unknown"));
    format!(
        r#"<section class="pkg-section split-section gate-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p></div><div class="detail-stack"><article><h3>{}</h3><ul>{}</ul></article></div></section>"#,
        html_escape(&tx(locale, "approvalGatesKicker", "approval gates")),
        html_escape(&tx(
            locale,
            "approvalGateHeading",
            "Human review metadata for risky commands"
        )),
        html_escape(&txf(
            locale,
            "approvalGateCopy",
            "The local approval-gate seed includes {count} rules for {name}. Covered entrypoints: {entrypoints}. Severity labels: {severities}. Coverage: {coverage}{reviewed}.",
            &[
                (
                    "count",
                    value_i64_key(gate, "rule_count").unwrap_or(0).to_string()
                ),
                ("name", package.display_name.clone()),
                (
                    "entrypoints",
                    if entrypoints.is_empty() {
                        package.display_name.clone()
                    } else {
                        entrypoints.clone()
                    }
                ),
                ("severities", severities.clone()),
                ("coverage", coverage.clone()),
                ("reviewed", reviewed_copy.clone()),
            ],
        )),
        html_escape(&tx(locale, "exampleGatedActions", "Example gated actions")),
        if rules.is_empty() {
            format!(
                "<li>{}</li>",
                html_escape(&tx(
                    locale,
                    "noApprovalRules",
                    "No rule descriptions were present."
                ))
            )
        } else {
            rules
        },
    )
}

fn render_executables(package: &PackageRow, locale: &Locale) -> String {
    let mut rows = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for item in value_array(&package.data.full, "executablesDetailed") {
        let name = value_str_key(item, "name")
            .or_else(|| value_str_key(item, "target"))
            .or_else(|| value_str_key(item, "source"))
            .unwrap_or_default();
        if name.is_empty() || !seen.insert(name.clone()) {
            continue;
        }
        rows.push(executable_row(
            &name,
            &value_str_key(item, "kind").unwrap_or_else(|| "executable".to_string()),
            &value_str_key(item, "exposure").unwrap_or_else(|| "global executable".to_string()),
            &value_str_key(item, "note").unwrap_or_default(),
            locale,
        ));
    }
    for item in value_array(&package.data.full, "binaries") {
        let name = value_str_key(item, "target")
            .or_else(|| value_str_key(item, "source"))
            .unwrap_or_default();
        if !name.is_empty() && seen.insert(name.clone()) {
            rows.push(executable_row(
                &name,
                "binary",
                "Homebrew cask binary",
                &value_str_key(item, "source").unwrap_or_default(),
                locale,
            ));
        }
    }
    let stub_exclusions = full_value(package, "extra")
        .and_then(|extra| extra.get("stub_exclusions"))
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(value_string)
                .collect::<std::collections::BTreeSet<_>>()
        })
        .unwrap_or_default();
    for alias in full_string_array(package, "aliases") {
        if seen.insert(alias.clone()) {
            rows.push(executable_row(
                &alias,
                "executable",
                if stub_exclusions.contains(&alias) {
                    "Automic Vault stub excluded"
                } else {
                    "indexed executable"
                },
                "Discovered from the local executable index.",
                locale,
            ));
        }
    }
    format!(
        r#"<section class="pkg-section" aria-labelledby="executables-title"><p class="section-kicker">{}</p><h2 id="executables-title">{}</h2><div class="table-wrap executable-table"><table><thead><tr><th>{}</th><th>{}</th><th>{}</th><th>{}</th></tr></thead><tbody>{}</tbody></table></div></section>"#,
        html_escape(&tx(locale, "executables", "executables")),
        html_escape(&tx(locale, "executablesTitle", "Installed executables")),
        html_escape(&tx(locale, "command", "Command")),
        html_escape(&tx(locale, "kind", "Kind")),
        html_escape(&tx(locale, "exposure", "Exposure")),
        html_escape(&tx(locale, "note", "Note")),
        if rows.is_empty() {
            format!(
                r#"<tr><td colspan="4">{}</td></tr>"#,
                html_escape(&tx(
                    locale,
                    "executableDataMissing",
                    "No executable data was present."
                ))
            )
        } else {
            rows.join("")
        }
    )
}

fn executable_row(name: &str, kind: &str, exposure: &str, note: &str, locale: &Locale) -> String {
    format!(
        "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td></tr>",
        html_escape(name),
        html_escape(&localized_executable_text(locale, kind)),
        html_escape(&localized_executable_text(locale, exposure)),
        html_escape(&localized_executable_text(locale, note))
    )
}

fn render_freshness(package: &PackageRow, generated_at: &str, locale: &Locale) -> String {
    let freshness = full_value(package, "versionFreshness").unwrap_or(&Value::Null);
    let manager = freshness.get("packageManager").unwrap_or(&Value::Null);
    let site = freshness.get("siteData").unwrap_or(&Value::Null);
    let upstream = freshness.get("upstream").unwrap_or(&Value::Null);
    let warnings = value_array(freshness, "warnings")
        .into_iter()
        .filter(|item| item.is_object())
        .map(|item| {
            let severity = value_str_key(item, "severity").unwrap_or_else(|| "info".to_string());
            let message = value_str_key(item, "message")
                .or_else(|| value_str_key(item, "kind"))
                .unwrap_or_else(|| "freshness".to_string());
            let evidence = value_str_key(item, "evidence").unwrap_or_default();
            let confidence = value_str_key(item, "confidence").unwrap_or_default();
            format!(
                r#"<li class="freshness-item freshness-{}"><strong>{}</strong><span>{}</span>{}{}</li>"#,
                html_escape(&severity),
                html_escape(&localized_standard_value(locale, &severity)),
                html_escape(&localized_freshness_message(locale, &message)),
                if evidence.is_empty() {
                    String::new()
                } else {
                    format!("<small>{}</small>", link_value(&evidence))
                },
                if confidence.is_empty() {
                    String::new()
                } else {
                    format!(
                        "<em>{}</em>",
                        html_escape(&txf(
                            locale,
                            "confidenceSuffix",
                            "{confidence} confidence",
                            &[(
                                "confidence",
                                localized_standard_value(locale, &confidence)
                            )],
                        ))
                    )
                }
            )
        })
        .collect::<String>();
    let warnings = if warnings.is_empty() {
        format!(
            r#"<li class="freshness-item freshness-info"><strong>{}</strong><span>{}</span></li>"#,
            html_escape(&localized_standard_value(locale, "ok")),
            html_escape(&tx(
                locale,
                "noFreshnessWarnings",
                "No freshness warnings were generated."
            ))
        )
    } else {
        warnings
    };
    let repository = value_str_key(upstream, "repository").unwrap_or_default();
    format!(
        r#"<section class="pkg-section split-section freshness-section" aria-labelledby="freshness-title" data-pagefind-ignore="all"><div><p class="section-kicker">{}</p><h2 id="freshness-title">{}</h2><p>{}</p></div><div><div class="freshness-metrics"><div><span>{}</span><strong>{}</strong></div><div><span>{}</span><strong>{}</strong></div><div><span>{}</span><strong>{}</strong></div><div><span>{}</span><strong>{}</strong></div><div><span>{}</span><strong>{}</strong></div><div><span>{}</span><strong>{}</strong></div></div>{}<ul class="freshness-list">{}</ul></div></section>"#,
        html_escape(&tx(locale, "freshness", "freshness")),
        html_escape(&tx(locale, "freshnessTitle", "Version and freshness")),
        html_escape(&tx(
            locale,
            "freshnessCopy",
            "These signals separate page generation age, package-manager activity, and upstream release comparison. Version lag is warned only when an evidence URL and comparable versions are present."
        )),
        html_escape(&tx(locale, "pageGenerated", "page generated")),
        html_escape(&fmt_date(generated_at)),
        html_escape(&tx(locale, "managerVersion", "manager version")),
        html_escape(
            &value_str_key(manager, "version")
                .unwrap_or_else(|| empty_as_unknown(&package.version).to_string())
        ),
        html_escape(&tx(locale, "managerUpdated", "manager updated")),
        html_escape(&fmt_date(
            &value_str_key(manager, "updatedAt").unwrap_or_else(|| package.last_updated_at.clone())
        )),
        html_escape(&tx(locale, "localData", "local data")),
        html_escape(&localized_standard_value(
            locale,
            &value_str_key(site, "status").unwrap_or_else(|| "unknown".to_string())
        )),
        html_escape(&tx(locale, "upstream", "upstream")),
        html_escape(&localized_standard_value(
            locale,
            &value_str_key(upstream, "comparison").unwrap_or_else(|| "not available".to_string())
        )),
        html_escape(&tx(locale, "upstreamLatestDetected", "latest detected")),
        html_escape(&localized_standard_value(
            locale,
            &value_str_key(upstream, "latestVersion").unwrap_or_else(|| "not detected".to_string())
        )),
        if repository.is_empty() {
            String::new()
        } else {
            format!(
                r#"<p class="freshness-repo"><a href="{}">{}</a></p>"#,
                html_escape(&repository),
                html_escape(&repository)
            )
        },
        warnings
    )
}

fn authoritative_package_version(package: &PackageRow) -> String {
    full_value(package, "versionFreshness")
        .and_then(|value| value.get("packageManager"))
        .and_then(|value| value_str_key(value, "version"))
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| package.version.clone())
}

fn display_last_verified(package: &PackageRow) -> String {
    let verified = full_str(package, "lastVerified");
    if verified.is_empty() {
        return String::new();
    }
    let authoritative = authoritative_package_version(package);
    if !package.version.is_empty() && !authoritative.is_empty() && package.version != authoritative
    {
        return String::new();
    }
    verified
}

fn render_install_metadata(package: &PackageRow, locale: &Locale) -> String {
    let mut rows = Vec::new();
    for (label, value) in [
        (
            tx(locale, "packageKey", "Package key"),
            package.package_key.clone(),
        ),
        (
            tx(locale, "version", "Version"),
            authoritative_package_version(package),
        ),
        (
            tx(locale, "packageManager", "Package manager"),
            package.provider_label.clone(),
        ),
        (
            tx(locale, "packageManagerPage", "Package manager page"),
            package.package_manager_url.clone(),
        ),
        (tx(locale, "homepage", "Homepage"), package.homepage.clone()),
        (
            tx(locale, "repository", "Repository"),
            package.repository.clone(),
        ),
        (
            tx(locale, "upstreamDocs", "Upstream docs"),
            full_str(package, "upstreamDocs"),
        ),
        (tx(locale, "license", "License"), package.license.clone()),
        (
            tx(locale, "sourceArchive", "Source archive"),
            full_str(package, "sourceArchive"),
        ),
        (
            tx(locale, "issueTracker", "Issue tracker"),
            full_str(package, "issueTracker"),
        ),
        (
            tx(locale, "lastUpdated", "Last updated"),
            package.last_updated_at.clone(),
        ),
        (
            tx(locale, "lastVerified", "Last verified"),
            display_last_verified(package),
        ),
        (
            tx(locale, "published", "Published"),
            full_str(package, "publishedAt"),
        ),
        (tx(locale, "pulse", "Pulse"), full_str(package, "pulseKind")),
        (tx(locale, "sha256", "SHA-256"), full_str(package, "sha256")),
        (
            tx(locale, "downloadUrl", "Download URL"),
            full_str(package, "url"),
        ),
    ] {
        if !value.is_empty() {
            rows.push((label.to_string(), value));
        }
    }
    push_joined_row(
        &mut rows,
        &tx(locale, "dependencies", "Dependencies"),
        &full_string_array(package, "dependencies"),
    );
    push_joined_row(
        &mut rows,
        &tx(locale, "buildDependencies", "Build dependencies"),
        &full_string_array(package, "buildDependencies"),
    );
    push_joined_row(
        &mut rows,
        &tx(locale, "usesFromMacos", "Uses from macOS"),
        &full_string_array(package, "usesFromMacos"),
    );
    if let Some(bottle) = full_value(package, "bottle").filter(|value| value.is_object()) {
        let mut detail = if bottle.get("available").and_then(Value::as_bool) == Some(true) {
            tx(locale, "available", "available")
        } else {
            tx(locale, "notRecorded", "not recorded")
        };
        let platforms = value_array(bottle, "platforms")
            .into_iter()
            .filter_map(value_string)
            .collect::<Vec<_>>();
        if !platforms.is_empty() {
            detail.push_str(&format!(
                " ({} {})",
                tx(locale, "onPlatforms", "on"),
                platforms.join(", ")
            ));
        }
        rows.push((tx(locale, "bottle", "Bottle"), detail));
    }
    if let Some(behavior) = full_value(package, "installBehavior").filter(|value| value.is_object())
    {
        if let Some(post_install) = behavior.get("postInstallDefined").and_then(Value::as_bool) {
            rows.push((
                if package.provider == "npm" {
                    tx(locale, "npmPostinstall", "npm postinstall")
                } else {
                    tx(locale, "homebrewPostinstall", "Homebrew post-install")
                }
                .to_string(),
                if post_install {
                    tx(locale, "defined", "defined")
                } else {
                    tx(locale, "notDefined", "not defined")
                }
                .to_string(),
            ));
        }
        rows.push((
            tx(locale, "service", "Service"),
            value_str_key(behavior, "service")
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| tx(locale, "serviceNone", "none declared")),
        ));
        if let Some(caveats) = value_str_key(behavior, "caveats").filter(|value| !value.is_empty())
        {
            rows.push((tx(locale, "caveats", "Caveats"), caveats));
        }
    }
    push_joined_row(
        &mut rows,
        &tx(locale, "keywords", "Keywords"),
        &full_string_array(package, "keywords"),
    );
    push_joined_row(
        &mut rows,
        &tx(locale, "classifiers", "Classifiers"),
        &full_string_array(package, "classifiers"),
    );
    let row_html = rows
        .into_iter()
        .map(|(label, value)| {
            format!(
                "<tr><th>{}</th><td>{}</td></tr>",
                html_escape(&label),
                link_value(&value)
            )
        })
        .collect::<String>();
    format!(
        r#"<section class="pkg-section"><p class="section-kicker">{}</p><h2>{}</h2><div class="table-wrap"><table><tbody>{}</tbody></table></div></section>"#,
        html_escape(&tx(locale, "packageMetadataKicker", "install metadata")),
        html_escape(&tx(locale, "metadataTitle", "Package metadata")),
        if row_html.is_empty() {
            format!(
                "<tr><th>{}</th><td>{}</td></tr>",
                html_escape(&tx(locale, "status", "Status")),
                html_escape(&tx(
                    locale,
                    "metadataEmpty",
                    "No resolver details were present."
                ))
            )
        } else {
            row_html
        }
    )
}

fn render_registry_insights(package: &PackageRow, locale: &Locale) -> String {
    let Some(insights) = registry_insights(package) else {
        return String::new();
    };
    let rows = registry_insight_rows(insights);
    if rows.is_empty() {
        return String::new();
    }
    let row_html = rows
        .into_iter()
        .take(28)
        .map(|(label, value)| {
            format!(
                "<tr><th>{}</th><td>{}</td></tr>",
                html_escape(&label),
                metadata_value_html(value)
            )
        })
        .collect::<String>();
    format!(
        r#"<section class="pkg-section registry-insights-section"><p class="section-kicker">{}</p><h2>{}</h2><div class="table-wrap registry-insights-table"><table><tbody>{}</tbody></table></div></section>"#,
        html_escape(&tx(locale, "registryFacts", "registry facts")),
        html_escape(&tx(
            locale,
            "registryInsightsTitle",
            "Source database details"
        )),
        row_html
    )
}

fn render_external_package_manager_matches(package: &PackageRow, locale: &Locale) -> String {
    let matches = external_package_manager_matches(package);
    if matches.is_empty() {
        return String::new();
    }
    let match_html = matches
        .into_iter()
        .take(16)
        .map(|item| external_package_match_card(item, locale))
        .collect::<String>();
    format!(
        r#"<section class="pkg-section split-section manager-match-section" aria-labelledby="manager-match-title"><div><p class="section-kicker">{}</p><h2 id="manager-match-title">{}</h2><p>{}</p></div><div class="manager-match-grid">{}</div></section>"#,
        html_escape(&tx(
            locale,
            "sourceDatabaseMatches",
            "source database matches"
        )),
        html_escape(&tx(
            locale,
            "otherPackageManagersTitle",
            "Other package-manager records"
        )),
        html_escape(&tx(
            locale,
            "otherPackageManagersCopy",
            "Matches are pulled from external package-manager indexes and kept separate from local Automic Vault package links."
        )),
        match_html
    )
}

fn external_package_match_card(item: &Value, locale: &Locale) -> String {
    let metadata = item
        .get("metadata")
        .filter(|value| value.is_object())
        .unwrap_or(&Value::Null);
    let version = value_str_key(metadata, "version").unwrap_or_default();
    let summary = value_str_key(metadata, "summary")
        .or_else(|| value_str_key(metadata, "description"))
        .unwrap_or_default();
    let homepage = value_str_key(metadata, "homepage").unwrap_or_default();
    let source = item
        .get("source")
        .filter(|value| value.is_object())
        .unwrap_or(&Value::Null);
    let source_url = value_str_key(source, "sourceUrl")
        .or_else(|| value_str_key(source, "source_url"))
        .unwrap_or_default();
    let source_label = value_str_key(source, "sourceLabel")
        .or_else(|| value_str_key(source, "source_label"))
        .unwrap_or_default();
    let display_name = value_str_key(item, "displayName")
        .or_else(|| value_str_key(item, "manager"))
        .unwrap_or_default();
    let package_id = value_str_key(item, "packageId")
        .or_else(|| value_str_key(item, "packageName"))
        .or_else(|| value_str_key(item, "package_name"))
        .unwrap_or_default();
    let confidence = value_f64_key(item, "confidence")
        .map(|value| format!("{:.0}%", value * 100.0))
        .or_else(|| value_str_key(item, "confidence"))
        .unwrap_or_default();
    let command = value_str_key(item, "command").unwrap_or_default();
    let mut fact_bits = Vec::new();
    for key in [
        "license",
        "section",
        "category",
        "architecture",
        "sourcePackage",
    ] {
        if let Some(value) = value_str_key(metadata, key).filter(|value| !value.is_empty()) {
            fact_bits.push(format!("{}: {value}", human_metadata_label(key)));
        }
    }
    for (key, label) in [
        ("dependencies", tx(locale, "dependencies", "dependencies")),
        ("provides", tx(locale, "provides", "provides")),
        (
            "optionalDependencies",
            tx(locale, "optionalDependencies", "optional deps"),
        ),
    ] {
        let count = metadata
            .get(key)
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or(0);
        if count > 0 {
            fact_bits.push(format!("{count} {label}"));
        }
    }
    if let Some(reason) = value_str_key(item, "reason").filter(|value| !value.is_empty()) {
        fact_bits.push(reason);
    }
    if let Some(matched_by) = value_str_key(item, "matchedBy").filter(|value| !value.is_empty()) {
        fact_bits.push(txf(
            locale,
            "matchedBy",
            "Matched by: {value}",
            &[("value", human_metadata_label(&matched_by))],
        ));
    }
    let facts = fact_bits
        .into_iter()
        .take(8)
        .map(|bit| format!("<li>{}</li>", html_escape(&bit)))
        .collect::<String>();
    let source_link = if !source_url.is_empty() {
        format!(
            r#"<a href="{}">{}</a>"#,
            html_escape(&source_url),
            html_escape(source_host_label(&source_url))
        )
    } else {
        String::new()
    };
    let source_label = html_escape(&source_label);
    let source_html = [source_label, source_link]
        .into_iter()
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>()
        .join(" · ");
    let evidence = value_str_key(item, "evidence").unwrap_or_default();
    let source_tail = [source_html, html_escape(&evidence)]
        .into_iter()
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>()
        .join(" · ");
    format!(
        r#"<article class="manager-match-card"><div class="manager-match-head"><strong>{}</strong><span>{}</span></div><p><code>{}</code>{}</p>{}{}{}{}{}</article>"#,
        html_escape(&display_name),
        html_escape(&confidence),
        html_escape(&package_id),
        if version.is_empty() {
            String::new()
        } else {
            format!(" <em>{}</em>", html_escape(&version))
        },
        if summary.is_empty() {
            String::new()
        } else {
            format!("<p>{}</p>", html_escape(&short_text(&summary, 180)))
        },
        if homepage.is_empty() {
            String::new()
        } else {
            format!(
                r#"<p><a href="{}">{}</a></p>"#,
                html_escape(&homepage),
                html_escape(&homepage)
            )
        },
        if command.is_empty() {
            String::new()
        } else {
            format!("<pre><code>{}</code></pre>", html_escape(&command))
        },
        if facts.is_empty() {
            String::new()
        } else {
            format!("<ul>{facts}</ul>")
        },
        if source_tail.is_empty() {
            String::new()
        } else {
            format!("<small>{source_tail}</small>")
        }
    )
}

fn render_related(package: &PackageRow, locale: &Locale) -> String {
    let hubs = value_array(&package.data.full, "packageHubs")
        .into_iter()
        .take(4)
        .filter_map(|hub| {
            let slug = value_str_key(hub, "slug")?;
            if slug.is_empty() {
                return None;
            }
            let label = value_str_key(hub, "label").unwrap_or_else(|| slug.clone());
            let reason = value_str_key(hub, "reason").unwrap_or_default();
            Some(format!(
                r#"<li><a href="{}">{}</a>{}</li>"#,
                html_escape(&locale_path(&format!("/pkg/{slug}/"), locale)),
                html_escape(&label),
                if reason.is_empty() {
                    String::new()
                } else {
                    format!("<span>{}</span>", html_escape(&reason))
                }
            ))
        })
        .collect::<Vec<_>>();
    let related = related_links(package, locale, "relatedPackages", 8, false);
    let workflow = related_links(package, locale, "relatedPackages", 6, true);
    let also = related_links(package, locale, "alsoAvailableVia", 4, false);
    let guides = core_security_guides(package, locale);
    let columns = [
        related_article(&tx(locale, "topicalHubs", "Topical hubs"), hubs),
        related_article(&tx(locale, "relatedTools", "Related tools"), related),
        related_article(
            &tx(locale, "sameWorkflow", "Same workflow"),
            [workflow, also].concat(),
        ),
        related_article(
            &tx(locale, "agentSecurityGuides", "Agent security guides"),
            guides,
        ),
    ]
    .into_iter()
    .filter(|value| !value.is_empty())
    .collect::<String>();
    format!(
        r#"<section class="pkg-section split-section related-section" aria-labelledby="related-title"><div><p class="section-kicker">{}</p><h2 id="related-title">{}</h2><p>{}</p></div><div class="related-columns">{}</div></section>"#,
        html_escape(&tx(locale, "packageGraph", "package graph")),
        html_escape(&tx(locale, "internalLinks", "Internal package links")),
        html_escape(&tx(
            locale,
            "packageGraphCopy",
            "Links come from deterministic package relationships, av.db category and tag curation, ecosystem matches, and package hub membership."
        )),
        columns
    )
}

fn render_sources(package: &PackageRow, locale: &Locale) -> String {
    let mut notes = full_string_array(package, "sourceNotes");
    notes.sort();
    notes.dedup();
    if notes.is_empty() {
        notes.push("local package generator".to_string());
    }
    let source_file = combined_yaml_source_card(package, locale);
    let items = notes
        .into_iter()
        .map(|note| format!("<li>{}</li>", html_escape(&note)))
        .collect::<String>();
    format!(
        r#"<section class="pkg-section split-section sources-section"><div><p class="section-kicker">{}</p><h2>{}</h2><p>{}</p></div><div class="detail-stack">{}<article><h3>{}</h3><ul>{}</ul></article></div></section>"#,
        html_escape(&tx(locale, "sourceTrail", "source trail")),
        html_escape(&tx(
            locale,
            "generatedFromRepositoryData",
            "Generated from repository data"
        )),
        tx(
            locale,
            "sourcesCopy",
            "This page is generated by <code>av-web</code> from the private package SQLite artifact built by <code>scripts/generate-pkg-sqlite.py</code>."
        ),
        source_file,
        html_escape(&tx(locale, "usedSources", "Used sources")),
        items
    )
}

fn combined_yaml_source_card(package: &PackageRow, locale: &Locale) -> String {
    let url = full_str(package, "combinedYamlUrl");
    if url.is_empty() {
        return String::new();
    }
    let path = full_str(package, "combinedYamlPath");
    let label = if path.is_empty() { &url } else { &path };
    format!(
        r#"<article><h3>{}</h3><p>{}</p><p><a href="{}"><code>{}</code></a></p></article>"#,
        html_escape(&tx(locale, "combinedYamlSource", "Combined YAML source")),
        html_escape(&tx(
            locale,
            "combinedYamlSourceCopy",
            "View the package source record on GitHub."
        )),
        html_escape(&url),
        html_escape(label)
    )
}

fn package_isotope(package: &PackageRow) -> bool {
    full_value(package, "isotope")
        .filter(|value| value.is_object())
        .is_some()
}

fn package_gate(package: &PackageRow) -> bool {
    full_value(package, "approvalGate")
        .filter(|value| value.is_object())
        .is_some()
}

fn label_for(package: &PackageRow, locale: &Locale) -> String {
    let mut labels = vec![package.provider.clone()];
    if package_isotope(package) {
        labels.push(tx(locale, "radioisotopeKicker", "protected-tool coverage"));
    }
    if package_gate(package) {
        labels.push(tx(locale, "approvalGatesKicker", "approval gates"));
    }
    if let Some(rank) = package.rank {
        labels.push(format!("{} {rank}", tx(locale, "rank", "rank")));
    }
    labels.join(" / ")
}

fn hub_group_sections(hubs: &[HubSummary], locale: &Locale) -> String {
    let mut sections = Vec::new();
    for (group, label) in [
        (
            "security",
            tx(locale, "hubSecurityGroupTitle", "Tooling hubs"),
        ),
        (
            "topical",
            tx(locale, "hubTopicalGroupTitle", "Topical hubs"),
        ),
        (
            "ecosystem",
            tx(locale, "hubEcosystemGroupTitle", "Ecosystem hubs"),
        ),
    ] {
        let mut group_hubs = hubs
            .iter()
            .filter(|summary| summary.hub.group == group)
            .collect::<Vec<_>>();
        group_hubs.sort_by(|left, right| {
            right
                .package_count
                .cmp(&left.package_count)
                .then_with(|| left.hub.title.cmp(&right.hub.title))
        });
        let cards = group_hubs
            .into_iter()
            .map(|summary| {
                format!(
                    r#"<a class="hub-card" href="{}"><span>{}</span><strong>{}</strong><small>{}</small></a>"#,
                    html_escape(&locale_path(&summary.hub.path, locale)),
                    html_escape(&summary.hub.title),
                    html_escape(&fmt_int(summary.package_count)),
                    html_escape(&short_text(&summary.hub.description, 96))
                )
            })
            .collect::<String>();
        if !cards.is_empty() {
            sections.push(format!(
                r#"<section class="hub-group"><h3>{}</h3><div class="hub-grid">{}</div></section>"#,
                html_escape(&label),
                cards
            ));
        }
    }
    sections.join("")
}

fn index_package_row(package: &PackageRow, locale: &Locale) -> String {
    format!(
        r#"<a class="package-row" href="{}"><span>{}</span><small>{}</small></a>"#,
        html_escape(&locale_path(&package.path, locale)),
        html_escape(&package.display_name),
        html_escape(&label_for(package, locale))
    )
}

fn hub_cluster_block(title: &str, packages: &[&PackageRow], locale: &Locale) -> String {
    if packages.is_empty() {
        return String::new();
    }
    let cards = packages
        .iter()
        .map(|package| hub_spoke_card(package, locale))
        .collect::<String>();
    format!(
        r#"<section class="pkg-section hub-cluster"><h2>{}</h2><div class="package-list hub-spoke-list">{}</div></section>"#,
        html_escape(title),
        cards
    )
}

fn hub_spoke_card(package: &PackageRow, locale: &Locale) -> String {
    format!(
        r#"<a class="package-row" href="{}"><span>{}</span><small>{}</small></a>"#,
        html_escape(&locale_path(&package.path, locale)),
        html_escape(&package.display_name),
        html_escape(&hub_package_reason(package, locale))
    )
}

fn hub_related_block(title: &str, links: &[String]) -> String {
    if links.is_empty() {
        return String::new();
    }
    format!(
        r#"<section class="pkg-section hub-cluster"><h2>{}</h2><div class="hub-related-list">{}</div></section>"#,
        html_escape(title),
        links.join("")
    )
}

fn related_hub_links(
    connection: &Connection,
    hub: &HubRow,
    locale: &Locale,
) -> Result<Vec<String>, String> {
    let mut statement = connection
        .prepare(
            "SELECT h.slug, h.title, h.description, COUNT(*) AS overlap_count
             FROM hub_packages current
             JOIN hub_packages related
               ON related.package_key = current.package_key
              AND related.hub_slug <> current.hub_slug
             JOIN hubs h ON h.slug = related.hub_slug
             WHERE current.hub_slug = ?1
             GROUP BY h.slug, h.title, h.description
             ORDER BY overlap_count DESC, h.title
             LIMIT 8",
        )
        .map_err(|err| format!("failed to prepare related hub query: {err}"))?;
    let rows = statement
        .query_map(params![hub.slug], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })
        .map_err(|err| format!("failed to query related hubs: {err}"))?;
    let links = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("failed to read related hubs: {err}"))?
        .into_iter()
        .map(|(slug, label, reason, count)| {
            let fallback_reason = tx(locale, "packageGraph", "package graph");
            let reason = if reason.is_empty() {
                fallback_reason.as_str()
            } else {
                reason.as_str()
            };
            format!(
                r#"<a class="hub-related-card" href="{}"><span>{}</span><small>{}</small><strong>{}</strong></a>"#,
                html_escape(&locale_path(&format!("/pkg/{slug}/"), locale)),
                html_escape(&label),
                html_escape(reason),
                html_escape(&fmt_int(count))
            )
        })
        .collect();
    Ok(links)
}

fn hub_package_reason(package: &PackageRow, locale: &Locale) -> String {
    if let Some(isotope) = full_value(package, "isotope").filter(|value| value.is_object()) {
        if let Some(title) = isotope
            .get("justification")
            .and_then(|justification| value_str_key(justification, "title"))
        {
            return title;
        }
    }
    if let Some(gate) = full_value(package, "approvalGate").filter(|value| value.is_object()) {
        return txf(
            locale,
            "hubPackageReasonApproval",
            "{count} approval-gate rules are present.",
            &[(
                "count",
                value_i64_key(gate, "rule_count")
                    .map(fmt_int)
                    .unwrap_or_else(|| "Local".to_string()),
            )],
        );
    }
    if let Some(geiger) = full_value(package, "geiger").filter(|value| value.is_object()) {
        if let Some(reason) = value_array(geiger, "reasons")
            .into_iter()
            .find_map(value_string)
        {
            return short_text(&reason, 140);
        }
    }
    if !package.summary.is_empty() {
        return short_text(&package.summary, 140);
    }
    let aliases = full_string_array(package, "aliases");
    if !aliases.is_empty() {
        return format!(
            "{}",
            txf(
                locale,
                "hubPackageReasonAlias",
                "Executable aliases include {aliases}.",
                &[(
                    "aliases",
                    aliases.into_iter().take(4).collect::<Vec<_>>().join(", "),
                )],
            )
        );
    }
    tx(
        locale,
        "hubPackageReasonDefault",
        "Matched package metadata for this hub.",
    )
}

fn hub_package_row(package: &PackageRow, reason: &str, locale: &Locale) -> String {
    let mut signals = Vec::new();
    if package_isotope(package) {
        signals.push(tx(locale, "radioisotopeKicker", "protected-tool coverage"));
    }
    if package_gate(package) {
        signals.push(tx(locale, "approvalGatesKicker", "approval gate"));
    }
    if let Some(geiger) = full_value(package, "geiger").filter(|value| value.is_object()) {
        signals.push(txf(
            locale,
            "riskLevel",
            "{level} risk",
            &[(
                "level",
                localized_standard_value(
                    locale,
                    &value_str_key(geiger, "level").unwrap_or_else(|| "unknown".to_string()),
                ),
            )],
        ));
    }
    let version = authoritative_package_version(package);
    if !version.is_empty() {
        signals.push(format!("v{}", version));
    }
    let reason = if reason.trim().is_empty() {
        hub_package_reason(package, locale)
    } else {
        reason.to_string()
    };
    format!(
        r#"<tr><td><a href="{}">{}</a></td><td>{}</td><td>{}</td><td>{}</td></tr>"#,
        html_escape(&package.path),
        html_escape(&package.display_name),
        html_escape(&package.provider_label),
        html_escape(&if signals.is_empty() {
            label_for(package, locale)
        } else {
            signals.join(", ")
        }),
        html_escape(&reason)
    )
}

fn schema_for_hub(
    hub: &HubRow,
    pages: Vec<&PackageRow>,
    description: &str,
    updated: &str,
    locale: &Locale,
) -> Value {
    let url = locale_url(&hub.path, locale);
    let items = pages
        .into_iter()
        .take(72)
        .enumerate()
        .map(|(index, package)| {
            json!({
                "@type": "ListItem",
                "position": index + 1,
                "url": locale_url(&package.path, locale),
                "name": package.display_name,
                "description": hub_package_reason(package, locale)
            })
        })
        .collect::<Vec<_>>();
    json!({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "@id": format!("{SITE_ORIGIN}/#website"), "name": SITE_NAME, "url": format!("{SITE_ORIGIN}/")},
            {"@type": "Organization", "@id": format!("{SITE_ORIGIN}/#organization"), "name": SITE_NAME, "url": format!("{SITE_ORIGIN}/")},
            {"@type": "Person", "@id": "https://github.com/mxcl#person", "name": "Max Howell", "url": "https://github.com/mxcl"},
            {
                "@type": "CollectionPage",
                "@id": format!("{url}#webpage"),
                "name": hub.title,
                "headline": hub.title,
                "url": url,
                "description": description,
                "inLanguage": locale.hreflang,
                "dateModified": updated,
                "isPartOf": {"@id": format!("{SITE_ORIGIN}/#website")},
                "author": {"@id": "https://github.com/mxcl#person"},
                "publisher": {"@id": format!("{SITE_ORIGIN}/#organization")},
                "mainEntity": {"@id": format!("{url}#list")}
            },
            {
                "@type": "ItemList",
                "@id": format!("{url}#list"),
                "name": hub.title,
                "itemListElement": items
            }
        ]
    })
}

fn full_value<'a>(package: &'a PackageRow, key: &str) -> Option<&'a Value> {
    package.data.full.get(key).filter(|value| !value.is_null())
}

fn full_str(package: &PackageRow, key: &str) -> String {
    full_opt_str(package, key).unwrap_or_default()
}

fn full_opt_str(package: &PackageRow, key: &str) -> Option<String> {
    full_value(package, key).and_then(value_string)
}

fn full_string_array(package: &PackageRow, key: &str) -> Vec<String> {
    let Some(items) = full_value(package, key).and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut seen = std::collections::BTreeSet::new();
    let mut values = Vec::new();
    for item in items {
        let value = value_string(item)
            .or_else(|| value_str_key(item, "label"))
            .or_else(|| value_str_key(item, "name"))
            .or_else(|| value_str_key(item, "target"))
            .or_else(|| value_str_key(item, "source"))
            .or_else(|| value_str_key(item, "package"))
            .or_else(|| value_str_key(item, "key"))
            .unwrap_or_default();
        let value = value.trim();
        if !value.is_empty() && seen.insert(value.to_string()) {
            values.push(value.to_string());
        }
    }
    values
}

fn taxonomy_tags(package: &PackageRow) -> Vec<String> {
    let Some(items) = full_value(package, "extra")
        .and_then(|extra| extra.get("pkgTaxonomy"))
        .and_then(|taxonomy| taxonomy.get("tags"))
        .and_then(Value::as_array)
    else {
        return Vec::new();
    };
    items.iter().filter_map(value_string).take(5).collect()
}

fn value_array<'a>(value: &'a Value, key: &str) -> Vec<&'a Value> {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(|items| items.iter().collect())
        .unwrap_or_default()
}

fn value_string(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => {
            let text = text.trim();
            (!text.is_empty()).then(|| text.to_string())
        }
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        _ => None,
    }
}

fn value_str_key(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(value_string)
}

fn value_i64_key(value: &Value, key: &str) -> Option<i64> {
    value.get(key).and_then(|value| {
        value
            .as_i64()
            .or_else(|| value.as_u64().and_then(|value| i64::try_from(value).ok()))
            .or_else(|| value.as_str().and_then(|value| value.parse().ok()))
    })
}

fn value_f64_key(value: &Value, key: &str) -> Option<f64> {
    value.get(key).and_then(|value| {
        value
            .as_f64()
            .or_else(|| value.as_str().and_then(|value| value.parse().ok()))
    })
}

fn first_non_empty(values: &[String]) -> Option<String> {
    values
        .iter()
        .map(|value| value.trim())
        .find(|value| !value.is_empty())
        .map(str::to_string)
}

fn fmt_date(value: &str) -> String {
    let value = value.trim();
    if value.len() >= 10 && value.as_bytes().get(4) == Some(&b'-') {
        value[..10].to_string()
    } else {
        value.to_string()
    }
}

fn fmt_int(value: i64) -> String {
    let mut digits = value.abs().to_string();
    let mut out = String::new();
    while digits.len() > 3 {
        let tail = digits.split_off(digits.len() - 3);
        if out.is_empty() {
            out = tail;
        } else {
            out = format!("{tail},{out}");
        }
    }
    if out.is_empty() {
        out = digits;
    } else if !digits.is_empty() {
        out = format!("{digits},{out}");
    }
    if value < 0 { format!("-{out}") } else { out }
}

fn locale_url(path: &str, locale: &Locale) -> String {
    format!("{SITE_ORIGIN}{}", locale_path(path, locale))
}

fn tx(locale: &Locale, key: &str, default: &str) -> String {
    if locale.code == "en" {
        return default.to_string();
    }
    let templates = I18N_PKG_TEMPLATES
        .get_or_init(|| serde_json::from_str(I18N_PKG_TEMPLATES_JSON).unwrap_or(Value::Null));
    templates
        .get(locale.code)
        .and_then(|items| items.get(key))
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_string()
}

fn txf(locale: &Locale, key: &str, default: &str, replacements: &[(&str, String)]) -> String {
    let mut value = tx(locale, key, default);
    for (name, replacement) in replacements {
        value = value.replace(&format!("{{{name}}}"), replacement);
    }
    value
}

fn localized_standard_value(locale: &Locale, value: &str) -> String {
    let trimmed = value.trim();
    let normalized = trimmed.to_ascii_lowercase().replace(['_', '-'], " ");
    let (key, default) = match normalized.as_str() {
        "available" => ("available", "available"),
        "behind" => ("valueBehind", "behind"),
        "critical" => ("valueCritical", "critical"),
        "current" => ("valueCurrent", "current"),
        "defined" => ("defined", "defined"),
        "draft" => ("valueDraft", "draft"),
        "green" => ("valueGreen", "green"),
        "high" => ("valueHigh", "high"),
        "info" => ("valueInfo", "info"),
        "inferred" => ("inferred", "inferred"),
        "low" => ("valueLow", "low"),
        "manual" => ("valueManual", "manual"),
        "medium" => ("valueMedium", "medium"),
        "not available" => ("notAvailable", "not available"),
        "not defined" => ("notDefined", "not defined"),
        "not detected" => ("notDetected", "not detected"),
        "not recorded" => ("notRecorded", "not recorded"),
        "not specified" => ("notSpecified", "not specified"),
        "ok" => ("valueOk", "ok"),
        "reviewed" => ("reviewed", "reviewed"),
        "strong" => ("valueStrong", "strong"),
        "uncategorized" => ("uncategorized", "uncategorized"),
        "unknown" => ("unknown", "unknown"),
        "verified" => ("verified", "verified"),
        "warning" => ("valueWarning", "warning"),
        _ => return trimmed.to_string(),
    };
    tx(locale, key, default)
}

fn localized_executable_text(locale: &Locale, value: &str) -> String {
    match value.trim() {
        "Automic Vault stub excluded" => tx(locale, "stubExcluded", "Automic Vault stub excluded"),
        "Discovered from the local executable index." => tx(
            locale,
            "localExecutableIndex",
            "Discovered from the local executable index.",
        ),
        "Homebrew cask binary" => tx(locale, "homebrewCaskBinary", "Homebrew cask binary"),
        "binary" => tx(locale, "binary", "binary"),
        "completion" => tx(locale, "completion", "completion"),
        "executable" => tx(locale, "executable", "executable"),
        "global executable" => tx(locale, "globalExecutable", "global executable"),
        "helper" => tx(locale, "helper", "helper"),
        "indexed executable" => tx(locale, "indexedExecutable", "indexed executable"),
        other => other.to_string(),
    }
}

fn localized_freshness_message(locale: &Locale, message: &str) -> String {
    match message.trim() {
        "freshness" => tx(locale, "freshness", "freshness"),
        "stale_source" => tx(
            locale,
            "freshnessWarningStaleSource",
            "Source data may be stale.",
        ),
        "Upstream has a newer patch release." => tx(
            locale,
            "freshnessWarningNewerPatch",
            "Upstream has a newer patch release.",
        ),
        other => other.to_string(),
    }
}

fn js_string_escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
        .replace('<', "\\u003c")
}

fn source_host_label(url: &str) -> &str {
    url.trim_start_matches("https://")
        .trim_start_matches("http://")
        .split('/')
        .next()
        .unwrap_or(url)
}

fn sentence_text(value: &str) -> String {
    let mut text = normalize_space(value);
    if text.is_empty() {
        return text;
    }
    if !matches!(text.as_bytes().last(), Some(b'.' | b'!' | b'?')) {
        text.push('.');
    }
    text
}

fn normalize_space(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn short_text(value: &str, limit: usize) -> String {
    let value = normalize_space(value);
    if value.chars().count() <= limit {
        return value;
    }
    let mut text = String::new();
    for word in value.split_whitespace() {
        let extra = if text.is_empty() { 0 } else { 1 };
        if text.chars().count() + word.chars().count() + extra > limit.saturating_sub(1) {
            break;
        }
        if !text.is_empty() {
            text.push(' ');
        }
        text.push_str(word);
    }
    if text.is_empty() {
        value
            .chars()
            .take(limit.saturating_sub(1))
            .collect::<String>()
            + "…"
    } else {
        text + "…"
    }
}

fn hero_sentence(package: &PackageRow) -> String {
    let summary = normalize_space(&package.summary);
    if !summary.is_empty() && !package.install_command.is_empty() {
        let verified = first_non_empty(&[
            display_last_verified(package),
            package.last_updated_at.clone(),
        ])
        .map(|value| fmt_date(&value))
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "from local package data".to_string());
        let alternate = alternate_install_sentence(package);
        let alternate = if alternate.is_empty() {
            String::new()
        } else {
            format!(" {alternate}")
        };
        return format!(
            "{} Version {} via {}; verified {}.{}",
            sentence_text(&summary),
            empty_as_unknown(&authoritative_package_version(package)),
            package.provider_label,
            verified,
            alternate
        );
    }
    if let Some(isotope) = full_value(package, "isotope").filter(|value| value.is_object()) {
        let title = isotope
            .get("justification")
            .and_then(|justification| value_str_key(justification, "title"))
            .unwrap_or_else(|| "secret handling".to_string());
        return format!(
            "Automic Vault tracks {} because {} affects agent-run command-line tools on macOS.",
            package.display_name,
            title.trim_end_matches('.').to_ascii_lowercase()
        );
    }
    if full_value(package, "approvalGate").is_some() {
        return format!(
            "Automic Vault has approval-gate metadata for {}, including high-risk commands and recommended human review points.",
            package.display_name
        );
    }
    if !summary.is_empty() {
        return format!("Package data for {}: {}", package.display_name, summary);
    }
    format!(
        "Package metadata for {}, from local pkg.so sources.",
        package.display_name
    )
}

fn localized_hero_sentence(package: &PackageRow, locale: &Locale) -> String {
    if locale.code == "en" {
        hero_sentence(package)
    } else {
        txf(
            locale,
            "heroSentence",
            "View install routes, executables, metadata, and security notes for {name}.",
            &[("name", package.display_name.clone())],
        )
    }
}

fn package_install_heading(package: &PackageRow, locale: &Locale) -> String {
    let managers = package_heading_managers(package).join(", ");
    txf(
        locale,
        "installWithManagerHeading",
        "Install {name} with {manager}",
        &[
            ("name", package.display_name.clone()),
            ("manager", managers),
        ],
    )
}

fn package_heading_managers(package: &PackageRow) -> Vec<String> {
    let mut managers = Vec::new();
    for item in install_command_entries(package) {
        let kind = value_str_key(&item, "kind").unwrap_or_default();
        let manager = item
            .get("source")
            .and_then(|source| value_str_key(source, "manager"))
            .or_else(|| value_str_key(&item, "manager"))
            .unwrap_or_default();
        if kind == "automic_vault" || manager.eq_ignore_ascii_case("Automic Vault") {
            continue;
        }
        let label = heading_manager_label(&manager);
        if !label.is_empty() && !managers.iter().any(|seen| seen == &label) {
            managers.push(label);
        }
    }
    if managers.is_empty() && !package.provider_label.is_empty() {
        managers.push(heading_manager_label(&package.provider_label));
    }
    managers
}

fn heading_manager_label(manager: &str) -> String {
    let trimmed = manager.trim();
    match trimmed.to_ascii_lowercase().as_str() {
        "" => String::new(),
        "alpine linux apk" | "apk" => "apk".to_string(),
        "arch linux pacman" | "pacman" => "pacman".to_string(),
        "debian" | "debian apt" | "ubuntu" | "apt" => "apt".to_string(),
        "fedora" | "fedora dnf" | "redhat" | "rhel" | "dnf" => "dnf".to_string(),
        "brew" | "homebrew" => "Homebrew".to_string(),
        "cask" | "homebrew cask" => "Homebrew Cask".to_string(),
        "nix" => "Nix".to_string(),
        "macports" => "MacPorts".to_string(),
        "opensuse" | "opensuse zypper" | "suse" | "zypper" => "zypper".to_string(),
        "python pip" | "pip" => "pip".to_string(),
        "windows package manager" | "winget" => "winget".to_string(),
        _ => trimmed.to_string(),
    }
}

fn meta_description(package: &PackageRow, locale: &Locale) -> String {
    let managers = package_heading_managers(package).join(", ");
    if locale.code != "en" {
        return short_text(
            &txf(
                locale,
                "metaDescription",
                "Install {name} with {manager}. View executables, metadata, and security notes.",
                &[
                    ("name", package.display_name.clone()),
                    ("manager", managers),
                ],
            ),
            155,
        );
    }
    let mut parts = Vec::new();
    parts.push(format!(
        "Install {} with {}.",
        package.display_name, managers
    ));
    if !package.summary.is_empty() {
        parts.push(package.summary.clone());
    }
    if !full_string_array(package, "executablesDetailed").is_empty()
        || !full_string_array(package, "aliases").is_empty()
    {
        parts.push("View executables, metadata, and security notes.".to_string());
    }
    if let Some(isotope) = full_value(package, "isotope").filter(|value| value.is_object()) {
        if let Some(title) = isotope
            .get("justification")
            .and_then(|justification| value_str_key(justification, "title"))
        {
            parts.push(format!("Protected-tool coverage: {title}."));
        }
    }
    if let Some(gate) = full_value(package, "approvalGate").filter(|value| value.is_object()) {
        if let Some(rule_count) = value_i64_key(gate, "rule_count") {
            parts.push(format!("Includes {rule_count} approval-gate rules."));
        }
    }
    short_text(&parts.join(" "), 155)
}

fn alternate_install_command(package: &PackageRow) -> Option<Value> {
    install_command_entries(package)
        .into_iter()
        .find(|item| {
            if value_str_key(item, "kind").as_deref() == Some("automic_vault") {
                return false;
            }
            let command = value_str_key(item, "command").unwrap_or_default();
            native_command_provider(&command)
                .map(|provider| provider != package.provider)
                .unwrap_or(false)
        })
        .or_else(|| {
            external_package_manager_matches(package)
                .into_iter()
                .find(|item| {
                    let command = value_str_key(item, "command").unwrap_or_default();
                    !command.is_empty() && native_command_provider(&command).is_none()
                })
                .cloned()
        })
}

fn native_command_provider(command: &str) -> Option<&'static str> {
    let command = command.trim();
    if command.starts_with("brew install --cask ") {
        Some("cask")
    } else if command.starts_with("brew install ") {
        Some("brew")
    } else if command.starts_with("npm install ") || command.starts_with("npm i ") {
        Some("npm")
    } else if command.starts_with("pip install ")
        || command.starts_with("pip3 install ")
        || command.starts_with("python -m pip install ")
        || command.starts_with("python3 -m pip install ")
    {
        Some("pip")
    } else if command.starts_with("cargo install ") {
        Some("cargo")
    } else {
        None
    }
}

fn alternate_install_sentence(package: &PackageRow) -> String {
    let Some(alternate) = alternate_install_command(package) else {
        return String::new();
    };
    let manager = value_str_key(&alternate, "manager")
        .unwrap_or_else(|| "another package manager".to_string());
    let command = value_str_key(&alternate, "command").unwrap_or_default();
    if command.is_empty() {
        String::new()
    } else {
        format!("Also installable with {manager}: {command}.")
    }
}

fn install_command_entries(package: &PackageRow) -> Vec<Value> {
    if let Some(items) = full_value(package, "installCommands")
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
    {
        return items.iter().cloned().collect();
    }
    let mut entries = vec![json!({
        "platform": "portable",
        "manager": "Automic Vault",
        "command": if package.install_command.is_empty() {
            format!("sudo av install {}", package.package_key)
        } else {
            package.install_command.clone()
        },
        "kind": "automic_vault",
        "confidence": 1.0,
        "evidence": "deterministic local package key"
    })];
    if !package.native_install_command.is_empty() {
        entries.push(json!({
            "platform": if package.provider == "brew" || package.provider == "cask" { "macos" } else { "portable" },
            "manager": package.provider_label,
            "command": package.native_install_command,
            "kind": "package_manager",
            "confidence": 1.0,
            "evidence": "provider-native install command"
        }));
    }
    entries
}

fn package_manager_install_entries(package: &PackageRow) -> Vec<Value> {
    install_command_entries(package)
        .into_iter()
        .filter(|item| {
            value_str_key(item, "kind").unwrap_or_default() != "automic_vault"
                && !value_str_key(item, "manager")
                    .unwrap_or_default()
                    .eq_ignore_ascii_case("Automic Vault")
        })
        .collect()
}

fn package_facts(package: &PackageRow, locale: &Locale) -> String {
    let mut facts = vec![metric(
        &tx(locale, "manager", "manager"),
        &package.provider_label,
    )];
    let version = authoritative_package_version(package);
    if !version.is_empty() {
        facts.push(metric(&tx(locale, "version", "version"), &version));
    }
    if !package.license.is_empty() {
        facts.push(metric(&tx(locale, "license", "license"), &package.license));
    }
    let tags = taxonomy_tags(package);
    if !tags.is_empty() {
        facts.push(metric(&tx(locale, "tags", "tags"), &tags.join(", ")));
    }
    if let Some(geiger) = full_value(package, "geiger").filter(|value| value.is_object()) {
        facts.push(metric(
            &tx(locale, "risk", "risk"),
            &localized_standard_value(
                locale,
                &value_str_key(geiger, "level").unwrap_or_else(|| "unknown".to_string()),
            ),
        ));
        facts.push(metric(
            &tx(locale, "classifierConfidence", "classifier confidence"),
            &localized_standard_value(
                locale,
                &value_str_key(geiger, "confidence").unwrap_or_else(|| "unknown".to_string()),
            ),
        ));
    }
    if let Some(rank) = full_value(package, "popularity")
        .and_then(|value| value_i64_key(value, "rank"))
        .or_else(|| package.rank.map(i64::from))
    {
        facts.push(metric(&tx(locale, "rank", "rank"), &fmt_int(rank)));
    }
    if let Some(popularity) = full_value(package, "popularity").filter(|value| value.is_object()) {
        if let Some(installs) = value_i64_key(popularity, "installs_per_365_days") {
            facts.push(metric(
                &tx(locale, "installs365d", "365d installs"),
                &fmt_int(installs),
            ));
        } else if let Some(downloads) = value_i64_key(popularity, "downloads_per_30_days") {
            facts.push(metric(
                &tx(locale, "downloads30d", "30d downloads"),
                &fmt_int(downloads),
            ));
        }
    }
    if full_value(package, "isotope").is_some() {
        facts.push(metric(
            &tx(locale, "radioisotopeKicker", "protected-tool coverage"),
            &tx(locale, "covered", "covered"),
        ));
    }
    if let Some(gate) = full_value(package, "approvalGate").filter(|value| value.is_object()) {
        if let Some(rule_count) = value_i64_key(gate, "rule_count") {
            facts.push(metric(
                &tx(locale, "approvalRules", "approval rules"),
                &fmt_int(rule_count),
            ));
        }
    }
    if let Some(verified) = Some(display_last_verified(package)).filter(|value| !value.is_empty()) {
        facts.push(metric(
            &tx(locale, "verified", "verified"),
            &fmt_date(&verified),
        ));
    } else if !package.last_updated_at.is_empty() {
        facts.push(metric(
            &tx(locale, "updated", "updated"),
            &fmt_date(&package.last_updated_at),
        ));
    }
    facts.join("")
}

fn metric(label: &str, value: &str) -> String {
    format!(
        r#"<div class="metric"><span>{}</span><strong>{}</strong></div>"#,
        html_escape(label),
        html_escape(value)
    )
}

fn security_heading(package: &PackageRow, locale: &Locale) -> String {
    if let Some(geiger) = full_value(package, "geiger").filter(|value| value.is_object()) {
        return txf(
            locale,
            "riskLevel",
            "Risk level: {level}",
            &[(
                "level",
                localized_standard_value(
                    locale,
                    &value_str_key(geiger, "level").unwrap_or_else(|| "unknown".to_string()),
                ),
            )],
        );
    }
    tx(
        locale,
        "radioisotopeMissingHeading",
        "No protected-tool coverage found yet",
    )
}

fn security_summary(package: &PackageRow, locale: &Locale) -> String {
    if let Some(geiger) = full_value(package, "geiger").filter(|value| value.is_object()) {
        let reasons = value_array(geiger, "reasons")
            .into_iter()
            .take(2)
            .filter_map(value_string)
            .map(|reason| sentence_text(reason.trim_end_matches('.')))
            .collect::<Vec<_>>();
        if !reasons.is_empty() {
            return reasons.join(" ");
        }
    }
    txf(
        locale,
        "radioisotopeMissingSummary",
        "No matching local secret-handling manifest was found for {name}. Package metadata is still published here so future coverage has a stable package URL.",
        &[("name", package.display_name.clone())],
    )
}

fn link_value(value: &str) -> String {
    if value.starts_with("https://") || value.starts_with("http://") {
        format!(
            r#"<a href="{}">{}</a>"#,
            html_escape(value),
            html_escape(value)
        )
    } else {
        html_escape(value)
    }
}

fn push_joined_row(rows: &mut Vec<(String, String)>, label: &str, values: &[String]) {
    if !values.is_empty() {
        rows.push((label.to_string(), values.join(", ")));
    }
}

fn registry_insights(package: &PackageRow) -> Option<&Value> {
    full_value(package, "registryInsights")
        .or_else(|| {
            full_value(package, "extra")
                .and_then(|extra| extra.get("registryInsights"))
                .filter(|value| !value.is_null())
        })
        .filter(|value| value.is_object())
}

fn registry_insight_rows(insights: &Value) -> Vec<(String, &Value)> {
    let preferred = [
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
    ];
    let mut rows = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for key in preferred {
        if let Some(value) = insights
            .get(key)
            .filter(|value| metadata_value_present(value))
        {
            seen.insert(key.to_string());
            rows.push((human_metadata_label(key), value));
        }
    }
    if let Some(object) = insights.as_object() {
        for (key, value) in object {
            if !seen.contains(key) && metadata_value_present(value) {
                rows.push((human_metadata_label(key), value));
            }
        }
    }
    rows
}

fn external_package_manager_matches(package: &PackageRow) -> Vec<&Value> {
    full_value(package, "externalPackageManagerMatches")
        .or_else(|| full_value(package, "externalMatches"))
        .or_else(|| {
            full_value(package, "extra")
                .and_then(|extra| extra.get("externalPackageManagerMatches"))
                .filter(|value| !value.is_null())
        })
        .and_then(Value::as_array)
        .map(|items| items.iter().collect())
        .unwrap_or_default()
}

fn metadata_value_present(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::String(text) => !text.trim().is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(items) => !items.is_empty(),
        Value::Bool(_) | Value::Number(_) => true,
    }
}

fn human_metadata_label(key: &str) -> String {
    let mut spaced = String::new();
    for (index, character) in key.chars().enumerate() {
        if character == '_' || character == '-' {
            spaced.push(' ');
        } else {
            if index > 0 && character.is_ascii_uppercase() {
                spaced.push(' ');
            }
            spaced.push(character);
        }
    }
    let normalized = normalize_space(&spaced);
    let mut words = Vec::new();
    for word in normalized.split_whitespace() {
        let lower = word.to_ascii_lowercase();
        let label = match lower.as_str() {
            "api" => "API".to_string(),
            "id" => "ID".to_string(),
            "json" => "JSON".to_string(),
            "npm" => "npm".to_string(),
            "pypi" => "PyPI".to_string(),
            "sha" => "SHA".to_string(),
            "sha256" => "SHA-256".to_string(),
            "url" => "URL".to_string(),
            _ => {
                let mut chars = word.chars();
                chars
                    .next()
                    .map(|first| first.to_uppercase().collect::<String>() + chars.as_str())
                    .unwrap_or_default()
            }
        };
        words.push(label);
    }
    words.join(" ")
}

fn metadata_value_html(value: &Value) -> String {
    match value {
        Value::Bool(flag) => html_escape(if *flag { "yes" } else { "no" }),
        Value::Number(number) => html_escape(&metadata_number_text(number)),
        Value::String(text) => link_value(text),
        Value::Array(items) => {
            let item_html = items
                .iter()
                .take(32)
                .filter_map(|item| {
                    let text = metadata_value_text(item);
                    (!text.trim().is_empty()).then(|| format!("<li>{}</li>", html_escape(&text)))
                })
                .collect::<String>();
            if item_html.is_empty() {
                String::new()
            } else {
                format!(r#"<ul class="chip-list compact-chip-list">{item_html}</ul>"#)
            }
        }
        Value::Object(items) => {
            let pair_html = items
                .iter()
                .filter(|(_key, value)| metadata_value_present(value))
                .take(24)
                .map(|(key, value)| {
                    format!(
                        "<div><dt>{}</dt><dd>{}</dd></div>",
                        html_escape(&human_metadata_label(key)),
                        metadata_value_html(value)
                    )
                })
                .collect::<String>();
            if pair_html.is_empty() {
                String::new()
            } else {
                format!(r#"<dl class="metadata-pair-list">{pair_html}</dl>"#)
            }
        }
        Value::Null => String::new(),
    }
}

fn metadata_value_text(value: &Value) -> String {
    match value {
        Value::Bool(flag) => if *flag { "yes" } else { "no" }.to_string(),
        Value::Number(number) => metadata_number_text(number),
        Value::String(text) => text.trim().to_string(),
        Value::Array(items) => items
            .iter()
            .take(32)
            .map(metadata_value_text)
            .filter(|text| !text.trim().is_empty())
            .collect::<Vec<_>>()
            .join(", "),
        Value::Object(items) => items
            .iter()
            .filter(|(_key, value)| metadata_value_present(value))
            .take(24)
            .map(|(key, value)| {
                format!(
                    "{}: {}",
                    human_metadata_label(key),
                    metadata_value_text(value)
                )
            })
            .filter(|text| !text.trim().ends_with(':'))
            .collect::<Vec<_>>()
            .join(", "),
        Value::Null => String::new(),
    }
}

fn metadata_number_text(number: &serde_json::Number) -> String {
    number
        .as_i64()
        .map(fmt_int)
        .or_else(|| {
            number
                .as_u64()
                .and_then(|value| i64::try_from(value).ok())
                .map(fmt_int)
        })
        .unwrap_or_else(|| number.to_string())
}

fn related_links(
    package: &PackageRow,
    locale: &Locale,
    key: &str,
    limit: usize,
    workflow_only: bool,
) -> Vec<String> {
    let workflow_rels = [
        "adjacent_workflow",
        "format_peer",
        "language_runtime_peer",
        "command_surface_peer",
        "security_surface_peer",
        "domain_peer",
    ];
    let mut links = full_value(package, key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|item| item.is_object())
                .filter_map(|item| {
                    let rel = value_str_key(item, "rel").unwrap_or_default();
                    let is_workflow = workflow_rels.contains(&rel.as_str());
                    if workflow_only != is_workflow && key == "relatedPackages" {
                        return None;
                    }
                    let provider = value_str_key(item, "provider")?;
                    let name = value_str_key(item, "name")
                        .or_else(|| value_str_key(item, "package"))
                        .or_else(|| value_str_key(item, "target"))?;
                    if provider.is_empty()
                        || name.is_empty()
                        || format!("{provider}:{name}") == package.package_key
                    {
                        return None;
                    }
                    let label = value_str_key(item, "label").unwrap_or_else(|| name.clone());
                    let reason = value_str_key(item, "reason").unwrap_or_default();
                    let href =
                        locale_path(&format!("/pkg/{}/{}/", provider, slugify(&name)), locale);
                    Some(format!(
                        r#"<li><a href="{}">{}</a>{}</li>"#,
                        html_escape(&href),
                        html_escape(&label),
                        if reason.is_empty() {
                            String::new()
                        } else {
                            format!("<span>{}</span>", html_escape(&reason))
                        }
                    ))
                })
                .take(limit)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    if links.is_empty() && key == "relatedPackages" && !workflow_only && package.provider == "brew"
    {
        links = full_string_array(package, "dependencies")
            .into_iter()
            .take(limit)
            .map(|dependency| {
                let href = locale_path(&format!("/pkg/brew/{}/", slugify(&dependency)), locale);
                format!(
                    r#"<li><a href="{}">{}</a><span>{} dependency.</span></li>"#,
                    html_escape(&href),
                    html_escape(&dependency),
                    html_escape(&package.provider_label)
                )
            })
            .collect();
    }
    links
}

fn related_article(title: &str, items: Vec<String>) -> String {
    let content = items
        .into_iter()
        .filter(|item| !item.is_empty())
        .collect::<String>();
    if content.is_empty() {
        String::new()
    } else {
        format!(
            "<article><h3>{}</h3><ul>{content}</ul></article>",
            html_escape(title)
        )
    }
}

fn core_security_guides(package: &PackageRow, locale: &Locale) -> Vec<String> {
    let mut links = vec![
        (
            locale_path("/secret-scanner-for-ai-agents/", locale),
            tx(locale, "guideSecretScannerTitle", "AI agent secret scanner"),
            tx(
                locale,
                "guideSecretScannerCopy",
                "Find plaintext credentials before an agent run starts.",
            ),
        ),
        (
            locale_path("/ai-agent-approval-gates/", locale),
            tx(locale, "guideApprovalGatesTitle", "AI agent approval gates"),
            tx(
                locale,
                "guideApprovalGatesCopy",
                "Put approvals in front of risky package and tool actions.",
            ),
        ),
        (
            locale_path("/docs/#secrets", locale),
            tx(locale, "guideSecretInjectionTitle", "Secret injection docs"),
            tx(
                locale,
                "guideSecretInjectionCopy",
                "Move supported secrets out of plaintext files and inject them into approved tools.",
            ),
        ),
    ];
    let haystack = format!(
        "{} {} {} {} {}",
        package.provider,
        package.name,
        package.display_name,
        package.summary,
        full_string_array(package, "aliases").join(" ")
    )
    .to_ascii_lowercase();
    if package.provider == "brew" || haystack.contains("homebrew") {
        links.push((
            locale_path("/download/", locale),
            tx(locale, "guideSecureHomebrewTitle", "Secure Homebrew tools"),
            tx(
                locale,
                "guideSecureHomebrewCopy",
                "Install Vault and scan the tools your Mac already uses.",
            ),
        ));
    }
    if haystack.contains("aws") || haystack.contains("cloud") {
        links.push((
            locale_path("/secure-aws-cli-credentials-ai-agents/", locale),
            tx(locale, "guideAwsCliTitle", "Secure AWS CLI credentials"),
            tx(
                locale,
                "guideAwsCliCopy",
                "Keep cloud keys out of ambient config files.",
            ),
        ));
    }
    if haystack.contains("github")
        || full_string_array(package, "aliases")
            .iter()
            .any(|alias| alias == "gh")
    {
        links.push((
            locale_path("/github-cli-token-security-ai-agents/", locale),
            tx(locale, "guideGithubCliTitle", "GitHub CLI token security"),
            tx(
                locale,
                "guideGithubCliCopy",
                "Protect source and release tokens used by local tools.",
            ),
        ));
    }
    links
        .into_iter()
        .take(5)
        .map(|(url, label, copy)| {
            format!(
                r#"<li><a href="{}">{}</a><span>{}</span></li>"#,
                html_escape(&url),
                html_escape(&label),
                html_escape(&copy)
            )
        })
        .collect()
}

fn slugify(value: &str) -> String {
    let mut slug = String::new();
    let mut last_dash = false;
    for character in value.chars().flat_map(char::to_lowercase) {
        if character.is_ascii_alphanumeric() {
            slug.push(character);
            last_dash = false;
        } else if !last_dash && !slug.is_empty() {
            slug.push('-');
            last_dash = true;
        }
    }
    while slug.ends_with('-') {
        slug.pop();
    }
    slug
}

fn schema_for_package(
    package: &PackageRow,
    description: &str,
    updated: &str,
    locale: &Locale,
) -> Value {
    let url = locale_url(&package.path, locale);
    let mut software = json!({
        "@type": "SoftwareApplication",
        "@id": format!("{url}#software"),
        "name": package.display_name,
        "applicationCategory": "DeveloperApplication",
        "operatingSystem": "macOS",
        "url": url,
        "description": description,
        "dateModified": updated,
        "inLanguage": locale.hreflang,
        "isPartOf": {"@id": format!("{SITE_ORIGIN}/#website")}
    });
    if let Some(object) = software.as_object_mut() {
        if !package.homepage.is_empty() {
            object.insert("sameAs".to_string(), json!(package.homepage));
        }
        let version = authoritative_package_version(package);
        if !version.is_empty() {
            object.insert("softwareVersion".to_string(), json!(version));
        }
        if !package.license.is_empty() {
            object.insert("license".to_string(), json!(package.license));
        }
        if !package.repository.is_empty() {
            object.insert("codeRepository".to_string(), json!(package.repository));
        }
        let dependencies = full_string_array(package, "dependencies");
        if !dependencies.is_empty() {
            object.insert(
                "softwareRequirements".to_string(),
                json!(
                    dependencies
                        .into_iter()
                        .take(16)
                        .collect::<Vec<_>>()
                        .join(", ")
                ),
            );
        }
    }
    let steps = package_manager_install_entries(package)
        .into_iter()
        .filter_map(|item| {
            let command = value_str_key(&item, "command")?;
            if command.is_empty() {
                return None;
            }
            let manager = value_str_key(&item, "manager").unwrap_or_else(|| "install".to_string());
            Some((manager, command))
        })
        .take(12)
        .enumerate()
        .map(|(index, (manager, command))| {
            json!({
                "@type": "HowToStep",
                "position": index + 1,
                "name": txf(
                    locale,
                    "schemaHowToStep",
                    "Run {manager} command",
                    &[("manager", manager.clone())],
                ),
                "text": command
            })
        })
        .collect::<Vec<_>>();
    json!({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "@id": format!("{SITE_ORIGIN}/#website"), "name": SITE_NAME, "url": format!("{SITE_ORIGIN}/")},
            {"@type": "Organization", "@id": format!("{SITE_ORIGIN}/#organization"), "name": SITE_NAME, "url": format!("{SITE_ORIGIN}/")},
            {"@type": "Person", "@id": "https://github.com/mxcl#person", "name": "Max Howell", "url": "https://github.com/mxcl"},
            software,
            {
                "@type": "TechArticle",
                "@id": format!("{url}#article"),
                "headline": txf(
                    locale,
                    "schemaTechArticleHeadline",
                    "Install {name} with {manager}",
                    &[
                        ("name", package.display_name.clone()),
                        ("manager", package.provider_label.clone()),
                    ],
                ),
                "description": description,
                "dateModified": updated,
                "inLanguage": locale.hreflang,
                "author": {"@id": "https://github.com/mxcl#person"},
                "reviewedBy": {"@id": "https://github.com/mxcl#person"},
                "publisher": {"@id": format!("{SITE_ORIGIN}/#organization")},
                "mainEntity": {"@id": format!("{url}#software")}
            },
            {
                "@type": "BreadcrumbList",
                "@id": format!("{url}#breadcrumbs"),
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": tx(locale, "home", "Home"), "item": locale_url("/", locale)},
                    {"@type": "ListItem", "position": 2, "name": tx(locale, "packages", "Packages"), "item": locale_url("/pkg/", locale)},
                    {"@type": "ListItem", "position": 3, "name": package.display_name, "item": url}
                ]
            },
            {
                "@type": "HowTo",
                "@id": format!("{url}#install-howto"),
                "name": txf(
                    locale,
                    "schemaHowToName",
                    "Install {name}",
                    &[("name", package.display_name.clone())],
                ),
                "step": steps
            }
        ]
    })
}

fn markdown_history_section(text: &mut String, package: &PackageRow, locale: &Locale) {
    let Some(history) = full_value(package, "history").filter(|value| value.is_object()) else {
        return;
    };
    let mut section = String::new();
    for paragraph in history_items(history, "summary") {
        section.push_str(&format!("{}\n\n", markdown_value(&paragraph)));
    }
    for (key, title) in [
        (
            "project-history",
            tx(locale, "projectHistory", "Project history"),
        ),
        (
            "adoption-history",
            tx(locale, "adoptionHistory", "Adoption history"),
        ),
        ("usage", tx(locale, "usage", "How it is used")),
        (
            "package-nerd-significance",
            tx(locale, "packageNerdSignificance", "Why package nerds care"),
        ),
    ] {
        let items = history_items(history, key);
        if items.is_empty() {
            continue;
        }
        section.push_str(&format!("### {}\n\n", title));
        for item in items {
            section.push_str(&format!("{}\n\n", markdown_value(&item)));
        }
    }
    for (key, title) in [
        ("timeline", tx(locale, "timeline", "Timeline")),
        (
            "related-projects",
            tx(locale, "relatedProjects", "Related projects"),
        ),
        ("sources", tx(locale, "sources", "Sources")),
    ] {
        let items = history_items(history, key);
        if items.is_empty() {
            continue;
        }
        section.push_str(&format!("### {}\n\n", title));
        for item in items {
            section.push_str(&format!("- {}\n", markdown_value(&item)));
        }
        section.push('\n');
    }
    if !section.is_empty() {
        text.push_str(&format!(
            "## {}\n\n{}",
            tx(locale, "historyTitle", "Project history and usage"),
            section
        ));
    }
}

fn markdown_install_groups(text: &mut String, package: &PackageRow, locale: &Locale) {
    let commands = package_manager_install_entries(package);
    if commands.is_empty() {
        return;
    }
    text.push_str(&format!(
        "{}:\n\n",
        tx(
            locale,
            "additionalInstallCommands",
            "Additional install commands"
        )
    ));
    for (platform, label) in [
        ("macos", tx(locale, "macos", "macOS")),
        ("linux", tx(locale, "linux", "Linux")),
        ("windows", tx(locale, "windows", "Windows")),
        (
            "portable",
            tx(
                locale,
                "platformPortableLanguageManagers",
                "Portable and language managers",
            ),
        ),
    ] {
        let items = commands
            .iter()
            .filter(|item| {
                value_str_key(item, "platform").unwrap_or_else(|| "portable".to_string())
                    == platform
            })
            .collect::<Vec<_>>();
        if items.is_empty() {
            continue;
        }
        text.push_str(&format!("### {label}\n\n"));
        for item in items {
            let manager = value_str_key(item, "manager").unwrap_or_else(|| "shell".to_string());
            let command = value_str_key(item, "command").unwrap_or_default();
            if command.is_empty() {
                continue;
            }
            let confidence = value_f64_key(item, "confidence")
                .map(|value| format!("{:.0}%", value * 100.0))
                .unwrap_or_else(|| tx(locale, "unknownConfidence", "unknown confidence"));
            text.push_str(&format!(
                "- {manager} ({confidence}):\n\n```sh\n{command}\n```\n"
            ));
            if let Some(evidence) = value_str_key(item, "evidence") {
                text.push_str(&format!(
                    "\n  {}: {evidence}\n",
                    tx(locale, "markdownEvidence", "Evidence")
                ));
            }
            text.push('\n');
        }
    }
}

fn markdown_agent_safety_section(text: &mut String, package: &PackageRow, locale: &Locale) {
    let Some(answer) = agent_safety_answer(package) else {
        return;
    };
    let Some(summary) = value_str_key(answer, "summary") else {
        return;
    };
    text.push_str(&format!(
        "## {}\n\n{}\n\n",
        tx(locale, "agentSafetyTitle", "Agent safety answer"),
        markdown_value(&summary)
    ));
    for (label, field) in [
        (
            tx(locale, "agentSafetyCredentialAccess", "Credential access"),
            "credentialAccess",
        ),
        (
            tx(locale, "agentSafetyRemoteMutation", "Remote mutation"),
            "remoteMutation",
        ),
        (
            tx(locale, "agentSafetyPublishRisk", "Publish/artifact risk"),
            "publishOrArtifactRisk",
        ),
        (
            tx(locale, "agentSafetyControl", "Recommended control"),
            "recommendedControl",
        ),
        (
            tx(locale, "agentSafetyGuidance", "Agent-use guidance"),
            "agentUseGuidance",
        ),
    ] {
        if let Some(value) = value_str_key(answer, field) {
            text.push_str(&format!("- **{}:** {}\n", label, markdown_value(&value)));
        }
    }
    text.push('\n');
}

fn markdown_value_list(text: &mut String, title: &str, items: &[String]) {
    if items.is_empty() {
        return;
    }
    text.push_str(&format!("\n## {title}\n\n"));
    for item in items {
        text.push_str(&format!("- {}\n", markdown_value(item)));
    }
}

fn markdown_value(value: &str) -> String {
    if value.starts_with("https://") || value.starts_with("http://") {
        format!("<{value}>")
    } else {
        value.to_string()
    }
}

fn executable_markdown_items(package: &PackageRow, locale: &Locale) -> Vec<String> {
    let mut items = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for item in value_array(&package.data.full, "executablesDetailed") {
        let name = value_str_key(item, "name")
            .or_else(|| value_str_key(item, "target"))
            .or_else(|| value_str_key(item, "source"))
            .unwrap_or_default();
        if name.is_empty() || !seen.insert(name.clone()) {
            continue;
        }
        let kind = value_str_key(item, "kind").unwrap_or_else(|| "executable".to_string());
        let note = value_str_key(item, "note").unwrap_or_default();
        items.push(if note.is_empty() {
            format!("{name} ({})", localized_executable_text(locale, &kind))
        } else {
            format!(
                "{name} ({}): {}",
                localized_executable_text(locale, &kind),
                localized_executable_text(locale, &note)
            )
        });
    }
    for item in value_array(&package.data.full, "binaries") {
        let name = value_str_key(item, "target")
            .or_else(|| value_str_key(item, "source"))
            .unwrap_or_default();
        if !name.is_empty() {
            items.push(format!("{name} ({})", tx(locale, "binary", "binary")));
        }
    }
    for alias in full_string_array(package, "aliases") {
        items.push(format!("{alias} ({})", tx(locale, "alias", "alias")));
    }
    items
}

fn markdown_install_behavior_items(package: &PackageRow, locale: &Locale) -> Vec<String> {
    let mut items = Vec::new();
    if let Some(behavior) = full_value(package, "installBehavior").filter(|value| value.is_object())
    {
        if let Some(post_install) = behavior.get("postInstallDefined").and_then(Value::as_bool) {
            items.push(format!(
                "{}: {}",
                tx(locale, "postInstallHook", "Post-install hook"),
                if post_install {
                    tx(locale, "defined", "defined")
                } else {
                    tx(locale, "notDefined", "not defined")
                }
            ));
        }
        if let Some(service) = value_str_key(behavior, "service").filter(|value| !value.is_empty())
        {
            items.push(format!("{}: {service}", tx(locale, "service", "Service")));
        }
        if let Some(caveats) = value_str_key(behavior, "caveats").filter(|value| !value.is_empty())
        {
            items.push(format!("{}: {caveats}", tx(locale, "caveats", "Caveats")));
        }
        let lifecycle = value_array(behavior, "lifecycleScripts")
            .into_iter()
            .filter_map(value_string)
            .collect::<Vec<_>>();
        if !lifecycle.is_empty() {
            items.push(format!(
                "{}: {}",
                tx(locale, "lifecycleScripts", "Lifecycle scripts"),
                lifecycle.join(", ")
            ));
        }
        if let Some(python) =
            value_str_key(behavior, "pythonRequires").filter(|value| !value.is_empty())
        {
            items.push(format!(
                "{}: {python}",
                tx(locale, "pythonRequires", "Python requires")
            ));
        }
        if let Some(count) = value_i64_key(behavior, "requiresDistCount") {
            items.push(format!(
                "{}: {}",
                tx(locale, "pypiDependencySpecs", "PyPI dependency specs"),
                fmt_int(count)
            ));
        }
    }
    if let Some(bottle) = full_value(package, "bottle").filter(|value| value.is_object()) {
        let mut detail = if bottle.get("available").and_then(Value::as_bool) == Some(true) {
            tx(locale, "available", "available")
        } else {
            tx(locale, "notAvailable", "not available")
        };
        let platforms = value_array(bottle, "platforms")
            .into_iter()
            .filter_map(value_string)
            .take(12)
            .collect::<Vec<_>>();
        if !platforms.is_empty() {
            detail.push_str(&format!(
                " {} {}",
                tx(locale, "onPlatforms", "on"),
                platforms.join(", ")
            ));
        }
        items.push(format!("{}: {detail}", tx(locale, "bottle", "Bottle")));
    }
    items
}

fn markdown_freshness_items(
    package: &PackageRow,
    generated_at: &str,
    locale: &Locale,
) -> Vec<String> {
    let freshness = full_value(package, "versionFreshness").unwrap_or(&Value::Null);
    let manager = freshness.get("packageManager").unwrap_or(&Value::Null);
    let site = freshness.get("siteData").unwrap_or(&Value::Null);
    let upstream = freshness.get("upstream").unwrap_or(&Value::Null);
    let mut items = vec![
        format!(
            "{}: {}",
            tx(locale, "pageGenerated", "Page generated"),
            if fmt_date(generated_at).is_empty() {
                tx(locale, "unknown", "unknown")
            } else {
                fmt_date(generated_at)
            }
        ),
        format!(
            "{}: {}",
            tx(locale, "managerVersion", "Package-manager version"),
            value_str_key(manager, "version").unwrap_or_else(|| {
                if package.version.is_empty() {
                    tx(locale, "unknown", "unknown")
                } else {
                    package.version.clone()
                }
            })
        ),
    ];
    if let Some(updated) = value_str_key(manager, "updatedAt") {
        items.push(format!(
            "{}: {}",
            tx(locale, "managerUpdated", "Package-manager updated"),
            fmt_date(&updated)
        ));
    }
    if let Some(status) = value_str_key(site, "status") {
        items.push(format!(
            "{}: {}",
            tx(locale, "localData", "Local data"),
            localized_standard_value(locale, &status)
        ));
    }
    if let Some(repository) = value_str_key(upstream, "repository") {
        items.push(format!(
            "{}: {repository}",
            tx(locale, "upstreamRepository", "Upstream repository")
        ));
    }
    if let Some(version) = value_str_key(upstream, "latestVersion") {
        items.push(format!(
            "{}: {} ({})",
            tx(locale, "upstreamLatestDetected", "Upstream latest detected"),
            version,
            localized_standard_value(
                locale,
                &value_str_key(upstream, "comparison").unwrap_or_else(|| "unknown".to_string())
            )
        ));
    }
    for item in value_array(freshness, "warnings").into_iter().take(8) {
        if item.is_object() {
            let severity = value_str_key(item, "severity").unwrap_or_else(|| "info".to_string());
            let message = value_str_key(item, "message").unwrap_or_default();
            if !message.is_empty() {
                items.push(format!(
                    "{}: {}",
                    localized_standard_value(locale, &severity),
                    localized_freshness_message(locale, &message)
                ));
            }
        }
    }
    items
}

fn markdown_security_section(text: &mut String, package: &PackageRow, locale: &Locale) {
    text.push_str(&format!(
        "\n## {}\n\n",
        tx(locale, "securityNotes", "Security Notes")
    ));
    text.push_str(&security_summary(package, locale));
    text.push_str("\n\n");
    if let Some(isotope) = full_value(package, "isotope").filter(|value| value.is_object()) {
        if let Some(title) = isotope
            .get("justification")
            .and_then(|justification| value_str_key(justification, "title"))
        {
            text.push_str(&format!(
                "- **{}:** {title}\n",
                tx(locale, "radioisotopeKicker", "Protected-tool coverage")
            ));
        }
    }
    if let Some(geiger) = full_value(package, "geiger").filter(|value| value.is_object()) {
        text.push_str(&format!(
            "- **{}:** {} / {}\n",
            tx(locale, "geigerRisk", "Geiger risk"),
            localized_standard_value(
                locale,
                &value_str_key(geiger, "level").unwrap_or_else(|| "unknown".to_string())
            ),
            localized_standard_value(
                locale,
                &value_str_key(geiger, "confidence").unwrap_or_else(|| "unknown".to_string())
            )
        ));
        for item in value_array(geiger, "reasons")
            .into_iter()
            .filter_map(value_string)
        {
            text.push_str(&format!("- {item}\n"));
        }
    }
    if let Some(gate) = full_value(package, "approvalGate").filter(|value| value.is_object()) {
        if let Some(rule_count) = value_i64_key(gate, "rule_count") {
            text.push_str(&format!(
                "- **{}:** {rule_count}\n",
                tx(locale, "approvalRules", "Approval gate rules")
            ));
        }
    }
    text.push('\n');
}

fn markdown_file_locations(text: &mut String, package: &PackageRow, locale: &Locale) {
    let config = path_location_markdown_items(package, "configFileLocations", locale);
    let credentials = path_location_markdown_items(package, "credentialsFileLocations", locale);
    if config.is_empty() && credentials.is_empty() {
        return;
    }
    text.push_str(&format!(
        "\n## {}\n\n{}\n\n",
        tx(
            locale,
            "localFilesTitle",
            "Configuration and credential file locations"
        ),
        tx(
            locale,
            "localFilesCopy",
            "These source-backed paths show where this package keeps local settings or durable credentials. Automic Vault can use them as review targets for secret scanning, migration, and command approval."
        )
    ));
    markdown_value_list(
        text,
        &tx(locale, "configFileLocationsTitle", "Configuration files"),
        &config,
    );
    markdown_value_list(
        text,
        &tx(locale, "credentialFileLocationsTitle", "Credential files"),
        &credentials,
    );
}

fn path_location_markdown_items(package: &PackageRow, key: &str, locale: &Locale) -> Vec<String> {
    path_location_groups(package, key)
        .into_iter()
        .map(|(platform, paths)| {
            format!(
                "{}: {}",
                localized_platform_label(locale, &platform),
                paths.join(", ")
            )
        })
        .collect()
}

fn markdown_combined_yaml_source(text: &mut String, package: &PackageRow, locale: &Locale) {
    let url = full_str(package, "combinedYamlUrl");
    if url.is_empty() {
        return;
    }
    let path = full_str(package, "combinedYamlPath");
    let label = if path.is_empty() {
        url.as_str()
    } else {
        path.as_str()
    };
    text.push_str(&format!(
        "\n## {}\n\n{} [{}]({})\n\n",
        tx(locale, "combinedYamlSource", "Combined YAML source"),
        tx(
            locale,
            "combinedYamlSourceCopy",
            "View the package source record on GitHub."
        ),
        markdown_value(label),
        url
    ));
}

fn markdown_registry_insights(text: &mut String, package: &PackageRow, locale: &Locale) {
    let Some(insights) = registry_insights(package) else {
        return;
    };
    let rows = registry_insight_rows(insights);
    if rows.is_empty() {
        return;
    }
    text.push_str(&format!(
        "## {}\n\n",
        tx(locale, "registryInsightsTitle", "Source Database Details")
    ));
    for (label, value) in rows.into_iter().take(28) {
        let item = metadata_value_text(value);
        if !item.is_empty() {
            text.push_str(&format!("- **{}:** {}\n", label, markdown_value(&item)));
        }
    }
    text.push('\n');
}

fn markdown_external_package_manager_matches(
    text: &mut String,
    package: &PackageRow,
    locale: &Locale,
) {
    let matches = external_package_manager_matches(package);
    if matches.is_empty() {
        return;
    }
    text.push_str(&format!(
        "## {}\n\n",
        tx(
            locale,
            "otherPackageManagersTitle",
            "Other Package-Manager Records"
        )
    ));
    for item in matches.into_iter().take(16) {
        let metadata = item
            .get("metadata")
            .filter(|value| value.is_object())
            .unwrap_or(&Value::Null);
        let mut bits = Vec::new();
        if let Some(manager) = value_str_key(item, "displayName")
            .or_else(|| value_str_key(item, "manager"))
            .filter(|value| !value.is_empty())
        {
            bits.push(manager);
        }
        if let Some(package_id) = value_str_key(item, "packageId")
            .or_else(|| value_str_key(item, "packageName"))
            .filter(|value| !value.is_empty())
        {
            bits.push(package_id);
        }
        if let Some(version) = value_str_key(metadata, "version").filter(|value| !value.is_empty())
        {
            bits.push(version);
        }
        let mut details = Vec::new();
        for key in ["reason", "evidence"] {
            if let Some(value) = value_str_key(item, key).filter(|value| !value.is_empty()) {
                details.push(value);
            }
        }
        if let Some(summary) = value_str_key(metadata, "summary")
            .or_else(|| value_str_key(metadata, "description"))
            .filter(|value| !value.is_empty())
        {
            details.push(short_text(&summary, 180));
        }
        if let Some(homepage) =
            value_str_key(metadata, "homepage").filter(|value| !value.is_empty())
        {
            details.push(homepage);
        }
        if bits.is_empty() && details.is_empty() {
            continue;
        }
        let suffix = if details.is_empty() {
            String::new()
        } else {
            format!(": {}", details.join(" | "))
        };
        text.push_str(&format!("- {}{}\n", bits.join(" - "), suffix));
    }
    text.push('\n');
}

fn markdown_related(text: &mut String, package: &PackageRow, locale: &Locale) {
    let mut items = Vec::new();
    if let Some(hubs) = full_value(package, "packageHubs").and_then(Value::as_array) {
        for item in hubs.iter().take(4) {
            let slug = value_str_key(item, "slug").unwrap_or_default();
            if slug.is_empty() {
                continue;
            }
            let label = value_str_key(item, "label").unwrap_or_else(|| slug.clone());
            let reason = value_str_key(item, "reason").unwrap_or_default();
            let url = locale_url(&format!("/pkg/{slug}/"), locale);
            items.push(markdown_link_item(&label, &url, &reason));
        }
    }
    for key in ["relatedPackages", "alsoAvailableVia"] {
        if let Some(values) = full_value(package, key).and_then(Value::as_array) {
            for item in values.iter().take(24) {
                let label = value_str_key(item, "label")
                    .or_else(|| value_str_key(item, "name"))
                    .unwrap_or_default();
                let provider = value_str_key(item, "provider").unwrap_or_default();
                let name = value_str_key(item, "name").unwrap_or_else(|| label.clone());
                let reason = value_str_key(item, "reason").unwrap_or_default();
                if label.is_empty() || provider.is_empty() || name.is_empty() {
                    continue;
                }
                let url = locale_url(&format!("/pkg/{}/{}/", provider, slugify(&name)), locale);
                items.push(markdown_link_item(&label, &url, &reason));
            }
        }
    }
    markdown_value_list(text, &tx(locale, "relatedLinks", "Related links"), &items);
}

fn markdown_link_item(label: &str, url: &str, reason: &str) -> String {
    if reason.is_empty() {
        format!("[{label}]({url})")
    } else {
        format!("[{label}]({url}) - {reason}")
    }
}

fn render_sitemap_index(connection: &Connection) -> Result<String, String> {
    let mut providers = connection
        .prepare("SELECT DISTINCT provider FROM packages WHERE indexable = 1 ORDER BY provider")
        .map_err(|err| format!("failed to prepare sitemap provider query: {err}"))?;
    let provider_rows = providers
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|err| format!("failed to query sitemap providers: {err}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("failed to read sitemap providers: {err}"))?;
    let lastmod = sitemap_lastmod(connection)?;
    let mut xml = String::from(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n",
    );
    sitemap_entry(&mut xml, "/pkg/sitemap-hubs.xml", &lastmod);
    // History content has no evidenced modification timestamp.
    if project_history::enabled() {
        xml.push_str(&format!("  <sitemap><loc>{SITE_ORIGIN}/sitemap-history.xml</loc></sitemap>\n"));
    }
    for provider in provider_rows {
        sitemap_entry(&mut xml, &format!("/pkg/sitemap-{provider}.xml"), &lastmod);
    }
    xml.push_str("</sitemapindex>\n");
    Ok(xml)
}

fn render_hub_sitemap(connection: &Connection) -> Result<String, String> {
    let lastmod = sitemap_lastmod(connection)?;
    let mut urls = vec![sitemap_url("/", &lastmod), sitemap_url("/feed/", &lastmod)];
    for hub in hubs(connection)? {
        urls.push(sitemap_url(&hub.path, &lastmod));
    }
    Ok(render_urlset(&urls))
}

fn render_provider_sitemap(connection: &Connection, provider: &str) -> Result<String, String> {
    let fallback_lastmod = sitemap_lastmod(connection)?;
    let mut statement = connection
        .prepare(
            "SELECT path, last_updated_at
             FROM packages
             WHERE provider = ?1 AND indexable = 1
             ORDER BY slug",
        )
        .map_err(|err| format!("failed to prepare provider sitemap query: {err}"))?;
    let rows = statement
        .query_map(params![provider], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?))
        })
        .map_err(|err| format!("failed to query provider sitemap: {err}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("failed to read provider sitemap rows: {err}"))?;
    let urls = rows
        .iter()
        .map(|(path, last_updated_at)| {
            sitemap_url(
                path,
                non_empty(
                    last_updated_at.as_deref().unwrap_or_default(),
                    &fallback_lastmod,
                ),
            )
        })
        .collect::<Vec<_>>();
    Ok(render_urlset(&urls))
}

fn sitemap_lastmod(connection: &Connection) -> Result<String, String> {
    Ok(metadata_string(connection, "generated_at")?
        .and_then(|value| value.split('T').next().map(str::to_string))
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "2026-06-03".to_string()))
}

fn sitemap_entry(xml: &mut String, path: &str, lastmod: &str) {
    let path = public_catalog_path(path);
    xml.push_str(&format!(
        "  <sitemap>\n    <loc>{}{}</loc>\n    <lastmod>{}</lastmod>\n  </sitemap>\n",
        SITE_ORIGIN,
        html_escape(&path),
        html_escape(lastmod)
    ));
}

fn sitemap_url(path: &str, lastmod: &str) -> String {
    let path = public_catalog_path(path);
    let mut lines = vec![
        "  <url>".to_string(),
        format!("    <loc>{}{}</loc>", SITE_ORIGIN, html_escape(&path)),
        format!("    <lastmod>{}</lastmod>", html_escape(lastmod)),
    ];
    for locale in LOCALES {
        lines.push(format!(
            "    <xhtml:link rel=\"alternate\" hreflang=\"{}\" href=\"{}{}\" />",
            html_escape(locale.hreflang),
            SITE_ORIGIN,
            html_escape(&locale_path(&path, locale))
        ));
    }
    lines.push(format!(
        "    <xhtml:link rel=\"alternate\" hreflang=\"x-default\" href=\"{}{}\" />",
        SITE_ORIGIN,
        html_escape(&path)
    ));
    lines.push("  </url>".to_string());
    lines.join("\n")
}

fn render_urlset(urls: &[String]) -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\" xmlns:xhtml=\"http://www.w3.org/1999/xhtml\">\n{}\n</urlset>\n",
        urls.join("\n")
    )
}

fn public_catalog_path(path: &str) -> String {
    if path == "/pkg" || path == "/pkg/" {
        "/".to_string()
    } else if let Some(rest) = path.strip_prefix("/pkg/") {
        format!("/{rest}")
    } else {
        path.to_string()
    }
}

fn flatten_legacy_pkg_path(path: &str) -> String {
    for locale in LOCALES {
        let legacy_prefix = format!("{}/pkg", locale.prefix);
        if path == legacy_prefix || path == format!("{legacy_prefix}/") {
            return if locale.prefix.is_empty() {
                "/".to_string()
            } else {
                format!("{}/", locale.prefix)
            };
        }
        if let Some(rest) = path.strip_prefix(&format!("{legacy_prefix}/")) {
            return format!("{}/{rest}", locale.prefix);
        }
    }
    path.to_string()
}

fn locale_path(path: &str, locale: &Locale) -> String {
    let path = public_catalog_path(path);
    if locale.prefix.is_empty() {
        path
    } else {
        format!("{}{}", locale.prefix, path)
    }
}

fn html_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn empty_as_unknown(value: &str) -> &str {
    if value.is_empty() { "unknown" } else { value }
}

fn non_empty<'a>(value: &'a str, fallback: &'a str) -> &'a str {
    if value.is_empty() { fallback } else { value }
}

fn parse_query(query: &str) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        let key = decode_query_component(key);
        if key.is_empty() {
            continue;
        }
        values.insert(key, decode_query_component(value));
    }
    values
}

fn decode_query_component(value: &str) -> String {
    let value = value.replace('+', " ");
    urlencoding::decode(&value)
        .map(|value| value.into_owned())
        .unwrap_or(value)
}

fn locale_for_search(path: &str, query: &BTreeMap<String, String>) -> String {
    if let Some(locale) = query.get("locale").filter(|value| !value.is_empty()) {
        return locale.to_string();
    }
    match path {
        "/de/search.json" => "de",
        "/fr/search.json" => "fr",
        "/ja/search.json" => "ja",
        "/zh-hans/search.json" => "zh-Hans",
        _ => "en",
    }
    .to_string()
}

fn search_response_json(
    db_path: &Path,
    path: &str,
    query: &BTreeMap<String, String>,
) -> Result<Vec<u8>, String> {
    let search_query = query.get("q").map(String::as_str).unwrap_or("").trim();
    let offset = query
        .get("offset")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(0);
    let limit = query
        .get("limit")
        .and_then(|value| value.parse::<usize>().ok())
        .map(|value| value.min(MAX_SEARCH_LIMIT))
        .unwrap_or(DEFAULT_SEARCH_LIMIT);
    let locale = locale_for_search(path, query);
    let page = search_documents(db_path, search_query, &locale, offset, limit)?;
    serde_json::to_vec(&page).map_err(|err| format!("failed to encode search response: {err}"))
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct SearchPage {
    query: String,
    locale: String,
    results: Vec<SearchResult>,
    #[serde(rename = "totalCount")]
    total_count: usize,
    #[serde(rename = "nextOffset", skip_serializing_if = "Option::is_none")]
    next_offset: Option<usize>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
struct SearchResult {
    title: String,
    url: String,
    summary: String,
    provider: String,
    #[serde(rename = "packageKey")]
    package_key: String,
    rank: Option<u32>,
    #[serde(skip)]
    search_text: String,
}

fn search_documents(
    db_path: &Path,
    query: &str,
    locale: &str,
    offset: usize,
    limit: usize,
) -> Result<SearchPage, String> {
    let normalized_query = query.trim().to_ascii_lowercase();
    if normalized_query.is_empty() {
        return Ok(SearchPage {
            query: query.to_string(),
            locale: locale.to_string(),
            results: Vec::new(),
            total_count: 0,
            next_offset: None,
        });
    }

    let like_pattern = format!("%{}%", escape_like(&normalized_query));
    let connection = open_database(db_path)?;
    let mut statement = connection
        .prepare(
            "SELECT path, title, summary, provider, package_key, rank, search_text
             FROM search_documents
             WHERE locale = ?1 AND lower(search_text) LIKE ?2 ESCAPE '\\'",
        )
        .map_err(|err| format!("failed to prepare search: {err}"))?;
    let rows = statement
        .query_map(params![locale, like_pattern], |row| {
            Ok(SearchResult {
                url: row.get(0)?,
                title: row.get(1)?,
                summary: row.get(2)?,
                provider: row.get(3)?,
                package_key: row.get(4)?,
                rank: row.get(5)?,
                search_text: row.get(6)?,
            })
        })
        .map_err(|err| format!("failed to search packages: {err}"))?;
    let mut results = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(|err| format!("failed to read search rows: {err}"))?;
    for result in &mut results {
        result.url = flatten_legacy_pkg_path(&result.url);
    }
    results.retain(|result| result.search_rank(&normalized_query).is_some());
    results.sort_by(|left, right| {
        left.search_sort_key(&normalized_query)
            .cmp(&right.search_sort_key(&normalized_query))
    });
    let total_count = results.len();
    let next_offset_value = offset.saturating_add(limit);
    let next_offset = (next_offset_value < total_count).then_some(next_offset_value);
    let results = results.into_iter().skip(offset).take(limit).collect();
    Ok(SearchPage {
        query: query.to_string(),
        locale: locale.to_string(),
        results,
        total_count,
        next_offset,
    })
}

fn escape_like(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

impl SearchResult {
    fn search_sort_key(&self, query: &str) -> (u8, usize, u32, String) {
        (
            self.search_rank(query).unwrap_or(u8::MAX),
            self.match_distance(query),
            self.rank.unwrap_or(u32::MAX),
            self.title.to_ascii_lowercase(),
        )
    }

    fn search_rank(&self, query: &str) -> Option<u8> {
        let title = self.title.to_ascii_lowercase();
        let key = self.package_key.to_ascii_lowercase();
        let url_name = self
            .url
            .trim_end_matches('/')
            .rsplit('/')
            .next()
            .unwrap_or("")
            .to_ascii_lowercase();
        let candidates = [title.as_str(), key.as_str(), url_name.as_str()];
        if candidates.iter().any(|candidate| *candidate == query) {
            return Some(0);
        }
        if candidates
            .iter()
            .any(|candidate| candidate.starts_with(query))
        {
            return Some(1);
        }
        if candidates.iter().any(|candidate| candidate.contains(query)) {
            return Some(2);
        }
        self.search_text
            .to_ascii_lowercase()
            .contains(query)
            .then_some(3)
    }

    fn match_distance(&self, query: &str) -> usize {
        [
            self.title.to_ascii_lowercase(),
            self.package_key.to_ascii_lowercase(),
            self.url
                .trim_end_matches('/')
                .rsplit('/')
                .next()
                .unwrap_or("")
                .to_ascii_lowercase(),
            self.search_text.to_ascii_lowercase(),
        ]
        .into_iter()
        .filter_map(|candidate| {
            if candidate == query {
                Some(0)
            } else if candidate.starts_with(query) {
                Some(candidate.len().saturating_sub(query.len()))
            } else {
                candidate
                    .find(query)
                    .map(|index| candidate.len().saturating_sub(query.len()) + index)
            }
        })
        .min()
        .unwrap_or(usize::MAX)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::params;
    use std::io::{Read, Write};
    use std::net::Shutdown;
    use std::sync::{Mutex, MutexGuard, OnceLock};
    use std::thread;
    use tempfile::{NamedTempFile, tempdir};

    struct PackageFixture<'a> {
        path: &'a str,
        provider: &'a str,
        slug: &'a str,
        package_key: &'a str,
        name: &'a str,
        display_name: &'a str,
        summary: &'a str,
        provider_label: &'a str,
        package_manager_url: &'a str,
        install_command: &'a str,
        native_install_command: &'a str,
        version: &'a str,
        category: &'a str,
        license: &'a str,
        homepage: &'a str,
        repository: &'a str,
        rank: Option<u32>,
        last_updated_at: &'a str,
        indexable: bool,
        data_json: String,
        search_text: &'a str,
    }

    fn insert_package(connection: &Connection, package: PackageFixture<'_>) {
        connection
            .execute(
                "INSERT INTO packages(path, provider, slug, package_key, name, display_name, summary,
                 provider_label, package_manager_url, install_command, native_install_command,
                 version, category, license, homepage, repository, rank, last_updated_at, indexable,
                 data_json, search_text)
                 VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21)",
                params![
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
                    if package.indexable { 1 } else { 0 },
                    package.data_json,
                    package.search_text
                ],
            )
            .expect("insert package");
    }

    fn test_database() -> NamedTempFile {
        let file = NamedTempFile::new().expect("temp database");
        let connection = Connection::open(file.path()).expect("open temp database");
        connection
            .execute_batch(
                "
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value) VALUES('schema', '1');
                CREATE TABLE search_documents(
                    path TEXT PRIMARY KEY,
                    locale TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    package_key TEXT NOT NULL,
                    rank INTEGER,
                    search_text TEXT NOT NULL
                );
                CREATE TABLE packages(
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
                CREATE TABLE hubs(
                    path TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE TABLE hub_packages(
                    hub_slug TEXT NOT NULL,
                    package_key TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(hub_slug, package_key)
                );
                ",
            )
            .expect("create schema");
        for (key, value) in [
            ("generated_at", r#""2026-06-03T19:54:51Z""#),
            ("last_modified", r#""Tue, 02 Jun 2026 19:54:51 GMT""#),
            ("source_hash", r#""coverage-fixture""#),
            (
                "manifest",
                r#"{"radioisotope_manifest_count":4,"source_file_count":42}"#,
            ),
        ] {
            connection
                .execute(
                    "INSERT INTO metadata(key, value) VALUES(?1, ?2)",
                    params![key, value],
                )
                .expect("insert metadata");
        }
        let aws_registry_insights = serde_json::json!({
            "sourceDatabase": "Homebrew formula API",
            "tap": "homebrew/core",
            "fullName": "awscli",
            "aliases": ["aws-cli"],
            "versionScheme": 1,
            "revision": 2,
            "distTags": {"stable": "2.0.0", "head": "2.1.0"},
            "dependencies": ["python@3.13", "openssl@3"],
            "requirements": {"macos": ">= Ventura"},
            "funding": {"url": "https://github.com/sponsors/aws"},
            "unpackedSize": 123456789,
            "latestUploadAt": "2026-06-01T12:00:00Z"
        });
        let aws_external_package_manager_matches = serde_json::json!([
            {
                "manager": "apt",
                "displayName": "Debian apt",
                "platform": "linux",
                "packageId": "awscli",
                "command": "sudo apt install awscli",
                "confidence": 0.82,
                "matchedBy": "exact_name",
                "reason": "Same package name in Debian package index.",
                "evidence": "Debian package metadata",
                "source": {
                    "sourceLabel": "Debian sid Packages",
                    "sourceUrl": "https://packages.debian.org/sid/awscli"
                },
                "metadata": {
                    "version": "2.0.0-1",
                    "summary": "Universal command line interface for Amazon Web Services",
                    "homepage": "https://aws.amazon.com/cli/",
                    "license": "Apache-2.0",
                    "section": "admin",
                    "architecture": "all",
                    "dependencies": ["python3", "python3-botocore"]
                }
            },
            {
                "manager": "winget",
                "displayName": "Windows Package Manager",
                "platform": "windows",
                "packageId": "Amazon.AWSCLI",
                "command": "winget install Amazon.AWSCLI",
                "confidence": 0.95,
                "matchedBy": "alias",
                "reason": "Known AWS CLI installer package.",
                "evidence": "WinGet package metadata",
                "source": {
                    "sourceLabel": "WinGet manifests",
                    "sourceUrl": "https://github.com/microsoft/winget-pkgs/tree/master/manifests/a/Amazon/AWSCLI"
                },
                "metadata": {
                    "version": "2.0.0",
                    "summary": "AWS CLI v2 installer for Windows",
                    "publisher": "Amazon Web Services",
                    "monikers": ["aws", "awscli"]
                }
            }
        ]);
        insert_package(
            &connection,
            PackageFixture {
                path: "/pkg/brew/awscli/",
                provider: "brew",
                slug: "awscli",
                package_key: "brew:awscli",
                name: "awscli",
                display_name: "awscli",
                summary: "AWS command line interface.",
                provider_label: "Homebrew",
                package_manager_url: "https://brew.example/awscli",
                install_command: "sudo av install awscli",
                native_install_command: "brew install awscli",
                version: "2.0.0",
                category: "developer-tools",
                license: "Apache-2.0",
                homepage: "https://aws.amazon.com/cli/",
                repository: "https://github.com/aws/aws-cli",
                rank: Some(2),
                last_updated_at: "2026-06-02",
                indexable: true,
                data_json: serde_json::json!({"full": {
                    "aliases": ["aws", "aws-vault"],
                    "history": {
                        "summary": ["AWS CLI is Amazon's command-line interface for controlling AWS services from local shells and automation systems."],
                        "project-history": ["The AWS CLI grew out of Amazon Web Services' need for a consistent command-line surface across service APIs."],
                        "adoption-history": ["It became a default package-manager install for cloud operators because many AWS docs, scripts, and CI workflows assume an `aws` executable is available."],
                        "usage": ["Package users commonly install it for authentication checks, S3 file movement, IAM inspection, and deployment automation."],
                        "package-nerd-significance": ["For package catalog users, awscli is a canonical high-authority CLI: one small executable exposes a large cloud control plane."],
                        "timeline": ["2013: AWS CLI v1 public documentation establishes the unified `aws` command.", "2020: AWS CLI v2 becomes the default generation of the tool."],
                        "related-projects": ["aws-vault", "aws-cdk"],
                        "sources": ["https://docs.aws.amazon.com/cli/latest/userguide/"]
                    },
                    "executablesDetailed": [
                        {"name": "aws", "kind": "executable", "exposure": "global executable", "note": "Primary CLI"},
                        {"target": "aws_completer", "kind": "completion", "note": "Shell completion"},
                        {"source": "aws-shell", "kind": "helper"}
                    ],
                    "binaries": [{"target": "aws-iam-authenticator", "source": "bin/aws-iam-authenticator"}],
                    "extra": {"stub_exclusions": ["aws-vault"]},
                    "registryInsights": aws_registry_insights,
                    "externalPackageManagerMatches": aws_external_package_manager_matches,
                    "security": ["approval gate"],
                    "keywords": ["cloud", "aws"],
                    "classifiers": ["devops"],
                    "installCommands": [
                        {"platform": "portable", "manager": "Automic Vault", "command": "sudo av install awscli", "kind": "automic_vault", "confidence": 1.0, "evidence": "deterministic local package key"},
                        {"platform": "macos", "manager": "Homebrew", "command": "brew install awscli", "kind": "package_manager", "confidence": 1.0, "source": {"source_label": "Homebrew Formula", "package_name": "awscli", "source_url": "https://github.com/Homebrew/homebrew-core/blob/HEAD/Formula/a/awscli.rb"}},
                        {"platform": "linux", "manager": "apt", "command": "sudo apt install awscli", "confidence": 0.82, "evidence": "Debian package metadata"},
                        {"platform": "windows", "manager": "winget", "command": "winget install Amazon.AWSCLI", "confidence": 0.95, "source": {"source_label": "WinGet", "package_id": "Amazon.AWSCLI", "source_url": "https://github.com/microsoft/winget-pkgs"}},
                        {"platform": "portable", "manager": "pip", "command": "python3 -m pip install awscli", "confidence": 0.91, "evidence": "PyPI package"}
                    ],
                    "isotope": {
                        "justification": {
                            "title": "AWS credential file coverage",
                            "detail": "Detects plaintext AWS access keys and moves them into the local vault."
                        },
                        "caveats": ["Profiles with SSO metadata are left in place."]
                    },
                    "isotopeReadmeHtml": "<p>Use <code>av isotope migrate</code> for AWS credentials.</p>",
                    "isotopeReadmeSource": "data/radioisotopes/aws-cli/README.md",
                    "geiger": {
                        "level": "high",
                        "confidence": "strong",
                        "category": "cloud",
                        "reasons": ["Reads cloud credentials from ~/.aws/credentials.", "Can mutate cloud infrastructure."],
                        "signals": ["cloud credentials", "remote state"]
                    },
                    "installBehavior": {
                        "postInstallDefined": true,
                        "service": "com.aws.cli",
                        "caveats": "Shell completions are installed.",
                        "lifecycleScripts": ["postinstall", "prepare"],
                        "prepareDefined": true,
                        "pythonRequires": ">=3.11",
                        "requiresDistCount": 4
                    },
                    "bottle": {"available": true, "platforms": ["arm64_ventura", "sonoma"]},
                    "approvalGate": {
                        "rule_count": 2,
                        "rules": ["aws iam create-access-key", "aws s3 rm --recursive"],
                        "severities": ["high", "critical"],
                        "entrypoints": ["aws"],
                        "coverage_status": "reviewed",
                        "reviewed_at": "2026-06-01"
                    },
                    "agentSafetyAnswer": {
                        "summary": "AWS CLI can read durable cloud credentials and mutate production infrastructure, so agent runs should use scoped profiles plus human approval for destructive commands.",
                        "credentialAccess": "Reads AWS shared credential and config files, SSO cache metadata, and environment variables such as AWS_ACCESS_KEY_ID.",
                        "remoteMutation": "Can create, delete, and reconfigure cloud resources across IAM, S3, EC2, EKS, and other AWS services.",
                        "publishOrArtifactRisk": "Can upload artifacts to S3, push container images through AWS services, and expose generated deployment assets.",
                        "recommendedControl": "Use protected AWS credential helpers, short-lived role sessions, and approval gates for IAM, delete, write, and deployment subcommands.",
                        "agentUseGuidance": "Allow read-only inventory commands by default, but require review before an agent changes infrastructure, secrets, policies, buckets, or deployment state."
                    },
                    "versionFreshness": {
                        "packageManager": {"version": "2.0.0", "updatedAt": "2026-06-02T00:00:00Z"},
                        "siteData": {"status": "current"},
                        "upstream": {"repository": "https://github.com/aws/aws-cli", "latestVersion": "2.0.1", "comparison": "behind"},
                        "warnings": [
                            {"severity": "warning", "message": "Upstream has a newer patch release.", "evidence": "https://github.com/aws/aws-cli/releases", "confidence": "high"},
                            {"kind": "stale_source"}
                        ]
                    },
                    "popularity": {"rank": 2, "installs_per_365_days": 1234567},
                    "dependencies": ["python@3.13", "openssl@3"],
                    "buildDependencies": ["pkgconf"],
                    "usesFromMacos": ["zlib"],
                    "upstreamDocs": "https://docs.aws.amazon.com/cli/latest/userguide/",
                    "sourceArchive": "https://example.test/awscli.tar.gz",
                    "issueTracker": "https://github.com/aws/aws-cli/issues",
                    "publishedAt": "2026-05-01",
                    "lastVerified": "2026-06-03T10:20:30Z",
                    "pulseKind": "current",
                    "sha256": "abc123",
                    "url": "https://example.test/awscli.tar.gz",
                    "packageHubs": [
                        {"slug": "cloud", "label": "Cloud", "reason": "Cloud control plane CLI"},
                        {"slug": "source-control", "label": "Source control", "reason": "Credential-adjacent workflows"}
                    ],
                    "relatedPackages": [
                        {"provider": "brew", "name": "aws-cdk", "label": "AWS CDK", "reason": "same cloud workflow", "rel": "adjacent_workflow"},
                        {"provider": "brew", "name": "cloudflared", "label": "cloudflared", "reason": "network tunnel", "rel": "domain_peer"},
                        {"provider": "brew", "name": "awscli", "label": "self", "rel": "domain_peer"}
                    ],
                    "alsoAvailableVia": [{"provider": "pip", "name": "awscli", "label": "awscli on PyPI", "reason": "Python package"}],
                    "sourceNotes": ["Homebrew formula", "Geiger classifier", "Homebrew formula"]
                }})
                .to_string(),
                search_text: "awscli brew:awscli aws cloud cli",
            },
        );
        insert_package(
            &connection,
            PackageFixture {
                path: "/pkg/npm/private-tool/",
                provider: "npm",
                slug: "private-tool",
                package_key: "npm:private-tool",
                name: "private-tool",
                display_name: "private-tool",
                summary: "Internal package with no public index page.",
                provider_label: "npm",
                package_manager_url: "",
                install_command: "",
                native_install_command: "npm install private-tool",
                version: "",
                category: "developer-tools",
                license: "",
                homepage: "",
                repository: "",
                rank: None,
                last_updated_at: "",
                indexable: false,
                data_json: serde_json::json!({"full": {
                    "approvalGate": {"rule_count": 1, "rules": [], "coverage_status": "draft"},
                    "installBehavior": {"postInstallDefined": false},
                    "dependencies": ["left-pad"]
                }})
                .to_string(),
                search_text: "private-tool npm internal",
            },
        );
        insert_package(
            &connection,
            PackageFixture {
                path: "/pkg/cargo/ripgrep/",
                provider: "cargo",
                slug: "ripgrep",
                package_key: "cargo:ripgrep",
                name: "ripgrep",
                display_name: "ripgrep",
                summary: "Line-oriented search tool.",
                provider_label: "Cargo",
                package_manager_url: "https://crates.io/crates/ripgrep",
                install_command: "sudo av install ripgrep",
                native_install_command: "cargo install ripgrep",
                version: "14.1.1",
                category: "developer-tools",
                license: "MIT OR Unlicense",
                homepage: "https://github.com/BurntSushi/ripgrep",
                repository: "https://github.com/BurntSushi/ripgrep",
                rank: Some(12),
                last_updated_at: "2026-06-01",
                indexable: true,
                data_json: serde_json::json!({"full": {
                    "aliases": ["rg"],
                    "executablesDetailed": [
                        {"name": "rg", "kind": "executable", "exposure": "global executable"}
                    ],
                    "registryInsights": {
                        "sourceDatabase": "crates.io DB dump",
                        "defaultVersion": "14.1.1",
                        "recentDownloads": 12345,
                        "totalDownloads": 45678
                    },
                    "installCommands": [
                        {"platform": "portable", "manager": "Automic Vault", "command": "sudo av install ripgrep", "kind": "automic_vault", "confidence": 1.0, "evidence": "deterministic local package key"},
                        {"platform": "portable", "manager": "Cargo", "command": "cargo install ripgrep", "kind": "package_manager", "confidence": 1.0, "evidence": "crates.io default version"}
                    ],
                    "installBehavior": {"postInstallDefined": false},
                    "popularity": {"rank": 12, "downloads_recent": 12345, "downloads_total": 45678},
                    "sourceArchive": "https://crates.io/api/v1/crates/ripgrep/14.1.1/download",
                    "sourceNotes": ["crates.io DB dump"]
                }})
                .to_string(),
                search_text: "ripgrep cargo:ripgrep rg search grep",
            },
        );
        for (path, slug, title, description, group) in [
            ("/pkg/cloud/", "cloud", "Cloud", "Cloud tools", "topical"),
            (
                "/pkg/risky-tools/",
                "risky-tools",
                "Risky tools",
                "Packages that need review.",
                "security",
            ),
            (
                "/pkg/javascript/",
                "javascript",
                "JavaScript",
                "Node and npm package ecosystem tools.",
                "ecosystem",
            ),
            (
                "/pkg/crates-cli-packages/",
                "crates-cli-packages",
                "crates.io command packages",
                "crates.io packages with local package pages and command-line or developer workflow metadata.",
                "ecosystem",
            ),
            (
                "/pkg/source-control/",
                "source-control",
                "Source control",
                "Source-control and repository workflow tools.",
                "topical",
            ),
        ] {
            connection
                .execute(
                    "INSERT INTO hubs(path, slug, title, description, group_name, data_json)
                     VALUES(?1, ?2, ?3, ?4, ?5, '{}')",
                    params![path, slug, title, description, group],
                )
                .expect("insert hub");
        }
        connection
            .execute(
                "INSERT INTO hub_packages(hub_slug, package_key, position, reason)
                 VALUES('cloud', 'brew:awscli', 1, 'Cloud CLI')",
                [],
            )
            .expect("insert hub package");
        connection
            .execute(
                "INSERT INTO hub_packages(hub_slug, package_key, position, reason)
                 VALUES('source-control', 'brew:awscli', 1, 'Credential-adjacent workflows')",
                [],
            )
            .expect("insert related hub package");
        connection
            .execute(
                "INSERT INTO hub_packages(hub_slug, package_key, position, reason)
                 VALUES('crates-cli-packages', 'cargo:ripgrep', 1, 'Matched crates.io Cargo package provider.')",
                [],
            )
            .expect("insert crates.io hub package");
        for (path, title, summary, provider, key, rank, text) in [
            (
                "/pkg/brew/awscli/",
                "awscli",
                "AWS command line interface.",
                "brew",
                "brew:awscli",
                Some(2_u32),
                "awscli brew:awscli aws command line interface cloud",
            ),
            (
                "/pkg/brew/aws-cdk/",
                "aws-cdk",
                "AWS CDK.",
                "brew",
                "brew:aws-cdk",
                Some(50_u32),
                "aws-cdk brew:aws-cdk aws infrastructure",
            ),
            (
                "/pkg/brew/cloudflared/",
                "cloudflared",
                "Tunnel client for AWS adjacent workflows.",
                "brew",
                "brew:cloudflared",
                Some(1_u32),
                "cloudflared tunnel client aws adjacent workflows",
            ),
            (
                "/de/pkg/brew/awscli/",
                "AWS CLI",
                "Deutsch AWS CLI.",
                "brew",
                "brew:awscli",
                Some(2_u32),
                "aws deutsch cloud cli",
            ),
        ] {
            connection
                .execute(
                    "INSERT INTO search_documents(path, locale, title, summary, provider, package_key, rank, search_text)
                     VALUES(?1, ?8, ?2, ?3, ?4, ?5, ?6, ?7)",
                    params![
                        path,
                        title,
                        summary,
                        provider,
                        key,
                        rank,
                        text,
                        if path.starts_with("/de/") { "de" } else { "en" }
                    ],
                )
                .expect("insert search row");
        }
        drop(connection);
        file
    }

    fn handle_raw_request(db_path: &Path, raw_request: &str, secret: Option<&str>) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind listener");
        let addr = listener.local_addr().expect("listener address");
        let state = AppState {
            bind_addr: addr.to_string(),
            db_path: db_path.to_path_buf(),
            public_db_path: db_path.with_extension("json"),
            feed_root: db_path.with_extension("feed"),
            origin_header: "x-test-origin".to_string(),
            origin_secret: secret.map(str::to_string),
        };
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept request");
            handle_connection(stream, &state).expect("handle request");
        });
        let mut client = TcpStream::connect(addr).expect("connect client");
        client
            .write_all(raw_request.as_bytes())
            .expect("write request");
        client
            .shutdown(Shutdown::Write)
            .expect("finish request body");
        let mut response = String::new();
        client.read_to_string(&mut response).expect("read response");
        server.join().expect("join server");
        response
    }

    fn read_raw_request(raw_request: &str) -> Result<Option<Request>, String> {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind listener");
        let addr = listener.local_addr().expect("listener address");
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept request");
            read_request(&stream)
        });
        let mut client = TcpStream::connect(addr).expect("connect client");
        client
            .write_all(raw_request.as_bytes())
            .expect("write request");
        client
            .shutdown(Shutdown::Write)
            .expect("finish request body");
        server.join().expect("join server")
    }

    fn serde_json_package(base: &PackageRow, full: Value) -> PackageRow {
        let mut package = base.clone();
        package.data.full = full;
        package
    }

    fn markdown_agent_safety_missing_summary(base: &PackageRow) -> String {
        let package = serde_json_package(base, serde_json::json!({"agentSafetyAnswer": {}}));
        let mut text = String::new();
        markdown_agent_safety_section(&mut text, &package, &LOCALES[0]);
        text
    }

    fn env_lock() -> &'static Mutex<()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
    }

    struct EnvGuard {
        _lock: MutexGuard<'static, ()>,
        values: Vec<(&'static str, Option<std::ffi::OsString>)>,
    }

    impl EnvGuard {
        fn set(values: &[(&'static str, Option<&str>)]) -> Self {
            let lock = env_lock().lock().unwrap();
            let guard = Self {
                _lock: lock,
                values: values
                    .iter()
                    .map(|(key, _)| (*key, env::var_os(key)))
                    .collect(),
            };
            for (key, value) in values {
                unsafe {
                    match value {
                        Some(value) => env::set_var(key, value),
                        None => env::remove_var(key),
                    }
                }
            }
            guard
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            for (key, value) in &self.values {
                unsafe {
                    match value {
                        Some(value) => env::set_var(key, value),
                        None => env::remove_var(key),
                    }
                }
            }
        }
    }

    #[test]
    fn app_state_request_and_query_helpers_cover_edge_cases() {
        let db = test_database();
        assert_database_ready(db.path()).unwrap();
        let empty_db = NamedTempFile::new().expect("empty database");
        assert!(
            assert_database_ready(empty_db.path())
                .unwrap_err()
                .contains("is not ready")
        );

        {
            let _env = EnvGuard::set(&[
                ("AV_WEB_BIND_ADDR", Some("127.0.0.1:3999")),
                ("AV_WEB_DB_PATH", Some(db.path().to_str().unwrap())),
                ("AV_WEB_ORIGIN_HEADER", Some("X-Coverage-Origin")),
                ("AV_WEB_ORIGIN_SECRET", Some("")),
            ]);
            let state = AppState::from_env();
            assert_eq!(state.bind_addr, "127.0.0.1:3999");
            assert_eq!(state.db_path, db.path());
            assert_eq!(state.origin_header, "x-coverage-origin");
            assert_eq!(state.origin_secret, None);
        }

        {
            let missing = db.path().with_file_name("missing-av-web.sqlite");
            let _env = EnvGuard::set(&[
                ("AV_WEB_BIND_ADDR", Some("127.0.0.1:0")),
                ("AV_WEB_DB_PATH", Some(missing.to_str().unwrap())),
                ("AV_WEB_ORIGIN_HEADER", None),
                ("AV_WEB_ORIGIN_SECRET", None),
            ]);
            assert!(run().unwrap_err().contains("failed to open"));
        }

        let request = read_raw_request(
            "get /pkg/search.json?q=aws+cli&blank=&bad=%ZZ HTTP/1.1\r\nHost: test\r\nX-Test: One\r\ncontinued\r\n\r\n",
        )
        .unwrap()
        .unwrap();
        assert_eq!(request.method, "GET");
        assert_eq!(request.path, "/pkg/search.json");
        assert_eq!(request.query.as_deref(), Some("q=aws+cli&blank=&bad=%ZZ"));
        assert_eq!(
            request.headers.get("x-test").map(String::as_str),
            Some("One")
        );

        assert_eq!(read_raw_request("").unwrap(), None);
        assert!(
            read_raw_request("BROKEN\r\n")
                .unwrap_err()
                .contains("invalid request line")
        );
        assert!(
            read_raw_request("GET /pkg/%FF HTTP/1.1\r\nHost: test\r\n\r\n")
                .unwrap_err()
                .contains("invalid request path encoding")
        );

        let parsed = parse_query("=ignored&q=aws+cli&bad=%ZZ&bare&limit=99");
        assert_eq!(parsed.get("q").map(String::as_str), Some("aws cli"));
        assert_eq!(parsed.get("bad").map(String::as_str), Some("%ZZ"));
        assert_eq!(parsed.get("bare").map(String::as_str), Some(""));
        assert_eq!(
            split_target("pkg/brew/awscli#fragment").unwrap(),
            ("/pkg/brew/awscli".to_string(), None)
        );
        assert_eq!(
            legacy_pkg_redirect_location("/fr/pkg/brew/awscli/", None).as_deref(),
            Some("/fr/brew/awscli/")
        );
        assert_eq!(
            legacy_pkg_redirect_location("/pkg/search.json", Some("q=aws")).as_deref(),
            Some("/search.json?q=aws")
        );
        assert_eq!(legacy_pkg_redirect_location("/not-pkg", None), None);
        assert!(is_search_path("/ja/search.json"));
        assert_eq!(
            locale_for_search("/zh-hans/search.json", &BTreeMap::new()),
            "zh-Hans"
        );
        assert_eq!(
            locale_for_search(
                "/search.json",
                &BTreeMap::from([("locale".to_string(), "ja".to_string())])
            ),
            "ja"
        );
        assert_eq!(empty_as_unknown(""), "unknown");
        assert_eq!(empty_as_unknown("1.2.3"), "1.2.3");
        assert_eq!(non_empty("", "fallback"), "fallback");
        assert_eq!(non_empty("value", "fallback"), "value");
        assert!(
            render_urlset(&[sitemap_url("/pkg/brew/awscli/", "2026-06-03")])
                .contains("hreflang=\"x-default\"")
        );
        assert_eq!(canonical_pkg_route("/fr/pkg").1, "/pkg/index.html");
        assert_eq!(
            canonical_pkg_route("/fr/brew/awscli/").1,
            "/pkg/brew/awscli/index.html"
        );
        assert_eq!(
            canonical_pkg_route("/pkg/brew/awscli").1,
            "/pkg/brew/awscli/index.html"
        );
        assert_eq!(
            canonical_pkg_route("/pkg/brew/awscli.json").1,
            "/pkg/brew/awscli.json"
        );
        assert_eq!(
            package_json_route("/pkg/brew/awscli.json"),
            Some(("brew", "awscli"))
        );
        assert_eq!(
            dynamic_response_for_path(db.path(), "/not-pkg").unwrap(),
            None
        );
    }

    #[test]
    fn static_assets_are_embedded_versioned_and_locale_independent() {
        let css = static_asset_response("/pkg/styles.css").expect("css response");
        let localized_css =
            static_asset_response("/fr/pkg/styles.css").expect("localized css response");
        let javascript = static_asset_response("/pkg/search.js").expect("javascript response");

        assert_eq!(css.body, localized_css.body);
        assert_eq!(css.content_type, "text/css; charset=utf-8");
        assert_eq!(css.cache_control, HTML_CACHE_CONTROL);
        assert!(!css.cache_control.contains("immutable"));
        assert_eq!(css.etag, format!("\"{}\"", asset_version(PKG_CSS)));
        assert!(String::from_utf8(css.body).unwrap().contains("--surface"));
        assert_eq!(
            javascript.content_type,
            "application/javascript; charset=utf-8"
        );
        assert!(static_asset_response("/pkg/brew/awscli/").is_none());
    }

    #[test]
    fn discover_feed_only_serves_documents_and_referenced_artwork() {
        let release = tempdir().expect("feed release");
        fs::create_dir_all(release.path().join("v2/pages")).expect("feed pages");
        fs::create_dir_all(release.path().join("assets")).expect("feed artwork");
        fs::write(release.path().join("v1.json"), br#"{"schemaVersion":1}"#).expect("v1");
        fs::write(
            release.path().join("v2.json"),
            br#"{"artwork":{"path":"feed/assets/cover.png"}}"#,
        )
        .expect("v2");
        fs::write(release.path().join("v2/pages/archive.json"), b"{}")
            .expect("archive page");
        fs::write(release.path().join("assets/cover.png"), b"png")
            .expect("referenced artwork");
        fs::write(release.path().join("assets/private.png"), b"png")
            .expect("unreferenced artwork");

        let v2 = discover_feed_response(release.path(), "/discover/feed/v2.json")
            .expect("read v2")
            .expect("v2 response");
        assert_eq!(v2.content_type, "application/json; charset=utf-8");
        assert_eq!(v2.cache_control, HTML_CACHE_CONTROL);
        assert!(v2.etag.starts_with('"'));
        assert!(discover_feed_response(release.path(), "/discover/feed/v2/pages/archive.json")
            .expect("read archive")
            .is_some());
        let artwork = discover_feed_response(release.path(), "/discover/feed/assets/cover.png")
            .expect("read artwork")
            .expect("artwork response");
        assert_eq!(artwork.content_type, "image/png");
        assert!(discover_feed_response(release.path(), "/discover/feed/assets/private.png")
            .expect("read private artwork")
            .is_none());
        for path in [
            "/discover/feed/../v1.json",
            "/discover/feed/v2/pages/../../v1.json",
            "/discover/feed/assets/cover.png.json",
            "/discover/feed/anything.txt",
        ] {
            assert!(discover_feed_response(release.path(), path)
                .expect("reject invalid route")
                .is_none(), "{path}");
        }
    }

    #[test]
    fn markdown_route_returns_markdown_content_type() {
        let db = test_database();
        let response = dynamic_response_for_path(db.path(), "/pkg/brew/awscli/index.md")
            .expect("query")
            .expect("markdown response");

        assert_eq!(response.content_type, "text/markdown; charset=utf-8");
        assert!(
            String::from_utf8(response.body)
                .expect("utf-8 markdown")
                .contains("## Install")
        );
    }

    #[test]
    fn adsense_covers_page_types_and_authorizes_publisher() {
        let db = test_database();
        for path in ["/", "/de/", "/brew/awscli/", "/fr/brew/awscli/", "/cloud/", "/feed/", "/privacy/"] {
            let response = dynamic_response_for_path(db.path(), path).unwrap().unwrap();
            let html = String::from_utf8(response.body).unwrap();
            let head = html.split("</head>").next().unwrap();
            assert_eq!(head.matches("adsbygoogle.js?client=ca-pub-5218328608281545").count(), 1, "{path}");
        }
        let ads = static_asset_response("/ads.txt").unwrap();
        assert_eq!(ads.content_type, "text/plain; charset=utf-8");
        assert_eq!(String::from_utf8(ads.body).unwrap(), "google.com, pub-5218328608281545, DIRECT, f08c47fec0942fa0\n");
    }

    #[test]
    fn dynamic_routes_render_package_hub_and_sitemap() {
        let db = test_database();
        let root = dynamic_response_for_path(db.path(), "/")
            .expect("query")
            .expect("root response");
        let localized_root = dynamic_response_for_path(db.path(), "/de/")
            .expect("query")
            .expect("localized root response");
        let robots = dynamic_response_for_path(db.path(), "/robots.txt")
            .expect("query")
            .expect("robots response");
        let root_sitemap = dynamic_response_for_path(db.path(), "/sitemap.xml")
            .expect("query")
            .expect("root sitemap response");
        let package = dynamic_response_for_path(db.path(), "/brew/awscli/")
            .expect("query")
            .expect("package response");
        let cargo_package = dynamic_response_for_path(db.path(), "/cargo/ripgrep/")
            .expect("query")
            .expect("cargo package response");
        let localized_package = dynamic_response_for_path(db.path(), "/fr/brew/awscli/")
            .expect("query")
            .expect("localized package response");
        let hub = dynamic_response_for_path(db.path(), "/cloud/")
            .expect("query")
            .expect("hub response");
        let hub_markdown = dynamic_response_for_path(db.path(), "/cloud/index.md")
            .expect("query")
            .expect("hub markdown response");
        let crates_hub = dynamic_response_for_path(db.path(), "/crates-cli-packages/")
            .expect("query")
            .expect("crates.io hub response");
        let crates_hub_markdown =
            dynamic_response_for_path(db.path(), "/crates-cli-packages/index.md")
                .expect("query")
                .expect("crates.io hub markdown response");
        let index = dynamic_response_for_path(db.path(), "/de/")
            .expect("query")
            .expect("localized index response");
        let sitemap_index = dynamic_response_for_path(db.path(), "/sitemap.xml")
            .expect("query")
            .expect("sitemap index response");
        let hub_sitemap = dynamic_response_for_path(db.path(), "/sitemap-hubs.xml")
            .expect("query")
            .expect("hub sitemap response");
        let sitemap = dynamic_response_for_path(db.path(), "/sitemap-brew.xml")
            .expect("query")
            .expect("sitemap response");
        let cargo_sitemap = dynamic_response_for_path(db.path(), "/sitemap-cargo.xml")
            .expect("query")
            .expect("cargo sitemap response");
        let package_json = dynamic_response_for_path(db.path(), "/brew/awscli.json")
            .expect("query")
            .expect("package json response");
        let new_json = dynamic_response_for_path(db.path(), "/new.json")
            .expect("query")
            .expect("new json response");
        let feed_page = dynamic_response_for_path(db.path(), "/feed/")
            .expect("query")
            .expect("feed page response");
        let localized_feed = dynamic_response_for_path(db.path(), "/de/feed/")
            .expect("query")
            .expect("localized feed response");
        let manifest = dynamic_response_for_path(db.path(), "/.manifest.json")
            .expect("query")
            .expect("manifest response");

        let root_html = String::from_utf8(root.body).expect("root html");
        let localized_root_html =
            String::from_utf8(localized_root.body).expect("localized root html");
        assert!(root_html.contains("<title>Package catalog | pkg.so</title>"));
        assert!(root_html.contains(r#"<link rel="canonical" href="https://pkg.so/">"#));
        assert!(root_html.contains(r#"<a class="brand" href="/""#));
        assert!(root_html.contains(r#"href="/#search">Search</a>"#));
        assert!(root_html.contains(r#"id="search" class="pkg-search""#));
        assert!(!root_html.contains(r#">Index</a>"#));
        assert!(!root_html.contains("Explore packages"));
        assert!(root_html.contains(r#"href="/feed/">Feed</a>"#));
        assert!(root_html.contains(r#"href="https://mxcl.dev">a mxcl project</a>"#));
        assert!(root_html.contains("https://github.com/mxcl/pkg.so"));
        assert!(localized_root_html.contains("<html lang=\"de\">"));
        assert!(
            localized_root_html.contains(r#"<link rel="canonical" href="https://pkg.so/de/">"#)
        );
        let expected_root_hreflang = [
            r#"<link rel="alternate" hreflang="en" href="https://pkg.so/">"#,
            r#"<link rel="alternate" hreflang="de" href="https://pkg.so/de/">"#,
            r#"<link rel="alternate" hreflang="fr" href="https://pkg.so/fr/">"#,
            r#"<link rel="alternate" hreflang="ja" href="https://pkg.so/ja/">"#,
            r#"<link rel="alternate" hreflang="zh-Hans" href="https://pkg.so/zh-hans/">"#,
            r#"<link rel="alternate" hreflang="x-default" href="https://pkg.so/">"#,
        ];
        for link in expected_root_hreflang {
            assert!(localized_root_html.contains(link), "missing {link}");
        }
        assert!(!localized_root_html.contains("https://pkg.so/de/de/"));
        let package_html = String::from_utf8(package.body).expect("package html");
        assert_eq!(robots.content_type, "text/plain; charset=utf-8");
        assert_eq!(
            String::from_utf8(robots.body).expect("robots text"),
            "User-agent: *\nAllow: /\nSitemap: https://pkg.so/sitemap.xml\n"
        );
        assert_eq!(root_sitemap.content_type, "application/xml; charset=utf-8");
        assert_eq!(manifest.content_type, "application/json; charset=utf-8");
        assert!(
            String::from_utf8(manifest.body)
                .unwrap()
                .contains("source_file_count")
        );
        assert!(
            String::from_utf8(root_sitemap.body)
                .expect("root sitemap xml")
                .contains("https://pkg.so/sitemap-brew.xml")
        );
        assert!(
            package_html.contains(r#"<link rel="canonical" href="https://pkg.so/brew/awscli/">"#)
        );
        assert!(package_html.contains(r#"<meta property="og:site_name" content="pkg.so">"#));
        assert!(package_html.contains("open package index"));
        assert!(!package_html.contains("class=\"brand-mark\" src="));
        assert!(package_html.contains("AWS credential file coverage"));
        assert!(package_html.contains("brew install awscli"));
        assert!(package_html.contains("Risk classifier"));
        assert!(package_html.contains("aws iam create-access-key"));
        assert!(package_html.contains("Agent safety answer"));
        assert!(
            package_html
                .contains("<title>Install awscli with Homebrew, apt, winget, pip | pkg.so</title>")
        );
        assert!(package_html.contains(
            r#"<meta name="description" content="Install awscli with Homebrew, apt, winget, pip. AWS command line interface. View executables, metadata, and security notes."#
        ));
        assert!(
            package_html.contains(
                r#"<h1 id="pkg-title">Install awscli with Homebrew, apt, winget, pip</h1>"#
            )
        );
        assert!(!package_html.contains(r#"<h1 id="pkg-title">Install awscli with Automic Vault"#));
        assert!(package_html.contains("Use protected AWS credential helpers"));
        assert!(package_html.contains("Project history and usage"));
        assert!(package_html.contains("default package-manager install for cloud operators"));
        assert!(package_html.contains("2013: AWS CLI v1 public documentation"));
        assert!(package_html.contains("https://docs.aws.amazon.com/cli/latest/userguide/"));
        assert!(package_html.contains("aws-iam-authenticator"));
        assert!(package_html.contains("Upstream has a newer patch release."));
        assert!(package_html.contains("Secure AWS CLI credentials"));
        assert!(package_html.contains("Source database details"));
        assert!(package_html.contains("Homebrew formula API"));
        assert!(package_html.contains("Other package-manager records"));
        assert!(package_html.contains("Debian apt"));
        assert!(package_html.contains("Amazon.AWSCLI"));
        assert!(package_html.contains("123,456,789"));
        assert!(package_html.contains("https://www.googletagmanager.com/gtag/js?id=G-CMPY7XG4QH"));
        assert!(package_html.contains("gtag('config', 'G-CMPY7XG4QH')"));

        let cargo_package_html = String::from_utf8(cargo_package.body).expect("cargo package html");
        assert!(cargo_package_html.contains("Line-oriented search tool."));
        assert!(cargo_package_html.contains("cargo install ripgrep"));
        assert!(cargo_package_html.contains("crates.io DB dump"));
        assert!(cargo_package_html.contains("Project links"));
        assert!(cargo_package_html.contains("<li>rg</li>"));
        assert!(!cargo_package_html.contains("Project history and usage"));

        let localized_package_html =
            String::from_utf8(localized_package.body).expect("localized package html");
        assert!(localized_package_html.contains("<html lang=\"fr\">"));
        assert!(
            localized_package_html.contains(
                "<title>Installer awscli avec Homebrew, apt, winget, pip | pkg.so</title>"
            )
        );
        assert!(localized_package_html.contains(
            r#"<meta name="description" content="Installez awscli avec Homebrew, apt, winget, pip. Consultez les exécutables, métadonnées et notes de sécurité.""#
        ));
        assert!(localized_package_html.contains("Copier"));
        assert!(localized_package_html.contains("Copié"));
        assert!(localized_package_html.contains("vérifié · 100%"));
        assert!(localized_package_html.contains("Comportement d'installation"));
        assert!(localized_package_html.contains("Homebrew déclare un hook post-install"));
        assert!(localized_package_html.contains("risque élevé · confiance forte"));
        assert!(localized_package_html.contains("Réponse sur la sécurité des agents"));
        assert!(
            localized_package_html.contains("Autres enregistrements de gestionnaires de paquets")
        );
        assert!(localized_package_html.contains("Sécuriser les identifiants AWS CLI"));
        assert!(!localized_package_html.contains(">Copy</button>"));
        assert!(!localized_package_html.contains("Install behavior"));
        assert!(
            localized_package_html
                .contains("https://www.googletagmanager.com/gtag/js?id=G-CMPY7XG4QH")
        );
        assert!(localized_package_html.contains("gtag('config', 'G-CMPY7XG4QH')"));

        let hub_html = String::from_utf8(hub.body).expect("hub html");
        assert!(hub_html.contains("Cloud CLI"));
        assert!(hub_html.contains("Protected tools"));
        assert!(hub_html.contains("Related hubs"));
        assert!(hub_html.contains("Source control"));
        assert!(hub_html.contains(r#"type="text/markdown""#));
        assert!(hub_html.contains("/cloud/index.md"));

        assert_eq!(hub_markdown.content_type, "text/markdown; charset=utf-8");
        let hub_markdown = String::from_utf8(hub_markdown.body).expect("hub markdown");
        assert!(hub_markdown.contains("# Cloud"));
        assert!(hub_markdown.contains("Cloud tools"));
        assert!(hub_markdown.contains("[awscli](https://pkg.so/brew/awscli/)"));

        let crates_hub_html = String::from_utf8(crates_hub.body).expect("crates.io hub html");
        assert!(crates_hub_html.contains("crates.io command packages"));
        assert!(crates_hub_html.contains("ripgrep"));
        assert!(crates_hub_html.contains("Cargo"));
        assert!(crates_hub_html.contains("Matched crates.io Cargo package provider."));
        assert!(crates_hub_html.contains("/crates-cli-packages/index.md"));

        assert_eq!(
            crates_hub_markdown.content_type,
            "text/markdown; charset=utf-8"
        );
        let crates_hub_markdown =
            String::from_utf8(crates_hub_markdown.body).expect("crates.io hub markdown");
        assert!(crates_hub_markdown.contains("# crates.io command packages"));
        assert!(crates_hub_markdown.contains("[ripgrep](https://pkg.so/cargo/ripgrep/)"));

        let index_html = String::from_utf8(index.body).expect("index html");
        assert!(index_html.contains("data-locale=\"de\""));
        assert!(index_html.contains(r#"<link rel="canonical" href="https://pkg.so/de/">"#));
        assert!(index_html.contains("Risky tools"));
        assert!(index_html.contains("JavaScript"));
        assert!(index_html.contains("crates.io command packages"));

        let sitemap_index_xml = String::from_utf8(sitemap_index.body).expect("sitemap index xml");
        assert!(sitemap_index_xml.contains("/sitemap-brew.xml"));
        assert!(sitemap_index_xml.contains("/sitemap-cargo.xml"));
        let hub_sitemap_xml = String::from_utf8(hub_sitemap.body).expect("hub sitemap xml");
        assert!(hub_sitemap_xml.contains("<loc>https://pkg.so/</loc>"));
        assert!(hub_sitemap_xml.contains("<loc>https://pkg.so/feed/</loc>"));
        assert!(!hub_sitemap_xml.contains("/pkg/data/"));
        assert!(hub_sitemap_xml.contains("/risky-tools/"));
        assert!(hub_sitemap_xml.contains("/crates-cli-packages/"));
        assert!(
            String::from_utf8(sitemap.body)
                .expect("sitemap xml")
                .contains("hreflang=\"zh-Hans\"")
        );
        assert!(
            String::from_utf8(cargo_sitemap.body)
                .expect("cargo sitemap xml")
                .contains("/cargo/ripgrep/")
        );

        assert_eq!(package_json.content_type, "application/json; charset=utf-8");
        let package_json: Value = serde_json::from_slice(&package_json.body).expect("package json");
        assert_eq!(package_json["packageKey"], "brew:awscli");
        assert_eq!(package_json["category"], "developer-tools");
        assert_eq!(package_json["data"]["approvalGate"]["rule_count"], 2);

        assert_eq!(new_json.content_type, "application/json; charset=utf-8");
        let new_json: Value = serde_json::from_slice(&new_json.body).expect("new json");
        let new_packages = new_json.as_array().expect("new packages");
        assert_eq!(new_packages.len(), 2);
        assert_eq!(new_packages[0]["packageKey"], "brew:awscli");
        assert_eq!(new_packages[1]["data"]["aliases"][0], "rg");

        assert_eq!(feed_page.content_type, "text/html; charset=utf-8");
        let feed_html = String::from_utf8(feed_page.body).expect("feed page html");
        assert!(feed_html.contains(r#"<link rel="canonical" href="https://pkg.so/feed/">"#));
        assert!(feed_html.contains("/new.json"));
        assert!(feed_html.contains("/.manifest.json"));
        assert!(feed_html.contains("/search.json?q=awscli"));
        assert!(feed_html.contains("q, limit, and offset"));
        assert!(feed_html.contains("/brew/awscli.json"));
        assert!(feed_html.contains("/db.json"));
        assert!(feed_html.contains("free of charge"));
        assert!(feed_html.contains("rate-limit, block, or take other action"));
        assert!(
            dynamic_response_for_path(db.path(), "/pkg/data/")
                .expect("query")
                .is_none()
        );
        let localized_feed_html =
            String::from_utf8(localized_feed.body).expect("localized feed html");
        assert!(
            localized_feed_html
                .contains(r#"<link rel="canonical" href="https://pkg.so/de/feed/">"#)
        );
        assert!(localized_feed_html.contains(r#"href="/de/feed/">Feed</a>"#));
    }

    #[test]
    fn landing_hubs_are_sorted_by_package_count_within_each_group() {
        let hubs = vec![
            HubSummary {
                hub: HubRow {
                    path: "/pkg/small/".into(),
                    slug: "small".into(),
                    title: "Small".into(),
                    description: "Small hub".into(),
                    group: "topical".into(),
                },
                package_count: 2,
            },
            HubSummary {
                hub: HubRow {
                    path: "/pkg/large/".into(),
                    slug: "large".into(),
                    title: "Large".into(),
                    description: "Large hub".into(),
                    group: "topical".into(),
                },
                package_count: 20,
            },
        ];
        let html = hub_group_sections(&hubs, &LOCALES[0]);
        assert!(html.find(">Large</span>").unwrap() < html.find(">Small</span>").unwrap());
    }

    #[test]
    fn dynamic_package_markdown_covers_rich_metadata_and_noindex() {
        let db = test_database();
        let markdown = dynamic_response_for_path(db.path(), "/pkg/brew/awscli/index.md")
            .expect("query")
            .expect("markdown response");
        let private_html = dynamic_response_for_path(db.path(), "/pkg/npm/private-tool/")
            .expect("query")
            .expect("private response");

        let text = String::from_utf8(markdown.body).expect("markdown");
        assert!(text.starts_with("# Install awscli with Homebrew, apt, winget, pip"));
        assert!(text.contains("Additional install commands"));
        assert!(text.contains("## Agent safety answer"));
        assert!(text.contains("Use protected AWS credential helpers"));
        assert!(text.contains("## Project history and usage"));
        assert!(text.contains("### Adoption history"));
        assert!(text.contains("default package-manager install for cloud operators"));
        assert!(text.contains("- 2020: AWS CLI v2 becomes the default generation of the tool."));
        assert!(text.contains("AWS credential file coverage"));
        assert!(text.contains("Upstream latest detected"));
        assert!(text.contains("[Cloud](https://pkg.so/cloud/)"));
        assert!(text.contains("Source archive"));
        assert!(text.contains("Source Database Details"));
        assert!(text.contains("Other Package-Manager Records"));
        assert!(text.contains("Homebrew formula API"));
        assert!(text.contains("Debian apt - awscli - 2.0.0-1"));

        assert!(
            dynamic_response_for_path(db.path(), "/pkg/npm/private-tool/index.md")
                .expect("query")
                .is_none()
        );
        assert!(
            String::from_utf8(private_html.body)
                .expect("private html")
                .contains("noindex,follow")
        );
    }

    #[test]
    fn dynamic_package_fallback_renderers_cover_sparse_metadata() {
        let db = test_database();
        let connection = Connection::open(db.path()).expect("open fixture database");
        insert_package(
            &connection,
            PackageFixture {
                path: "/pkg/brew/sparse/",
                provider: "brew",
                slug: "sparse",
                package_key: "brew:sparse",
                name: "sparse",
                display_name: "sparse",
                summary: "",
                provider_label: "Homebrew",
                package_manager_url: "",
                install_command: "",
                native_install_command: "",
                version: "",
                category: "developer-tools",
                license: "",
                homepage: "",
                repository: "",
                rank: None,
                last_updated_at: "2026-06-04",
                indexable: true,
                data_json: serde_json::json!({"full": {
                    "aliases": [
                        {"label": "sparse-label"},
                        {"name": "sparse-name"},
                        {"target": "sparse-target"},
                        {"source": "sparse-source"},
                        {"package": "sparse-package"},
                        {"key": "sparse-key"},
                        7,
                        true
                    ],
                    "geiger": {
                        "level": "medium",
                        "confidence": "low",
                        "category": "network",
                        "reasons": [],
                        "signals": []
                    },
                    "installBehavior": {"postInstallDefined": false},
                    "bottle": {"available": false},
                    "dependencies": ["openssl@3"],
                    "configFileLocations": {
                        "unix": ["$XDG_CONFIG_HOME/sparse/config.yml", "~/.config/sparse/config.yml"],
                        "windows": "%AppData%\\Sparse\\config.yml"
                    },
                    "credentialsFileLocations": {
                        "unix": "~/.config/sparse/hosts.yml"
                    },
                    "popularity": {"downloads_per_30_days": 12345},
                    "extra": {
                        "pkgTaxonomy": {
                            "category": "developer-tools",
                            "tags": ["source-control", "cli", "testing", "automation", "rust", "extra"]
                        },
                        "registryInsights": {
                            "apiURL": "https://example.test/api",
                            "customMetric": 9876,
                            "enabled": true,
                            "emptyList": [],
                            "nested": {"inner_id": "value"},
                            "list": ["alpha", false, 12]
                        }
                    },
                    "externalMatches": [
                        {
                            "manager": "apk",
                            "packageName": "sparse",
                            "evidence": "Alpine package index",
                            "metadata": {
                                "description": "Sparse package in an external index.",
                                "homepage": "https://example.test/sparse"
                            }
                        },
                        {"metadata": {}}
                    ],
                    "packageHubs": [
                        {"slug": "", "label": "skip"},
                        {"slug": "cloud", "label": "Cloud"}
                    ],
                    "relatedPackages": [
                        {"provider": "brew", "name": "sparse", "label": "self", "rel": "domain_peer"},
                        {"provider": "", "name": "bad", "rel": "domain_peer"},
                        {"provider": "npm", "package": "left-pad", "label": "left pad", "rel": "domain_peer"},
                        {"provider": "brew", "target": "curl", "label": "curl", "rel": "other"}
                    ],
                    "alsoAvailableVia": [
                        {"provider": "pip", "name": "sparse", "label": "sparse-py"}
                    ],
                    "combinedYamlPath": "combined/sparse.yml",
                    "combinedYamlUrl": "https://github.com/mxcl/pkgdb/blob/main/combined/sparse.yml",
                    "sourceNotes": [42, true, {"label": "fixture note"}]
                }})
                .to_string(),
                search_text: "sparse brew fallback rendering",
            },
        );
        connection
            .execute(
                "INSERT INTO hub_packages(hub_slug, package_key, position, reason)
                 VALUES('cloud', 'brew:sparse', 2, '')",
                [],
            )
            .expect("insert sparse hub package");
        connection
            .execute(
                "INSERT INTO hubs(path, slug, title, description, group_name, data_json)
                 VALUES('/pkg/empty-related/', 'empty-related', 'Empty related', '', 'topical', '{}')",
                [],
            )
            .expect("insert empty related hub");
        connection
            .execute(
                "INSERT INTO hub_packages(hub_slug, package_key, position, reason)
                 VALUES('empty-related', 'brew:sparse', 1, '')",
                [],
            )
            .expect("insert empty related hub package");

        let sparse = package_by_provider_slug(&connection, "brew", "sparse")
            .expect("query sparse")
            .expect("sparse package");
        let mut manifest_with_counts = metadata_json(&connection, "manifest")
            .expect("read manifest")
            .expect("manifest");
        manifest_with_counts["package_count"] = serde_json::json!(99);
        manifest_with_counts["approval_gate_count"] = serde_json::json!(11);
        connection
            .execute(
                "UPDATE metadata SET value = ?1 WHERE key = 'manifest'",
                params![serde_json::to_string(&manifest_with_counts).unwrap()],
            )
            .expect("update manifest counts");
        assert!(
            render_index_page(&connection, &LOCALES[0])
                .expect("render counted index")
                .contains(">99<")
        );
        assert_eq!(metadata_json(&connection, "missing").unwrap(), None);
        assert!(
            hub_signal_condition("non_low_geiger")
                .unwrap()
                .contains("geiger")
        );
        assert_eq!(hub_signal_condition("unknown"), None);

        let html = render_package_page(&sparse, &LOCALES[0], "2026-06-07T00:00:00Z");
        assert!(!html.contains("Package manager source"));
        assert!(!html.contains("Homebrew metadata was not linked in local data."));
        assert!(html.contains("No classifier reasons were present."));
        assert!(html.contains("No classifier signals were present."));
        assert!(html.contains("No Homebrew post-install hook is recorded"));
        assert!(html.contains("No Homebrew bottle metadata was recorded."));
        assert!(html.contains("30d downloads"));
        assert!(html.contains("Configuration and credential file locations"));
        assert!(html.contains("source-control, cli, testing, automation, rust"));
        assert!(html.contains("$XDG_CONFIG_HOME/sparse/config.yml"));
        assert!(html.contains("%AppData%\\Sparse\\config.yml"));
        assert!(html.contains("Combined YAML source"));
        assert!(html.contains("https://github.com/mxcl/pkgdb/blob/main/combined/sparse.yml"));
        assert!(html.contains("12,345"));
        assert!(html.contains("Source database details"));
        assert!(html.contains("Custom Metric"));
        assert!(html.contains("Sparse package in an external index."));

        let mut cargo_like = serde_json_package(
            &sparse,
            serde_json::json!({
                "executablesDetailed": [
                    {"name": "sparse", "kind": "binary", "exposure": "cargo-installed executable"}
                ],
                "homepage": "",
                "upstreamDocs": "https://docs.rs/sparse"
            }),
        );
        cargo_like.provider = "cargo".to_string();
        cargo_like.provider_label = "Cargo".to_string();
        cargo_like.package_manager_url = "https://crates.io/crates/sparse".to_string();
        cargo_like.repository = "https://github.com/example/sparse".to_string();
        let cargo_like_overview = render_overview(&cargo_like, &LOCALES[0]);
        assert!(cargo_like_overview.contains("Project links"));
        assert!(cargo_like_overview.contains("https://github.com/example/sparse"));
        assert!(cargo_like_overview.contains("https://docs.rs/sparse"));
        assert!(cargo_like_overview.contains("https://crates.io/crates/sparse"));
        assert!(cargo_like_overview.contains("<li>sparse</li>"));
        assert!(!cargo_like_overview.contains("Not present in the local metadata."));

        let markdown = render_package_markdown(&sparse, &LOCALES[0], "2026-06-07T00:00:00Z");
        assert!(markdown.contains("Bottle: not available"));
        assert!(markdown.contains("Configuration files"));
        assert!(markdown.contains("Unix: $XDG_CONFIG_HOME/sparse/config.yml"));
        assert!(markdown.contains("Credential files"));
        assert!(markdown.contains(
            "[combined/sparse.yml](https://github.com/mxcl/pkgdb/blob/main/combined/sparse.yml)"
        ));
        assert!(markdown.contains("left pad"));
        assert!(markdown.contains("fixture note"));
        assert!(markdown.contains("apk - sparse"));

        let dynamic = dynamic_response_for_path(db.path(), "/fr/pkg/brew/sparse/")
            .expect("dynamic sparse query")
            .expect("dynamic sparse response");
        assert_eq!(dynamic.content_type, "text/html; charset=utf-8");
        assert!(
            String::from_utf8(dynamic.body)
                .expect("localized sparse html")
                .contains("sparse")
        );
        assert!(
            dynamic_response_for_path(db.path(), "/pkg/missing-hub/")
                .expect("missing hub query")
                .is_none()
        );

        let hub_row = hub_package_row(&sparse, "", &LOCALES[0]);
        assert!(hub_row.contains("Executable aliases include"));
        assert!(hub_row.contains("sparse-label"));
        let hub = hub_by_slug(&connection, "cloud")
            .expect("query cloud hub")
            .expect("cloud hub");
        let hub_markdown = render_hub_markdown(&connection, &hub, &LOCALES[0])
            .expect("render sparse hub markdown");
        assert!(hub_markdown.contains("Executable aliases include"));
        let related_hubs = related_hub_links(&connection, &hub, &LOCALES[0]).expect("related hubs");
        assert!(related_hubs.join("").contains("package graph"));
        let hub_schema = schema_for_hub(
            &hub,
            vec![&sparse],
            "Sparse hub description",
            "2026-06-07",
            &LOCALES[0],
        );
        assert_eq!(
            hub_schema["@graph"][1]["@type"],
            serde_json::Value::String("Organization".to_string())
        );

        let mut isotope_only = sparse.clone();
        isotope_only.summary.clear();
        isotope_only.install_command.clear();
        isotope_only.data.full = serde_json::json!({"isotope": {}});
        assert!(hero_sentence(&isotope_only).contains("secret handling"));
        let isotope_security = render_security(&isotope_only, &LOCALES[0]);
        assert!(isotope_security.contains("Protected-tool coverage"));
        assert!(isotope_security.contains("No caveats were listed"));

        let mut gate_only = sparse.clone();
        gate_only.summary.clear();
        gate_only.install_command.clear();
        gate_only.data.full = serde_json::json!({
            "approvalGate": {"rule_count": "7", "rules": [], "coverage_status": "draft"}
        });
        assert!(hero_sentence(&gate_only).contains("approval-gate metadata"));
        assert!(render_gate(&gate_only, &LOCALES[0]).contains("No rule descriptions"));
        let mut security_markdown = String::new();
        markdown_security_section(&mut security_markdown, &gate_only, &LOCALES[0]);
        assert!(security_markdown.contains("Approval gate rules"));

        let mut bare = sparse.clone();
        bare.summary.clear();
        bare.install_command.clear();
        bare.version.clear();
        bare.last_updated_at.clear();
        bare.data.full = serde_json::json!({});
        assert!(hero_sentence(&bare).contains("local pkg.so sources"));
        assert!(render_executables(&bare, &LOCALES[0]).contains("No executable data"));
        assert!(render_agent_safety_answer(&bare, &LOCALES[0]).is_empty());
        assert!(render_registry_insights(&bare, &LOCALES[0]).is_empty());
        assert!(render_external_package_manager_matches(&bare, &LOCALES[0]).is_empty());
        assert!(security_heading(&bare, &LOCALES[0]).contains("No protected-tool"));
        assert!(related_article("Empty", Vec::new()).is_empty());

        let mut summary_only = bare.clone();
        summary_only.summary = "Sparse summary without an install command".to_string();
        assert!(hero_sentence(&summary_only).contains("Package data for"));

        let mut version_mismatch = sparse.clone();
        version_mismatch.summary = "Sparse summary for version reconciliation.".to_string();
        version_mismatch.install_command = "sudo av install sparse".to_string();
        version_mismatch.version = "2.0.0".to_string();
        version_mismatch.data.full = serde_json::json!({
            "versionFreshness": {
                "packageManager": {"version": "2.1.0"}
            },
            "lastVerified": "2026-05-23"
        });
        let mismatch_html =
            render_package_page(&version_mismatch, &LOCALES[0], "2026-06-07T00:00:00Z");
        assert!(mismatch_html.contains("Version 2.1.0 via Homebrew; verified 2026-06-04."));
        assert!(!mismatch_html.contains("verified 2026-05-23"));
        assert!(mismatch_html.contains(r#""softwareVersion": "2.1.0""#));

        let mut dependency_fallback = sparse.clone();
        dependency_fallback.data.full = serde_json::json!({"dependencies": ["zlib"]});
        let dependency_links = related_links(
            &dependency_fallback,
            &LOCALES[0],
            "relatedPackages",
            4,
            false,
        );
        assert!(dependency_links.join("").contains("zlib"));

        assert_eq!(
            install_command_manager_label(&serde_json::json!({"manager": "apk"})),
            "Alpine Linux apk"
        );
        assert_eq!(
            install_command_manager_label(&serde_json::json!({"manager": "dnf"})),
            "Fedora dnf"
        );
        assert_eq!(
            install_command_manager_label(&serde_json::json!({"manager": "npm"})),
            "npm"
        );
        assert_eq!(heading_manager_label("debian"), "apt");
        assert_eq!(heading_manager_label("ubuntu"), "apt");
        assert_eq!(heading_manager_label("Debian apt"), "apt");
        assert_eq!(heading_manager_label("fedora"), "dnf");
        assert_eq!(heading_manager_label("openSUSE zypper"), "zypper");
        assert_eq!(
            install_command_manager_label(
                &serde_json::json!({"manager": "shell", "source": {"manager": "winget"}})
            ),
            "Windows Package Manager"
        );
        assert_eq!(
            native_command_provider("brew install --cask visual-studio-code"),
            Some("cask")
        );
        assert_eq!(native_command_provider("npm i sparse"), Some("npm"));
        assert_eq!(
            native_command_provider("python3 -m pip install sparse"),
            Some("pip")
        );
        assert_eq!(
            native_command_provider("cargo install ripgrep"),
            Some("cargo")
        );
        assert_eq!(native_command_provider("curl https://example.test"), None);
        assert_eq!(fmt_int(-12345), "-12,345");
        assert_eq!(short_text("superlong", 1), "…");
        assert_eq!(slugify("Example++"), "example");
        assert_eq!(html_hreflang_links("https://example.test/pkg/"), "");
        assert_eq!(metadata_value_html(&serde_json::Value::Null), "");
        assert_eq!(metadata_value_html(&serde_json::json!([])), "");
        assert_eq!(metadata_value_html(&serde_json::json!({})), "");
        assert_eq!(metadata_value_text(&serde_json::json!({"empty": ""})), "");
        assert!(metadata_value_present(&serde_json::json!(false)));
        assert_eq!(
            value_string(&serde_json::json!(false)),
            Some("false".to_string())
        );
        assert_eq!(
            value_i64_key(&serde_json::json!({"n": "42"}), "n"),
            Some(42)
        );
        assert_eq!(
            value_f64_key(&serde_json::json!({"n": "0.75"}), "n"),
            Some(0.75)
        );
        assert_eq!(parse_query("&&a=b").get("a").map(String::as_str), Some("b"));

        let mut empty_metadata = sparse.clone();
        empty_metadata.package_key.clear();
        empty_metadata.provider_label.clear();
        empty_metadata.last_updated_at.clear();
        empty_metadata.data.full = serde_json::json!({});
        assert!(
            render_install_metadata(&empty_metadata, &LOCALES[0])
                .contains("No resolver details were present.")
        );

        let mut npm_behavior = sparse.clone();
        npm_behavior.provider = "npm".to_string();
        npm_behavior.data.full = serde_json::json!({
            "installBehavior": {"postInstallDefined": true},
            "bottle": {"available": true}
        });
        assert!(
            render_install_behavior_signals(&npm_behavior, &LOCALES[0])
                .contains("npm package metadata declares a postinstall script.")
        );
        assert!(
            render_install_behavior_signals(
                &serde_json_package(&sparse, serde_json::json!({"installBehavior": {}})),
                &LOCALES[0]
            )
            .is_empty()
        );

        let mut executable_edges = sparse.clone();
        executable_edges.data.full = serde_json::json!({
            "executablesDetailed": [
                {"name": ""},
                {"name": "dup"},
                {"name": "dup"}
            ],
            "binaries": [
                {"target": "dup"},
                {"source": "binary-source"}
            ],
            "extra": {"stub_exclusions": ["alias-skip"]},
            "aliases": ["alias-skip", "dup"]
        });
        let executable_html = render_executables(&executable_edges, &LOCALES[0]);
        assert!(executable_html.contains("Automic Vault stub excluded"));
        assert!(executable_html.contains("binary-source"));

        let mut freshness_edges = sparse.clone();
        freshness_edges.data.full = serde_json::json!({
            "versionFreshness": {
                "packageManager": {"version": "1.0.0"},
                "siteData": {},
                "upstream": {}
            }
        });
        assert!(
            render_freshness(&freshness_edges, "", &LOCALES[0])
                .contains("No freshness warnings were generated.")
        );

        let empty_registry = serde_json_package(
            &sparse,
            serde_json::json!({"registryInsights": {"empty": [], "blank": ""}}),
        );
        assert!(render_registry_insights(&empty_registry, &LOCALES[0]).is_empty());

        let manager_card = external_package_match_card(
            &serde_json::json!({
                "displayName": "Chocolatey",
                "packageId": "Sparse.Tool",
                "confidence": "manual",
                "command": "choco install sparse",
                "matchedBy": "package_id",
                "source": {"sourceLabel": "Chocolatey", "sourceUrl": "https://community.chocolatey.org/packages/sparse"},
                "metadata": {
                    "version": "1.2.3",
                    "license": "MIT",
                    "dependencies": ["dep"],
                    "optionalDependencies": ["optional"],
                    "provides": ["sparse"],
                    "sourcePackage": "sparse-src"
                }
            }),
            &LOCALES[0],
        );
        assert!(manager_card.contains("choco install sparse"));
        assert!(manager_card.contains("Matched by: Package ID"));
        assert!(
            external_package_match_card(&serde_json::json!({"displayName": "Empty"}), &LOCALES[0])
                .contains("Empty")
        );

        assert!(markdown_agent_safety_missing_summary(&sparse).is_empty());
        assert!(
            render_agent_safety_answer(
                &serde_json_package(&sparse, serde_json::json!({"agentSafetyAnswer": {}})),
                &LOCALES[0]
            )
            .is_empty()
        );
        assert!(
            install_command_source_html(&serde_json::json!({"source": {}}), &LOCALES[0]).is_empty()
        );

        let command_edges = serde_json_package(
            &sparse,
            serde_json::json!({
                "dependencies": ["zlib"],
                "installCommands": [
                    {"platform": "portable", "manager": "Automic Vault", "command": "sudo av install sparse"},
                    {"platform": "macos", "manager": "Homebrew", "command": "", "confidence": 0.2},
                    {"platform": "linux", "manager": "apt", "command": "sudo apt install sparse"}
                ]
            }),
        );
        let command_edge_steps = schema_for_package(
            &command_edges,
            "Install sparse.",
            "2026-06-07",
            &LOCALES[0],
        )["@graph"][6]["step"]
            .as_array()
            .unwrap()
            .clone();
        assert_eq!(command_edge_steps.len(), 1);
        assert!(
            !command_edge_steps[0]["text"]
                .as_str()
                .unwrap()
                .contains("sudo av install")
        );
        let mut install_groups = String::new();
        markdown_install_groups(&mut install_groups, &command_edges, &LOCALES[0]);
        assert!(install_groups.contains("unknown confidence"));

        assert!(hub_package_reason(&gate_only, &LOCALES[0]).contains("approval-gate"));
        let geiger_reason = serde_json_package(
            &sparse,
            serde_json::json!({
                "geiger": {"reasons": ["This package reads credentials from many user paths and may mutate remote state when automation runs without review."]}
            }),
        );
        assert!(hub_package_reason(&geiger_reason, &LOCALES[0]).contains("reads credentials"));
        let mut summary_reason = bare.clone();
        summary_reason.summary = "Summary reason for a sparse package.".to_string();
        assert!(hub_package_reason(&summary_reason, &LOCALES[0]).contains("Summary reason"));
        assert!(
            hub_package_reason(&bare, &LOCALES[0])
                .contains("Matched package metadata for this hub.")
        );
        assert!(
            hub_package_row(&bare, "", &LOCALES[0])
                .contains("Matched package metadata for this hub.")
        );

        assert_eq!(sentence_text(""), "");
        assert_eq!(metadata_value_present(&serde_json::Value::Null), false);
        assert_eq!(metadata_value_present(&serde_json::json!("  ")), false);
        assert_eq!(
            metadata_number_text(&serde_json::Number::from(u64::MAX)),
            u64::MAX.to_string()
        );
        assert_eq!(
            alternate_install_sentence(&serde_json_package(
                &sparse,
                serde_json::json!({"installCommands": [
                    {"manager": "Automic Vault", "command": "sudo av install sparse"},
                    {"manager": "brew", "command": ""}
                ]})
            )),
            ""
        );
        let mut default_command_package = serde_json_package(&sparse, serde_json::json!({}));
        default_command_package.install_command = "sudo av install sparse".to_string();
        let default_command = install_command_entries(&default_command_package);
        assert_eq!(
            default_command[0]["command"],
            serde_json::json!("sudo av install sparse")
        );

        let behavior_items = markdown_install_behavior_items(
            &serde_json_package(
                &sparse,
                serde_json::json!({
                    "installBehavior": {
                        "postInstallDefined": true,
                        "service": "com.example.sparse",
                        "caveats": "Review shell completions.",
                        "lifecycleScripts": ["prepare"],
                        "pythonRequires": ">=3.12",
                        "requiresDistCount": 3
                    },
                    "bottle": {"available": true, "platforms": ["arm64_tahoe"]}
                }),
            ),
            &LOCALES[0],
        );
        assert!(behavior_items.join("\n").contains("com.example.sparse"));
        let freshness_items = markdown_freshness_items(
            &serde_json_package(
                &sparse,
                serde_json::json!({
                    "versionFreshness": {
                        "upstream": {
                            "repository": "https://example.test/sparse",
                            "latestVersion": "2.0.0",
                            "comparison": "behind"
                        },
                        "warnings": [
                            {"severity": "warning", "message": ""},
                            {"message": "Manual review needed."}
                        ]
                    }
                }),
            ),
            "not-a-date",
            &LOCALES[0],
        );
        assert!(freshness_items.join("\n").contains("Manual review needed."));

        let mut registry_markdown = String::new();
        markdown_registry_insights(&mut registry_markdown, &bare, &LOCALES[0]);
        assert!(registry_markdown.is_empty());
        markdown_registry_insights(&mut registry_markdown, &empty_registry, &LOCALES[0]);
        assert!(registry_markdown.is_empty());

        let mut external_markdown = String::new();
        markdown_external_package_manager_matches(&mut external_markdown, &bare, &LOCALES[0]);
        assert!(external_markdown.is_empty());
        let external_edges = serde_json_package(
            &sparse,
            serde_json::json!({
                "externalPackageManagerMatches": [
                    {},
                    {"displayName": "OnlyName"},
                    {"packageId": "NoDetails"}
                ]
            }),
        );
        markdown_external_package_manager_matches(
            &mut external_markdown,
            &external_edges,
            &LOCALES[0],
        );
        assert!(external_markdown.contains("OnlyName"));
        let external_command = serde_json_package(
            &sparse,
            serde_json::json!({
                "externalPackageManagerMatches": [
                    {"manager": "Debian apt", "command": "sudo apt install sparse-tools"}
                ]
            }),
        );
        assert_eq!(
            alternate_install_sentence(&external_command),
            "Also installable with Debian apt: sudo apt install sparse-tools."
        );

        let mut related_markdown = String::new();
        markdown_related(
            &mut related_markdown,
            &serde_json_package(
                &sparse,
                serde_json::json!({
                    "packageHubs": [{"slug": ""}, {"slug": "cloud"}],
                    "relatedPackages": [
                        {"provider": "", "name": "skip", "label": "skip"},
                        {"provider": "brew", "name": "curl", "label": "curl"}
                    ]
                }),
            ),
            &LOCALES[0],
        );
        assert!(related_markdown.contains("[curl]"));

        assert!(hub_cluster_block("Empty", &[], &LOCALES[0]).is_empty());
        assert!(hub_related_block("Empty", &[]).is_empty());
        assert!(
            core_security_guides(
                &serde_json_package(&sparse, serde_json::json!({"aliases": ["gh"]})),
                &LOCALES[0]
            )
            .join("")
            .contains("GitHub CLI token security")
        );
        assert_eq!(
            SearchResult {
                title: "Exact".to_string(),
                url: "/pkg/brew/exact/".to_string(),
                summary: String::new(),
                provider: "brew".to_string(),
                package_key: "brew:exact".to_string(),
                rank: None,
                search_text: "exact".to_string(),
            }
            .search_rank("exact"),
            Some(0)
        );
        let ranked = SearchResult {
            title: "Exact".to_string(),
            url: "/pkg/brew/exact-tool/".to_string(),
            summary: String::new(),
            provider: "brew".to_string(),
            package_key: "brew:exact-tool".to_string(),
            rank: Some(9),
            search_text: "body match text".to_string(),
        };
        assert_eq!(ranked.search_rank("exa"), Some(1));
        assert_eq!(ranked.search_rank("tool"), Some(2));
        assert_eq!(ranked.search_rank("body"), Some(3));
        assert_eq!(ranked.search_rank("absent"), None);
        assert_eq!(ranked.match_distance("exact"), 0);
        assert!(ranked.match_distance("match") < usize::MAX);
        assert_eq!(ranked.match_distance("absent"), usize::MAX);
    }

    #[test]
    fn missing_route_returns_none() {
        let db = test_database();

        assert!(
            dynamic_response_for_path(db.path(), "/pkg/sitemap-gem.xml")
                .expect("query")
                .is_none()
        );
    }

    #[test]
    fn search_prefers_exact_and_prefix_before_summary_matches() {
        let db = test_database();
        let results = search_documents(db.path(), "aws", "en", 0, 10).expect("search");

        assert_eq!(
            results
                .results
                .iter()
                .map(|result| result.title.as_str())
                .collect::<Vec<_>>(),
            vec!["awscli", "aws-cdk", "cloudflared"]
        );
    }

    #[test]
    fn search_json_uses_locale_limit_offset_and_query_decoding() {
        let db = test_database();
        let decoded = parse_query("q=aws+cli&limit=1&offset=0");
        let query = parse_query("q=aws&limit=1&offset=0");
        let empty = search_documents(db.path(), "   ", "en", 0, 10).expect("empty search");
        let de = search_response_json(db.path(), "/de/search.json", &parse_query("q=aws"))
            .expect("de search");
        let paged = search_response_json(db.path(), "/search.json", &query).expect("search");

        assert_eq!(decoded.get("q").map(String::as_str), Some("aws cli"));
        assert_eq!(empty.results, Vec::new());
        let de_page: Value = serde_json::from_slice(&de).expect("de page");
        assert_eq!(de_page["locale"], "de");
        assert_eq!(de_page["results"][0]["title"], "AWS CLI");
        let page: Value = serde_json::from_slice(&paged).expect("page");
        assert_eq!(page["query"], "aws");
        assert_eq!(page["results"].as_array().expect("results").len(), 1);
        assert_eq!(page["nextOffset"], 1);
    }

    #[test]
    fn request_handler_covers_health_redirect_search_head_and_errors() {
        let db = test_database();

        let health = handle_raw_request(
            db.path(),
            "GET /healthz HTTP/1.1\r\nHost: test\r\n\r\n",
            Some("secret"),
        );
        assert!(health.starts_with("HTTP/1.1 200 OK"));
        assert!(health.ends_with("ok\n"));

        let robots = handle_raw_request(
            db.path(),
            "GET /robots.txt HTTP/1.1\r\nHost: test\r\n\r\n",
            None,
        );
        assert!(robots.starts_with("HTTP/1.1 200 OK"));
        assert!(robots.contains("Sitemap: https://pkg.so/sitemap.xml"));

        let forbidden = handle_raw_request(
            db.path(),
            "GET /pkg/ HTTP/1.1\r\nHost: test\r\n\r\n",
            Some("secret"),
        );
        assert!(forbidden.starts_with("HTTP/1.1 403 Forbidden"));

        let redirected = handle_raw_request(
            db.path(),
            "GET /pkg?q=aws HTTP/1.1\r\nHost: test\r\nx-test-origin: secret\r\n\r\n",
            Some("secret"),
        );
        assert!(redirected.starts_with("HTTP/1.1 301 Moved Permanently"));
        assert!(redirected.contains("location: /?q=aws"));

        let search = handle_raw_request(
            db.path(),
            "GET /search.json?q=aws&limit=1 HTTP/1.1\r\nHost: test\r\n\r\n",
            None,
        );
        assert!(search.starts_with("HTTP/1.1 200 OK"));
        assert!(search.contains("application/json; charset=utf-8"));
        assert!(search.contains("\"nextOffset\":1"));

        fs::write(db.path().with_extension("json"), b"{\"entries\":{}}\n")
            .expect("write public db fixture");
        let public_db = handle_raw_request(
            db.path(),
            "GET /db.json HTTP/1.1\r\nHost: test\r\n\r\n",
            None,
        );
        assert!(public_db.starts_with("HTTP/1.1 200 OK"));
        assert!(public_db.contains("application/json; charset=utf-8"));
        assert!(public_db.ends_with("{\"entries\":{}}\n"));

        let head = handle_raw_request(
            db.path(),
            "HEAD /brew/awscli/ HTTP/1.1\r\nHost: test\r\n\r\n",
            None,
        );
        assert!(head.starts_with("HTTP/1.1 200 OK"));
        assert!(!head.contains("AWS credential file coverage"));

        let method = handle_raw_request(
            db.path(),
            "POST /brew/ HTTP/1.1\r\nHost: test\r\n\r\n",
            None,
        );
        assert!(method.starts_with("HTTP/1.1 405 Method Not Allowed"));

        let missing = handle_raw_request(
            db.path(),
            "GET /brew/nope/ HTTP/1.1\r\nHost: test\r\n\r\n",
            None,
        );
        assert!(missing.starts_with("HTTP/1.1 404 Not Found"));
    }

    #[test]
    fn conditional_requests_support_etags_dates_and_precedence() {
        let mut headers = BTreeMap::new();
        let etag = "\"current\"";
        let modified = "Tue, 02 Jun 2026 19:54:51 GMT";

        headers.insert(
            "if-none-match".to_string(),
            "\"old\", W/\"current\"".to_string(),
        );
        assert!(request_is_not_modified(&headers, etag, modified));
        headers.insert("if-none-match".to_string(), "*".to_string());
        assert!(request_is_not_modified(&headers, etag, modified));
        headers.insert("if-none-match".to_string(), "\"old\"".to_string());
        headers.insert(
            "if-modified-since".to_string(),
            "Wed, 03 Jun 2026 00:00:00 GMT".to_string(),
        );
        assert!(!request_is_not_modified(&headers, etag, modified));
        headers.remove("if-none-match");
        assert!(request_is_not_modified(&headers, etag, modified));
        headers.insert(
            "if-modified-since".to_string(),
            "Mon, 01 Jun 2026 00:00:00 GMT".to_string(),
        );
        assert!(!request_is_not_modified(&headers, etag, modified));
    }

    #[test]
    fn handler_returns_empty_304_for_pages_search_redirects_and_misses() {
        let db = test_database();
        for target in [
            "/brew/awscli/",
            "/search.json?q=aws&limit=1",
            "/pkg",
            "/brew/nope/",
        ] {
            let initial = handle_raw_request(
                db.path(),
                &format!("GET {target} HTTP/1.1\r\nHost: test\r\n\r\n"),
                None,
            );
            let etag = initial
                .lines()
                .find_map(|line| line.strip_prefix("etag: "))
                .expect("cacheable response etag");
            assert!(initial.contains("cache-control: public, max-age=300, s-maxage=300"));

            let conditional = handle_raw_request(
                db.path(),
                &format!("GET {target} HTTP/1.1\r\nHost: test\r\nIf-None-Match: {etag}\r\n\r\n"),
                None,
            );
            assert!(
                conditional.starts_with("HTTP/1.1 304 Not Modified"),
                "target {target}: {conditional}"
            );
            assert!(conditional.ends_with("\r\n\r\n"));
            assert!(!conditional.contains("content-length:"));
        }

        let head = handle_raw_request(
            db.path(),
            "HEAD /brew/awscli/ HTTP/1.1\r\nHost: test\r\nIf-None-Match: *\r\n\r\n",
            None,
        );
        assert!(head.starts_with("HTTP/1.1 304 Not Modified"));
        assert!(head.ends_with("\r\n\r\n"));
    }

    #[test]
    fn origin_token_rejects_missing_and_invalid_headers() {
        let headers = BTreeMap::new();
        assert!(!origin_request_authorized(
            "/pkg/",
            &headers,
            "x-test-origin",
            Some("secret")
        ));
        assert!(origin_request_authorized(
            "/healthz",
            &headers,
            "x-test-origin",
            Some("secret")
        ));

        let mut headers = BTreeMap::new();
        headers.insert("x-test-origin".to_string(), "wrong".to_string());
        assert!(!origin_request_authorized(
            "/pkg/",
            &headers,
            "x-test-origin",
            Some("secret")
        ));
        headers.insert("x-test-origin".to_string(), "secret".to_string());
        assert!(origin_request_authorized(
            "/pkg/",
            &headers,
            "x-test-origin",
            Some("secret")
        ));
    }
}
