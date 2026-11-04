#!/usr/bin/env bash
# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
# tests/test_review_openfga_migrate.py (the review of PR #374) where Docker is: CI's kind job (issue #422). It pulls the
# two images the test reads from components/openfga/deployment.yaml (the migrate container's PostgreSQL and the image
# volume's OpenFGA), by digest, then runs the module with INKCAST_MIGRATE_HARNESS=required, so a harness that cannot
# run fails instead of skipping. Needs docker and the package installed (pyyaml).
set -euo pipefail
cd "$(dirname "$0")/../.."

images="$(PYTHONPATH=src:tests python3 -c 'from test_review_openfga_migrate import pinned_images; print(*pinned_images(), sep="\n")')"
for image in $images; do
  echo "pull $image"
  docker pull -q "$image"
done
INKCAST_MIGRATE_HARNESS=required PYTHONPATH=src:tests python3 -m unittest -v tests.test_review_openfga_migrate
