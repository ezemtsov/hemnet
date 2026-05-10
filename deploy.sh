#!/usr/bin/env bash
# Commit the freshly-rebuilt index.html and push to GitHub.
# GitHub Pages picks up the change in ~1 minute.

set -euo pipefail
cd "$(dirname "$0")"

git add index.html
if git diff --staged --quiet; then
  echo "✓ no changes to deploy"
  exit 0
fi
git commit -m "deploy: refresh map for $(date +%F)"
git push
echo "✓ pushed to origin/main — Pages will rebuild shortly"
