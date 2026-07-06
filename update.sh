#!/bin/bash
# 一鍵更新：抓資料 → 分級 → 回測 → 網站 → push（使用者說「更新股市預警」＝執行本腳本）
set -e
cd "$(dirname "$0")"
.venv/bin/python fetch_goodinfo.py
.venv/bin/python fetch_official.py
.venv/bin/python analyze.py
.venv/bin/python backtest.py          # 有快取，只補新季度
.venv/bin/python fetch_listing_dates.py   # 列入日期（有快取）
.venv/bin/python gen_site.py
git add -A
git commit -m "data: 更新 $(date '+%Y-%m-%d %H:%M')" || { echo "無變更"; exit 0; }
git push
# read-back 驗證（憲法規則）
gh api repos/deankhho/stock-watchdog/commits --jq '.[0].sha[0:7] + " 已上遠端"'
