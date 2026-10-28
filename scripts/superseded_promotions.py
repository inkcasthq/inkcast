# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The promotion PRs that a new one supersedes (issue #298), one number per line.

    gh pr list --state open --label deploy --json number,headRefName,author,labels |
      python3 scripts/superseded_promotions.py --environment staging --current promote/staging-<12 hex>

Each older promotion PR would deploy an older digest, so merging one rolls the environment back. Only the PRs
promote.yml itself opened qualify: the deploy label, the Actions bot as author, and a head of exactly
promote/<environment>-<12 hex>. A person's PR, another environment's, and the new PR itself are never listed.
Standard library only: it runs on the runner's own Python."""

import argparse
import json
import re
import sys

BOT = "app/github-actions"
LABEL = "deploy"


def superseded(prs: list[dict], environment: str, current: str) -> list[int]:
    head = re.compile(rf"promote/{re.escape(environment)}-[0-9a-f]{{12}}")
    return [
        pr["number"] for pr in prs
        if pr["author"]["login"] == BOT
        and any(label["name"] == LABEL for label in pr["labels"])
        and head.fullmatch(pr["headRefName"])
        and pr["headRefName"] != current
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", required=True, choices=["staging", "production"])
    parser.add_argument("--current", required=True, help="the head branch of the promotion PR just opened")
    args = parser.parse_args()
    for number in superseded(json.load(sys.stdin), args.environment, args.current):
        print(number)


if __name__ == "__main__":
    main()
