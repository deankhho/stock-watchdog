#!/bin/bash
# 一鍵更新：抓資料 → 分級 → 回測 → 網站 → push（使用者說「更新股市預警」＝執行本腳本）
# --no-push：跑到 gen_site.py 就停，跳過 git add/commit/push/read-back
#   （供 E2E 在獨立 worktree 驗證用，仍會打真實外部 API、寫真實 data/ 與 docs/）
set -e
cd "$(dirname "$0")"
NO_PUSH=false
for arg in "$@"; do
  [ "$arg" = "--no-push" ] && NO_PUSH=true
done
.venv/bin/python fetch_goodinfo.py
.venv/bin/python fetch_official.py
.venv/bin/python analyze.py
# fetch_netvalue_history 與 backtest 互不依賴，順序不拘（各自獨立快取，見計畫第4節解耦設計）
.venv/bin/python fetch_netvalue_history.py
.venv/bin/python backtest.py          # 有快取，只補新季度
.venv/bin/python fetch_listing_dates.py   # 列入日期（有快取）
.venv/bin/python gen_site.py
if [ "$NO_PUSH" = true ]; then
  echo "--no-push：跳過 git add/commit/push"
  exit 0
fi
git add -A
git commit -m "data: 更新 $(date '+%Y-%m-%d %H:%M')" || { echo "無變更"; exit 0; }
git push
# read-back 驗證（憲法規則）
gh api repos/deankhho/stock-watchdog/commits --jq '.[0].sha[0:7] + " 已上遠端"'
