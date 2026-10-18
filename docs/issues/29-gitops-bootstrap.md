# Delivery: GitOps bootstrap (Argo CD or Flux)

labels: deploy

Argo CD Application / Flux Kustomization for `deploy/kustomize/environments/{staging,production}`; promote.yml PRs are the only way digests change; production sync requires the merged, CODEOWNER-approved PR.
