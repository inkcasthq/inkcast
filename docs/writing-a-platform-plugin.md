# Writing a platform plug-in

A platform is one Python class. Built-ins live in `src/inkcast/platforms/`. Third-party plug-ins ship as their own package and register an entry point. Inkcast discovers them at start-up, and a broken plug-in is logged and skipped rather than crashing the server.

## Steps for a new platform (e.g. Hashnode)

1. Create `inkcast_hashnode/__init__.py`:

```python
from inkcast.platforms import Account, CredentialField, Destination, Platform, PlatformError, Prepared, PublishResult


class HashnodePlatform(Platform):
    id = "hashnode"                          # used in tool calls and front matter blocks
    name = "Hashnode"
    default_api_url = "https://gql.hashnode.com"
    destination_label = "publication"        # or None if there's only "your profile"
    credential_fields = [CredentialField(key="token", label="Personal access token",
                                         help="Hashnode → Settings → Developer → Generate new token.")]
    connect_help = "Paste a Hashnode personal access token."

    async def verify(self, creds) -> Account:
        # Call the platform with the credentials; raise PlatformError("…") if they don't work.
        ...

    def prepare(self, fields, body) -> Prepared:
        # PURE: no network. `fields` = story fields with this platform's overrides applied:
        # title, subtitle, tags, canonical_url, cover_image, draft, destination, + any custom keys
        # from the front matter block `hashnode:`. Enforce the platform's limits here and add warnings.
        return Prepared(platform=self.id, title=fields["title"], payload={...}, preview={...},
                        destination=fields.get("destination"), warnings=[])

    async def publish(self, creds, account, prepared, destination) -> PublishResult:
        data = await self.request("POST", "", headers={...}, json={...})   # helper maps 429/5xx to retryable errors
        return PublishResult(url=..., remote_id=..., status="published")

    # Optional:
    async def destinations(self, creds, account) -> list[Destination]: ...
    def not_connected_help(self, fields) -> str | None: ...
```

2. Register it in the plug-in's `pyproject.toml`:

```toml
[project.entry-points."inkcast.platforms"]
hashnode = "inkcast_hashnode:HashnodePlatform"
```

3. `pip install` it next to Inkcast and restart. `inkcast_platforms` now lists it.

## Rules of thumb

- **`prepare` must be pure and its output JSON-serialisable.** It runs for dry runs, and its result is stored for scheduled posts.
- **Be precise with retries.** Raise `PlatformError(..., retryable=True)` only for transient failures. `Platform.request()` already does this for 429, 5xx and network errors, and passes a 429's `Retry-After` as `retry_after` (seconds), which the scheduler honours up to its policy's largest delay. A plug-in that reads a 429 itself can do the same with `inkcast.libs.platforms.base.retry_after_seconds()`, which reads both forms of the header.
- **Never put the title in the body** unless the platform requires it (Medium does). The story body never includes it.
- **Word errors for the person reading Claude's reply.** Say what went wrong and how to fix it.
- **Test against a fake of the platform's API.** Copy the pattern in `tests/fakes.py`.

---
<!-- inkcast-nav -->
[Docs](../docs/README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)
