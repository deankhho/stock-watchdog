#!/usr/bin/env python3
"""
analyze.py — S3：分級引擎（純本地）
讀 data/netvalue.json + data/official.json → data/report.json

五分級（互斥，判定順序：recover → official → predict_in → edge → margin_risk；門檻
`threshold` 依面額換算，面額10元股為5，非10元面額股依 full_delivery_threshold()）：
  recover     在官方全額交割名單 且 最新淨值 >= threshold   → 恢復候選（連兩季達標即恢復）
  official    在官方全額交割名單（淨值仍 < threshold）      → 現況
  predict_in  不在名單 且 淨值 < threshold                 → 預測下次財報後打入
  edge        threshold <= 淨值 < 6                        → 危險邊緣（市場觀察緩衝區，
                                                              非法規門檻，見 Phase 0 spec）
  margin_risk 6 <= 淨值 < 10                                → 市場觀察緩衝區；🔴 這一級不等於
                                                              「信用交易資格」——面額10元股兩者
                                                              重合，面額非10元股信用交易資格
                                                              另外看 credit_eligibility()（讀
                                                              保留盈餘，不是淨值），2026-08-20
                                                              Phase 0 修正，勿再當同一件事

用法：python analyze.py [--selftest]
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import crossings

BASE = Path(__file__).parent
NV_FILE = BASE / "data" / "netvalue.json"
OF_FILE = BASE / "data" / "official.json"
PAR_FILE = BASE / "data" / "par_value.json"
BALANCE_SHEET_FILE = BASE / "data" / "balance_sheet.json"
NV_HISTORY_DIR = BASE / "data" / "netvalue_history"
OUT = BASE / "data" / "report.json"

# 🔴 兩套時間語意，不得混用（計畫實作護欄 A）：
#   業務日期（今天是哪一天）→ Asia/Taipei 當地日曆日。GitHub runner 跑 UTC，
#     台灣凌晨 00:00-08:00 會被算成前一天，讓季別早退一季。
#   資料年齡（timestamp age）→ elapsed time 絕對時刻相減，與時區無關，
#     前端 JS 用同一套語意（Date.now() - Date.parse()）。
TPE = ZoneInfo("Asia/Taipei")

# 業務規則常數（出處見 BLUEPRINT / docs/rules.html）
NET_VALUE_FULL_DELIVERY = 5.0    # 證交所營業細則第49條；櫃買中心業務規則
NET_VALUE_NO_MARGIN = 10.0       # 有價證券得為融資融券標準
NET_VALUE_WATCH = 15.0           # 觀察池門檻（Phase B 用）；非法規門檻，是抓取/顯示範圍
REPORT_DEADLINES = [(3, 31), (5, 15), (8, 14), (11, 14)]  # 年報/Q1/Q2/Q3
# 截止日 →（季別歸屬年份偏移, 第幾季）。3/31 交的是「去年」年報，故偏移 -1。
DEADLINE_QUARTER = {(3, 31): (-1, 4), (5, 15): (0, 1), (8, 14): (0, 2), (11, 14): (0, 3)}
# 公告期＝季末次日 ~ 截止日＋3 天（早鳥公告都落在這段，實證 3523 迎輝 8/10 就交了 26Q2）
FILING_WINDOWS = [((1, 1), (4, 3)), ((4, 1), (5, 18)),
                  ((7, 1), (8, 17)), ((10, 1), (11, 17))]


def taipei_today() -> date:
    """業務用「今天」——一律台北，不吃 runner 的 UTC"""
    return datetime.now(TPE).date()


def data_age_days(iso_str: str, now: datetime = None) -> int:
    """資料年齡＝經過時間（天），非日曆日差。

    🔴 參數是「時刻」不是「日期」。無 offset 的舊字串視為 Asia/Taipei
    （歷史資料多由家用機寫入；下次成功執行就會換成帶 offset 的新格式）。
    """
    ts = datetime.fromisoformat(iso_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=TPE)
    now = now or datetime.now(TPE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TPE)
    return int((now - ts).total_seconds() // 86400)


def latest_expected_quarter(today: date = None) -> str:
    """今天理論上「應該已公告」的最新季別，如 '26Q2'（台北日曆日）。
    用途：backtest 快取失效判斷——快取比這個舊就代表沒跟上。"""
    today = today or taipei_today()
    best = None
    for y in (today.year - 1, today.year):
        for (m, d), (off, q) in DEADLINE_QUARTER.items():
            dl = date(y, m, d)
            if dl <= today and (best is None or dl > best[0]):
                best = (dl, f"{(y + off) % 100:02d}Q{q}")
    return best[1]


def in_filing_window(today: date = None) -> bool:
    """是否落在財報公告期（台北日曆日）。公告期內每天補抓一次，以免漏掉早鳥。"""
    today = today or taipei_today()
    return any(date(today.year, *s) <= today <= date(today.year, *e)
               for s, e in FILING_WINDOWS)


def days_to_next_report(today: date = None) -> tuple:
    """距最近一個財報截止日的天數與日期字串"""
    today = today or taipei_today()
    candidates = []
    for y in (today.year, today.year + 1):
        for m, d in REPORT_DEADLINES:
            dt = date(y, m, d)
            if dt >= today:
                candidates.append(dt)
    nxt = min(candidates)
    return (nxt - today).days, nxt.isoformat()


# ══════════════════════════════════════════════════════════════════════════
# Phase 0 分類語義 spec（2026-08-20 頁籤重新設計，外審3輪定案，
# 計畫全文＋外審裁決見 ~/.claude/plans/deep-stargazing-tide.md）
#
# edge/margin_risk/watch 這三層是「淨值緩衝區」，不是法規門檻本身——這是「市場觀察緩衝區」，
# 距離 threshold（全額交割門檻，已面額化）越遠風險越低，跟「能不能信用交易」是分開的兩件事：
#   對面額10元股：緩衝區跟信用交易門檻剛好都用同一個 10 元，兩軸重合，不用另外判斷
#   對面額非10元股：緩衝區只反映全額交割門檻遠近；信用交易資格另外用 credit_eligibility()
#     （見下方，讀 fetch_balance_sheet.py 的保留盈餘）判斷，不會因為淨值落在 6~10 就被當成
#     信用警戒——這是跟舊版最大的差異，舊版把「淨值6~10」跟「信用警戒」直接畫等號
#
# 資料品質 state contract：分類語義封閉的是「業務意義」，「用什麼資料算出這個分類」也要
# 講清楚。full_delivery_threshold() 面額資料缺失時靜默回退固定 5.0（既有行為，範圍外，
# 不重寫）；但這次新增的 credit_eligibility() 一律三態（可/否/未知），未知時不得顯示成
# 「不可」或悶掉不顯示，跟 gen_site.py 既有的 SBL/warrants 三值 fail-open 邏輯是同一套
# 設計語言——之後再加新的資料維度都要比照這個 contract。
# ══════════════════════════════════════════════════════════════════════════

# 🔴 15~17 元歸 safe 是刻意的（發現 F）：STOP_NET_VALUE 拉高到 17 是為了讓
# crossings.py 的「前季 ≥10、最新季 <10」判定能有前季基準，不代表 15~17 要在網站分級呈現。
# safe 不進 report.json 的 groups，但淨值與季度仍寫入 quarter_seen.json（detect_new_reports
# 迭代全部 netvalue rows，不是 report groups）。勿因「抓了卻沒用在分級」而把
# STOP_NET_VALUE 改回 12——那會讓 crossings.py 的母體重新失去前季基準。
def classify(nv: float, in_official: bool, threshold: float = NET_VALUE_FULL_DELIVERY) -> str:
    """threshold：全額交割門檻，預設 5.0（面額10元股適用）。面額非10元的個股
    應傳入面額二分之一（見 fetch_par_value.py），不可全部套固定 5.0。"""
    if in_official:
        return "recover" if nv >= threshold else "official"
    if nv < threshold:
        return "predict_in"
    if nv < 6.0:
        return "edge"
    if nv < NET_VALUE_NO_MARGIN:
        return "margin_risk"
    if nv < NET_VALUE_WATCH:
        return "watch"
    return "safe"


def full_delivery_threshold(code: str, par: dict) -> float:
    """全額交割門檻＝面額二分之一（營業細則第49條：淨值低於股本二分之一）。
    查無面額資料（未收錄／par_value.json 整包缺失或降級）一律回退固定 5.0（面額10元股適用，
    佔母體98%以上），不可用 0 或 None 之類的假值硬套。par 值可能是新 schema
    {"par":..,"shares":..} 或遷移期間殘留的舊 scalar float，兩者都要能處理。"""
    p = par.get(code)
    if isinstance(p, dict):
        p = p.get("par")
    return p / 2 if p else NET_VALUE_FULL_DELIVERY


def credit_eligibility(code: str, par: dict, balance_sheet: dict):
    """面額非10元股的信用交易（融資融券）資格——《有價證券得為融資融券標準》第2/4條：
    面額10元股門檻是「淨值≥票面(10元)」（既有 margin_risk/watch 分級已涵蓋，這裡不重複判斷，
    回 None）；無面額或非10元面額股門檻是「最近一個會計年度決算無累積虧損」，跟淨值無關。

    fetch_balance_sheet.py 抓的「保留盈餘」是淨額（正負併記，官方沒有拆開累積虧損/未分配盈餘
    兩個獨立欄位）：2026-08-20 用 9 檔真實股票交叉驗證過（台積電/聯發科正值健康；
    華義*/永悅健康-創/合騏*/康霈* 皆負值，且皆為淨值極低的邊緣/預測打入層股票，跟已知現況
    相符；KY 股鼎固-KY/艾美特-KY 正常收錄，非結構性缺漏）——負值＝有累積虧損，正值/零＝無。

    回傳 "可"/"否"/"未知"，或 None（代表面額10元股，這欄不適用，不該問這個問題）。
    None 跟 "未知" 意義不同：None＝這檔股票不該顯示這欄；"未知"＝該顯示但目前答不出來
    （balance_sheet 資料降級，或這檔本季財報尚未申報／查無資料，如 KY 股個案曾觀察到的
    申報延遲，不代表整個資料源排除該類公司）——"未知" 一律不得顯示成 "否"（fail-open，
    跟 gen_site.py 既有 SBL/warrants 三值邏輯同一套設計語言，見 Phase 0 spec）。"""
    p = par.get(code)
    face = p.get("par") if isinstance(p, dict) else p
    if face is None or face == 10.0:
        return None
    if balance_sheet.get("state") != "ok":
        return "未知"
    row = balance_sheet.get("rows", {}).get(code)
    if row is None:
        return "未知"
    return "否" if row["retained_earnings"] < 0 else "可"


def _shares_for(code: str, par: dict):
    """par 資料裡的已發行股數；查無資料或是舊 scalar schema（遷移期間殘留快取，
    沒有股數欄位）一律回 None，不可拋例外或猜測。"""
    p = par.get(code)
    return p.get("shares") if isinstance(p, dict) else None


RECOVER_NET_VALUE_TOTAL = 300_000_000   # 營業細則第49條：淨值總額「逾」3億元（嚴格大於）


def recover_eligibility(code: str, market: str, threshold: float, cur_nv: float, nv_q: str,
                        hist_rows: list, par: dict) -> tuple:
    """全額交割恢復資格市場感知判定（v4 計畫，經 3 輪外審定案）。
    threshold 必須是呼叫端已經算好的 full_delivery_threshold(code, par) 回傳值，
    不可另外重算或傳入其他來源——分級用的門檻與這裡判定用的門檻永遠是同一個數字。

    上市（營業細則第49條）：最新兩個相鄰季度皆需 nv>=threshold，且淨值總額（以目前
    股數估算）逾3億元；上櫃（業務規則第12條）：最新一季 nv>=threshold 且較前期增加，
    無3億元下限。

    🔴 關鍵不變式：3億元子項沒過或無法確認時絕不能回傳 eligible。

    回傳 (state, detail)，state ∈ {"eligible","not_yet","unknown"}，detail 一律有值。"""
    calib = crossings.calibrate_history(code, cur_nv, nv_q, hist_rows)
    if calib is None:
        return "unknown", "面額校準資料不足，無法確認恢復資格"

    rows = calib["rows"]
    idx = crossings.quarter_index(nv_q)
    prev_candidates = [(q, v) for q, v in rows.items() if crossings.quarter_index(q) == idx - 1]
    if len(rows) < 2 or not prev_candidates:
        return "unknown", "缺乏相鄰兩季淨值資料，無法確認恢復資格"
    prev_q, prev_nv = prev_candidates[0]
    cur_nv_c = rows[nv_q]

    if market == "上櫃":
        if cur_nv_c >= threshold and cur_nv_c > prev_nv:
            return "eligible", f"淨值條件已符合（{prev_q}→{nv_q} 較前期增加）"
        return "not_yet", f"每股淨值未達標或未較前期（{prev_q}）增加"

    # 上市（含未知市場別一律比照上市邏輯，因為上市條件較嚴格，不會誤放行）
    if not (prev_nv >= threshold and cur_nv_c >= threshold):
        return "not_yet", f"每股淨值尚未連續兩季（{prev_q}/{nv_q}）達標"

    shares = _shares_for(code, par)
    if shares is None:
        return "unknown", "每股淨值已達標，缺股數資料無法確認3億元門檻"

    total_prev = prev_nv * shares
    total_cur = cur_nv_c * shares
    if total_prev <= RECOVER_NET_VALUE_TOTAL or total_cur <= RECOVER_NET_VALUE_TOTAL:
        return "not_yet", f"每股淨值已達標，但淨值總額（{prev_q}/{nv_q}，以目前股數估算）未逾3億元"
    return "eligible", (f"淨值條件已符合（{prev_q}/{nv_q}，以目前股數估算，"
                        "個股曾減資/增資者可能失真）")


def selftest():
    assert classify(4.2, True) == "official"
    assert classify(5.5, True) == "recover"
    assert classify(4.2, False) == "predict_in"
    assert classify(5.5, False) == "edge"
    assert classify(8.0, False) == "margin_risk"
    assert classify(12.0, False) == "watch"
    assert classify(14.99, False) == "watch"
    assert classify(15.0, False) == "safe"
    assert classify(16.5, False) == "safe"
    # 面額非10元的個股：全額交割門檻是面額二分之一，不是固定5元（2026-08-17修正，
    # 查證營業細則第49條：「淨值已低於財務報告所列示股本二分之一」）
    assert classify(0.6, False, threshold=0.5) == "edge"          # 高於自己門檻(0.5)，不是 predict_in
    assert classify(0.4, False, threshold=0.5) == "predict_in"    # 低於自己門檻(0.5)
    assert classify(3.0, True, threshold=0.5) == "recover"        # 面額1元股，淨值3遠高於門檻0.5
    assert classify(0.3, True, threshold=0.5) == "official"       # 面額1元股仍全額交割中
    assert classify(4.2, False) == "predict_in"                   # 沒給 threshold 時預設仍是5.0，行為不變
    # full_delivery_threshold()：查得到面額用面額二分之一，查不到／未收錄一律回退5.0
    # 新 schema {code: {"par":..,"shares":..}} 與舊 schema {code: float}（遷移期間
    # 可能殘留的快取）都要能處理
    assert full_delivery_threshold("3086", {"3086": {"par": 1.0, "shares": 100}}) == 0.5
    assert full_delivery_threshold("2330", {"3086": {"par": 1.0, "shares": 100}}) == 5.0  # 沒收錄，回退固定值
    assert full_delivery_threshold("2330", {}) == 5.0                # par 資料整包缺失，回退固定值
    assert full_delivery_threshold("3086", {"3086": 1.0}) == 0.5     # 舊 scalar schema 仍可用

    # === recover_eligibility()：全額交割恢復資格市場感知精確判定 ===
    def hrow(date_, quarter, net_value):
        return {"date": date_, "quarter": quarter, "net_value": net_value}

    # 1. 上市，兩期都過，3億元也過 → eligible
    state, detail = recover_eligibility(
        "1111", "上市", 5.0, 7.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 6.0), hrow("2026-06-30", "26Q2", 7.0)],
        {"1111": {"par": 10.0, "shares": 100_000_000}})
    assert state == "eligible", (state, detail)

    # 2. 上市，每股過但3億元剛好等於3億（邊界，嚴格大於才算過）→ not_yet
    state, detail = recover_eligibility(
        "2222", "上市", 5.0, 7.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 6.0), hrow("2026-06-30", "26Q2", 7.0)],
        {"2222": {"par": 10.0, "shares": 50_000_000}})   # 26Q1: 6.0*5000萬=3億整
    assert state == "not_yet", (state, detail)

    # 3. 上市，每股過但3億元其中一期未過 → not_yet
    state, detail = recover_eligibility(
        "3333", "上市", 5.0, 7.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 6.0), hrow("2026-06-30", "26Q2", 7.0)],
        {"3333": {"par": 10.0, "shares": 40_000_000}})   # 兩期都 <=3億
    assert state == "not_yet", (state, detail)

    # 4. 上市，每股過但缺 shares → unknown（不可是 eligible）
    state, detail = recover_eligibility(
        "4444", "上市", 5.0, 7.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 6.0), hrow("2026-06-30", "26Q2", 7.0)],
        {"4444": {"par": 10.0, "shares": None}})
    assert state == "unknown", (state, detail)

    # 5. 上市，每股條件一期沒過（26Q1未達5.0門檻）→ not_yet
    state, detail = recover_eligibility(
        "5555", "上市", 5.0, 7.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 4.0), hrow("2026-06-30", "26Q2", 7.0)],
        {"5555": {"par": 10.0, "shares": 100_000_000}})
    assert state == "not_yet", (state, detail)

    # 6. 上市，非相鄰季（26Q1→26Q3，中間缺26Q2）→ unknown
    state, detail = recover_eligibility(
        "6666", "上市", 5.0, 7.0, "26Q3",
        [hrow("2026-03-31", "26Q1", 6.0), hrow("2026-09-30", "26Q3", 7.0)],
        {"6666": {"par": 10.0, "shares": 100_000_000}})
    assert state == "unknown", (state, detail)

    # 7. 上市，只一季資料 → unknown
    state, detail = recover_eligibility(
        "7777", "上市", 5.0, 7.0, "26Q2",
        [hrow("2026-06-30", "26Q2", 7.0)],
        {"7777": {"par": 10.0, "shares": 100_000_000}})
    assert state == "unknown", (state, detail)

    # 8. 上櫃，最新一期達標但未較前期增加 → not_yet
    state, detail = recover_eligibility(
        "8888", "上櫃", 5.0, 5.5, "26Q2",
        [hrow("2026-03-31", "26Q1", 6.0), hrow("2026-06-30", "26Q2", 5.5)],
        {})
    assert state == "not_yet", (state, detail)

    # 9. 上櫃，最新一期達標且較前期增加 → eligible（上櫃無3億元子項）
    state, detail = recover_eligibility(
        "9999", "上櫃", 5.0, 6.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 5.0), hrow("2026-06-30", "26Q2", 6.0)],
        {})
    assert state == "eligible", (state, detail)

    # 10. 倍率不可信（ratio 比對不到 PAR_FACTORS）→ unknown
    state, detail = recover_eligibility(
        "1010", "上市", 5.0, 8.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 274.0), hrow("2026-06-30", "26Q2", 274.4)],
        {"1010": {"par": 10.0, "shares": 100_000_000}})
    assert state == "unknown", (state, detail)

    # 11. par 資料是舊 scalar 格式（遷移期間殘留快取）→ shares 視為缺失 → unknown
    state, detail = recover_eligibility(
        "1011", "上市", 5.0, 7.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 6.0), hrow("2026-06-30", "26Q2", 7.0)],
        {"1011": 10.0})
    assert state == "unknown", (state, detail)
    d, s = days_to_next_report(date(2026, 7, 6))
    assert s == "2026-08-14" and d == 39, (d, s)

    # === credit_eligibility()：面額非10元股信用交易資格（Phase 0，2026-08-20）===
    bs_ok = {"state": "ok", "rows": {
        "3086": {"retained_earnings": -536.0, "quarter": "26Q2", "name": "華義*"},
        "2923": {"retained_earnings": 29406627.0, "quarter": "26Q2", "name": "鼎固-KY"},
    }}
    # 面額10元股（或面額資料缺失回退預設值）→ None，這欄不適用，不是"未知"
    assert credit_eligibility("2330", {"2330": {"par": 10.0}}, bs_ok) is None
    assert credit_eligibility("2330", {}, bs_ok) is None            # 面額資料缺失，回退視同10元
    # 面額非10元、保留盈餘負值 → "否"
    assert credit_eligibility("3086", {"3086": {"par": 1.0}}, bs_ok) == "否"
    # 面額非10元、保留盈餘正值 → "可"（KY股一併驗證，非結構性缺漏）
    assert credit_eligibility("2923", {"2923": {"par": 5.0}}, bs_ok) == "可"
    # balance_sheet 整包降級 → "未知"（不可悶成"否"）
    assert credit_eligibility("3086", {"3086": {"par": 1.0}},
                              {"state": "degraded", "rows": {}}) == "未知"
    # 面額非10元但該股本季查無資料（如觀察到的個股申報延遲）→ "未知"，不是"否"
    assert credit_eligibility("9999", {"9999": {"par": 2.5}}, bs_ok) == "未知"
    # 面額資料是舊 scalar 格式（遷移期間殘留快取）仍可用
    assert credit_eligibility("3086", {"3086": 1.0}, bs_ok) == "否"

    # --- 資料年齡：elapsed time（絕對時刻相減），不是台北日曆日差 ---
    now = datetime(2026, 8, 10, 11, 55, 21, tzinfo=TPE)
    assert data_age_days("2026-07-14T11:55:21+08:00", now) == 27
    # 差一秒不足整天 → 仍算 26 天（證明是 floor 經過時間，不是日期相減）
    assert data_age_days("2026-07-14T11:55:22+08:00", now) == 26
    # 無 offset 的舊字串視為 Asia/Taipei，結果與帶 offset 相同（向後相容）
    assert data_age_days("2026-07-14T11:55:21", now) == 27
    # 同一時刻的不同表示法必須算出相同年齡（證明是絕對時刻運算）
    assert data_age_days("2026-07-14T03:55:21+00:00", now) == 27

    # --- 業務日期：Asia/Taipei 日曆日 ---
    assert latest_expected_quarter(date(2026, 8, 10)) == "26Q1"
    assert latest_expected_quarter(date(2026, 8, 14)) == "26Q2"
    assert latest_expected_quarter(date(2026, 4, 1)) == "25Q4"
    assert latest_expected_quarter(date(2026, 1, 5)) == "25Q3"
    assert in_filing_window(date(2026, 7, 1)) is True
    assert in_filing_window(date(2026, 8, 17)) is True
    assert in_filing_window(date(2026, 9, 25)) is False

    print("selftest OK")


QSEEN_FILE = BASE / "data" / "quarter_seen.json"


def stock_status(code: str, is_full: bool, disposal: dict, margin: dict, market: str) -> dict:
    """官方現況（S8）：全額交割/處置/信用交易實際狀態
    信用註記（MI_MARGN 官方說明實測）：O=停止融資、X=停止融券、!=停止買賣；
    上市不在餘額表＝非融資融券標的；上櫃全板在表、看 Note 內 O/X。"""
    st = {"full_delivery": is_full}
    d = disposal.get(code)
    st["disposal"] = {"reason": d["reason"][:20], "period": d["period"]} if d else None
    m = margin.get(code)
    if is_full:
        credit = "停止信用（全額交割）"
    elif m is None:
        credit = "非信用交易標的"   # 雙市場同義：停止中的股仍會留在餘額表（8444實證），不在表=非標的
    else:
        mark = m["mark"]
        has_o, has_x = "O" in mark, "X" in mark
        if "!" in mark:
            credit = "停止買賣"
        elif has_o and has_x:
            credit = "停資停券"
        elif has_o:
            credit = "停止融資"
        elif has_x:
            credit = "停止融券"
        else:
            credit = "可信用交易"
    st["credit"] = credit
    return st


def detect_new_reports(rows: list) -> dict:
    """偵測「交出新財報」：與上次記錄的財報季度比對（quarter_seen.json）
    回傳 {code: {"delta": Δ淨值, "prev_nv":, "prev_q":, "crossing": 警示, "since": 首見日}}
    首次建檔（無基準）不標記，只建基準。"""
    seen = json.loads(QSEEN_FILE.read_text()) if QSEEN_FILE.exists() else {}
    first_init = not seen
    today = taipei_today().isoformat()
    new_map = {}
    for r in rows:
        code, q, nv = r["code"], r.get("nv_quarter", ""), r["net_value"]
        prev = seen.get(code)
        if prev and q and q != prev["quarter"]:
            seen[code] = {"quarter": q, "nv": nv, "first_seen": today,
                          "prev_nv": prev["nv"], "prev_q": prev["quarter"]}
        elif not prev:
            seen[code] = {"quarter": q, "nv": nv, "first_seen": today}
        # 🆕 標記維持 14 天（新財報季內給使用者充分注意時間）
        cur = seen.get(code, {})
        if not first_init and cur.get("prev_q") and \
           (taipei_today() - date.fromisoformat(cur["first_seen"])).days <= 14:
            new_map[code] = {"delta": round(cur["nv"] - cur["prev_nv"], 2),
                             "prev_nv": cur["prev_nv"], "prev_q": cur["prev_q"],
                             "since": cur["first_seen"],
                             "crossing": (
                                 "跌破5元（恐列全額交割）" if cur["prev_nv"] >= 5 > cur["nv"] else
                                 "回升5元以上（恢復條件累計中）" if cur["prev_nv"] < 5 <= cur["nv"] else
                                 "跌破10元（恐停信用交易）" if cur["prev_nv"] >= 10 > cur["nv"] else
                                 "回升10元以上（信用恢復條件）" if cur["prev_nv"] < 10 <= cur["nv"] else None)}
    QSEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1))
    return new_map


def main():
    if "--selftest" in sys.argv:
        selftest()
        return

    nv_data = json.loads(NV_FILE.read_text())
    of_data = json.loads(OF_FILE.read_text())
    official_codes = {x["code"] for x in of_data["full_delivery"]}
    official_by_code = {x["code"]: x for x in of_data["full_delivery"]}
    market_map = of_data["market_map"]
    try:
        par_data = (json.loads(PAR_FILE.read_text()) if PAR_FILE.exists()
                   else {"state": "empty", "par": {}})
    except Exception:
        par_data = {"state": "empty", "par": {}}
    par = par_data.get("par", {})
    try:
        balance_sheet = (json.loads(BALANCE_SHEET_FILE.read_text()) if BALANCE_SHEET_FILE.exists()
                        else {"state": "empty", "rows": {}})
    except Exception:
        balance_sheet = {"state": "empty", "rows": {}}
    # 🔴 若整批仍是舊 scalar 格式，代表新版 fetch_par_value.py 已部署但資料還沒
    # 重新抓過一次成功——印一行 warning 避免 isinstance 防呆悄悄把「schema 遷移
    # 其實沒發生」吞成看起來正常的大量 unknown（fail-safe 變 fail-silent）。
    if par and not any(isinstance(v, dict) for v in par.values()):
        print("::warning::par_value.json 仍是舊 scalar schema，恢復資格3億元子項將全部判 unknown")

    history = {}
    if NV_HISTORY_DIR.exists():
        for fp in NV_HISTORY_DIR.glob("*.json"):
            history[fp.stem] = json.loads(fp.read_text())

    days, next_dl = days_to_next_report()
    new_reports = detect_new_reports(nv_data["rows"])
    groups = {"predict_in": [], "edge": [], "margin_risk": [],
              "recover": [], "official": [], "watch": []}

    seen = set()
    for r in nv_data["rows"]:
        threshold = full_delivery_threshold(r["code"], par)
        cat = classify(r["net_value"], r["code"] in official_codes, threshold)
        if cat == "safe":
            continue
        # KY (* in name): FinMind net_value unreliable -> skip predict_in
        if '*' in r.get('name', '') and cat == 'predict_in':
            continue
        item = dict(r)
        if r["code"] in new_reports:
            item["new_report"] = new_reports[r["code"]]
        item["status"] = stock_status(r["code"], r["code"] in official_codes,
                                      of_data.get("disposal", {}),
                                      of_data.get("margin_status", {}),
                                      market_map.get(r["code"], ""))
        item["market"] = market_map.get(r["code"], "")
        item["gap"] = round(r["net_value"] - threshold, 2)
        item["fd_threshold"] = threshold
        item["credit_eligibility"] = credit_eligibility(r["code"], par, balance_sheet)
        item["goodinfo_url"] = f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={r['code']}"
        if cat == "recover":
            hist_rows = history.get(r["code"], {}).get("rows", [])
            state, detail = recover_eligibility(
                r["code"], item["market"], threshold, r["net_value"], r.get("nv_quarter", ""),
                hist_rows, par)
            item["recover_status"] = {"state": state, "detail": detail}
        groups[cat].append(item)
        seen.add(r["code"])

    # 官方名單中沒出現在淨值排行的（排行只抓到 12 元，理論上都會在；防漏）
    for x in of_data["full_delivery"]:
        if x["code"] not in seen:
            groups["official"].append({
                "code": x["code"], "name": x["name"], "market": x["market"],
                "price": None, "net_value": None, "nv_quarter": "",
                "gap": None,
                "goodinfo_url": f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={x['code']}",
                "note": "淨值排行未見（可能停止交易）"})

    # 🔴 年齡一律算自「資料最後一次成功抓取的時間」（netvalue.json / official.json
    # 的 fetched_at），禁止改用 report 產生時間／workflow 執行時間／commit 時間——
    # 否則會變成「CI 今天跑成功→看起來新鮮→其實裡面是 27 天前的淨值」。
    # 此指標依賴 fetch_goodinfo.py「失敗即退出、不覆寫舊檔」的行為，改動該行為會使本指標失效。
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(TPE).isoformat(),
        "nv_fetched_at": nv_data["fetched_at"],
        "official_fetched_at": of_data["fetched_at"],
        "nv_age_days": data_age_days(nv_data["fetched_at"]),
        "official_age_days": data_age_days(of_data["fetched_at"]),
        "degraded": of_data.get("degraded", {}),
        "days_to_report": days, "next_report_deadline": next_dl,
        "new_reports_count": len(new_reports),
        "new_reports_crossings": sum(1 for v in new_reports.values() if v["crossing"]),
        "groups": groups,
        "tpex_other_flags": of_data.get("tpex_other_flags", []),
    }, ensure_ascii=False, indent=1))
    print(f"分級完成 → {OUT}")
    for k, v in groups.items():
        print(f"  {k}: {len(v)} 檔")
    print(f"下一財報截止 {next_dl}（{days} 天後）")


if __name__ == "__main__":
    main()
