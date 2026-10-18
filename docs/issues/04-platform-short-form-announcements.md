# Platforms: short-form announcements (Bluesky, Mastodon, LinkedIn, X)

labels: platform

After a story is published, optionally post a short announcement with the story URL.

- new plug-in kind "announce" that runs after the long-form posts in the same group succeed (needs URLs)
- Bluesky (app password), Mastodon (OAuth per instance) first; LinkedIn and X need approved developer apps and paid/partner API access — investigate cost and approval before building

**Done when:** dependency ordering in the publisher + one working announce plug-in.
