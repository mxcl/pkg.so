//! Web editions of the published Discover editorials, including the linked archive.
use super::*;
use pulldown_cmark::{Event, Parser, Tag, html};
use std::collections::HashSet;

fn field<'a>(article: &'a Value, key: &str) -> &'a str {
    article[key].as_str().unwrap_or("")
}

fn article_path(article: &Value) -> String {
    format!(
        "/blog/{}/",
        field(article, "id")
            .trim_start_matches("editorial:")
            .replace(':', "/")
    )
}

fn articles(root: &Path) -> Result<Vec<Value>, String> {
    let mut path = root.join("v2.json");
    let mut pages = HashSet::new();
    let mut ids = HashSet::new();
    let mut articles = Vec::new();
    loop {
        if !pages.insert(path.clone()) {
            return Err("cycle in Discover archive".into());
        }
        let raw = fs::read(&path).map_err(|err| format!("read {}: {err}", path.display()))?;
        let page: Value =
            serde_json::from_slice(&raw).map_err(|err| format!("invalid Discover page: {err}"))?;
        let content = page["content"]
            .as_array()
            .ok_or("missing Discover content")?;
        for item in content {
            if field(item, "type") == "editorial" && ids.insert(field(item, "id").to_string()) {
                articles.push(item.clone());
            }
        }
        let Some(next) = page["nextPageURL"].as_str() else {
            break;
        };
        // Legacy archive URLs name files in this release too; never fetch remote URLs.
        let name = next.rsplit('/').next().unwrap_or("");
        if !is_safe_feed_filename(name) || !name.ends_with(".json") {
            return Err("invalid Discover archive link".into());
        }
        path = fs::canonicalize(root.join("v2/pages").join(name))
            .map_err(|err| format!("missing Discover archive: {err}"))?;
        if !path.starts_with(root) {
            return Err("Discover archive escapes release".into());
        }
    }
    articles.sort_by(|a, b| {
        field(b, "publishedAt")
            .cmp(field(a, "publishedAt"))
            .then_with(|| field(a, "id").cmp(field(b, "id")))
    });
    Ok(articles)
}

pub(super) fn latest_feature(feed_root: &Path) -> String {
    let result = fs::canonicalize(feed_root)
        .map_err(|err| err.to_string())
        .and_then(|root| articles(&root));
    let articles = match result {
        Ok(articles) => articles,
        Err(err) => {
            eprintln!("blog feature unavailable: {err}");
            return String::new();
        }
    };
    let Some(article) = articles.first() else {
        return String::new();
    };
    format!(
        r#"<section class="pkg-section" aria-labelledby="latest-post-title"><article class="blog-preview blog-feature" lang="en">{}<div><p class="eyebrow">Latest from the blog · {}</p><h2 id="latest-post-title"><a href="{}">{}</a></h2><p>{}</p><a class="button secondary" href="{}">Read the story <span aria-hidden="true">→</span></a></div></article></section>"#,
        artwork(article, true),
        date(article),
        html_escape(&article_path(article)),
        html_escape(field(article, "title")),
        html_escape(field(article, "deck")),
        html_escape(&article_path(article))
    )
}

fn markdown(body: &str) -> String {
    let events = Parser::new(body).map(|event| match event {
        Event::Html(text) | Event::InlineHtml(text) => Event::Text(text),
        Event::Start(Tag::Link {
            link_type,
            dest_url,
            title,
            id,
        }) => {
            let dest_url = if dest_url.starts_with("https://") || dest_url.starts_with("http://") {
                dest_url
            } else {
                "".into()
            };
            Event::Start(Tag::Link {
                link_type,
                dest_url,
                title,
                id,
            })
        }
        Event::Start(Tag::Image {
            link_type,
            dest_url: _,
            title,
            id,
        }) => Event::Start(Tag::Image {
            link_type,
            dest_url: "".into(),
            title,
            id,
        }),
        other => other,
    });
    let mut output = String::new();
    html::push_html(&mut output, events);
    output
}

fn artwork(article: &Value, eager: bool) -> String {
    let art = &article["artwork"];
    let Some(relative) = field(art, "path").strip_prefix("feed/") else {
        return String::new();
    };
    if !relative.starts_with("assets/") || safe_discover_feed_path(relative).is_none() {
        return String::new();
    }
    format!(
        r#"<img class="blog-art" src="/discover/feed/{}" alt="{}" loading="{}" width="1536" height="1024">"#,
        html_escape(relative),
        html_escape(field(art, "alt")),
        if eager { "eager" } else { "lazy" }
    )
}

fn date(article: &Value) -> String {
    let published = field(article, "publishedAt");
    format!(
        r#"<time datetime="{}">{}</time>"#,
        html_escape(published),
        html_escape(published.split('T').next().unwrap_or(published))
    )
}

pub(super) fn response(feed_root: &Path, path: &str) -> Result<Option<StoredResponse>, String> {
    if !path.starts_with("/blog/") {
        return Ok(None);
    }
    let root = match fs::canonicalize(feed_root) {
        Ok(root) => root,
        Err(_) => return Ok(None),
    };
    let articles = articles(&root)?;
    let (content_type, body) = if path == "/blog/sitemap.xml" {
        let mut urls = vec![format!("<url><loc>{SITE_ORIGIN}/blog/</loc></url>\n")];
        for article in &articles {
            urls.push(format!(
                "<url><loc>{SITE_ORIGIN}{}</loc></url>\n",
                html_escape(&article_path(article))
            ));
        }
        ("application/xml; charset=utf-8", render_urlset(&urls))
    } else {
        let locale = &LOCALES[0];
        let mut body = site_nav(locale);
        body.push_str("<main class=\"blog\">");
        let (title, description, schema) = if path == "/blog/" {
            body.push_str("<header class=\"blog-heading\"><p class=\"eyebrow\">Discover</p><h1>The pkg.so blog</h1><p class=\"lede\">Stories about useful software, the people behind it, and how to put it to work.</p></header><div class=\"blog-index\">");
            for (index, article) in articles.iter().enumerate() {
                body.push_str(&format!(r#"<article class="blog-preview">{}<div><p class="eyebrow">{}</p><h2><a href="{}">{}</a></h2><p>{}</p></div></article>"#,
                    artwork(article, index == 0), date(article), html_escape(&article_path(article)), html_escape(field(article, "title")), html_escape(field(article, "deck"))));
            }
            if articles.is_empty() {
                body.push_str("<p>No stories published yet. <a href=\"/\">Browse the package catalog</a>.</p>");
            }
            body.push_str("</div>");
            (
                "The pkg.so blog".to_string(),
                "Stories about useful software and how to put it to work.".to_string(),
                json!({"@context":"https://schema.org", "@type":"Blog", "name":"The pkg.so blog", "url":format!("{SITE_ORIGIN}/blog/")}),
            )
        } else {
            let Some(article) = articles
                .iter()
                .find(|article| article_path(article) == path)
            else {
                return Ok(None);
            };
            let title = field(article, "title");
            let deck = field(article, "deck");
            body.push_str(&format!(r#"<article><header class="blog-heading"><a href="/blog/">← All stories</a><p class="eyebrow">{}</p><h1>{}</h1><p class="lede">{}</p></header>{}<div class="blog-prose">{}</div></article>"#,
                date(article), html_escape(title), html_escape(deck), artwork(article, true), markdown(field(article, "body"))));
            if let Some(sources) = article["sources"].as_array() {
                let mut links = String::new();
                for source in sources {
                    let url = field(source, "url");
                    if url.starts_with("https://") || url.starts_with("http://") {
                        links.push_str(&format!(
                            r#"<li><a href="{}">{}</a></li>"#,
                            html_escape(url),
                            html_escape(field(source, "title"))
                        ));
                    }
                }
                if !links.is_empty() {
                    body.push_str(&format!(
                        "<section class=\"blog-prose\"><h2>Sources</h2><ul>{links}</ul></section>"
                    ));
                }
            }
            (
                format!("{title} | pkg.so"),
                deck.to_string(),
                json!({"@context":"https://schema.org", "@type":"BlogPosting", "headline":title, "description":deck, "datePublished":field(article,"publishedAt"), "url":format!("{SITE_ORIGIN}{path}")}),
            )
        };
        body.push_str("</main>");
        body.push_str(&site_footer(locale));
        (
            "text/html; charset=utf-8",
            html_doc(
                locale,
                &title,
                &description,
                &format!("{SITE_ORIGIN}{path}"),
                "index,follow",
                "",
                &schema.to_string().replace('<', "\\u003c"),
                &body,
                "",
            ),
        )
    };
    let body = body.into_bytes();
    Ok(Some(StoredResponse {
        content_type: content_type.into(),
        etag: etag_for_bytes(&body),
        body,
        last_modified: fs::metadata(root.join("v2.json"))
            .and_then(|m| m.modified())
            .map(httpdate::fmt_http_date)
            .unwrap_or_else(|_| FALLBACK_LAST_MODIFIED.into()),
        cache_control: HTML_CACHE_CONTROL.into(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn article(id: &str, published: &str) -> Value {
        json!({"type":"editorial", "id":id, "publishedAt":published, "title":"Tools < worth knowing", "deck":"A useful discovery", "body":"## Details\n\n**Useful** and [documented](https://example.com).", "artwork":{"path":"feed/assets/cover.png", "alt":"An illustrated toolkit"}})
    }

    #[test]
    fn archive_renders_unique_articles_and_updates_with_publication() {
        let temp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temp.path()).unwrap();
        fs::create_dir_all(root.join("v2/pages")).unwrap();
        let latest = article("editorial:2026-10-12:new", "2026-10-12T00:00:00Z");
        let old = article("editorial:2026-07-23:old", "2026-07-23T00:00:00Z");
        fs::write(root.join("v2.json"), json!({"content":[latest], "nextPageURL":"https://mxcl.dev/package-manager-manager/feed/v2/pages/old.json"}).to_string()).unwrap();
        fs::write(
            root.join("v2/pages/old.json"),
            json!({"content":[latest, old], "nextPageURL":null}).to_string(),
        )
        .unwrap();
        let feature = latest_feature(&root);
        assert!(feature.contains("/blog/2026-10-12/new/"));
        assert!(!feature.contains("/blog/2026-07-23/old/"));
        assert!(feature.contains("Tools &lt; worth knowing"));
        let index = response(&root, "/blog/").unwrap().unwrap();
        let page = String::from_utf8(index.body.clone()).unwrap();
        assert_eq!(page.matches("class=\"blog-preview\"").count(), 2);
        assert!(
            page.find("/blog/2026-10-12/new/").unwrap()
                < page.find("/blog/2026-07-23/old/").unwrap()
        );
        assert!(!page.contains("hreflang"));
        let old_page = response(&root, "/blog/2026-07-23/old/").unwrap().unwrap();
        let old_html = String::from_utf8(old_page.body).unwrap();
        assert!(old_html.contains("<h2>Details</h2>"));
        assert!(old_html.contains("<strong>Useful</strong>"));
        assert!(old_html.contains("Tools &lt; worth knowing"));
        assert!(old_html.contains("/discover/feed/assets/cover.png"));
        let sitemap = response(&root, "/blog/sitemap.xml").unwrap().unwrap();
        assert!(
            String::from_utf8(sitemap.body)
                .unwrap()
                .contains("https://pkg.so/blog/2026-07-23/old/")
        );
        assert!(response(&root, "/blog/missing/").unwrap().is_none());
        assert!(response(&root, "/blog/../../v2.json").unwrap().is_none());
        fs::write(
            root.join("v2.json"),
            json!({"content":[], "nextPageURL":null}).to_string(),
        )
        .unwrap();
        let empty = response(&root, "/blog/").unwrap().unwrap();
        assert_ne!(index.etag, empty.etag);
        assert!(latest_feature(&root).is_empty());
        fs::write(root.join("v2.json"), "broken JSON").unwrap();
        assert!(latest_feature(&root).is_empty());
        assert!(latest_feature(&root.join("missing")).is_empty());
        assert!(
            String::from_utf8(empty.body)
                .unwrap()
                .contains("No stories published yet")
        );
    }

    #[test]
    fn markdown_cannot_inject_html_or_active_urls() {
        let rendered = markdown(
            "<script>alert(1)</script>\n\n[bad](javascript:alert%281%29) [good](https://example.com)\n\n![bad](https://tracker.example/pixel)",
        );
        assert!(!rendered.contains("<script>"));
        assert!(!rendered.contains("javascript:"));
        assert!(!rendered.contains("https://tracker.example"));
        assert!(rendered.contains("href=\"https://example.com\""));
    }

    #[test]
    fn broken_archive_is_not_silently_published() {
        let temp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temp.path()).unwrap();
        fs::create_dir_all(root.join("v2/pages")).unwrap();
        let cycle =
            json!({"content":[], "nextPageURL":"https://pkg.so/discover/feed/v2/pages/cycle.json"})
                .to_string();
        fs::write(root.join("v2.json"), &cycle).unwrap();
        fs::write(root.join("v2/pages/cycle.json"), &cycle).unwrap();
        assert!(articles(&root).unwrap_err().contains("cycle"));
        fs::remove_file(root.join("v2/pages/cycle.json")).unwrap();
        assert!(articles(&root).is_err());
    }
}
