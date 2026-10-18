# Platform: WordPress plug-in

labels: platform

Self-hosted WordPress REST API (`/wp-json/wp/v2/posts`) with an application password; WordPress.com via its OAuth API later.

- credential fields: site URL, username, application password
- markdown → HTML conversion (WordPress stores HTML); tags/categories by name (create missing tags)
- canonical: only with an SEO plug-in field (Yoast/RankMath) — document, don't guess

**Done when:** fake WP API + tests, including tag creation and HTML conversion.
