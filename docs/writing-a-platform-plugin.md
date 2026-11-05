# Writing a platform plug-in

A platform is one Python class. Built-ins live in `src/inkcast/platforms/`. Third-party plug-ins ship as their own package and register an entry point. Inkcast discovers them at start-up, and a broken plug-in is logged and skipped rather than crashing the server.

## Steps for a new platform (e.g. Hashnode)

1. Create `inkcast_hashnode/__init__.py`:

```python
from inkcast.platforms import Account, CredentialField, Destination, Platform, PlatformError, Prepared, PublishResult


class HashnodePlatform(Platform):
    id = "hashnode"                          # used in tool calls and front matter blocks
    name = "Hashnode"                        # shown to people, and in a post's notes: see the rules below
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
        # from the front matter block `hashnode:`, as JSON carries them (a date is its ISO 8601 text).
        # Enforce the platform's limits here and add warnings.
        return Prepared(platform=self.id, title=fields["title"], payload={...}, preview={...},
                        destination=fields.get("destination"), warnings=[])

    async def publish(self, creds, account, prepared, destination) -> PublishResult:
        data = await self.request("POST", "", headers={...}, json={...})   # helper maps 429/5xx to retryable errors
        return PublishResult(url=..., remote_id=..., status="published")   # the platform's URL or id: see below

    # Optional:
    async def destinations(self, creds, account) -> list[Destination]: ...
    def not_connected_help(self, fields) -> str | None: ...
    capabilities = frozenset({"update", "find"})    # what the pipeline may ask of the plug-in, beyond publishing
    async def update(self, creds, account, remote_id, prepared, destination) -> PublishResult: ...
    async def find_existing(self, creds, account, prepared) -> PublishResult | None: ...   # see the rule below
```

2. Register it in the plug-in's `pyproject.toml`:

```toml
[project.entry-points."inkcast.platforms"]
hashnode = "inkcast_hashnode:HashnodePlatform"
```

3. `pip install` it next to Inkcast and restart. `inkcast_platforms` now lists it.

## Rules of thumb

- **`name` is 1 to 40 printable characters (`str.isprintable()`), without `;`.** Inkcast writes it into the notes on a post ("a second copy may be on Hashnode") and stores a post's notes as one text, joined with `"; "`. A `;` in the name would cut such a note in two, and Inkcast would no longer recognise it: an update would drop the note that the platform may have the post twice. Set it on the class, as above; a property is not a name. If `__init__` sets `self.name`, that name follows the rule too. A plug-in whose name breaks the rule is not loaded, and Inkcast logs an error naming its entry point or its id.
- **`prepare` must be pure and its output JSON-serialisable.** It runs for dry runs, and its result is stored for scheduled posts.
- **`fields` holds what JSON can carry:** text, numbers, true/false, null, and lists and mappings of those. A date in the front matter arrives as its ISO 8601 text; binary data, sets and keys that aren't text are refused before `prepare` runs. Check the shape of a custom key yourself (`str(fields.get("series"))`): a `prepare` that raises anything but `PlatformError` is reported as that platform's failure, and logged, never as a 500.
- **Be precise with retries.** Raise `PlatformError(..., retryable=True)` only for transient failures. `Platform.request()` already does this for 429, 5xx and network errors, and passes a 429's `Retry-After` as `retry_after` (seconds), which the scheduler honours up to its policy's largest delay. A plug-in that reads a 429 itself can do the same with `inkcast.libs.platforms.base.retry_after_seconds()`, which reads both forms of the header.
- **`publish` returns what the platform gave the post: its `url`, its `remote_id`, or both.** One is enough; with neither, the pipeline records the send as `unconfirmed`, never `published`: a 2xx that names nothing (a 204, a 202 with no body; `Platform.request()` returns `None` for an empty body) may have published the post, but nothing can find it again, so a person checks. The same holds for `find_existing`: a result naming neither is read as "can't tell". When the platform's answer tells you *less* than a 2xx normally would (it accepted the request for later processing, say), raise `PlatformError(..., uncertain=True)` yourself with the reason, rather than returning an empty result. **Upgrading a plug-in whose platform's documented success is an empty `202` or `204`:** since this rule, every such send ends `unconfirmed`; make `publish` fetch what the answer points at (its `Location` header, or a lookup of the post you just created) and return that URL or id.
- **`find_existing` returns only the post this payload created.** The pipeline calls it (`"find"` in `capabilities`) when a send was interrupted or answered without saying, and acts on the answer: a result with a `url` or a `remote_id` is recorded as the post's copy; `None` means the platform has nothing like it, and the post is sent again, so be sure. The title alone can mislead: an older post, a draft, or a first copy a person was told to remove can share it. So compare everything the platform's listing lets you compare with what `publish` sent (the body, a canonical URL, an id you wrote into the post, besides the title), and return the one post that agrees on all of it. Anything else is "can't tell": two posts that agree, or one that shares the title but not the content. Then return a `PublishResult` naming neither `url` nor `remote_id`, with the reason in `notes`: the pipeline leaves the post to a person (`unconfirmed`) and logs the reason. Take the listing's fields from the platform's documentation *and* its API's actual answer, and compare only what both give: DEV's `/articles/me/all` carries the body and the published flag beyond its documented schema, and not the creation time the schema documents (`src/inkcast/libs/platforms/devto.py`).
- **Never put the title in the body** unless the platform requires it (Medium does). The story body never includes it.
- **Word errors for the person reading Claude's reply.** Say what went wrong and how to fix it.
- **Test against a fake of the platform's API.** Copy the pattern in `tests/fakes.py`.

---
<!-- inkcast-nav -->
[Docs](../docs/README.md) · [Licensing](../LICENSING.md) · [Legal](../legal/README.md) · [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) · [Inkcast README](../README.md)
