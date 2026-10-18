# Media: upload local images

labels: media

Relative images currently produce a warning. Let the MCP client upload images (base64) and rewrite links:

- Medium `POST /v1/images`; other platforms via Inkcast object storage with public URLs
- size/type limits, virus scanning hook

**Done when:** a post with `./img/x.png` publishes with working images on Medium and DEV.
