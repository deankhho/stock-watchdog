#!/usr/bin/env python3
"""
gen_site.py — S4：靜態網站（docs/index.html + docs/rules.html）
單檔、vanilla JS、深色儀表板風、RWD（使用者主要手機看）。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import crossings

BASE = Path(__file__).parent
REPORT = BASE / "data" / "report.json"
BACKTEST = BASE / "data" / "backtest.json"
LISTING = BASE / "data" / "listing_dates.json"
NV_FILE = BASE / "data" / "netvalue.json"
NV_HISTORY_DIR = BASE / "data" / "netvalue_history"
NV_HISTORY_STATUS = BASE / "data" / "netvalue_history_status.json"
AUDIT_FILE = BASE / "data" / "audit.json"
SBL_FILE = BASE / "data" / "sbl.json"
WARRANTS_FILE = BASE / "data" / "warrants.json"
TRADING_CHANGES_FILE = BASE / "data" / "trading_changes.json"
DOCS = BASE / "docs"


def listing_line(code: str, market: str, listing: dict) -> str:
    """官方名單股的列入日期說明行（來源：歷史名單首次出現日）"""
    info = listing.get(code)
    if not info:
        return ""
    rule = "證交所營業細則第49條" if market == "上市" else "櫃買中心業務規則"
    if info["precision"] == "day" and info["since"]:
        return (f'<div class="ev">📅 依{rule}於 <b>{info["since"]}</b> 列為全額交割'
                f'（歷史名單首次出現日；實際公告事由請查官方公告）</div>')
    if info["precision"] == "before_window" and info["since"]:
        return (f'<div class="ev">📅 {info["since"]} 觀測窗起點前已列為全額交割'
                f'（列入超過兩年，依{rule}）</div>')
    return ""

GROUP_LABEL = {"predict_in": "🔴 預測打入", "recover": "🟢 恢復候選",
               "edge": "🟠 危險邊緣", "official": "⚪ 全額交割中",
               "dropped": "⬇️ 最近一季掉落", "margin_risk": "🟡 信用警戒",
               "watch": "🔵 觀察池"}

AUDIT_TIER_LABEL = {"clean": "無保留意見", "note": "保留意見（樣板性質常見，非個股特有警訊）",
                    "danger": "⚠️ 繼續經營疑慮／無法表示意見", "unknown_type": "查核類型格式未知"}
AUDIT_TIER_CLASS = {"clean": "g", "note": "", "danger": "r", "unknown_type": ""}
AUDIT_STATE_LABEL = {"not_filed": "尚未申報", "unknown_code": "查無資料", "fetch_error": "抓取失敗"}


def render_audit_block(code: str, audit: dict) -> str:
    """會計師查核意見展開區塊。🔴 逐檔判斷差集（發現：舊快照差集必須逐檔處理）：
    在 audit.json 的 rows 內 → 顯示（含是否過期，由前端 JS 依 fetched_at 當下算）；
    不在快照內 → 「本輪尚無查核資料」，不顯示任何 tier。"""
    if not audit or audit.get("state") == "empty":
        return '<div class="hist-none">會計師查核資料尚未取得</div>'
    row = (audit.get("rows") or {}).get(code)
    if not row:
        return '<div class="hist-none">本輪尚無查核資料</div>'
    if row.get("state") != "ok":
        label = AUDIT_STATE_LABEL.get(row["state"], row["state"])
        return f'<div class="hist-none">會計師查核：{label}</div>'

    tier = row.get("tier") or "unknown_type"
    cls = AUDIT_TIER_CLASS.get(tier, "")
    label = AUDIT_TIER_LABEL.get(tier, tier)
    fetched_at = row.get("fetched_at", "")
    firm = row.get("firm") or ""
    quarter = row.get("quarter") or ""
    extra_note = ('保留意見在一般上市股出現率約 50%（2026-08-13 抽樣 30 檔），非個股特有警訊。'
                  if tier == "note" else "")
    return (f'<div class="audit-block" data-audit-fetched-at="{fetched_at}">'
           f'<span class="audit-chip {cls}" data-audit-chip>{label}</span> '
           f'<span class="note" style="display:inline">{quarter}・{firm}・'
           f'<span data-audit-age></span></span>'
           f'<div class="ev dim">此為該公司最新可得的查核資訊，未必對應本次淨值轉折的那一季。'
           f'{extra_note}</div>'
           f'</div>')


def has_short_channel(code: str, sbl: dict, warrants: dict):
    """回傳 True(確認有放空管道)／False(確認沒有)／None(資料不足無法確認)。
    Phase B（2026-08-21，計畫見 ~/.claude/plans/deep-stargazing-tide.md）：edge/watch 過濾用。
    🔴 fail-open 契約：兩個資料源都要 state=="ok" 才能給確定結論；SBL 或 warrants 任一
    degraded/empty 都回 None，呼叫端把 None 當「保留不濾掉」處理——不可把「不確定」當
    「沒有」，否則單一 API 故障會讓整個頁籤消失（跟 render_short_channel_block() 展開列
    的三值邏輯是同一套設計語言，但那裡三個管道各自獨立顯示，這裡是給過濾用的單一布林結論，
    所以縮成三態不是三個獨立欄位）。"""
    if sbl.get("state") != "ok" or warrants.get("state") != "ok":
        return None
    sbl_avail = code in (sbl.get("codes") or [])
    warrant_avail = code in (warrants.get("put_codes") or [])
    return sbl_avail or warrant_avail


def filter_short_channel_tier(rows: list, sbl: dict, warrants: dict) -> tuple:
    """edge/watch 專用：只保留有放空管道的股票；has_short_channel() 回 None（資料降級，
    無法確認）一律保留（fail-open），不可濾掉。回傳 (kept_rows, filtered_count,
    fail_open_count)——filtered_count 是「兩個資料源都 ok、確認沒有管道」被濾掉的檔數，
    fail_open_count 是「資料降級、暫時保留沒濾」的檔數，兩者分開統計供頁籤標題顯示，
    使用者才看得出檔數變化是資料狀態波動還是真的股票變多/變少。"""
    kept, filtered_count, fail_open_count = [], 0, 0
    for r in rows:
        has = has_short_channel(r["code"], sbl, warrants)
        if has is None:
            fail_open_count += 1
            kept.append(r)
        elif has:
            kept.append(r)
        else:
            filtered_count += 1
    return kept, filtered_count, fail_open_count


def render_short_channel_block(code: str, status: dict, sbl: dict, warrants: dict) -> str:
    """放空管道揭露（發現 K／§8）：用於 predict_in／edge／watch 頁籤展開列（watch 於
    2026-08-21 Phase B 併入，因為 edge/watch 都套用 filter_short_channel_tier() 過濾，
    使用者需要在展開列看到判斷依據）。
    🔴 三個管道語意各自獨立，不可合併成單一結論：
      融資融券：用既有 status['credit']（analyze.stock_status() 已解析 O/X/! mark）——
      🔴 不可只判斷「代號有沒有在 margin_status 表內」：3259 這種「結構上在表內，
      但目前 mark=OX（停資停券）」的案例會被誤判成「具資格」，已修正
      （2026-08-16 使用者用真實個股發現此 bug）
      借券 SBL：不在清單 → ✗；在清單內只講「具資格」，不講「有券可借」（發現 K 的語意上限）
      認售權證：原計畫判定沒有逐檔端點、永遠「未知」；2026-08-16 使用者追問後找到
      TWSE t187ap37_L＋ISIN cp950 橋接（fetch_warrants.py），資料新鮮時可给定判斷；
      資料降級/未取得時才回退「未知」——此時三者皆✗才是真的不可達，資料齊全時
      三者皆✗是可能發生的真實狀態，不必迴避
    sbl／warrants 資料過期或降級時，一律回退成「未知」，不可顯示 ✓／具資格（見 §8 freshness 契約）。"""
    credit = (status or {}).get("credit", "")
    margin_ok = credit == "可信用交易"
    if credit == "可信用交易":
        margin_text = None
    elif credit == "非信用交易標的":
        margin_text = "✗ 非融資融券標的"
    elif credit:
        margin_text = f"✗ 結構上為信用交易標的，但現況「{credit}」不可用"
    else:
        margin_text = "融資融券狀態未知"

    sbl_state = sbl.get("state")
    sbl_fetched_at = sbl.get("fetched_at", "")
    sbl_avail = code in (sbl.get("codes") or [])
    sbl_degraded = sbl_state not in ("ok",)

    if sbl_degraded:
        sbl_text = "借券資格未知（資料降級，暫無法判斷）"
    elif sbl_avail:
        sbl_text = "具借券資格，實際可借量需洽券商"
    else:
        sbl_text = "✗ 不在借券標的清單"

    warrants_state = warrants.get("state")
    warrants_fetched_at = warrants.get("fetched_at", "")
    warrant_avail = code in (warrants.get("put_codes") or [])
    warrants_degraded = warrants_state not in ("ok",)

    if warrants_degraded:
        warrant_text = ("需自行查詢（資料未取得）"
                        '（<a href="https://www.twse.com.tw/zh/products/warrant/summary.html" '
                        'target="_blank" onclick="event.stopPropagation()">TWSE 權證專區</a>）')
    elif warrant_avail:
        warrant_text = "具有效認售權證，實際成交量／流動性需另查"
    else:
        warrant_text = "✗ 目前無有效認售權證"

    unknown_any = sbl_degraded or warrants_degraded
    all_neg = (not margin_ok) and (not sbl_degraded) and (not sbl_avail) \
             and (not warrants_degraded) and (not warrant_avail)
    partial_neg = (not margin_ok) and (not sbl_degraded) and (not sbl_avail) and unknown_any

    if all_neg:
        summary = "已知管道（融資融券／借券／認售權證）皆無可用放空工具"
    elif partial_neg:
        summary = "已知管道（融券／借券）皆無；認售權證資料未取得，需自行確認"
    else:
        summary = ""

    return (f'''<div class="short-block" data-sbl-fetched-at="{sbl_fetched_at}"
     data-sbl-state="{sbl_state or ''}" data-sbl-avail="{'1' if sbl_avail else '0'}"
     data-warrants-fetched-at="{warrants_fetched_at}" data-warrants-state="{warrants_state or ''}"
     data-warrants-avail="{'1' if warrant_avail else '0'}">
  <div class="ev">融資融券：{margin_text or '具融資融券資格（可信用交易）'}</div>
  <div class="ev" data-sbl-line>借券：{sbl_text}</div>
  <div class="ev" data-warrants-line>認售權證：{warrant_text}</div>
  {f'<div class="ev dim">{summary}</div>' if summary else ''}
</div>''')


def render_long_channel_block(code: str, status: dict, warrants: dict) -> str:
    """作多管道揭露：只用於 recover 頁籤展開列（跟放空區塊對稱，2026-08-17 使用者要求——
    了解股票脫離全額交割時，除了直接買股票，還有哪些工具可用）。
    🔴 融資的可用判斷跟放空區塊刻意不同：這裡只在意「融資買進」本身，
    credit=='停止融券'（只停放空用的融券）不影響融資買進，要算可用；
    放空區塊只認 credit=='可信用交易' 是它自己的既有邏輯，此處不動、不比照。
    認購權證：跟認售權證共用同一套資料／freshness 契約（fetch_warrants.py），
    降級/未取得時一律回退「未知」，不可誤顯示 ✓。"""
    credit = (status or {}).get("credit", "")
    margin_ok = credit in ("可信用交易", "停止融券")
    if margin_ok:
        margin_text = None
    elif credit == "非信用交易標的":
        margin_text = "✗ 非融資標的"
    elif credit:
        margin_text = f"✗ 結構上為信用交易標的，但現況「{credit}」不可用"
    else:
        margin_text = "融資狀態未知"

    warrants_state = warrants.get("state")
    warrants_fetched_at = warrants.get("fetched_at", "")
    call_avail = code in (warrants.get("call_codes") or [])
    warrants_degraded = warrants_state not in ("ok",)

    if warrants_degraded:
        call_text = ("需自行查詢（資料未取得）"
                     '（<a href="https://www.twse.com.tw/zh/products/warrant/summary.html" '
                     'target="_blank" onclick="event.stopPropagation()">TWSE 權證專區</a>）')
    elif call_avail:
        call_text = "具有效認購權證，實際成交量／流動性需另查"
    else:
        call_text = "✗ 目前無有效認購權證"

    all_neg = (not margin_ok) and (not warrants_degraded) and (not call_avail)
    summary = "已知管道（融資／認購權證）皆無可用作多工具" if all_neg else ""

    return (f'''<div class="long-block" data-call-fetched-at="{warrants_fetched_at}"
     data-call-state="{warrants_state or ''}" data-call-avail="{'1' if call_avail else '0'}">
  <div class="ev">融資：{margin_text or '具融資資格'}</div>
  <div class="ev" data-call-line>認購權證：{call_text}</div>
  {f'<div class="ev dim">{summary}</div>' if summary else ''}
</div>''')


CREDIT_ELIGIBILITY_LABEL = {
    "可": "🟢 無累積虧損（可信用交易資格）",
    "否": "🔴 有累積虧損（信用交易資格不符）",
    "未知": "❔ 資料不足，無法確認",
}


def render_credit_eligibility_block(credit_elig) -> str:
    """面額非10元股的信用交易資格（Phase 0，2026-08-20，《有價證券得為融資融券標準》
    第2/4條：面額非10元股門檻看「有無累積虧損」，不是淨值）。所有頁籤展開列共用（不像
    放空/作多管道只限特定頁籤）——這一欄跟淨值分級（危險邊緣/信用警戒/觀察池）是兩個獨立
    判斷軸，面額10元股兩者剛好重合，credit_elig 傳 None 時不渲染任何東西（不是這檔股票
    該回答的問題，不可悶掉顯示成「未知」或「否」）。"""
    if credit_elig is None:
        return ""
    label = CREDIT_ELIGIBILITY_LABEL.get(credit_elig, "❔ 資料不足，無法確認")
    return (f'<div class="audit-heading">信用交易資格（面額非10元股）</div>'
           f'<div class="ev">{label}</div>')


RECOVER_STATE_LABEL = {
    "eligible": "🟢 淨值條件已符合（估算）",
    "not_yet": "🟡 淨值條件尚未符合",
    "unknown": "❔ 資料不足，無法確認",
}


def render_recover_status_block(recover_status: dict) -> str:
    """恢復資格三態呈現，只用於 recover 頁籤展開列。
    🔴 不得直接顯示內部 state token（英文 eligible/not_yet/unknown）給使用者，
    一律轉成完整中文句子，避免使用者把內部技術狀態誤讀成官方認定結果。
    detail 一律顯示；eligible 狀態如果 detail 非空（代表帶精度但書），額外顯示
    ⚠️ 警示行，不能讓最需要看到的但書被藏起來。"""
    if not recover_status:
        return ""
    state = recover_status.get("state")
    detail = recover_status.get("detail", "")
    label = RECOVER_STATE_LABEL.get(state, RECOVER_STATE_LABEL["unknown"])
    lines = [f'<div class="ev">恢復資格：{label}</div>']
    if detail:
        cls = "warn" if state == "eligible" else "dim"
        prefix = "⚠️ " if state == "eligible" else ""
        lines.append(f'<div class="ev {cls}">{prefix}{detail}</div>')
    return f'<div class="recover-block">{"".join(lines)}</div>'


CROSS_STATE_LABEL = {
    "confirmed": ("🔴", "確認掉落"), "no_drop": ("🟢", "未掉落"),
    "unknown": ("❔", "資料不明"), "unreliable": ("⚠️", "資料不可靠"),
    "suspect": ("⚠️", "疑似異常"), "source_conflict": ("⚠️", "來源矛盾"),
}
CROSS_REASON_LABEL = {
    "fetch_failed": "抓取失敗", "budget_exhausted": "本輪未輪到",
    "quarter_mismatch": "兩來源季別不同步", "not_adjacent": "缺中間季度",
    "no_prev": "無前季資料",
}
CROSS_STATE_ORDER = {"confirmed": 0, "source_conflict": 1, "suspect": 2,
                     "unreliable": 3, "unknown": 4, "no_drop": 5}


def render_dropped_panel(cross: dict) -> tuple:
    """dropped 頁籤：回傳 (tab_btn_html, panel_html)。
    🔴 不依賴 backtest.json，cross 完全來自 crossings.detect_margin_drops()（第 4 節解耦）。
    表格逐列列出母體全部個股（不只 confirmed），data_state 標色顯示——
    讓「0 檔掉落」與「0 檔掉落但 N 檔資料不明」可以被使用者親眼分辨（發現 G/M）。"""
    counts, universe = cross["counts"], cross["universe"]
    rows_sorted = sorted(cross["rows"], key=lambda r: CROSS_STATE_ORDER.get(r["data_state"], 9))
    n_confirmed = counts.get("confirmed", 0)

    tab_btn = (f'<button class="tab" data-t="dropped">⬇️ 最近一季掉落'
              f'<span class="n">{n_confirmed}</span></button>')

    reason_bits = "、".join(f'{CROSS_REASON_LABEL.get(k, k)} {v} 檔'
                            for k, v in cross["unknown_reasons"].items())
    summary = (f'母體 {universe} 檔（現況淨值 &lt;10 全部個股，非官方名單聯集）・'
              f'確認掉落 {n_confirmed} 檔・未掉落 {counts.get("no_drop", 0)} 檔・'
              f'資料不明 {counts.get("unknown", 0)} 檔'
              + (f'（{reason_bits}）' if reason_bits else '') +
              f'・資料不可靠 {counts.get("unreliable", 0)} 檔・'
              f'疑似異常 {counts.get("suspect", 0)} 檔・'
              f'來源矛盾 {counts.get("source_conflict", 0)} 檔')

    trs = []
    for r in rows_sorted:
        icon, label = CROSS_STATE_LABEL.get(r["data_state"], ("", r["data_state"]))
        period = f'{r["from_q"]} → {r["to_q"]}' if r.get("from_q") else (r.get("to_q") or "-")
        reason = CROSS_REASON_LABEL.get(r.get("reason"), r.get("reason") or "")
        trail = r.get("trail") or []
        chips = "".join(
            f'<div class="q {"r" if t["net_value"] < 5 else "y" if t["net_value"] < 10 else "g"}'
            f'{" lowconf" if t["confidence"] == "extrapolated" else ""}">'
            f'<span>{t["quarter"]}</span>{t["net_value"]:.2f}</div>'
            for t in trail)
        detail = (f'<div class="tl">{chips}</div>'
                  '<div class="ev">虛線／淡色＝較舊季度，套用同一比例回推、未逐季驗證，'
                  '信心低於判定用的最新一對（見上方期間欄）</div>') if trail else \
                 '<div class="hist-none">無多季歷史資料</div>'
        trs.append(f"""<tr class="main" onclick="tog(this)">
  <td><a href="https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={r['code']}" target="_blank" onclick="event.stopPropagation()">{r['code']}</a></td>
  <td>{r['name']}</td><td>{r.get('market','')}</td>
  <td>{icon} {label}{('<span class=note>' + reason + '</span>') if reason else ''}</td>
  <td>{period} <span class="exp">▾</span></td>
  <td class="num">{fmt(r.get('prev_nv'))}</td>
  <td class="num">{fmt(r.get('cur_nv'))}</td>
</tr>
<tr class="detail"><td colspan="7">{detail}</td></tr>""")
    table = f"""<table><thead><tr><th>代號</th><th>名稱</th><th>市場</th><th>狀態</th>
    <th>期間</th><th>前季淨值</th><th>本季淨值</th></tr></thead>
  <tbody>{''.join(trs) or '<tr><td colspan=7 class=empty>（目前無）</td></tr>'}</tbody></table>"""

    panel = f"""<section class="panel" data-t="dropped">
  <p class="desc">前季淨值 ≥10、最新季 &lt;10——已跌破融資融券標準。各檔比較期間不同，逐列標示，
  點列展開可看多季淨值軌跡。依現有資料判定，不等於已驗證的財報事件；
  本清單依當前資料每輪重算，公司更正申報後可能異動。</p>
  <p class="desc" style="opacity:.8">{summary}</p>
  {table}
</section>"""
    return tab_btn, panel


def _short_company_name(name: str) -> str:
    """「虹光精密工業股份有限公司」→「虹光精密工業」，跟站內其他地方的短名慣例一致。"""
    return name[:-6] if name.endswith("股份有限公司") else name


def render_stock_refs_chips(stocks: list) -> str:
    """變更交易公告頁籤用：把 content 裡抓出的股號/股名/動作做成醒目 chip，
    不必逐字讀法規公文才知道是哪幾檔股票（2026-08-18 使用者要求）。"""
    if not stocks:
        return ""
    chips = []
    for s in stocks:
        label = f'{s["code"]} {_short_company_name(s["name"])}'
        if s.get("action"):
            label += f' · {s["action"]}'
        chips.append(f'<span class="chip cstock">{label}</span>')
    return f'<div class="tc-stocks">{"".join(chips)}</div>'


def render_recover_announcement_block(code: str, trading_changes: dict) -> str:
    """把官方「變更交易方法」公告連結回 recover 頁籤展開列（2026-08-18 使用者要求）——
    使用者能直接對照本站 recover_eligibility() 的估算跟官方是否真的已經公告恢復，
    不必自己去對照兩個頁籤。trading_changes 缺檔/降級或查無提及這個代號的公告 →
    一律回空字串，不特別顯示「查無資料」這種容易被誤讀成警訊的文字。"""
    if not trading_changes:
        return ""
    lines = []
    for m in trading_changes.get("matched") or []:
        for ref in m.get("stocks") or []:
            if ref["code"] != code:
                continue
            action = ref.get("action") or "變更交易方法"
            lines.append(f'<div class="ev">📋 {m.get("announce_date","")} 官方公告：{action}</div>')
    if not lines:
        return ""
    return f'<div class="recover-announce">{"".join(lines)}</div>'


def render_trading_changes_panel(tc: dict) -> tuple:
    """trading_changes 頁籤：回傳 (tab_btn_html, panel_html)。
    資料來自 fetch_trading_changes.py 每日累積的 data/trading_changes.json，
    不經 report.json 的 groups（跟 dropped 頁籤同一種特例處理）。"""
    matched = tc.get("matched") or []
    state = tc.get("state")
    fetched_at = tc.get("fetched_at", "")

    tab_btn = (f'<button class="tab" data-t="trading_changes">📋 變更交易公告'
              f'<span class="n">{len(matched)}</span></button>')

    items_sorted = sorted(matched, key=lambda m: (m.get("filed_date", ""), m.get("filed_time", "")),
                          reverse=True)
    cards = []
    for m in items_sorted:
        content_html = m.get("content", "").replace("\n", "<br>")
        cards.append(f"""<div class="tc-item">
  <div class="tc-meta">{m.get('announce_date','')} 公告・{m.get('category','')}・{m.get('department','')}</div>
  {render_stock_refs_chips(m.get("stocks") or [])}
  <div class="tc-content">{content_html}</div>
</div>""")
    body = "".join(cards) or '<div class="empty">（累積 0 則，每日自動檢查中，出現符合條件的公告才會列出）</div>'

    staleness = '' if state == 'ok' else '（🔴 資料降級，沿用上次成功檢查的累積紀錄）'
    panel = f"""<section class="panel" data-t="trading_changes">
  <p class="desc">每日自動檢查證交所公告系統（本日公告），逐日累積本季「變更交易方法／信用交易」
  相關公告（已排除處置股／注意股）。⚠️ 用關鍵字比對輔助判斷，非官方逐檔 API，
  正式事由與細節請以<a href="https://mops.twse.com.tw/" target="_blank">公開資訊觀測站</a>公告全文為準。
  最後檢查：{fetched_at}{staleness}</p>
  {body}
</section>"""
    return tab_btn, panel


def gen_backtest_page():
    """docs/backtest.html — 近兩年每季淨值時間線（<5 紅、<10 黃、其餘綠）"""
    if not BACKTEST.exists():
        return False
    bt = json.loads(BACKTEST.read_text())
    rows = []
    stocks = sorted(bt["stocks"].items(),
                    key=lambda kv: (kv[1]["group"], kv[1]["current_nv"] or 0))
    for code, s in stocks:
        cells = "".join(
            f'<div class="q {"r" if h["hit5"] else "y" if h["hit10"] else "g"}" '
            f'title="{h["quarter"]} 淨值 {h["net_value"]}">'
            f'<span>{h["quarter"]}</span>{h["net_value"]:.2f}</div>'
            for h in s["history"])
        evs = "".join(f'<div class="ev">📌 {e["text"]}</div>'
                      for e in s.get("events", []))
        rows.append(f"""<div class="row">
  <div class="head"><a href="https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={code}"
    target="_blank">{code}</a> {s['name']} <span class="mk">{s.get('market','')}</span>
  <span class="grp">{GROUP_LABEL.get(s['group'], s['group'])}</span></div>
  <div class="tl">{cells or '（無資料）'}</div>
  {evs or '<div class="ev dim">近兩年未觸發門檻事件</div>'}</div>""")
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>歷史驗證（近兩年淨值）</title>
<style>
body {{ font-family:"Noto Sans TC","Microsoft JhengHei",sans-serif; background:#0B0C10;
  color:#F2F4F8; padding:20px; max-width:900px; margin:0 auto; }}
h1 {{ font-size:20px; }} a {{ color:#60a5fa; text-decoration:none; }}
.meta {{ font-size:12px; color:#9BA3B4; margin:6px 0 16px; }}
.row {{ background:#13151B; border-radius:12px; padding:12px 14px; margin-bottom:10px; }}
.head {{ font-size:14px; font-weight:600; margin-bottom:8px; }}
.mk {{ font-size:11px; color:#9BA3B4; font-weight:400; }}
.grp {{ float:right; font-size:12px; font-weight:400; }}
.tl {{ display:flex; gap:4px; flex-wrap:wrap; }}
.q {{ flex:1; min-width:64px; text-align:center; padding:6px 2px; border-radius:6px;
  font-size:13px; font-weight:700; font-variant-numeric:tabular-nums; }}
.q span {{ display:block; font-size:10px; font-weight:400; opacity:.75; }}
.q.r {{ background:#7f1d1d; color:#fecaca; }}
.q.y {{ background:#713f12; color:#fde68a; }}
.q.g {{ background:#14532d; color:#bbf7d0; }}
.ev {{ font-size:12px; color:#93c5fd; margin-top:6px; line-height:1.7; }}
.ev.dim {{ color:#5C6474; }}
.ev.warn {{ color:#fbbf24; font-weight:600; }}
</style></head><body>
<h1>歷史驗證：近兩年每季淨值</h1>
<div class="meta"><a href="index.html">← 回預警表</a>・紅=淨值&lt;5（全額交割門檻）・
黃=&lt;10（停信用門檻）・綠=安全・資料 FinMind（{bt['generated_at'][:16].replace('T',' ')}）・
用途：對照各股跌破門檻的季度與官方列入時點，驗證規則有效性</div>
{''.join(rows)}
</body></html>"""
    (DOCS / "backtest.html").write_text(html)
    return True

RULE_NOTE = ("⚠️ 上市／上櫃規定不同：上市依證交所營業細則第49條、上櫃依櫃買中心業務規則"
             "（上櫃另有管理股票/分盤交易制度）——表格「市場」欄區分適用規定，細節見規則頁")

# Phase A（2026-08-20，計畫全文見 ~/.claude/plans/deep-stargazing-tide.md）：全面改寫成
# Phase 0 定義的雙軸語言——淨值緩衝區（距全額交割門檻遠近）× 信用交易資格（面額10元股跟
# 緩衝區重合；非10元面額股是獨立判斷，見展開列「信用交易資格」區塊，不是同一件事）。
BUFFER_NOTE = ("⚠️ 這是「市場觀察緩衝區」，不是法規門檻本身——面額10元股緩衝區跟信用交易10元"
              "門檻剛好重合；非10元面額股緩衝區只反映全額交割門檻遠近，信用交易資格是獨立判斷"
              "（看有無累積虧損，不是淨值），見展開列「信用交易資格」區塊。")

TABS = [
    ("predict_in", "🔴 預測打入",
     "已達成個股全額交割門檻（面額10元股為5元，非10元面額股依面額換算）、但官方尚未公告——"
     "財報審閱到變更交易方法生效通常有數個工作日～一週的作業時間差（依交易所/櫃買排程而定），"
     "本站是提前偵測到已達門檻的空窗期，不是預測未來。" + RULE_NOTE
     + " ⚠️ 不代表存在可執行的交易機會；展開列「可否放空」逐檔顯示融資融券／借券資格，"
       "認售權證請自行查詢，勿依單一管道推論其餘管道狀態。"),
    ("recover", "🟢 恢復候選",
     "是「全額交割中」頁籤的子集——同樣在官方全額交割名單上，差別只在淨值已回升到門檻以上。"
     "恢復條件依市場不同：上市（營業細則第49條）要最新兩期財報淨值皆達標且淨值總額逾3億元；"
     "上櫃（業務規則第12條）只要最新一期達標且較前期增加，無3億元門檻，較上市寬鬆。"
     "⚠️ 淨值只是列入事由之一（另有會計師意見、財報未依限公告、重整等款）——若係因淨值列入，"
     "達標後可望下次審查恢復；若因其他事由列入，淨值回升不生效（如大飲淨值11元仍在名單）。"
     "各股實際列入原因官方 API 不提供，需查證交所/櫃買公告。"
     "⚠️ 淨值總額以目前股數估算，個股曾減資/增資者可能失真（既有限制）。"
     "目標使用方式：「全額交割中」代表目前仍受交割限制、交易不便；「恢復候選」代表淨值已達標，"
     "若通過下次審查有機會摘帽恢復普通交易，市場可能提前反應「摘帽行情」——搭配展開列"
     "「可否作多」判斷有沒有現成工具可以布局。"),
    ("edge", "🟠 危險邊緣",
     "淨值介於個股全額交割門檻與 6 元之間——再虧一季恐跌破全額交割門檻。" + BUFFER_NOTE
     + RULE_NOTE
     + " ⚠️ 本頁籤只列有放空管道（SBL 借券或認售權證，資料完整時才排除）的股票；管道存在"
       "不代表實際可成交/有量，展開列「可否放空」逐檔顯示狀態，仍需自行確認。放空管道資料"
       "暫時取得不到時，該檔會先保留不濾掉（標題數字會註明幾檔屬於這種情況）。"),
    ("margin_risk", "🟡 信用警戒",
     "淨值 6~10。" + BUFFER_NOTE
     + " 面額10元股：低於 10 元將停止融資融券（依「有價證券得為融資融券標準」，上市上櫃同適用；"
       "審查按季排定，恢復生效約在申報截止後5個營業日，實際生效日以官方公告為準）。"),
    ("official", "⚪ 全額交割中",
     "官方現行變更交易方法名單（上市：證交所 TWT85U；上櫃：櫃買 cmode），淨值仍未達門檻。"
     "淨值已回升的子集另外歸在「恢復候選」頁籤，兩者不是並列分類。"),
    ("dropped", "⬇️ 最近一季掉落",
     "跌破的是融資融券 10 元門檻，跟全額交割無關（除非同時也跌破全額交割門檻）——"
     "白話講：這批股票剛失去信用交易（融資融券）資格，還能正常買賣，只是不能再融資融券。"
     "各檔比較期間不同，逐列標示。"),
    ("watch", "🔵 觀察池 10~15",
     "淨值 10~15 且未列官方名單。" + BUFFER_NOTE
     + " ⚠️ 本頁籤只列有放空管道（SBL 借券或認售權證，資料完整時才排除）的股票；管道存在"
       "不代表實際可成交/有量，展開列「可否放空」逐檔顯示狀態，仍需自行確認。放空管道資料"
       "暫時取得不到時，該檔會先保留不濾掉（標題數字會註明幾檔屬於這種情況）。"),
    ("trading_changes", "📋 變更交易公告",
     "每日檢查公開資訊觀測站（MOPS）底下彙整證交所／櫃買中心公告的系統，逐日累積本季"
     "「變更交易方法／信用交易」相關公告（已排除處置股／注意股）——內容就是證交所/櫃買中心"
     "自己發的公告，只是透過這個彙整入口一次涵蓋兩個交易所，不是另一套獨立資料源。"),
]


def fmt(v, nd=2):
    return "-" if v is None else f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def history_row(code: str, bt_stocks: dict, market: str = "", listing: dict = None) -> str:
    """個股展開列：近八季淨值 chips + 事件判讀（無資料則提示）"""
    s = bt_stocks.get(code)
    if not s:
        return ('<div class="hist-none">近八季資料未納入回測股池'
                '（v1 僅含預測打入/邊緣/恢復/名單股）</div>')
    if s.get("unreliable"):
        return ('<div class="hist-none">歷史淨值資料單位異常（FinMind 對部分 KY 股'
                '欄位不一致），為避免誤導不顯示——請以 goodinfo 個股頁為準</div>')
    if s.get("par_factor", 1) != 1:
        note = (f'<div class="hist-none">此股面額非 10 元'
                f'（已按 1/{s["par_factor"]} 校準歷史淨值）</div>')
    else:
        note = ""
    chips = "".join(
        f'<div class="q {"r" if h["hit5"] else "y" if h["hit10"] else "g"}">'
        f'<span>{h["quarter"]}</span>{h["net_value"]:.2f}</div>'
        for h in s["history"])
    evs = "".join(f'<div class="ev">{e["text"]}</div>' for e in s.get("events", []))
    lst = listing_line(code, market, listing or {})
    return f'{note}<div class="tl">{chips}</div>{lst}{evs or "<div class=ev>近兩年未觸發門檻事件</div>"}'


# ===== S8：watchlist 自選 + TradingView 迷你圖（純前端，普通字串注入避免 f-string 雙括號地獄）=====
S8_CSS = """
.star { cursor:pointer; margin-right:6px; font-size:13px; user-select:none; }
tr.watched { background:rgba(96,165,250,.07); }
#watchbar { margin:10px 0 0; display:flex; flex-wrap:wrap; gap:6px; }
.wchip { display:inline-flex; align-items:center; gap:6px; background:#13151B;
  border:1px solid rgba(255,255,255,.1); border-radius:10px; padding:5px 10px;
  font-size:12px; cursor:pointer; }
.wchip .al { color:#fbbf24; font-weight:700; }
.wchip .wt { color:#9BA3B4; font-size:11px; }
.tvrow { margin-bottom:8px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.tvbtn { background:#1d4ed8; color:#dbeafe; border:none; border-radius:8px;
  padding:5px 10px; font-size:12px; cursor:pointer; }
.tvbtn:disabled { opacity:.6; cursor:default; }
.tvlink { font-size:12px; }
.tvbox { margin-bottom:10px; }
"""

S8_JS = """
// ===== S8：watchlist 自選（localStorage）+ TradingView 迷你圖 lazy-load =====
const WKEY='sw_watchlist';
function getW(){try{return JSON.parse(localStorage.getItem(WKEY))||[]}catch(e){return[]}}
function setW(a){localStorage.setItem(WKEY,JSON.stringify(a));}
function togWatch(code){const w=getW();const i=w.indexOf(code);
  if(i>=0)w.splice(i,1);else w.push(code);setW(w);refreshStars();renderWatchBar();}
// 從表格列反查該檔資訊（名稱/所在籤頁），避免另存第二份資料
function rowInfo(code){
  const s=document.querySelector('.star[data-code="'+code+'"]');
  if(!s)return null;
  const tr=s.closest('tr'), p=s.closest('.panel');
  return {tr:tr, tab:p.dataset.t, name:tr.children[1].textContent.trim().split(' ')[0]};
}
const TABL={}; tabs.forEach(b=>TABL[b.dataset.t]=b.textContent.replace(/\\d+$/,'').trim());
function refreshStars(){const w=getW();document.querySelectorAll('.star').forEach(s=>{
  const on=w.indexOf(s.dataset.code)>=0;
  s.textContent=on?'\\u2b50':'\\u2606';
  s.closest('tr').classList.toggle('watched',on);});}
function renderWatchBar(){
  const bar=document.getElementById('watchbar'); const w=getW();
  if(!w.length){bar.innerHTML='<span class="wchip" style="cursor:default;opacity:.6">\\u2606 點列表左側星號可釘選自選股，重新整理仍保留（僅存於本機瀏覽器）</span>';return;}
  bar.innerHTML=w.map(c=>{
    const info=rowInfo(c);
    if(!info)return '<span class="wchip" onclick="togWatch(\\''+c+'\\')">'+c+'（不在本期名單，點此移除）</span>';
    const alert=(info.tab==='predict_in'||info.tab==='official')?' <span class="al">\\u26a0</span>':'';
    return '<span class="wchip" onclick="gotoRow(\\''+c+'\\')">\\u2b50 '+c+' '+info.name+alert+'<span class="wt">'+(TABL[info.tab]||info.tab)+'</span></span>';
  }).join('');
}
function gotoRow(code){const info=rowInfo(code);if(!info)return;
  show(info.tab);info.tr.nextElementSibling.classList.add('on');
  info.tr.scrollIntoView({behavior:'smooth',block:'center'});}
// K線：TWSE/TPEx 政府API（無需外部CDN）+ Canvas 自繪
// 台股慣例：紅漲綠跌
async function loadChart(code,market,btn){
  const box=btn.closest('td').querySelector('.tvbox');
  if(box.dataset.lwc)return; box.dataset.lwc='1';
  btn.disabled=true; btn.textContent='\u23f3 \u8f09\u5165\u4e2d...';
  try{
    const candles=await _fetchTW(code,market);
    if(!candles.length)throw new Error('\u7121\u8cc7\u6599');
    const cv=document.createElement('canvas');
    cv.style='width:100%;display:block;';
    cv.width=box.clientWidth||680; cv.height=320;
    box.insertBefore(cv,box.firstChild);
    _drawK(cv,candles);
    new ResizeObserver(()=>{cv.width=box.clientWidth;_drawK(cv,candles);}).observe(box);
    btn.textContent='\U0001f4c8 K\u7dda\u5716\uff08\u65e5K\uff0c\u8fd13\u500b\u6708\uff09';
  }catch(e){
    const err=document.createElement('div');
    err.style='color:#f87171;font-size:12px;padding:8px;background:#220000;border-radius:6px;margin-bottom:6px;';
    err.textContent='\u26a0\ufe0f K\u7dda\u8f09\u5165\u5931\u6557\uff1a'+e.message;
    box.insertBefore(err,box.firstChild);
    box.dataset.lwc=''; btn.disabled=false; btn.textContent='\U0001f4c8 K\u7dda\u5716\uff08\u91cd\u8a66\uff09';
  }
}
async function _fetchTW(code,market){
  const now=new Date(); const candles=[];
  for(let m=2;m>=0;m--){
    const d=new Date(now.getFullYear(),now.getMonth()-m,1);
    const y=d.getFullYear(); const mm=String(d.getMonth()+1).padStart(2,'0');
    try{
      if(market==='\u4e0a\u5e02'){
        const j=await fetch('https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date='+y+mm+'01&stockNo='+code).then(r=>r.json());
        if(j.stat!=='OK'||!j.data)continue;
        for(const r of j.data){
          const p=s=>parseFloat(s.replace(/,/g,''));
          const o=p(r[3]),h=p(r[4]),l=p(r[5]),c=p(r[6]);
          if(isNaN(o))continue;
          const dt=r[0].split('/'); candles.push({dt:dt[1]+'/'+dt[2],o,h,l,c});
        }
      }else{
        const roc=y-1911;
        const j=await fetch('https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php?l=zh-tw&d='+roc+'/'+mm+'/01&s=0,asc&stk_no='+code+'&_='+Date.now()).then(r=>r.json());
        if(!j.aaData)continue;
        for(const r of j.aaData){
          const p=s=>parseFloat(String(s).replace(/,/g,''));
          const o=p(r[3]),h=p(r[4]),l=p(r[5]),c=p(r[6]);
          if(isNaN(o))continue;
          const dt=r[0].split('/'); candles.push({dt:dt[1]+'/'+dt[2],o,h,l,c});
        }
      }
    }catch(e){}
  }
  return candles;
}
function _drawK(cv,data){
  const ctx=cv.getContext('2d');
  const W=cv.width,H=cv.height,PT=10,PR=55,PB=28,PL=8;
  const cw=W-PL-PR,ch=H-PT-PB;
  const ps=data.flatMap(c=>[c.h,c.l]);
  const mn=Math.min(...ps),mx=Math.max(...ps),rng=mx-mn||1;
  ctx.fillStyle='#13151B'; ctx.fillRect(0,0,W,H);
  const toY=p=>PT+ch-(p-mn)/rng*ch;
  const bg=cw/data.length;
  const bw=Math.max(2,Math.floor(bg)-1);
  [0,.25,.5,.75,1].forEach(f=>{
    const y=toY(mn+rng*f);
    ctx.strokeStyle='#1e2030'; ctx.lineWidth=0.5;
    ctx.beginPath(); ctx.moveTo(PL,y); ctx.lineTo(W-PR,y); ctx.stroke();
    ctx.fillStyle='#9CA3AF'; ctx.font='10px monospace'; ctx.textAlign='left';
    ctx.fillText((mn+rng*f).toFixed(1),W-PR+3,y+4);
  });
  data.forEach((c,i)=>{
    const x=PL+(i+0.5)*bg;
    const up=c.c>=c.o;
    ctx.strokeStyle=ctx.fillStyle=up?'#ef4444':'#22c55e';
    ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x,toY(c.h)); ctx.lineTo(x,toY(c.l)); ctx.stroke();
    const by=toY(Math.max(c.o,c.c)),bh=Math.max(1,toY(Math.min(c.o,c.c))-by);
    ctx.fillRect(x-bw/2,by,bw,bh);
  });
  ctx.fillStyle='#9CA3AF'; ctx.font='10px sans-serif'; ctx.textAlign='center';
  [0,Math.floor(data.length/2),data.length-1].forEach(i=>{
    if(data[i])ctx.fillText(data[i].dt,PL+(i+0.5)*bg,H-6);
  });
}
"""


def main():
    rep = json.loads(REPORT.read_text())
    g = rep["groups"]
    bt_stocks = (json.loads(BACKTEST.read_text())["stocks"]
                 if BACKTEST.exists() else {})
    listing = json.loads(LISTING.read_text()) if LISTING.exists() else {}

    # 🔴 dropped 頁籤完全不依賴 backtest.json，獨立讀 netvalue_history（第 4 節解耦）
    nv_data = json.loads(NV_FILE.read_text()) if NV_FILE.exists() else {"rows": []}
    nv_history = {}
    if NV_HISTORY_DIR.exists():
        for fp in NV_HISTORY_DIR.glob("*.json"):
            nv_history[fp.stem] = json.loads(fp.read_text())
    nv_history_status = (json.loads(NV_HISTORY_STATUS.read_text())
                         if NV_HISTORY_STATUS.exists() else {})
    cross = crossings.detect_margin_drops(nv_data, nv_history, nv_history_status)

    # Phase B：讀 audit.json（會計師查核意見）。缺檔或壞檔一律等同 state:empty，
    # 絕不讓整站產不出來（見計畫 §5「讀取端防呆」）
    try:
        audit = json.loads(AUDIT_FILE.read_text()) if AUDIT_FILE.exists() else {"state": "empty", "rows": {}}
    except Exception:
        audit = {"state": "empty", "rows": {}}

    # §8：放空管道揭露（predict_in／edge 專用）。融資融券用既有 r['status']['credit']
    # （report.json 每列已含，analyze.stock_status() 算好的，不必另讀 official.json）；
    # sbl 獨立檔案，缺檔或壞檔一律等同 state:empty，絕不讓整站產不出來
    try:
        sbl = json.loads(SBL_FILE.read_text()) if SBL_FILE.exists() else {"state": "empty", "codes": []}
    except Exception:
        sbl = {"state": "empty", "codes": []}
    try:
        warrants = (json.loads(WARRANTS_FILE.read_text()) if WARRANTS_FILE.exists()
                   else {"state": "empty", "put_codes": [], "call_codes": []})
    except Exception:
        warrants = {"state": "empty", "put_codes": [], "call_codes": []}
    try:
        trading_changes = (json.loads(TRADING_CHANGES_FILE.read_text())
                           if TRADING_CHANGES_FILE.exists() else {"state": "empty", "matched": []})
    except Exception:
        trading_changes = {"state": "empty", "matched": []}

    tab_btns, panels = [], []
    for key, label, desc in TABS:
        if key == "dropped":
            tab_btn, panel = render_dropped_panel(cross)
            tab_btns.append(tab_btn)
            panels.append(panel)
            continue
        if key == "trading_changes":
            tab_btn, panel = render_trading_changes_panel(trading_changes)
            tab_btns.append(tab_btn)
            panels.append(panel)
            continue
        rows = g.get(key, [])
        fail_open_count = 0
        if key in ("edge", "watch"):
            rows, _filtered_count, fail_open_count = filter_short_channel_tier(rows, sbl, warrants)
        n_note = (f'<span class="n" title="{fail_open_count} 檔因放空管道資料未取得暫未過濾">'
                 f'{len(rows)}（{fail_open_count}檔資料未取得暫未過濾）</span>'
                 if fail_open_count else f'<span class="n">{len(rows)}</span>')
        tab_btns.append(f'<button class="tab" data-t="{key}">{label}{n_note}</button>')
        def status_chip(r):
            st = r.get("status") or {}
            chips = []
            if st.get("full_delivery"):
                chips.append('<span class="chip cfull">全額交割</span>')
            if st.get("disposal"):
                chips.append(f'<span class="chip cdisp" title="{st["disposal"]["reason"]}">'
                             f'處置至{st["disposal"]["period"].split("～")[-1]}</span>')
            credit = st.get("credit", "")
            if credit == "可信用交易":
                chips.append('<span class="chip cok">可信用</span>')
            elif credit:
                chips.append(f'<span class="chip cstop">{credit}</span>')
            return "".join(chips) or "-"

        def nr_badge(r):
            nr = r.get("new_report")
            if not nr:
                return "", ""
            d = nr["delta"]
            cross = f'<span class="cross">⚡{nr["crossing"]}</span>' if nr["crossing"] else ""
            badge = f'<span class="newb">🆕新財報</span>'
            delta = (f'<span class="delta {"neg" if d < 0 else "pos"}">'
                     f'{"▼" if d < 0 else "▲"}{abs(d):.2f}（{nr["prev_q"]}→）</span>{cross}')
            return badge, delta
        trs_list = []
        # 新財報排前面
        for r in sorted(rows, key=lambda x: not x.get("new_report")):
            badge, delta = nr_badge(r)
            # S8：TradingView symbol 前綴（上市 TWSE、上櫃 TPEX）
            tvp = 'TWSE' if r.get('market') == '上市' else 'TPEX'
            short_html = (f'<div class="audit-heading">可否放空</div>'
                         f'{render_short_channel_block(r["code"], r.get("status"), sbl, warrants)}'
                         if key in ("predict_in", "edge", "watch") else "")
            long_html = (f'<div class="audit-heading">可否作多</div>'
                        f'{render_long_channel_block(r["code"], r.get("status"), warrants)}'
                        if key == "recover" else "")
            recover_status_html = (render_recover_status_block(r.get("recover_status"))
                                   if key == "recover" else "")
            recover_announce_html = (render_recover_announcement_block(r["code"], trading_changes)
                                     if key == "recover" else "")
            credit_elig_html = render_credit_eligibility_block(r.get("credit_eligibility"))
            trs_list.append(f"""<tr class="main{' isnew' if badge else ''}" onclick="tog(this)">
  <td><span class="star" data-code="{r['code']}" onclick="event.stopPropagation();togWatch('{r['code']}')">☆</span><a href="{r['goodinfo_url']}" target="_blank" onclick="event.stopPropagation()">{r['code']}</a></td>
  <td>{r['name']} {badge}</td><td>{r.get('market','')}</td>
  <td class="stcell">{status_chip(r)}</td>
  <td class="num">{fmt(r.get('price'))}</td>
  <td class="num nv">{fmt(r.get('net_value'))}{delta}</td>
  <td class="num {'neg' if (r.get('gap') or 0) < 0 else 'pos'}"{f' title="面額非10元，全額交割門檻為每股{r["fd_threshold"]}元"' if r.get('fd_threshold') not in (None, 5.0) else ''}>{fmt(r.get('gap'))}</td>
  <td>{r.get('nv_quarter','')}{('<span class=note>' + r['note'] + '</span>') if r.get('note') else ''} <span class="exp">▾</span></td>
</tr>
<tr class="detail"><td colspan="8"><div class="tvrow"><button class="tvbtn" onclick="loadChart('{r['code']}','{r.get('market','')}',this)">📈 K線圖</button><a class="tvlink" target="_blank" href="https://tw.tradingview.com/chart/?symbol={tvp}%3A{r['code']}">TradingView ↗</a><a class="tvlink" target="_blank" href="https://www.wantgoo.com/stock/{r['code']}/technical-chart">Wantgoo ↗</a></div><div class="tvbox"></div>{history_row(r['code'], bt_stocks, r.get('market',''), listing)}<div class="audit-heading">會計師查核意見</div>{render_audit_block(r['code'], audit)}{credit_elig_html}{short_html}{recover_status_html}{recover_announce_html}{long_html}</td></tr>""")
        trs = "".join(trs_list)
        panels.append(f"""<section class="panel" data-t="{key}">
  <p class="desc">{desc}</p>
  <table><thead><tr><th>代號</th><th>名稱</th><th>市場</th><th>官方現況</th><th>股價</th>
    <th>每股淨值</th><th>距門檻</th><th>財報季度</th></tr></thead>
  <tbody>{trs or '<tr><td colspan=8 class=empty>（目前無）</td></tr>'}</tbody></table>
</section>""")

    # 只把 source_fetched_at 傳給前端，年齡由瀏覽器當下算（存下來的天數會凍結）
    degraded_json = json.dumps(rep.get("degraded", {}), ensure_ascii=False)
    # 只傳 state/fetched_at/reason，不傳整包 rows（rows 已經逐檔渲染進各列的 audit-block 了）
    audit_state_json = json.dumps({"state": audit.get("state"), "fetched_at": audit.get("fetched_at"),
                                   "reason": audit.get("reason")}, ensure_ascii=False)

    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>全額交割／信用交易預警</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:"Noto Sans TC","Microsoft JhengHei",sans-serif; background:#0B0C10;
  color:#F2F4F8; padding:20px; max-width:1080px; margin:0 auto; }}
h1 {{ font-size:20px; letter-spacing:-.01em; }}
.meta {{ font-size:12px; color:#9BA3B4; margin:6px 0 4px; line-height:1.8; }}
.deadline {{ display:inline-block; background:#7f1d1d; color:#fecaca; padding:2px 10px;
  border-radius:8px; font-size:12px; font-weight:600; }}
.meta a {{ color:#60a5fa; }}
.tabs {{ display:flex; gap:6px; margin:16px 0 12px; flex-wrap:wrap; }}
.tab {{ padding:8px 12px; border:1px solid rgba(255,255,255,.1); background:#13151B;
  color:#9BA3B4; border-radius:10px; font-size:13px; cursor:pointer; }}
.tab.on {{ background:#F2F4F8; color:#0B0C10; font-weight:700; }}
.tab .n {{ margin-left:6px; font-size:11px; opacity:.7; }}
.desc {{ font-size:13px; color:#9BA3B4; margin-bottom:10px; }}
.panel {{ display:none; }} .panel.on {{ display:block; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; background:#13151B;
  border-radius:12px; overflow:hidden; }}
th {{ text-align:left; padding:10px 8px; color:#5C6474; font-size:12px;
  border-bottom:1px solid rgba(255,255,255,.08); cursor:pointer; white-space:nowrap; }}
td {{ padding:9px 8px; border-bottom:1px solid rgba(255,255,255,.05); }}
td a {{ color:#60a5fa; text-decoration:none; font-family:ui-monospace,monospace; }}
.num {{ font-variant-numeric:tabular-nums; }}
.nv {{ font-weight:700; }}
.neg {{ color:#f87171; }} .pos {{ color:#34d399; }}
.empty {{ color:#5C6474; text-align:center; padding:24px; }}
.note {{ display:block; font-size:11px; color:#f59e0b; }}
tr.main {{ cursor:pointer; }}
tr.detail {{ display:none; background:#0F1116; }}
tr.detail.on {{ display:table-row; }}
tr.detail td {{ padding:12px; }}
.exp {{ color:#5C6474; font-size:11px; }}
.tl {{ display:flex; gap:4px; flex-wrap:wrap; margin-bottom:8px; }}
.q {{ min-width:58px; text-align:center; padding:4px 2px; border-radius:6px;
  font-size:12px; font-weight:700; font-variant-numeric:tabular-nums; }}
.q span {{ display:block; font-size:9px; font-weight:400; opacity:.75; }}
.q.r {{ background:#7f1d1d; color:#fecaca; }}
.q.y {{ background:#713f12; color:#fde68a; }}
.q.g {{ background:#14532d; color:#bbf7d0; }}
.q.lowconf {{ opacity:.55; border:1px dashed rgba(255,255,255,.35); }}
.ev {{ font-size:12px; color:#93c5fd; line-height:1.7; }}
.ev.dim {{ color:#5C6474; }}
.ev.warn {{ color:#fbbf24; font-weight:600; }}
.hist-none {{ font-size:12px; color:#5C6474; }}
.tc-item {{ background:#13151B; border:1px solid rgba(255,255,255,.08); border-radius:10px;
  padding:12px; margin-bottom:10px; }}
.tc-meta {{ font-size:11px; color:#9BA3B4; margin-bottom:6px; }}
.tc-content {{ font-size:13px; line-height:1.7; white-space:normal; }}
.audit-heading {{ font-size:11px; color:#5C6474; text-transform:uppercase; letter-spacing:.04em;
  margin:12px 0 6px; padding-top:10px; border-top:1px solid rgba(255,255,255,.08); }}
.audit-block {{ font-size:12px; }}
.audit-chip {{ display:inline-block; font-size:11px; font-weight:600; padding:2px 8px;
  border-radius:6px; background:#3f3f46; color:#e4e4e7; }}
.audit-chip.g {{ background:#14532d; color:#86efac; }}
.audit-chip.r {{ background:#7f1d1d; color:#fecaca; }}
.audit-chip.stale {{ opacity:.5; }}
.updbtn {{ display:inline-block; margin-left:8px; padding:2px 10px; border-radius:8px;
  background:#1d4ed8; color:#dbeafe; font-size:12px; text-decoration:none; }}
.banner {{ margin:12px 0 0; padding:10px 14px; border-radius:10px; background:#172554;
  border:1px solid #1d4ed8; font-size:13px; line-height:1.7; }}
.banner.warn {{ background:#422006; border-color:#a16207; color:#fde68a; }}
.banner.bad {{ background:#450a0a; border-color:#b91c1c; color:#fecaca; font-weight:600; }}
.age.warn {{ color:#fbbf24; font-weight:600; }}
.age.bad {{ color:#f87171; font-weight:700; }}
.newb {{ background:#facc15; color:#713f12; font-size:10px; font-weight:700;
  padding:1px 6px; border-radius:6px; margin-left:4px; vertical-align:middle; }}
.delta {{ display:block; font-size:11px; font-weight:400; }}
.cross {{ display:block; font-size:11px; color:#fbbf24; font-weight:700; }}
tr.isnew {{ background:rgba(250,204,21,.06); }}
.stcell {{ white-space:nowrap; }}
.chip {{ display:inline-block; font-size:10px; font-weight:600; padding:2px 7px;
  border-radius:6px; margin:1px 2px 1px 0; }}
.chip.cfull {{ background:#3f3f46; color:#e4e4e7; }}
.chip.cdisp {{ background:#7c2d12; color:#fdba74; }}
.chip.cstop {{ background:#7f1d1d; color:#fecaca; }}
.chip.cok {{ background:#14532d; color:#86efac; }}
.chip.cstock {{ background:#1e293b; color:#93c5fd; font-size:11px; }}
.tc-stocks {{ margin-bottom:8px; }}
.recover-announce {{ margin-top:6px; padding-top:6px; border-top:1px dashed rgba(255,255,255,.12); }}
@media (max-width:640px) {{
  th:nth-child(5), td:nth-child(5) {{ display:none; }}   /* 手機藏股價，保留現況 */
}}
@media (max-width:640px) {{
  body {{ padding:12px; }}
  th:nth-child(8), td:nth-child(8) {{ display:none; }}
  table {{ font-size:12px; }}
}}
{S8_CSS}</style></head><body>
<h1>全額交割／信用交易預警</h1>
<div class="meta">淨值資料：{rep['nv_fetched_at'][:16].replace('T',' ')}（goodinfo）
<span class="age" data-testid="nv-age" data-ts="{rep['nv_fetched_at']}"></span>・
官方名單：{rep['official_fetched_at'][:16].replace('T',' ')}（證交所/櫃買中心）
<span class="age" data-testid="official-age" data-ts="{rep['official_fetched_at']}"></span>・
<a href="rules.html">分級規則與法規依據</a>・<a href="backtest.html">歷史驗證</a><br>
<span class="deadline">下一財報截止：{rep['next_report_deadline']}（{rep['days_to_report']} 天後）</span>
<a class="updbtn" href="https://github.com/deankhho/stock-watchdog/actions/workflows/update.yml"
  target="_blank">🔄 觸發更新</a></div>
<noscript><div class="banner bad">本頁的「資料是否過期」指標需要 JavaScript 才會顯示。
請開啟 JS，否則無法判斷下方數字是不是舊資料。</div></noscript>
<div id="freshness"></div>
{f'''<div class="banner">🆕 <b>偵測到 {rep["new_reports_count"]} 檔交出新財報</b>
（14 天內），其中 <b style="color:#fbbf24">{rep["new_reports_crossings"]} 檔穿越門檻</b>
（跌破5元恐列全額交割／跌破10元恐停信用／回升）——各表 🆕 列已排最前，
淨值欄顯示變化量 ▲▼</div>''' if rep.get("new_reports_count") else
'<div class="banner" style="opacity:.7">目前名單內尚無新一季財報（各檔財報季度見表末欄）；新財報公布後此處會醒目提示「哪些股票交出財報、淨值變化、是否穿越門檻」</div>'}
<div id="watchbar"></div>
<div class="tabs">{''.join(tab_btns)}</div>
{''.join(panels)}
<script>
/* 資料新鮮度：年齡一律在瀏覽器端、以「經過時間」計算。
   為什麼不在產生網頁時算好：若整條管線停擺，烤死的天數會凍結在最後一次成功的值，
   資料明明越來越舊、頁面卻永遠顯示同一個數字——監控指標自己變成謊報。
   為什麼用經過時間而非日曆日相減：時間戳是絕對時刻，這樣算與瀏覽器時區無關。 */
const DEGRADED = {degraded_json};
const AUDIT_STATE = {audit_state_json};
const AUDIT_STALE_DAYS = 30;
const DEG_LABEL = {{tpex_cmode:'上櫃變更交易名單', disposal:'處置股名單',
                    margin_status:'信用交易現況'}};
const NV_WARN=5, NV_BAD=10, DEG_BAD=3;
const ageDays = ts => Math.floor((Date.now() - Date.parse(ts)) / 86400000);

document.querySelectorAll('.age').forEach(el => {{
  const d = ageDays(el.dataset.ts);
  if (!isFinite(d)) return;
  el.textContent = d <= 0 ? '（今日）' : '（已 ' + d + ' 天未更新）';
  if (el.dataset.testid === 'nv-age') {{
    if (d >= NV_BAD) el.classList.add('bad');
    else if (d >= NV_WARN) el.classList.add('warn');
  }}
}});

/* 會計師查核意見逐列年齡：超過 30 天不顯示 tier 色塊，只留純文字＋過期天數
   （同樣不在產生網頁時凍結天數，理由同上——見發現「陳舊上限」） */
document.querySelectorAll('[data-audit-fetched-at]').forEach(el => {{
  const ts = el.dataset.auditFetchedAt;
  const ageEl = el.querySelector('[data-audit-age]');
  if (!ts) {{ if (ageEl) ageEl.textContent = '時間未知'; return; }}
  const d = ageDays(ts);
  if (!isFinite(d)) {{ if (ageEl) ageEl.textContent = '時間未知'; return; }}
  if (ageEl) ageEl.textContent = d <= 0 ? '今日' : d + ' 天前';
  if (d > AUDIT_STALE_DAYS) {{
    const chip = el.querySelector('[data-audit-chip]');
    if (chip) {{
      chip.classList.add('stale');
      chip.textContent = '資料已過期 ' + d + ' 天（不顯示查核結論）';
    }}
  }}
}});

/* 借券資格過期時絕不顯示「具借券資格」，一律回退成未知（§8 freshness 契約）——
   state 已在產生網頁時判斷過一次，這裡補的是「build 時 state=ok，但頁面被看到時
   已經是好幾天前的舊 build」這個無法在產生當下預知的情境，跟 DEGRADED 共用同一個
   「額外來源」壞掉門檻（DEG_BAD=3 天）以維持全站一致 */
const SBL_STALE_DAYS = DEG_BAD;
document.querySelectorAll('[data-sbl-fetched-at]').forEach(el => {{
  const ts = el.dataset.sblFetchedAt;
  const state = el.dataset.sblState;
  const avail = el.dataset.sblAvail === '1';
  const line = el.querySelector('[data-sbl-line]');
  if (!line) return;
  const d = ts ? ageDays(ts) : null;
  const stale = state !== 'ok' || d === null || !isFinite(d) || d > SBL_STALE_DAYS;
  if (stale && avail) {{
    line.textContent = '借券：借券資格未知（資料' + (d === null || !isFinite(d) ? '時間未知' : d + ' 天前') + '，暫無法判斷）';
  }}
}});

/* 認售權證同款過期回退（同一套 freshness 契約）——過期時不可再顯示「具有效認售權證」
   這種肯定判斷，一律回退未知，理由同 SBL */
document.querySelectorAll('[data-warrants-fetched-at]').forEach(el => {{
  const ts = el.dataset.warrantsFetchedAt;
  const state = el.dataset.warrantsState;
  const avail = el.dataset.warrantsAvail === '1';
  const line = el.querySelector('[data-warrants-line]');
  if (!line) return;
  const d = ts ? ageDays(ts) : null;
  const stale = state !== 'ok' || d === null || !isFinite(d) || d > SBL_STALE_DAYS;
  if (stale && avail) {{
    line.textContent = '認售權證：需自行查詢（資料' + (d === null || !isFinite(d) ? '時間未知' : d + ' 天前') + '，暫無法判斷）';
  }}
}});

/* 認購權證同款過期回退（作多區塊，同一套 freshness 契約，同一份 warrants.json 來源） */
document.querySelectorAll('[data-call-fetched-at]').forEach(el => {{
  const ts = el.dataset.callFetchedAt;
  const state = el.dataset.callState;
  const avail = el.dataset.callAvail === '1';
  const line = el.querySelector('[data-call-line]');
  if (!line) return;
  const d = ts ? ageDays(ts) : null;
  const stale = state !== 'ok' || d === null || !isFinite(d) || d > SBL_STALE_DAYS;
  if (stale && avail) {{
    line.textContent = '認購權證：需自行查詢（資料' + (d === null || !isFinite(d) ? '時間未知' : d + ' 天前') + '，暫無法判斷）';
  }}
}});

(function(){{
  const box = document.getElementById('freshness');
  const nvEl = document.querySelector('[data-testid="nv-age"]');
  const nvAge = nvEl ? ageDays(nvEl.dataset.ts) : 0;
  if (nvAge >= NV_BAD) {{
    box.insertAdjacentHTML('beforeend',
      '<div class="banner bad" data-testid="stale-banner">🔴 淨值資料已 ' + nvAge +
      ' 天未更新——「預測即將打入」「危險邊緣」等欄位是用舊淨值算的，<u>目前不可信</u>。' +
      '請在家用機執行 update.sh 補資料。</div>');
  }} else if (nvAge >= NV_WARN) {{
    box.insertAdjacentHTML('beforeend',
      '<div class="banner warn" data-testid="stale-banner">⚠️ 淨值資料已 ' + nvAge +
      ' 天未更新，預測欄位的即時性下降。</div>');
  }}
  const degs = Object.entries(DEGRADED).map(([k, v]) =>
    ({{name: DEG_LABEL[k] || k, age: v.source_fetched_at ? ageDays(v.source_fetched_at) : null}}));
  if (degs.length) {{
    const worst = Math.max(...degs.map(d => d.age === null ? 99 : d.age));
    /* 明講是「哪一塊」舊了——頁面同時混著新舊資料時，只說「部分降級」無從判斷哪個欄位能信 */
    const detail = degs.map(d => d.name + '沿用 ' +
      (d.age === null ? '舊' : d.age + ' 天前') + '資料').join('、');
    box.insertAdjacentHTML('beforeend',
      '<div class="banner ' + (worst >= DEG_BAD ? 'bad' : 'warn') +
      '" data-testid="degraded-banner">' + (worst >= DEG_BAD ? '🔴 ' : '⚠️ ') +
      detail + '，其餘為最新。</div>');
  }}
  /* empty 與 degraded 是不同性質（沒有資料 vs 有舊資料且已過期），渲染成不同畫面 */
  if (AUDIT_STATE.state === 'degraded') {{
    const d = AUDIT_STATE.fetched_at ? ageDays(AUDIT_STATE.fetched_at) : null;
    box.insertAdjacentHTML('beforeend',
      '<div class="banner warn" data-testid="audit-degraded-banner">⚠️ 會計師查核資料為' +
      (d === null ? '較舊' : d + ' 天前') + '的舊資料（' + (AUDIT_STATE.reason || '本輪抓取未通過') +
      '），展開列仍顯示上次抓到的結果。</div>');
  }} else if (AUDIT_STATE.state === 'empty') {{
    box.insertAdjacentHTML('beforeend',
      '<div class="banner" style="opacity:.7" data-testid="audit-empty-banner">' +
      '會計師查核資料尚未取得。</div>');
  }}
}})();

const tabs=document.querySelectorAll('.tab'), panels=document.querySelectorAll('.panel');
function show(t){{tabs.forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 panels.forEach(p=>p.classList.toggle('on',p.dataset.t===t));}}
tabs.forEach(b=>b.onclick=()=>show(b.dataset.t));
show('predict_in');
function tog(tr){{ tr.nextElementSibling.classList.toggle('on'); }}
// 點表頭排序
document.querySelectorAll('th').forEach((th)=>th.onclick=()=>{{
  const tb=th.closest('table').querySelector('tbody');
  const i=[...th.parentNode.children].indexOf(th);
  // 主列+展開列成對排序（否則展開內容會錯位）
  const pairs=[...tb.querySelectorAll('tr.main')].map(m=>[m,m.nextElementSibling]);
  const asc=th.dataset.asc!=='1'; th.dataset.asc=asc?'1':'0';
  pairs.sort(([a],[b])=>{{
    const x=a.children[i]?.textContent.trim(), y=b.children[i]?.textContent.trim();
    const nx=parseFloat(x), ny=parseFloat(y);
    const c=(isNaN(nx)||isNaN(ny))?x.localeCompare(y):nx-ny;
    return asc?c:-c;}});
  pairs.forEach(([m,d])=>{{tb.appendChild(m); if(d) tb.appendChild(d);}});}});
{S8_JS}
</script></body></html>"""

    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html)

    rules = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>分級規則與法規依據</title>
<style>
body {{ font-family:"Noto Sans TC","Microsoft JhengHei",sans-serif; background:#0B0C10;
  color:#F2F4F8; padding:20px; max-width:800px; margin:0 auto; line-height:1.9; font-size:14px; }}
h1 {{ font-size:20px; }} h2 {{ font-size:16px; margin:20px 0 8px; color:#60a5fa; }}
a {{ color:#60a5fa; }} .src {{ font-size:12px; color:#9BA3B4; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; margin:8px 0; }}
th,td {{ border:1px solid rgba(255,255,255,.12); padding:8px; text-align:left; }}
</style></head><body>
<h1>分級規則與法規依據</h1><p><a href="index.html">← 回預警表</a></p>

<h2>本站分級邏輯</h2>
<p class="src">⚠️ 下表「距門檻」一欄是<b>個股全額交割門檻</b>（面額10元股為5元，面額非10元股依面額
換算，見下方「重要限制」），不是固定 5 元；「淨值緩衝區」是本站自訂的市場觀察範圍，不是法規
門檻本身——面額10元股緩衝區跟信用交易10元門檻剛好重合，面額非10元股緩衝區只反映全額交割
門檻遠近，信用交易資格是獨立判斷（看有無累積虧損，不是淨值），見「停止信用交易」一節。</p>
<table>
<tr><th>分級</th><th>條件</th><th>意涵</th></tr>
<tr><td>🔴 預測打入</td><td>淨值 &lt; 個股全額交割門檻 且未列官方名單</td><td>已達成門檻但官方審查/公告生效有作業時間差（通常數個工作日～一週），本站是提前偵測到已達門檻的空窗期，不是預測未來</td></tr>
<tr><td>🟢 恢復候選</td><td>已列名單但最新淨值 ≥ 個股全額交割門檻</td><td>是「⚪ 全額交割中」的子集——淨值回升，實際恢復條件上市／上櫃不同（見下表「恢復普通交易」），恢復常伴隨行情。展開列「恢復資格」是本站依此表條件算出的輔助判定（淨值條件已符合／尚未符合／資料不足無法確認），不是官方認定的替代品——是否真的恢復仍需無其他列入事由，請查官方公告</td></tr>
<tr><td>🟠 危險邊緣</td><td>個股全額交割門檻 ~ 6 元</td><td>再虧損一季可能跌破門檻；只列有放空管道（SBL借券/認售權證，資料完整時才排除）的股票，展開列「可否放空」逐檔顯示狀態</td></tr>
<tr><td>🟡 信用警戒</td><td>淨值 6 ~ 10 元</td><td>面額10元股：低於 10 元將停止融資融券；面額非10元股此區間不代表信用警戒，資格看展開列「信用交易資格」</td></tr>
<tr><td>⚪ 全額交割中</td><td>官方現行名單</td><td>買賣需預收全額款券；淨值已回升的子集另列「🟢 恢復候選」</td></tr>
<tr><td>🔵 觀察池</td><td>淨值 10 ~ 15 元</td><td>尚未觸及信用交易/全額交割相關門檻的市場觀察範圍，非法規分級；只列有放空管道的股票（同危險邊緣）</td></tr>
<tr><td>⬇️ 最近一季掉落</td><td>前季淨值 ≥10、最新季 &lt;10</td><td>跌破的是融資融券10元門檻，跟全額交割無關（除非同時也跌破全額交割門檻）</td></tr>
</table>

<h2>法規依據（上市／上櫃分別適用）</h2>
<table>
<tr><th></th><th>上市（證交所）</th><th>上櫃（櫃買中心）</th></tr>
<tr><td><b>打入全額交割</b></td>
<td>營業細則第 49 條：最近期財報淨值低於「財報所列示股本二分之一」→ 列為變更交易方法股票
（面額 10 元股即每股淨值 5 元，面額非 10 元的個股門檻不同，見下方重要限制）</td>
<td>業務規則第12條：條文與上市相同（最近期個別財務報告淨值低於股本二分之一）；另有<b>管理股票、分盤交易、停止買賣</b>等狀態（本站「全額交割中」籤頁另列旗標）——淨值進一步探底於股本<b>十分之三</b>以下者，額外採行分盤交易（每30分鐘撮合一次）</td></tr>
<tr><td><b>恢復普通交易</b></td>
<td>營業細則第49條第2項第1款：<b>最近「二」期</b>財務報告均淨值逾<b>3億元</b>並達股本二分之一以上 → 恢復</td>
<td>業務規則第12條第4項第1款：<b>最近「一」期</b>財務報告淨值達股本二分之一以上，<b>且較前期增加</b> → 恢復（不需連續兩期，也沒有3億元下限，比上市寬鬆）</td></tr>
<tr><td><b>⚠️ 重要限制</b></td>
<td colspan="2">變更交易方法的列入事由<b>不只淨值一款</b>（營業細則第49條列有多款：淨值低於股本二分之一、
財報未依限公告申報、會計師出具無法表示意見/否定意見或繼續經營疑慮、聲請重整、存款不足退票、
董事/監察人不足、資金貸與或背書保證違規等）。<b>本站僅能監測淨值款</b>——因其他事由列入者，
淨值回升不會恢復，必須該事由消滅（官方 API 無原因欄位，實際事由請查
<a href="https://mops.twse.com.tw/" target="_blank">公開資訊觀測站</a>公告）。
實例：大飲(1213) 淨值 11 元仍在全額交割名單。<br>
🔴 「淨值低於股本二分之一」不是固定 5 元：面額 10 元的股票股本二分之一剛好等於每股淨值 5 元
（母體約 98% 屬此類），但面額 1／2.5／5 元等個股（實測約 23 檔）門檻不同，本站已按個股實際面額換算，
展開列淨值欄位標示「距門檻」而非固定「距5元」，游標移到數字上可看到該股實際門檻。</td></tr>
<tr><td><b>停止信用交易</b></td>
<td colspan="2">「有價證券得為融資融券標準」第2/4條——<b>面額10元股</b>：每股淨值低於票面(10元)
→ 停止融資融券，回升達10元以上恢復；<b>無面額或非10元面額股</b>：門檻不是淨值，是
「最近一個會計年度決算<b>有無累積虧損</b>」→ 有累積虧損停止、消滅後恢復（本站讀官方資產
負債表「保留盈餘」科目判斷，負值視為有累積虧損，見展開列「信用交易資格」）。
審查按季排定（年報+Q1約每年5月、Q2/半年報約8-9月、Q3約11月），生效日約在財報法定申報
截止日後5個營業日——本站僅提供預估區間，實際生效日以官方公告為準，個股實際申報時間可能
早於法定截止日，本站無法逐股追蹤。</td></tr>
</table>
<p class="src">出處：<a href="https://twse-regulation.twse.com.tw/" target="_blank">證交所法規知識庫</a>／
<a href="https://www.tpex.org.tw/" target="_blank">櫃買中心</a>／
<a href="https://law.moj.gov.tw/" target="_blank">全國法規資料庫</a>（條文全文以官方最新版本為準；兩市場規定細節不同，實際以主管機關公告日為準）</p>
<p><b>財報申報期限</b>（一般上市櫃公司）：年報 3/31、Q1 5/15、Q2 8/14、Q3 11/14；
金控、銀行、保險等另有規定。<br>
<span class="src">出處：<a href="https://www.fsc.gov.tw/" target="_blank">金管會</a>「公開發行公司財務報告及營運情形公告申報特殊適用範圍辦法」</span></p>

<h2>資料來源</h2>
<p>每股淨值：goodinfo.tw 每股淨值排行（含財報季度標記）<br>
官方名單：證交所 openapi TWT85U（變更交易）＋櫃買中心 openapi tpex_cmode<br>
產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<p class="src">⚠️ 本站僅供研究參考，非投資建議。資料可能延遲或有誤，交易前請以官方公告為準。</p>
</body></html>"""
    (DOCS / "rules.html").write_text(rules)
    bt = gen_backtest_page()
    print(f"已生成 docs/index.html + rules.html" + ("+ backtest.html" if bt else "（backtest.json 未就緒，略過歷史頁）"))


def selftest():
    """render_recover_status_block()：恢復資格三態呈現，只用於 recover 頁籤展開列。
    🔴 前端不得直接顯示內部 state token（英文 eligible/not_yet/unknown），一律轉成
    完整中文句子——避免使用者把內部技術狀態誤讀成官方認定結果。"""
    # 1. eligible 且 detail 非空（精度但書）→ 顯示中文標籤＋⚠️但書，不可裸露 "eligible"
    html = render_recover_status_block({"state": "eligible",
        "detail": "淨值條件已符合（26Q1/26Q2，以目前股數估算，個股曾減資/增資者可能失真）"})
    assert "淨值條件已符合" in html, html
    assert "⚠️" in html and "個股曾減資/增資者可能失真" in html, html
    assert "eligible" not in html, html

    # 2. not_yet：顯示 detail 說明，不可裸露 "not_yet"
    html = render_recover_status_block({"state": "not_yet",
        "detail": "每股淨值已達標，但淨值總額（26Q1/26Q2，以目前股數估算）未逾3億元"})
    assert "未逾3億元" in html, html
    assert "not_yet" not in html, html

    # 3. unknown：顯示 detail 說明，不可裸露 "unknown"
    html = render_recover_status_block({"state": "unknown",
        "detail": "每股淨值已達標，缺股數資料無法確認3億元門檻"})
    assert "缺股數資料" in html, html
    assert "unknown" not in html, html

    # 4. 沒有 recover_status（非本輪範圍的舊資料/官方名單補漏那種 item）→ 空字串
    assert render_recover_status_block(None) == ""
    assert render_recover_status_block({}) == ""

    # === render_stock_refs_chips()：變更交易公告頁籤用，把 content 裡抓出的
    # 股號/股名/動作做成醒目 chip，不必逐字讀法規公文才知道是哪幾檔股票 ===
    html = render_stock_refs_chips([
        {"code": "8105", "name": "凌巨科技股份有限公司", "action": "停止買賣"},
        {"code": "2380", "name": "虹光精密工業股份有限公司", "action": "恢復交易方法"},
    ])
    assert "8105" in html and "凌巨科技" in html and "停止買賣" in html, html
    assert "2380" in html and "虹光精密工業" in html and "恢復交易方法" in html, html
    # 「股份有限公司」尾綴修剪成短名，跟站內其他地方的短名慣例一致
    assert "股份有限公司" not in html, html
    # 1b. action=None（沒有編號清單的簡單公告）→ 只顯示代號/股名，不留空字樣
    html = render_stock_refs_chips([{"code": "1234", "name": "測試股份有限公司", "action": None}])
    assert "1234" in html and "測試" in html, html
    # 1c. 空清單 → 空字串
    assert render_stock_refs_chips([]) == ""

    # === render_recover_announcement_block()：把官方變更交易公告連結回恢復候選展開列
    # ——使用者能直接對照本站估算的「恢復資格」跟官方是否真的已經公告恢復 ===
    tc_with_match = {"matched": [
        {"content": "…", "announce_date": "115/08/17", "filed_date": "115/08/17",
         "stocks": [{"code": "2380", "name": "虹光精密工業股份有限公司", "action": "恢復交易方法"},
                    {"code": "8105", "name": "凌巨科技股份有限公司", "action": "停止買賣"}]},
    ]}
    html = render_recover_announcement_block("2380", tc_with_match)
    assert "恢復交易方法" in html and "115/08/17" in html, html
    assert "凌巨科技" not in html, html   # 同一則公告裡不相干的其他代號不能混進來
    # 2b. 沒有任何公告提到這個代號 → 空字串（不是「查無資料」這種容易被誤讀成警訊的文字）
    assert render_recover_announcement_block("9999", tc_with_match) == ""
    # 2c. trading_changes 缺檔/降級 → 空字串，不可拋例外
    assert render_recover_announcement_block("2380", {}) == ""
    assert render_recover_announcement_block("2380", None) == ""

    # === render_long_channel_block()：融資／認購權證兩管道，只用於 recover 頁籤展開列 ===
    # 1. 融資可用（可信用交易）＋ 認購權證存在，state=ok
    html = render_long_channel_block("1234", {"credit": "可信用交易"},
                                     {"state": "ok", "fetched_at": "2026-08-17", "call_codes": ["1234"]})
    assert "具融資資格" in html, html
    assert "具有效認購權證" in html, html
    assert "認售" not in html, html          # 不能把認售的文字複製貼過來

    # 2. 「停止融券」只擋放空，不擋融資買進——跟放空區塊（只認「可信用交易」）刻意不同
    html = render_long_channel_block("1234", {"credit": "停止融券"},
                                     {"state": "empty", "call_codes": []})
    assert "具融資資格" in html, html

    # 3. 「停止融資」才真的擋到融資買進，要顯示現況、不可顯示可用
    html = render_long_channel_block("1234", {"credit": "停止融資"},
                                     {"state": "empty", "call_codes": []})
    assert "具融資資格" not in html, html
    assert "停止融資" in html, html

    # 4. 非信用交易標的
    html = render_long_channel_block("1234", {"credit": "非信用交易標的"},
                                     {"state": "empty", "call_codes": []})
    assert "✗ 非融資標的" in html, html

    # 5. 該代號沒有認購權證、資料新鮮 → 顯示明確的 ✗，不迴避
    html = render_long_channel_block("9999", {"credit": "可信用交易"},
                                     {"state": "ok", "fetched_at": "2026-08-17", "call_codes": ["1234"]})
    assert "✗ 目前無有效認購權證" in html, html

    # 6. 認購權證資料降級 → 一律回退「未知」，即使代號剛好在舊清單裡也不能顯示 ✓
    html = render_long_channel_block("1234", {"credit": "可信用交易"},
                                     {"state": "degraded", "call_codes": ["1234"]})
    assert "需自行查詢" in html, html
    assert "具有效認購權證" not in html, html

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    main()
