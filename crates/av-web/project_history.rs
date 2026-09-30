//! A bounded, frozen project experiment. No rank selection or content generation at request time.
use super::*;
use std::sync::OnceLock;

fn cohort() -> &'static Value {
    static COHORT: OnceLock<Value> = OnceLock::new();
    COHORT.get_or_init(|| {
        let data: Value =
            serde_json::from_str(include_str!("../../data/project-history-cohort.json"))
                .expect("valid frozen project history cohort");
        assert_eq!(data["projects"].as_array().unwrap().len(), 25);
        data
    })
}

pub(super) fn enabled() -> bool {
    cohort()["enabled"].as_bool() == Some(true)
}

fn projects() -> &'static [Value] {
    cohort()["projects"].as_array().unwrap()
}

fn field<'a>(project: &'a Value, name: &str) -> &'a str {
    project[name].as_str().unwrap_or("")
}

fn repository_identity(repository: &str) -> String {
    repository
        .trim_end_matches('/')
        .trim_end_matches(".git")
        .to_ascii_lowercase()
}

fn project_for_package(package: &PackageRow) -> Option<&'static Value> {
    projects().iter().find(|project| {
        field(project, "status") == "ready"
            && (value_array(project, "package_keys")
                .iter()
                .any(|key| key.as_str() == Some(&package.package_key))
                || (!package.repository.is_empty()
                    && value_array(project, "repository_aliases")
                        .iter()
                        .any(|repo| {
                            repo.as_str().is_some_and(|repo| {
                                repository_identity(repo)
                                    == repository_identity(&package.repository)
                            })
                        })))
    })
}

fn source_package(connection: &Connection, project: &Value) -> Result<Option<PackageRow>, String> {
    let Some((provider, name)) = field(project, "source_key").split_once(':') else {
        return Err("invalid history source key".into());
    };
    // Use the existing package query and route stored in SQLite, including versioned slugs.
    let slug: Option<String> = connection
        .query_row(
            "SELECT slug FROM packages WHERE package_key = ?1",
            params![format!("{provider}:{name}")],
            |row| row.get(0),
        )
        .optional()
        .map_err(|err| format!("failed to find history source: {err}"))?;
    let package = if let Some(slug) = slug {
        package_by_provider_slug(connection, provider, &slug)?
    } else {
        None
    };
    let mut package = package.unwrap_or_else(|| PackageRow {
        path: String::new(),
        provider: provider.into(),
        slug: String::new(),
        package_key: field(project, "source_key").into(),
        name: field(project, "name").into(),
        display_name: field(project, "name").into(),
        summary: String::new(),
        provider_label: String::new(),
        package_manager_url: String::new(),
        install_command: String::new(),
        native_install_command: String::new(),
        version: String::new(),
        category: String::new(),
        license: field(project, "license").into(),
        homepage: field(project, "homepage").into(),
        repository: field(project, "repository").into(),
        rank: None,
        last_updated_at: String::new(),
        indexable: true,
        data: PackageData { full: json!({}) },
    });
    // Frozen project material is copied verbatim from existing curation, independently
    // of whether the catalog has an installable package page (e.g. ICU's library).
    let snapshot = &project["history_snapshot"];
    package.data.full["history"] = if field(project, "history_mode") == "legacy" {
        json!({"summary": [], "project-history": snapshot, "sources": value_array(project, "citations")})
    } else {
        snapshot.clone()
    };
    Ok(Some(package)
        .filter(|package| suitable_history(package, field(project, "history_mode") == "legacy")))
}

fn suitable_history(package: &PackageRow, legacy: bool) -> bool {
    let Some(history) = full_value(package, "history") else {
        return false;
    };
    legacy
        && !history_items(history, "project-history").is_empty()
        && !history_items(history, "sources").is_empty()
        || [
            "summary",
            "project-history",
            "adoption-history",
            "timeline",
            "usage",
            "package-nerd-significance",
            "related-projects",
            "sources",
        ]
        .iter()
        .all(|key| !history_items(history, key).is_empty())
}

pub(super) fn package_link(package: &PackageRow) -> String {
    if !enabled() {
        return String::new();
    }
    let Some(project) = project_for_package(package) else {
        return String::new();
    };
    format!(
        r#"<a class="button secondary" href="{}">The history of {}</a>"#,
        html_escape(field(project, "path")),
        html_escape(field(project, "name"))
    )
}

pub(super) fn is_history_path(path: &str) -> bool {
    projects()
        .iter()
        .any(|project| field(project, "path") == path)
}

pub(super) fn response(
    connection: &Connection,
    path: &str,
) -> Result<Option<(String, &'static str)>, String> {
    if !enabled() {
        return Ok(None);
    }
    if path == "/sitemap-history.xml" {
        let mut urls = Vec::new();
        for project in projects()
            .iter()
            .filter(|project| field(project, "status") == "ready")
        {
            if source_package(connection, project)?.is_some() {
                // No refresh timestamp masquerading as an Article publication/modification date.
                urls.push(format!(
                    "  <url><loc>{SITE_ORIGIN}{}</loc></url>\n",
                    html_escape(field(project, "path"))
                ));
            }
        }
        return Ok(Some((
            render_urlset(&urls),
            "application/xml; charset=utf-8",
        )));
    }
    let Some(project) = projects()
        .iter()
        .find(|project| field(project, "status") == "ready" && field(project, "path") == path)
    else {
        return Ok(None);
    };
    let Some(package) = source_package(connection, project)? else {
        return Ok(None);
    };
    Ok(Some((
        render_page(connection, project, &package)?,
        "text/html; charset=utf-8",
    )))
}

pub(super) fn index_links(connection: &Connection) -> Result<String, String> {
    if !enabled() {
        return Ok(String::new());
    }
    let mut links = String::new();
    for project in projects()
        .iter()
        .filter(|project| field(project, "status") == "ready")
    {
        if source_package(connection, project)?.is_some() {
            links.push_str(&format!(
                r#"<a class="package-row" href="{}"><span>The history of {}</span></a>"#,
                html_escape(field(project, "path")),
                html_escape(field(project, "name"))
            ));
        }
    }
    if links.is_empty() {
        return Ok(links);
    }
    Ok(format!(
        r#"<section class="pkg-section split-section" aria-labelledby="project-histories-title"><div><p class="section-kicker">project histories</p><h2 id="project-histories-title">The projects behind the packages</h2><p>Origins, adoption and the details that matter to package nerds, with sources.</p></div><div class="package-list">{links}</div></section>"#
    ))
}

fn render_page(
    connection: &Connection,
    project: &Value,
    package: &PackageRow,
) -> Result<String, String> {
    let locale = &LOCALES[0];
    let name = field(project, "name");
    let canonical = format!("{SITE_ORIGIN}{}", field(project, "path"));
    let history = full_value(package, "history").unwrap();
    let description = history_items(history, "summary")
        .first()
        .cloned()
        .or_else(|| history_items(history, "project-history").first().cloned())
        .unwrap_or_default();
    let mut software = json!({
        "@type": "SoftwareSourceCode", "@id": format!("{canonical}#project"),
        "name": name, "codeRepository": field(project, "repository"), "license": {"@type": "CreativeWork", "name": field(project, "license")}
    });
    if field(project, "repository").is_empty() {
        software.as_object_mut().unwrap().remove("codeRepository");
    }
    if field(project, "license").is_empty() {
        software.as_object_mut().unwrap().remove("license");
    }
    if !package.homepage.is_empty() {
        software["url"] = json!(package.homepage);
    }
    let schema = json!({
        "@context": "https://schema.org", "@graph": [software, {
            "@type": "Article", "@id": format!("{canonical}#article"), "url": canonical,
            "headline": format!("The history of {name}"), "name": field(project, "title"),
            "description": description, "inLanguage": "en", "mainEntityOfPage": canonical,
            "about": {"@id": format!("{canonical}#project")},
            "citation": value_array(project, "citations")
        }]
    });
    let schema_json = serde_json::to_string_pretty(&schema)
        .unwrap()
        .replace('<', "\\u003c");
    let mut body = site_nav(locale);
    let install_action = if package.path.is_empty() {
        String::new()
    } else {
        format!(
            r#"<a class="button primary" href="{}">Install {}</a>"#,
            html_escape(&package.path),
            html_escape(name)
        )
    };
    body.push_str(&format!(r##"<main><nav class="breadcrumbs" aria-label="Breadcrumbs"><a href="/">Home</a><span>/</span><span aria-current="page">History</span></nav><section class="pkg-hero" aria-labelledby="pkg-title"><div class="hero-copy"><p class="eyebrow">project history</p><h1 id="pkg-title">The history of {}</h1><p class="lede">{}</p><div class="hero-actions">{}<a class="button secondary" href="#history">Read the history</a></div></div></section>"##,
        html_escape(name), html_escape(&description), install_action));
    // Preserve all source descriptions; URL-only evidence also gets ordinary links.
    let mut history_view = package.clone();
    history_view.data.full["history"]["sources"] = json!([]);
    body.push_str(&render_history(&history_view, locale));
    body.push_str("<section class=\"pkg-section\" aria-labelledby=\"history-citations-title\"><h2 id=\"history-citations-title\">Sources</h2><ul>");
    let citations = value_array(project, "citations");
    let mut linked = std::collections::BTreeSet::new();
    for note in history_items(history, "sources") {
        if let Some(url) = citations
            .iter()
            .filter_map(|value| value.as_str())
            .find(|url| note.contains(url))
        {
            body.push_str(&format!(
                r#"<li><a href="{}">{}</a></li>"#,
                html_escape(url),
                html_escape(&note)
            ));
            linked.insert(url);
        } else {
            body.push_str(&format!("<li>{}</li>", html_escape(&note)));
        }
    }
    for url in citations.iter().filter_map(|value| value.as_str()) {
        if !linked.contains(url) {
            body.push_str(&history_list_item(url));
        }
    }
    body.push_str("</ul></section>");
    // Ordinary links to every evidenced ecosystem representation, without changing installs.
    let mut statement = connection
        .prepare("SELECT path, package_key, provider_label FROM packages ORDER BY path")
        .map_err(|err| format!("failed to prepare history package links: {err}"))?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|err| format!("failed to read history package links: {err}"))?;
    let mut links = String::new();
    for row in rows {
        let (path, key, label) = row.map_err(|err| err.to_string())?;
        if value_array(project, "package_keys")
            .iter()
            .any(|value| value.as_str() == Some(&key))
        {
            links.push_str(&format!(
                r#"<a class="package-row" href="{}"><span>{}</span><small>{}</small></a>"#,
                html_escape(&path),
                html_escape(&key),
                html_escape(&label)
            ));
        }
    }
    if !links.is_empty() {
        body.push_str(&format!(r#"<section class="pkg-section" aria-labelledby="history-packages-title"><h2 id="history-packages-title">Install {}</h2><div class="package-list">{links}</div></section>"#, html_escape(name)));
    }
    body.push_str("</main>");
    body.push_str(&site_footer(locale));
    Ok(html_doc(
        locale,
        field(project, "title"),
        &description,
        &canonical,
        "index,follow",
        "",
        &schema_json,
        &body,
        "",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frozen_cohort_is_bounded_and_unique() {
        let mut slugs = std::collections::BTreeSet::new();
        let mut keys = std::collections::BTreeSet::new();
        assert_eq!(projects().len(), 25);
        assert_eq!(
            projects()
                .iter()
                .filter(|p| field(p, "status") == "ready")
                .count(),
            25
        );
        for project in projects() {
            assert!(slugs.insert(field(project, "path")));
            assert!(field(project, "path").ends_with("/history/"));
            for key in value_array(project, "package_keys") {
                assert!(keys.insert(key.as_str().unwrap()));
            }
        }
        assert!(enabled());
        assert!(!is_history_path("/curl/history/"));
        assert_eq!(
            repository_identity("https://github.com/FFmpeg/FFmpeg.git/"),
            "https://github.com/ffmpeg/ffmpeg"
        );
    }
    #[test]
    fn existing_snapshot_renders_cited_project_material_and_routes() {
        let Ok(path) = std::env::var("PKG_HISTORY_TEST_DB") else {
            return;
        };
        let connection = open_database(Path::new(&path)).unwrap();
        let mut count = 0;
        for project in projects().iter().filter(|p| field(p, "status") == "ready") {
            let package = source_package(&connection, project)
                .unwrap()
                .expect("existing cited source");
            let html = render_page(&connection, project, &package).unwrap();
            let canonical = format!("{SITE_ORIGIN}{}", field(project, "path"));
            assert_eq!(html.matches("<h1 ").count(), 1);
            assert!(html.contains(&format!(
                "The history of {}",
                html_escape(field(project, "name"))
            )));
            assert!(html.contains(&format!(
                "<title>{}</title>",
                html_escape(field(project, "title"))
            )));
            assert!(html.contains(&format!("rel=\"canonical\" href=\"{canonical}\"")));
            assert!(!html.contains("hreflang="));
            let json_text = html
                .split("<script type=\"application/ld+json\">")
                .nth(1)
                .unwrap()
                .split("</script>")
                .next()
                .unwrap();
            let schema: Value = serde_json::from_str(json_text).unwrap();
            assert_eq!(schema["@graph"][1]["@type"], "Article");
            assert_eq!(schema["@graph"][0]["@type"], "SoftwareSourceCode");
            assert!(schema["@graph"][1].get("datePublished").is_none());
            assert!(schema["@graph"][1].get("author").is_none());
            let history = full_value(&package, "history").unwrap();
            for url in value_array(project, "citations")
                .iter()
                .filter_map(|value| value.as_str())
            {
                assert!(html.contains(&format!("href=\"{}\"", html_escape(&url))));
                assert!(
                    schema["@graph"][1]["citation"]
                        .as_array()
                        .unwrap()
                        .contains(&json!(url))
                );
            }
            for paragraph in history_items(history, "project-history") {
                assert!(html.contains(&html_escape(&paragraph)));
            }
            for key in value_array(project, "package_keys") {
                let mut representation = package.clone();
                representation.package_key = key.as_str().unwrap().to_string();
                assert_eq!(
                    field(project_for_package(&representation).unwrap(), "path"),
                    field(project, "path")
                );
            }
            for provider in PROVIDERS {
                let mut representation = package.clone();
                representation.provider = provider.to_string();
                representation.package_key = format!("{provider}:different-spelling");
                representation.repository = field(project, "repository").to_string();
                assert_eq!(
                    field(project_for_package(&representation).unwrap(), "path"),
                    field(project, "path")
                );
            }
            assert!(
                response(&connection, field(project, "path"))
                    .unwrap()
                    .is_some()
            );
            if let Ok(output) = std::env::var("PKG_HISTORY_PREVIEW_DIR") {
                fs::create_dir_all(&output).unwrap();
                fs::write(
                    Path::new(&output).join(format!("{}.html", field(project, "slug"))),
                    html,
                )
                .unwrap();
            }
            count += 1;
        }
        assert_eq!(count, 25);
        assert!(
            render_sitemap_index(&connection)
                .unwrap()
                .contains("sitemap-history.xml")
        );
        assert_eq!(
            index_links(&connection)
                .unwrap()
                .matches("class=\"package-row\"")
                .count(),
            25
        );
    }
}
