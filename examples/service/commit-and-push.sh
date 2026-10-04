#!/bin/sh
# Example --exec hook for `aicr watch`: commit imported edits and conflict files
# in the source repo, scan them for secrets, push. Copy it into your source repo.
set -e
cd "$(dirname "$0")"
git add -A rules skills hooks aicr.toml 2>/dev/null || true
git diff --cached --quiet && exit 0
git commit -q -m "import: local edits from $(hostname)"
gitleaks git --no-banner --redact --log-opts="@{u}..HEAD" . # no push if a secret is found
git push -q
