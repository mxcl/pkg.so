# pkg.so project history design

This is an English content-page addition for package users. Preserve the existing catalog style and install behavior.

The authoritative runtime tokens remain in `crates/av-web/pkg.css`: Space Grotesk for interface/body type, IBM Plex Mono for utility text, the final dark catalog theme with green `--hot` accents and the existing border/grid language (later CSS declarations override the earlier base palette). No token values are duplicated or changed by this experiment.

History pages reuse `html_doc`, `site_nav`, `site_footer`, `render_history`, breadcrumbs, `pkg-hero`, `hero-actions`, `button`, `pkg-section`, `split-section` and `package-list`. The page’s job is to explain a project’s origins and adoption with evidence, then link back to existing install pages. Prominent install-page history links remain ordinary anchors.

Use one H1, native anchors, existing visible focus/hover styling and responsive breakpoints. No async widgets or new application workflow are introduced. Preserve document scrolling and existing reduced-motion behavior. The frozen history cohort is English only, with self-canonical routes and no false translated hreflang claims.

The English Discover blog at `/blog/` also reuses the catalog shell, tokens, and navigation. Illustrated article previews lead to readable, single-column Markdown stories with permanent date/slug URLs. The feed owns editorial copy and artwork; the blog follows the published v2 archive without generating or translating content. Blog routes are self-canonical with no translated hreflang claims.
