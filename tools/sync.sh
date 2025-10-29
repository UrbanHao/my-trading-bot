#!/usr/bin/env bash
set -euo pipefail

branch=$(git rev-parse --abbrev-ref HEAD)
echo "Current branch: $branch"
echo "== Git status =="
git status -s

# 若有 ws_client.py 等新檔未追蹤，自動加入
git add -A

# 若沒有變更就退出
if git diff --cached --quiet; then
  echo "No staged changes. Nothing to commit."
  exit 0
fi

msg="${1:-chore(sync): auto-sync working tree}"
git commit -m "$msg"
git push -u origin "$branch"
echo "✅ Pushed to origin/$branch"
