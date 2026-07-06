# stock-watchdog 完整執行藍圖（設計目標：任何模型可獨立完整執行）

> **給執行模型的指示**：本文件是自足藍圖。逐步驟執行，每步驟末尾有「驗收」——不通過不得進下一步。
> 進度記錄在專案根目錄 `STATUS.md`（每完成一步驟即更新），中斷後任何模型讀 STATUS.md 接續。
> 第 0 步：把本藍圖全文複製到 `~/claude-code/stock-watchdog/BLUEPRINT.md`。

## Context（為什麼做）

參考陳信宏《股市提款卡》：在財報公布前用最新每股淨值，提前推算將被「打入全額交割」（淨值<5 元）或「恢復普通交易」（連兩季 ≥5）、「停止信用交易」（淨值<10）的股票——公告會引發連續跌停或反彈，提前知道就有交易機會。資料源：goodinfo.tw 淨值排行＋證交所/櫃買中心官方名單與法規。

使用者決定：**靜態 HTML + GitHub Pages、手動執行更新、上市＋上櫃全部**。

## 本 session 已實證的事實（執行模型不要重試已否定的路）

1. **goodinfo.tw 擋非瀏覽器抓取**（WebFetch 回空內容，已實測）→ 只能 Playwright
2. **Playwright 必須指定系統 Chrome**：venv 沒裝瀏覽器 → `p.chromium.launch(executable_path='/usr/bin/google-chrome')`（此路徑本機已驗證存在且可用）
3. 反偵測參數（來自 `~/claude-code/playwright-tools/scraper.py`，已在生產使用）：
   `args=['--no-sandbox','--disable-blink-features=AutomationControlled']` + 桌面 UA
4. 可用 python 環境：`~/claude-code/ppt-skill-extractor/.venv`（有 playwright、PIL）；stock 相關依賴在 `~/stock-venv` 或自建 venv（建議專案自建：`python3 -m venv .venv && .venv/bin/pip install playwright pandas requests lxml`）
5. TWSE 法規知識庫直連條文 URL 會參數錯誤（已實測）→ 用 openapi / 官方公告頁 / law.moj.gov.tw
6. git push 後必須 `gh api repos/deankhho/<repo>/commits --jq '.[0].sha'` read-back 驗證（使用者憲法規則）
7. commit 訊息格式照本 repo 慣例，結尾加 Co-Authored-By（見既有 commits）

## 業務規則（寫死在 analyze.py 的常數區，附出處註解）

```
全額交割門檻   NET_VALUE_FULL_DELIVERY = 5.0   # 證交所營業細則第49條第1項第1款；櫃買中心業務規則第12條之1
恢復條件       連續 2 次財報淨值 >= 5.0（最近一次必須 >=5）
停止信用交易   NET_VALUE_NO_MARGIN = 10.0      # 有價證券得為融資融券標準第4條
財報截止日     Q1=5/15, Q2=8/14, Q3=11/14, 年報=3/31（一般公司；金控/銀行/保險另有規定，網站註明）
```
執行時任務：用 WebFetch 到 law.moj.gov.tw 與 twse-regulation.twse.com.tw 搜尋上述條文**確認現行版本**，把原文段落與網址寫進 `docs/rules.html`。若查不到就引用金管會/證交所新聞稿，並在頁面標註「條文連結待補」。

## 專案結構

```
~/claude-code/stock-watchdog/          # 新 git repo（public——Pages 免費層需公開；皆公開市場資料）
├── BLUEPRINT.md        # 本文件
├── STATUS.md           # 進度斷點（每步驟更新）
├── .venv/              # python3 -m venv
├── fetch_goodinfo.py   # S1
├── fetch_official.py   # S2
├── analyze.py          # S3
├── gen_site.py         # S4
├── update.sh           # S5 一鍵：S1→S2→S3→S4→commit→push→read-back
├── data/               # netvalue.json / official.json / report.json（進 git，作歷史紀錄）
└── docs/               # GitHub Pages 根：index.html + rules.html
```

## S1 fetch_goodinfo.py（Playwright 抓淨值排行）

目標網址（使用者提供，上市；上櫃把 MARKET_CAT 換成對應參數，執行時在頁面下拉選單確認參數值）：
```
https://goodinfo.tw/tw/StockList.asp?RPT_TIME=&MARKET_CAT=熱門排行&INDUSTRY_CAT=每股淨值最低@@每股淨值@@每股淨值最低
```
實作要點：
- Playwright 開頁 → `wait_until='networkidle'` → 表格在 `#tblStockList`（執行時 F12 確認 selector，若不同以實際為準——**先 print 頁面 title 與表格數確認有載到**）
- 逐頁抓（頁面有「下一頁」或下拉分頁），**抓到每股淨值 ≥ 12 元即停**（門檻 10 元留 buffer）
- 每頁間 `time.sleep(random.uniform(3,6))`；連續失敗 3 次中止並報錯，**不得用舊資料靜默頂替**
- 解析：`page.content()` → pandas `read_html`（goodinfo 表格多層表頭，取含「代號」「名稱」「每股淨值」的表）
- 輸出 `data/netvalue.json`：
```json
{"fetched_at":"ISO時間","market":"TWSE|TPEX 合併","rows":[
  {"code":"1234","name":"某某","price":3.21,"net_value":4.87,"market":"上市"}]}
```
**驗收**：`rows` 筆數 >50；淨值遞增；抽 3 檔人工開 goodinfo 個股頁核對淨值一致（誤差容許四捨五入）。被擋（空表/驗證頁）→ 改用 headless=False 重試一次；仍失敗 → 停下回報使用者，不硬繞。

## S2 fetch_official.py（官方名單）

依序嘗試（找到能用的就停，把最終用的 URL 寫進 STATUS.md）：
1. **openapi.twse.com.tw** swagger（https://openapi.twse.com.tw/）搜尋「變更交易」「全額交割」「融資融券」相關端點
2. 證交所公告頁：https://www.twse.com.tw/ 下「交易資訊」→ 變更交易方法證券、暫停融資融券
3. 櫃買中心 https://www.tpex.org.tw/ 對應頁（櫃買也有 openapi：https://www.tpex.org.tw/openapi/）
4. 都不行 → Playwright 抓公告頁表格（同 S1 模式）
- 注意：TWSE 舊 CSV 端點常是 **Big5 編碼**（`resp.content.decode('big5', errors='replace')`）
- 輸出 `data/official.json`：`{"full_delivery":[{code,name,market,since}], "no_margin":[...], "fetched_at":...}`
**驗收**：全額交割名單筆數與官網頁面人工比對一致；兩市場都有資料。

## S3 analyze.py（分級引擎，純本地）

讀 netvalue.json + official.json → `data/report.json`。五分級（互斥，依序判定）：
```
⚪ official   : code 在官方全額交割名單
🟢 recover    : 在官方名單 且 最新淨值 >= 5      （恢復候選）
   ↑注意：recover 優先於 official 判定（先判 recover）
🔴 predict_in : 不在名單 且 淨值 < 5             （預測打入）
🟠 edge       : 5 <= 淨值 < 6                    （危險邊緣）
🟡 margin_risk: 6 <= 淨值 < 10 或在停止信用名單   （信用警戒）
```
每檔加欄位：`gap = net_value - 5.0`（距門檻）、`days_to_report`（距下一個財報截止日，用今天日期算 5/15、8/14、11/14、3/31 中最近的未來日）、`goodinfo_url = https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={code}`。
**驗收**：任選官方名單一檔淨值 4.x → 應為 ⚪；名單外淨值 4.x → 🔴；名單內淨值 5.5 → 🟢。寫 3 個 assert 在 analyze.py 的 `--selftest`。

## S4 gen_site.py（靜態網站）

參考 `~/claude-code/ppt-skill-extractor/gen_template_gallery.py` 的寫法（單檔 HTML、vanilla JS、無外部依賴）：
- `docs/index.html`：五分級籤頁（含各級筆數）、表格（代號連 goodinfo、名稱、市場、股價、淨值、距門檻、財報倒數）、點欄位排序、搜尋框、頁首「資料時間：{fetched_at}」+ 財報倒數醒目提示
- `docs/rules.html`：法規依據（條文摘錄+官方連結+本站分級邏輯說明）
- RWD：手機單欄（使用者主要在手機看）
- 風格沿用深色儀表板（參考 saas_templates/tpl-0001_dashboard.html 的設計 token）
**驗收**：本機 chrome 開啟，五籤頁切換正常、排序正常、手機寬度（DevTools 375px）可讀。

## S5 update.sh

```bash
#!/bin/bash
set -e
cd "$(dirname "$0")"
.venv/bin/python fetch_goodinfo.py
.venv/bin/python fetch_official.py
.venv/bin/python analyze.py
.venv/bin/python gen_site.py
git add -A && git commit -m "data: 更新 $(date '+%Y-%m-%d %H:%M')" && git push
gh api repos/deankhho/stock-watchdog/commits --jq '.[0].sha[0:7]'   # read-back
```
使用者說「**更新股市預警**」＝執行此腳本。

## S6 部署與收尾

1. `gh repo create stock-watchdog --public --source . --push`
2. 開 Pages：`gh api -X POST repos/deankhho/stock-watchdog/pages -f 'source[branch]=main' -f 'source[path]=/docs'`（失敗則給使用者手動步驟：repo Settings → Pages → main /docs）
3. Pages URL：https://deankhho.github.io/stock-watchdog/ ——請使用者手機實測
4. 記憶：新增 `~/.claude/projects/-home-khho6-claude-code/memory/project_stock_watchdog.md`（含：一鍵指令、資料源 URL、已驗證的坑、下一步），MEMORY.md 加索引行；執行 `bash ~/.claude/hooks/sync-push.sh` 並 read-back
5. **另存一條 feedback 記憶**：使用者要求「額度受限時，工作藍圖必須寫到其他模型可獨立執行的顆粒度（含已否定路徑、精確指令、驗收標準、斷點檔）」——存 `feedback_blueprint_for_next_model.md`

## 斷點續作機制

- 每完成一個 S 步驟：更新 `STATUS.md`（格式：`- [x] S1 完成 2026-07-06 21:00，netvalue.json 132 筆`＋遇到的問題與解法）
- 中斷後：新 session 讀 BLUEPRINT.md + STATUS.md 即可從斷點接續，不需要本對話的 context

## 風險與退路

| 風險 | 退路 |
|---|---|
| goodinfo 封鎖加劇 | FinMind `TaiwanStockBalanceSheet`（`~/stock-analysis/data_fetcher.py` 的 `_finmind()` 直接複製），全市場逐檔慢但可行；只算官方名單±淨值<12 的股池 |
| TWSE openapi 無對應端點 | Playwright 抓公告頁（S1 同模式） |
| Pages 不想公開 repo | 退為本機 docs/index.html + 手機用 \\wsl.localhost 路徑（降級體驗） |

## S7 backtest.py — 近兩年歷史驗證（2026-07-06 使用者追加）

需求：抓股池（S1 全部 300 檔＋官方名單股）**近兩年（8 季）每季淨值與財報**，
對照規則推算「歷史上哪季應打入全額交割/停信用/恢復」，供使用者驗證規則有效性。
- 資料源：FinMind `TaiwanStockBalanceSheet`（權益總額÷股本×10=每股淨值），
  包裝器抄 `~/stock-analysis/data_fetcher.py` `_finmind()`；免 token 有速率限制，
  加 sleep 0.5s/檔與進度續跑（cache data/history/<code>.json）
- 輸出 `data/backtest.json`：每檔 8 季 [{quarter, net_value, hit_5, hit_10}]
- 網站加「歷史驗證」籤頁：每檔一列迷你時間線（8 季淨值，<5 紅點、<10 黃點），
  可與官方名單 since 日期對照
- 驗收：抽 2 檔已知全額交割股，其歷史淨值跌破 5 的季度 應早於/等於 官方列入時點

## S8 現況/處置欄位（進行中，2026-07-07 斷點）

需求：每檔顯示官方實際狀態（是否已全額交割/處置中/停止信用），解決「信用警戒只是推算、看不出現況」。
已實證的資料源（直接用，勿重探）：
- 處置股：TWSE `openapi /announcement/punish`（35筆，含 ReasonsOfDisposition/DispositionPeriod/DispositionMeasures）；
  TPEx `openapi /tpex_disposal_information`（41筆）
- 停止信用（上市）：`exchangeReport/MI_MARGN?response=json&selectType=ALL` → tables[1] 逐股表，
  末欄「註記」（樣本 'X '＝停止信用註記，正式實作前先抓官方註記代號說明表核對 X/O 等含義）
- 停止信用（上櫃）：`openapi /tpex_mainboard_margin_balance` 有 `Note` 欄（8444 綠河在表內，
  單純「在表內」不能當可信用代理——全板都在表內，**必須看 Note 欄**）
實作：fetch_official.py 加 punish+註記 → analyze 每檔 status flags → gen_site 加「現況」欄
（chips：⚪全額交割/⚠處置中(至X日)/🚫停信用/✓正常），五籤頁通用。

## S9 新財報 Email 通知（待實作，2026-07-07 使用者需求）

- 財報常在截止前 ~20 天陸續公布 → 現有每日 Actions 排程已覆蓋；截止前可手動加密
- **偵測到新增目標交出財報（new_reports_count>0）→ email 至 khho@nlma.gov.tw**
- 建議實作：GitHub Actions 步驟判斷 report.json 的 new_reports_count>0 →
  呼叫使用者既有 GAS webhook（GAS MailApp.sendEmail，token 存 Script Properties，
  沿用 LINE Bot 那支 GAS 加一個 doGet 分支）；或 Actions 直接用 SMTP secret。
  優先 GAS 路線（重用既有基礎設施、免新 secret）。
- 信件內容：新財報檔數、各檔 代號/名稱/季度/Δ淨值/門檻穿越警示、網站連結
