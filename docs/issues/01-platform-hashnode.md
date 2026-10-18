# Platform: Hashnode plug-in

labels: platform

Hashnode GraphQL API (`https://gql.hashnode.com`) with a personal access token.

- `verify`: `me { id username name }`
- destinations: the user's publications (Hashnode posts always belong to a publication)
- map subtitle → `subtitle`, canonical → `originalArticleURL`, cover → `coverImageOptions`, tags (Hashnode needs tag slugs/ids)
- drafts via `createDraft`; scheduled publishing stays in Inkcast

**Done when:** fake GraphQL API + tests; dry run shows tag mapping; docs table updated.
