# Legal: ship full licence texts for vendored bundles

labels: legal,supply-chain

Apache-2.0 (swagger-ui-dist) requires redistributing its licence and any NOTICE file; MIT (redoc) requires its copyright and permission notice. Extend `scripts/vendor-assets.sh` to also extract `package/LICENSE` (and `package/NOTICE` if present) from each verified tarball into `src/inkcast/static/vendor/licenses/`, record them in ASSETS.lock, and assert their presence in `tests/test_vendored_assets.py`. Run `scripts/vendor-assets.sh --update` on a machine with npm registry access.
