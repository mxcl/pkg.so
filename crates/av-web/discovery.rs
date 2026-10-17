//! Precomputed discovery shelves share the catalog's atomic SQLite publication.
use super::*;

pub(super) fn revalidate_daily(response: &mut StoredResponse) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let midnight =
        std::time::UNIX_EPOCH + std::time::Duration::from_secs(now.as_secs() / 86400 * 86400);
    let stored =
        httpdate::parse_http_date(&response.last_modified).unwrap_or(std::time::UNIX_EPOCH);
    if midnight > stored {
        response.last_modified = httpdate::fmt_http_date(midnight);
    }
}

pub(super) fn document(connection: &Connection) -> Result<Value, String> {
    let mut data = metadata_json(connection, "discovery")?
        .unwrap_or_else(|| json!({"schema": 1, "trending": [], "recentlyDiscovered": []}));
    // Stop advertising old rankings if scheduled refreshes have stopped.
    let today: String = connection
        .query_row("SELECT date('now')", [], |row| row.get(0))
        .map_err(|err| err.to_string())?;
    for (section, field, days) in [
        ("trending", "trend.endDate", 3),
        ("independentTaps", "trend.endDate", 3),
        ("recentlyDiscovered", "admittedAt", 30),
    ] {
        if let Some(items) = data[section].as_array_mut() {
            items.retain(|item| {
                let date = if field == "trend.endDate" {
                    &item["discovery"]["trend"]["endDate"]
                } else {
                    &item["discovery"][field]
                };
                let date = date.as_str().unwrap_or("");
                connection
                    .query_row(
                        "SELECT julianday(?1) - julianday(?2) BETWEEN 0 AND ?3",
                        params![today, date, days],
                        |row| row.get::<_, bool>(0),
                    )
                    .unwrap_or(false)
            });
        }
    }
    Ok(data)
}

fn reason(item: &Value, trending: bool) -> String {
    if !trending {
        return format!(
            "Discovered {}",
            item["discovery"]["admittedAt"].as_str().unwrap_or("")
        );
    }
    let trend = &item["discovery"]["trend"];
    let count = trend["installs30d"].as_i64().unwrap_or(0);
    let change = match trend["growthPercent"].as_f64() {
        Some(value) => format!("{value:.0}% higher daily activity"),
        None => "Growing from a small prior baseline".to_string(),
    };
    format!(
        "{} recorded installs in 30 days · {} · through {}",
        fmt_int(count),
        change,
        trend["endDate"].as_str().unwrap_or("")
    )
}

fn rows(items: Vec<&Value>, trending: bool, limit: usize) -> String {
    items.iter().take(limit).map(|item| {
        let tap = item["tap"].as_str().map(|tap| format!("Third-party tap: {tap}"))
            .unwrap_or_else(|| "Homebrew".to_string());
        format!(r#"<li class="discovery-row"><a href="{}"><strong>{}</strong></a><p>{}</p><p class="discovery-evidence">{}</p><small>{}</small></li>"#,
            html_escape(item["path"].as_str().unwrap_or("/")),
            html_escape(item["displayName"].as_str().unwrap_or("")),
            html_escape(item["summary"].as_str().unwrap_or("")),
            html_escape(&reason(item, trending)), html_escape(&tap))
    }).collect()
}

pub(super) fn feature(connection: &Connection) -> Result<String, String> {
    let data = document(connection)?;
    let trending = value_array(&data, "trending");
    let recent = value_array(&data, "recentlyDiscovered");
    let (items, is_trending) = if trending.is_empty() {
        (recent, false)
    } else {
        (trending, true)
    };
    if items.is_empty() {
        return Ok(String::new());
    }
    Ok(format!(
        r#"<section class="pkg-section" aria-labelledby="discovery-preview"><p class="section-kicker">package discovery</p><h2 id="discovery-preview"><a href="/new-and-trending/">New &amp; trending</a></h2><ol class="discovery-list">{}</ol><a href="/new-and-trending/">Explore new and trending packages →</a></section>"#,
        rows(items, is_trending, 3)
    ))
}

pub(super) fn page(connection: &Connection) -> Result<String, String> {
    let data = document(connection)?;
    let mut body = format!(
        r##"{}<main><section class="pkg-hero"><div class="hero-copy"><p class="eyebrow">package discovery</p><h1>New &amp; trending</h1><p class="lede">Tools gaining attention, and packages newly discovered in the catalog. Including independent Homebrew taps.</p><div class="hero-actions"><a href="#trending">Trending</a><a href="#taps">Independent taps</a><a href="#recent">Recently discovered</a><a href="#method">How we measure</a></div></div></section>"##,
        site_nav(&LOCALES[0])
    );
    if data["sourceHealth"]["reportErrors"]
        .as_object()
        .is_some_and(|errors| !errors.is_empty())
    {
        body.push_str("<p class=\"pkg-section\">Some Homebrew reports could not be refreshed. Entries are shown only while their source data remains current.</p>");
    }
    for (section, title, anchor, trending) in [
        ("trending", "Trending", "trending", true),
        ("independentTaps", "Independent taps", "taps", true),
        ("recentlyDiscovered", "Recently discovered", "recent", false),
    ] {
        let items = value_array(&data, section);
        body.push_str(&format!(r#"<section class="pkg-section" aria-labelledby="{anchor}"><h2 id="{anchor}">{title}</h2>"#));
        if items.is_empty() {
            body.push_str(if trending { "<p>No packages currently meet the activity and freshness thresholds. <a href=\"/\">Browse the catalog</a>.</p>" }
                          else { "<p>No new discoveries in the last 30 days. Check back as the catalog grows.</p>" });
        } else {
            body.push_str(&format!(
                "<ol class=\"discovery-list\">{}</ol>",
                rows(items, trending, 60)
            ));
        }
        body.push_str("</section>");
    }
    body.push_str(r#"<section class="pkg-section" aria-labelledby="method"><h2 id="method">How we measure</h2><p>Trending compares the latest 30 days of recorded Homebrew installs with the preceding 60 days. We require at least 30 installs, a meaningful increase, and fresh, aligned reports. Ranking balances growth with the size of the increase; small baselines are smoothed. Independent taps uses the same criteria within third-party taps.</p><p>Installation events are not unique users. Homebrew analytics cover participating installations, and package updates can affect activity. Newly discovered means new to this catalog, not necessarily newly released. We verify tap definitions before listing packages; unsupported definitions wait for resolution.</p><p><a href="https://formulae.brew.sh/analytics/">Homebrew analytics</a> · <a href="/new-and-trending.json">Download this list as JSON</a></p></section></main>"#);
    body.push_str(&site_footer(&LOCALES[0]));
    let schema = json!({"@context": "https://schema.org", "@type": "CollectionPage",
        "name": "New & trending packages", "url": "https://pkg.so/new-and-trending/"});
    Ok(html_doc(
        &LOCALES[0],
        "New & trending packages | pkg.so",
        "Discover growing Homebrew tools and newly indexed packages, including third-party taps, with source-backed installation instructions.",
        "https://pkg.so/new-and-trending/",
        "index,follow",
        "",
        &schema.to_string(),
        &body,
        "",
    ))
}

pub(super) fn package_evidence(package: &PackageRow) -> String {
    let Some(source) = package.data.full.get("tapSource").filter(|v| v.is_object()) else {
        return String::new();
    };
    let discovery = &package.data.full["discovery"];
    format!(
        r#"<section class="pkg-section"><h2>Homebrew tap</h2><p>This package is distributed through <strong>{}</strong>, a third-party tap.</p><p><a href="{}">View the verified package definition</a> · Last verified {}</p><p>Resolution status: {}. <a href="/new-and-trending/">Explore new and trending packages</a>.</p></section>"#,
        html_escape(source["tap"].as_str().unwrap_or("")),
        html_escape(source["url"].as_str().unwrap_or("")),
        html_escape(discovery["lastVerifiedAt"].as_str().unwrap_or("unknown")),
        html_escape(discovery["status"].as_str().unwrap_or("unknown"))
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn database(date: &str) -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection
            .execute_batch("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);")
            .unwrap();
        let item = json!({"path": "/cask/mattt-tap-imcp/", "displayName": "iMCP <script>",
            "summary": "Connect your apps", "tap": "mattt/tap", "discovery": {
                "admittedAt": date, "trend": {"endDate": date, "installs30d": 141, "growthPercent": 58.4}}});
        connection
            .execute(
                "INSERT INTO metadata VALUES ('discovery', ?1)",
                [
                    json!({"schema": 1, "trending": [item.clone()], "independentTaps": [item.clone()], "recentlyDiscovered": [item]})
                        .to_string(),
                ],
            )
            .unwrap();
        connection
    }

    #[test]
    fn discovery_is_server_rendered_escaped_and_english_only() {
        let connection = database("2000-01-01");
        let today: String = connection
            .query_row("SELECT date('now')", [], |row| row.get(0))
            .unwrap();
        let connection = database(&today);
        let html = page(&connection).unwrap();
        assert!(html.contains("iMCP &lt;script&gt;"));
        assert!(html.contains("141 recorded installs in 30 days"));
        assert!(html.contains("Third-party tap: mattt/tap"));
        assert!(html.contains("id=\"taps\""));
        assert!(html.contains("href=\"/cask/mattt-tap-imcp/\""));
        assert!(html.contains("https://pkg.so/new-and-trending/"));
        assert!(!html.contains("hreflang="));
        assert!(feature(&connection).unwrap().contains("iMCP"));
    }

    #[test]
    fn stale_snapshots_and_missing_metadata_have_honest_empty_states() {
        let connection = database("2000-01-01");
        assert!(
            document(&connection).unwrap()["trending"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert!(document(&connection).unwrap()["independentTaps"].as_array().unwrap().is_empty());
        assert!(feature(&connection).unwrap().is_empty());
        assert!(
            page(&connection)
                .unwrap()
                .contains("No packages currently meet")
        );
        connection.execute("DELETE FROM metadata", []).unwrap();
        assert!(page(&connection).unwrap().contains("No new discoveries"));
    }
}
