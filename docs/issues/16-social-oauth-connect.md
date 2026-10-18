# Social epic: OAuth connect flow, token store and refresh

labels: epic:social,security

One-time connect link → Continue to <platform> → static callback `/connect/oauth/<platform>/callback` → immediate code exchange (X codes live 30 s) → encrypted TokenSet. Refresh job ahead of expiry (X 2 h, TikTok 24 h / 365 d, IG ~60 d); rotate on refresh; connection health `ok | needs_reauth | scope_missing`.

**Done when:** fakes for all three token endpoints; expiry and partial-scope paths tested; tokens never logged.

Part of the X / Instagram / TikTok epic: docs/design/social-platforms-epic.md
