# pkg.so project history design

This is an English content-page addition for package users. Preserve the existing catalog style and install behavior.

The authoritative runtime tokens remain in `crates/av-web/pkg.css`: Space Grotesk for interface/body type, IBM Plex Mono for utility text, the final dark catalog theme with green `--hot` accents and the existing border/grid language (later CSS declarations override the earlier base palette). No token values are duplicated or changed by this experiment.

History pages reuse `html_doc`, `site_nav`, `site_footer`, `render_history`, breadcrumbs, `pkg-hero`, `hero-actions`, `button`, `pkg-section`, `split-section` and `package-list`. The page’s job is to explain a project’s origins and adoption with evidence, then link back to existing install pages. Prominent install-page history links remain ordinary anchors.

Use one H1, native anchors, existing visible focus/hover styling and responsive breakpoints. No async widgets or new application workflow are introduced. Preserve document scrolling and existing reduced-motion behavior. The frozen history cohort is English only, with self-canonical routes and no false translated hreflang claims.

The English Discover blog at `/blog/` also reuses the catalog shell, tokens, and navigation. Illustrated article previews lead to readable, single-column Markdown stories with permanent date/slug URLs. The feed owns editorial copy and artwork; the blog follows the published v2 archive without generating or translating content. Blog routes are self-canonical with no translated hreflang claims.

The English catalog landing page features the newest published article immediately after its introduction, before search. Reuse the blog preview layout and omit the feature when the feed is empty or unavailable so catalog browsing remains available.

Articles link to their explicitly identified packages in a compact “Tools in this story” section below the introduction. Resolve canonical URLs from the catalog; missing or non-indexable packages link to their upstream homepage instead. English package HTML and Markdown pages link back to their three newest matching articles, after the overview on HTML pages. Match package IDs exactly, normalize Homebrew cask IDs, and omit backlinks when the feed is unavailable.

The English `/new-and-trending/` content page and homepage preview reuse the same shell and tokens. Package names lead; the signature detail is an adjacent evidence line showing activity, comparison, and reporting date. Lists remain server-rendered and usable without JavaScript. Separate trending from recently discovered; never label an initial import or catalog observation as an upstream launch. Empty and stale results explain the absence and link to the catalog. The page is self-canonical with no translated hreflang alternatives. Tap provenance is shown on English package pages; full identities survive in installation commands and stable routes.
