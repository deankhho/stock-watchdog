#!/usr/bin/env python3
"""
crossings.py — 掉落判定引擎：偵測「前季淨值 ≥10、最新季 <10」的最近一次跨越
不吃 report、不吃 backtest.json，母體與歷史資料來源見 detect_margin_drops() docstring。
用法：python crossings.py --selftest
"""

import sys

CROSS_NV = 10.0
SUSPECT_RATIO = 3.0
SOURCE_CONFLICT_TOLERANCE = 0.5
PAR_FACTORS = (2, 4, 5, 10, 20)   # 合理面額倍率：面額5→2、2.5→4、1→10、0.5→20

# unknown 判定優先序（先報「管線出問題」再報「資料本來就沒有」）
UNKNOWN_PRIORITY = ["fetch_failed", "budget_exhausted", "quarter_mismatch", "not_adjacent", "no_prev"]


def quarter_index(q: str) -> int:
    """'26Q1' → 105（年份是西元後兩碼，不是民國，見發現 U；年*4+季，供相鄰判定）"""
    year = int(q[:2])
    quarter = int(q[3])
    return year * 4 + quarter


def low_netvalue_pool(netvalue: dict) -> list:
    """母體：netvalue.json 中 net_value < CROSS_NV 的全部列（不經 report groups，
    避免 KY * 股被 analyze.py 排除掉造成落差，見發現 T）。
    crossings／fetch_netvalue_history／fetch_audit 三處共用同一函式算出，
    避免母體定義各自漂移（見驗證 §4「股池一致性斷言」的動機）。"""
    return [r for r in netvalue["rows"] if r["net_value"] < CROSS_NV]


def _latest_by_quarter(hist_rows: list) -> dict:
    """同季多筆（更正申報）→ 取 date 最新的一筆，不可當成兩季"""
    by_q = {}
    for x in hist_rows:
        q = x["quarter"]
        if q not in by_q or x["date"] > by_q[q]["date"]:
            by_q[q] = x
    return by_q


def calibrate_history(code: str, cur_nv: float, nv_q: str, hist_rows: list):
    """建立面額校準 factor，並用它校準 hist_rows 全部已知季度。
    只拿「同季」FinMind 值與 goodinfo cur_nv 比較算 factor（不可跨季比，見發現 W），
    抽自 detect_margin_drops() 既有邏輯，供它與 recover_eligibility() 共用。

    hist_rows 為空、nv_q 不在其中（quarter_mismatch）、或 ratio 對不到
    PAR_FACTORS（unreliable）→ None。
    成功 → {"factor": float, "rows": {quarter: calibrated_net_value}}
    （全部已知季度都校準，不只判定用的那兩季；缺季/相鄰性判斷交給呼叫端）。"""
    if not hist_rows:
        return None
    by_q = _latest_by_quarter(hist_rows)
    if nv_q not in by_q:
        return None
    finmind_cur = by_q[nv_q]["net_value"]
    if cur_nv == 0:
        factor = 1
    else:
        ratio = finmind_cur / cur_nv
        if ratio > 1.5 or ratio < 0.67:
            snapped = round(ratio)
            if snapped in PAR_FACTORS:
                factor = snapped
            else:
                return None
        else:
            factor = 1
    rows = {q: round(v["net_value"] / factor, 2) for q, v in by_q.items()}
    return {"factor": factor, "rows": rows}


def detect_margin_drops(netvalue: dict, history: dict, status: dict, par: dict = None) -> dict:
    """該檔最新相鄰兩季：前季淨值 ≥10、後季 <10。
    history/status 來自 fetch_netvalue_history.py，不是 backtest.json（第 4 節）。

    🔴 不吃 report：
    這個判定不該依賴 UI 分級，母體直接從 netvalue 算，避免多一個隱性耦合。

    與 backtest.detect_events 的 margin_stop 不同（見發現 D）：
    - 不排除同時跌破 5 元的個案（11→4 也算掉落）
    - 只認序列最後一組相鄰季，不回溯歷史事件
    - 兩季必須相鄰（如 26Q1→26Q2）；中間缺季一律 unknown，不跨季比較

    參數：
      netvalue — data/netvalue.json 內容（{"rows": [{code,name,market,net_value,nv_quarter}, ...]}）
      history  — {code: {"fetched_at": ..., "rows": [{date,quarter,net_value}, ...]}}
                 （來自 fetch_netvalue_history.py 的 data/netvalue_history/<code>.json，只含最新 3 季）
      status   — data/netvalue_history_status.json 內容，取 incomplete_codes 分辨
                 budget_exhausted（沒輪到）與 fetch_failed（抓了但失敗）
      par      — data/par_value.json 的 "par" 欄（2026-08-21 新增，可省略＝沿用舊行為）：
                 《有價證券得為融資融券標準》第2/4條，面額10元股門檻是淨值≥票面(10元)，
                 但面額非10元股門檻是「有無累積虧損」，跟淨值10元完全無關——面額已知且
                 非10元的股票，一律回 data_state="not_applicable"，不可套用淨值10元判斷
                 出「確認掉落」，那對這些股票不是真的法規事件（見 analyze.py::
                 credit_eligibility()，同一批股票的信用交易資格應該看那邊，不是這裡）。
                 面額查不到（par 為 None 或整包缺失）沿用舊行為，當成面額10元處理——
                 跟 full_delivery_threshold() 的既有回退慣例一致。
    """
    par = par or {}
    incomplete = status.get("incomplete_codes", {}) if status else {}
    universe_rows = low_netvalue_pool(netvalue)
    universe = len(universe_rows)

    rows = []
    counts = {"confirmed": 0, "no_drop": 0, "unknown": 0,
              "unreliable": 0, "suspect": 0, "source_conflict": 0, "not_applicable": 0}
    unknown_reasons = {}

    def add_row(code, name, market, nv_q, data_state, reason=None,
                from_q=None, prev_nv=None, cur_nv=None, trail=None):
        counts[data_state] += 1
        if data_state == "unknown":
            unknown_reasons[reason] = unknown_reasons.get(reason, 0) + 1
        rows.append({"code": code, "name": name, "market": market,
                     "data_state": data_state, "reason": reason,
                     "from_q": from_q, "to_q": nv_q,
                     "prev_nv": prev_nv, "cur_nv": cur_nv,
                     "trail": trail or []})

    for r in universe_rows:
        code, name, market = r["code"], r.get("name", ""), r.get("market", "")
        cur_nv, nv_q = r["net_value"], r.get("nv_quarter", "")

        p = par.get(code)
        face = p.get("par") if isinstance(p, dict) else p
        if face is not None and face != 10.0:
            add_row(code, name, market, nv_q, "not_applicable")
            continue

        if code in incomplete:
            reason = incomplete[code]
            reason = reason if reason in ("fetch_failed", "budget_exhausted") else "budget_exhausted"
            add_row(code, name, market, nv_q, "unknown", reason)
            continue

        h = history.get(code)
        hist_rows = h.get("rows", []) if h else []
        calib = calibrate_history(code, cur_nv, nv_q, hist_rows)
        if calib is None:
            # calibrate_history() 只回 None，不分原因；為維持既有分項計數，這裡用
            # 廉價的重新檢查判斷是哪一種（不重算 factor/ratio，只是分類已失敗的原因）。
            if not hist_rows:
                add_row(code, name, market, nv_q, "unknown", "no_prev")
            elif nv_q not in _latest_by_quarter(hist_rows):
                add_row(code, name, market, nv_q, "unknown", "quarter_mismatch")
            else:
                add_row(code, name, market, nv_q, "unreliable")
            continue

        # 多季回溯顯示用（不影響判定）：calibrate_history() 已把 by_q 全部季度用同一組
        # factor 校準好，這裡只需要排序＋標 confidence。
        # 🔴 factor 只在 nv_q 這一季有跨來源校準依據；nv_q 與緊鄰前一季（判定用的那一對）
        # 是 Phase A 正式邏輯已驗證的範圍，再更早的季度是「套用同一比例回推，未逐季驗證」
        # （同計畫發現 W續：無法排除更早期間曾發生面額變更），trail 逐項標 confidence 讓
        # 前端能視覺區分，不可讓使用者誤以為整條 trail 都跟判定同等可信。
        idx = quarter_index(nv_q)
        rows_calibrated = calib["rows"]
        trail = [{"quarter": q, "net_value": v,
                 "confidence": "high" if quarter_index(q) >= idx - 1 else "extrapolated"}
                for q, v in sorted(rows_calibrated.items(), key=lambda kv: quarter_index(kv[0]))]

        # 只有當季自己一筆（無任何前季資料）→ no_prev；
        # 有前季但不是緊鄰前一季（中間缺季）→ not_adjacent
        if len(rows_calibrated) < 2:
            add_row(code, name, market, nv_q, "unknown", "no_prev", trail=trail)
            continue
        prev_candidates = [(q, v) for q, v in rows_calibrated.items() if quarter_index(q) == idx - 1]
        if not prev_candidates:
            add_row(code, name, market, nv_q, "unknown", "not_adjacent", trail=trail)
            continue
        prev_q, prev_nv = prev_candidates[0]
        cur_nv_calibrated = rows_calibrated[nv_q]

        # 可信度守門：prev/cur 任一 <=0 或符號翻轉 → suspect（不算倍率，除零／負值防呆）
        if prev_nv <= 0 or cur_nv_calibrated <= 0 or (prev_nv > 0) != (cur_nv_calibrated > 0):
            add_row(code, name, market, nv_q, "suspect",
                    from_q=prev_q, prev_nv=prev_nv, cur_nv=cur_nv_calibrated,
                    trail=trail)
            continue
        ratio2 = max(prev_nv, cur_nv_calibrated) / min(prev_nv, cur_nv_calibrated)
        if ratio2 > SUSPECT_RATIO:
            add_row(code, name, market, nv_q, "suspect",
                    from_q=prev_q, prev_nv=prev_nv, cur_nv=cur_nv_calibrated,
                    trail=trail)
            continue

        # source_conflict：goodinfo 當期值與 FinMind 同季校準值，需落在 10 元同一側
        # 才算一致；容差 ≤0.5 元
        if abs(cur_nv_calibrated - cur_nv) > SOURCE_CONFLICT_TOLERANCE and \
           (cur_nv_calibrated >= CROSS_NV) != (cur_nv >= CROSS_NV):
            add_row(code, name, market, nv_q, "source_conflict",
                    from_q=prev_q, prev_nv=prev_nv, cur_nv=cur_nv_calibrated,
                    trail=trail)
            continue

        data_state = "confirmed" if prev_nv >= CROSS_NV > cur_nv_calibrated else "no_drop"
        add_row(code, name, market, nv_q, data_state,
                from_q=prev_q, prev_nv=prev_nv, cur_nv=cur_nv_calibrated,
                trail=trail)

    return {"rows": rows, "universe": universe, "counts": counts,
            "unknown_reasons": unknown_reasons}


def margin_risk_trend(code: str, cur_nv: float, nv_q: str, hist_rows: list) -> dict:
    """margin_risk 頁籤（6<=nv<10）淨值趨勢：本季 vs 上季比較，不是門檻達成判準
    （2026-08-21 Phase D 執行前修正：信用交易恢復是單季檢查，沒有 recover_eligibility()
    那種「連兩季/較前期增加」複雜度；margin_risk tier 定義本身 nv<10，不可能出現「已達標」
    這個結構性事實，原計畫的三態達標分組設計不成立，改成單純的趨勢方向）。

    重用 calibrate_history() 的面額校準機制（跟 detect_margin_drops() 同一套，避免面額不同
    股票的歷史淨值被誤判方向），只是最終分類條件從「是否跨越10元」換成「本季是否高於上季」。

    回傳 {"state": "up"|"down"|"flat"|"unknown", "reason": str|None,
          "prev_q": str|None, "prev_nv": float|None, "cur_nv": float}
    state 判定（沿用 detect_margin_drops() 同款防呆，suspect/source_conflict 情境一律 unknown，
    不可讓校準異常的資料被誤判成明確的漲跌趨勢）："""
    calib = calibrate_history(code, cur_nv, nv_q, hist_rows)
    if calib is None:
        reason = "no_prev" if not hist_rows else (
            "quarter_mismatch" if nv_q not in _latest_by_quarter(hist_rows) else None)
        return {"state": "unknown", "reason": reason or "unreliable",
               "prev_q": None, "prev_nv": None, "cur_nv": cur_nv}

    idx = quarter_index(nv_q)
    rows_calibrated = calib["rows"]
    if len(rows_calibrated) < 2:
        return {"state": "unknown", "reason": "no_prev",
               "prev_q": None, "prev_nv": None, "cur_nv": cur_nv}
    prev_candidates = [(q, v) for q, v in rows_calibrated.items() if quarter_index(q) == idx - 1]
    if not prev_candidates:
        return {"state": "unknown", "reason": "not_adjacent",
               "prev_q": None, "prev_nv": None, "cur_nv": cur_nv}
    prev_q, prev_nv = prev_candidates[0]
    cur_nv_calibrated = rows_calibrated[nv_q]

    if prev_nv <= 0 or cur_nv_calibrated <= 0 or (prev_nv > 0) != (cur_nv_calibrated > 0):
        return {"state": "unknown", "reason": "suspect",
               "prev_q": prev_q, "prev_nv": prev_nv, "cur_nv": cur_nv_calibrated}
    ratio2 = max(prev_nv, cur_nv_calibrated) / min(prev_nv, cur_nv_calibrated)
    if ratio2 > SUSPECT_RATIO:
        return {"state": "unknown", "reason": "suspect",
               "prev_q": prev_q, "prev_nv": prev_nv, "cur_nv": cur_nv_calibrated}
    if abs(cur_nv_calibrated - cur_nv) > SOURCE_CONFLICT_TOLERANCE and \
       (cur_nv_calibrated >= CROSS_NV) != (cur_nv >= CROSS_NV):
        return {"state": "unknown", "reason": "source_conflict",
               "prev_q": prev_q, "prev_nv": prev_nv, "cur_nv": cur_nv_calibrated}

    if cur_nv_calibrated > prev_nv:
        state = "up"
    elif cur_nv_calibrated < prev_nv:
        state = "down"
    else:
        state = "flat"
    return {"state": state, "reason": None,
           "prev_q": prev_q, "prev_nv": prev_nv, "cur_nv": cur_nv_calibrated}


def selftest():
    def hrow(date_, quarter, net_value):
        return {"date": date_, "quarter": quarter, "net_value": net_value}

    # === 0. calibrate_history()：純計算，抽出後供 detect_margin_drops／
    #    recover_eligibility 共用 ===
    # 0a. 同面額（ratio 在 [0.67,1.5] 內）→ factor=1，全部已知季度都校準（原樣，只是取整）
    calib = calibrate_history("1111", 9.5, "26Q2",
        [hrow("2025-12-31", "25Q4", 10.5), hrow("2026-03-31", "26Q1", 10.2),
         hrow("2026-06-30", "26Q2", 9.5)])
    assert calib == {"factor": 1, "rows": {"25Q4": 10.5, "26Q1": 10.2, "26Q2": 9.5}}, calib

    # 0b. 面額異常股：finmind 值恆為 goodinfo 的 10 倍 → factor=10，全部季度校準
    calib = calibrate_history("3086", 0.95, "26Q1",
        [hrow("2025-12-31", "25Q4", 10.5), hrow("2026-03-31", "26Q1", 9.5)])
    assert calib == {"factor": 10, "rows": {"25Q4": 1.05, "26Q1": 0.95}}, calib

    # 0c. ratio 比對不到 PAR_FACTORS → None（不可信）
    calib = calibrate_history("7777", 8.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 274.0), hrow("2026-06-30", "26Q2", 274.4)])
    assert calib is None, calib

    # 0d. hist_rows 空 → None
    assert calibrate_history("4444", 9.0, "26Q2", []) is None

    # 0e. nv_q 不在歷史資料中（quarter_mismatch）→ None
    calib = calibrate_history("6666", 9.0, "26Q2",
        [hrow("2025-12-31", "25Q4", 11.0), hrow("2026-03-31", "26Q1", 10.5)])
    assert calib is None, calib

    # 0f. cur_nv==0 → factor=1（除零防呆，不崩潰）
    calib = calibrate_history("0000", 0, "26Q1", [hrow("2026-03-31", "26Q1", 5.0)])
    assert calib == {"factor": 1, "rows": {"26Q1": 5.0}}, calib

    # 0g. 同季多筆更正申報 → 取 date 最新一筆算 ratio／校準
    calib = calibrate_history("1357", 9.0, "26Q2",
        [hrow("2026-03-31", "26Q1", 11.0),
         hrow("2026-06-30", "26Q2", 15.0),
         hrow("2026-07-20", "26Q2", 9.0)])
    assert calib == {"factor": 1, "rows": {"26Q1": 11.0, "26Q2": 9.0}}, calib

    def nv(rows):
        return {"rows": rows}

    def h(code, rows, fetched_at="2026-08-14"):
        return {code: {"fetched_at": fetched_at, "rows": rows}}

    empty_status = {"incomplete_codes": {}}

    # 1. 正常掉落：25Q4=10.5 → 26Q2=9.5（26Q1→26Q2 相鄰，最新一季 <10）
    result = detect_margin_drops(
        nv([{"code": "1111", "name": "A", "market": "上市", "net_value": 9.5, "nv_quarter": "26Q2"}]),
        h("1111", [hrow("2025-12-31", "25Q4", 10.5), hrow("2026-03-31", "26Q1", 10.2),
                   hrow("2026-06-30", "26Q2", 9.5)]),
        empty_status)
    assert result["counts"]["confirmed"] == 1, result
    assert result["rows"][0]["from_q"] == "26Q1" and result["rows"][0]["to_q"] == "26Q2"
    assert result["rows"][0]["data_state"] == "confirmed"
    # trail：3 季全部出現、依季序排列；判定用的那一對（26Q1/26Q2）標 high，更早的 25Q4 標 extrapolated
    trail = result["rows"][0]["trail"]
    assert [t["quarter"] for t in trail] == ["25Q4", "26Q1", "26Q2"], trail
    assert [t["confidence"] for t in trail] == ["extrapolated", "high", "high"], trail
    assert trail[-1]["net_value"] == 9.5, trail

    # 2. 11→4（直接跌破 5 元也算掉落，不可被排除——發現 D 回歸）
    result = detect_margin_drops(
        nv([{"code": "2222", "name": "B", "market": "上市", "net_value": 4.0, "nv_quarter": "26Q2"}]),
        h("2222", [hrow("2026-03-31", "26Q1", 11.0), hrow("2026-06-30", "26Q2", 4.0)]),
        empty_status)
    assert result["counts"]["confirmed"] == 1, result

    # 3. 回升（<10→≥10）→ no_drop，不是 confirmed
    #    goodinfo 當期 9.9（<10，仍在母體內）；FinMind 同季校準值 10.3（差 0.4 在容差內，
    #    不觸發 source_conflict）；prev 8.0<10、cur_calibrated 10.3>=10 → 判定往上穿越，非掉落
    result = detect_margin_drops(
        nv([{"code": "3333", "name": "C", "market": "上市", "net_value": 9.9, "nv_quarter": "26Q2"}]),
        h("3333", [hrow("2026-03-31", "26Q1", 8.0), hrow("2026-06-30", "26Q2", 10.3)]),
        empty_status)
    assert result["counts"]["no_drop"] == 1 and result["counts"]["confirmed"] == 0, result

    # 4. 只有一季資料（無任何前季）→ unknown(no_prev)
    result = detect_margin_drops(
        nv([{"code": "4444", "name": "D", "market": "上市", "net_value": 9.0, "nv_quarter": "26Q2"}]),
        h("4444", [hrow("2026-06-30", "26Q2", 9.0)]),
        empty_status)
    assert result["counts"]["unknown"] == 1
    assert result["unknown_reasons"]["no_prev"] == 1, result

    # 4b. 真正沒有任何歷史（rows=[]）→ unknown(no_prev)
    result = detect_margin_drops(
        nv([{"code": "44b", "name": "D2", "market": "上市", "net_value": 9.0, "nv_quarter": "26Q2"}]),
        h("44b", []),
        empty_status)
    assert result["unknown_reasons"].get("no_prev") == 1, result

    # 5. 兩季不相鄰（26Q1→26Q3，中間缺 26Q2）→ unknown(not_adjacent)
    result = detect_margin_drops(
        nv([{"code": "5555", "name": "E", "market": "上市", "net_value": 9.0, "nv_quarter": "26Q3"}]),
        h("5555", [hrow("2026-03-31", "26Q1", 11.0), hrow("2026-09-30", "26Q3", 9.0)]),
        empty_status)
    assert result["unknown_reasons"].get("not_adjacent") == 1, result

    # 6. FinMind 停在舊季（goodinfo 已到 26Q2，FinMind 只有到 26Q1）→ unknown(quarter_mismatch)
    result = detect_margin_drops(
        nv([{"code": "6666", "name": "F", "market": "上市", "net_value": 9.0, "nv_quarter": "26Q2"}]),
        h("6666", [hrow("2025-12-31", "25Q4", 11.0), hrow("2026-03-31", "26Q1", 10.5)]),
        empty_status)
    assert result["unknown_reasons"].get("quarter_mismatch") == 1, result

    # 7. unreliable 旗標：同季比值不落在合理面額倍率內 → unreliable，不判定
    result = detect_margin_drops(
        nv([{"code": "7777", "name": "G-KY*", "market": "上市", "net_value": 8.0, "nv_quarter": "26Q2"}]),
        h("7777", [hrow("2026-03-31", "26Q1", 274.0), hrow("2026-06-30", "26Q2", 274.4)]),  # ratio=34.3, 不在允許清單
        empty_status)
    assert result["counts"]["unreliable"] == 1, result

    # 8. 倍率 >3 → suspect（減資/資料異常表徵；用同季 factor=1 情境，避免與 unreliable 分支混淆）
    result = detect_margin_drops(
        nv([{"code": "8888", "name": "H", "market": "上市", "net_value": 1.0, "nv_quarter": "26Q2"}]),
        h("8888", [hrow("2026-03-31", "26Q1", 8.0), hrow("2026-06-30", "26Q2", 1.0)]),  # ratio2=8 > 3
        empty_status)
    assert result["counts"]["suspect"] == 1, result

    # 8b. prev<=0 → suspect（不算倍率，除零防呆）
    result = detect_margin_drops(
        nv([{"code": "88b", "name": "H2", "market": "上市", "net_value": 3.0, "nv_quarter": "26Q2"}]),
        h("88b", [hrow("2026-03-31", "26Q1", -1.0), hrow("2026-06-30", "26Q2", 3.0)]),
        empty_status)
    assert result["counts"]["suspect"] == 1, result

    # 8c. 符號翻轉（正→負）→ suspect
    result = detect_margin_drops(
        nv([{"code": "88c", "name": "H3", "market": "上市", "net_value": -2.0, "nv_quarter": "26Q2"}]),
        h("88c", [hrow("2026-03-31", "26Q1", 5.0), hrow("2026-06-30", "26Q2", -2.0)]),
        empty_status)
    assert result["counts"]["suspect"] == 1, result

    # 9. 同季兩來源分居 10 元兩側 → source_conflict，不可判 confirmed
    #    goodinfo 當期 9.4（<10），FinMind 同季 10.3（>=10），差 0.9 > 容差 0.5
    result = detect_margin_drops(
        nv([{"code": "9999", "name": "I", "market": "上市", "net_value": 9.4, "nv_quarter": "26Q2"}]),
        h("9999", [hrow("2026-03-31", "26Q1", 11.0), hrow("2026-06-30", "26Q2", 10.3)]),
        empty_status)
    assert result["counts"]["source_conflict"] == 1, result
    assert result["counts"]["confirmed"] == 0, result

    # 10. 3086/4157 型已知面額異常股：同季 factor 校準後不產生假掉落（發現 W 續回歸）
    #     真實序列 25Q4=1.05 → 26Q1=0.95（面額1元，FinMind 恆高 10 倍），goodinfo 當期 0.95
    result = detect_margin_drops(
        nv([{"code": "3086", "name": "華義*", "market": "上市", "net_value": 0.95, "nv_quarter": "26Q1"}]),
        h("3086", [hrow("2025-12-31", "25Q4", 10.5), hrow("2026-03-31", "26Q1", 9.5)]),
        empty_status)
    assert result["counts"]["confirmed"] == 0, result   # 校準後 1.05→0.95，從未接近10，不可判掉落
    assert result["rows"][0]["data_state"] == "no_drop", result
    # KY * 股仍在母體內（不被排除），且母體計數含它
    assert result["universe"] == 1

    # 10b. trail 的 factor 必須跟判定用的是同一組（不可對不同季分別重算），
    #      即使 4 季全部都遠高於 10（面額異常股原始值），校準後全部落在 1 元附近
    result = detect_margin_drops(
        nv([{"code": "3086b", "name": "華義*", "market": "上市", "net_value": 0.95, "nv_quarter": "26Q1"}]),
        h("3086b", [hrow("2025-06-30", "25Q2", 11.2), hrow("2025-09-30", "25Q3", 10.9),
                   hrow("2025-12-31", "25Q4", 10.5), hrow("2026-03-31", "26Q1", 9.5)]),
        empty_status)
    trail = result["rows"][0]["trail"]
    assert [t["net_value"] for t in trail] == [1.12, 1.09, 1.05, 0.95], trail
    assert [t["confidence"] for t in trail] == ["extrapolated", "extrapolated", "high", "high"], trail

    # 11. budget_exhausted → unknown，且 reason 正確
    status_incomplete = {"incomplete_codes": {"1234": "budget_exhausted"}}
    result = detect_margin_drops(
        nv([{"code": "1234", "name": "J", "market": "上市", "net_value": 9.0, "nv_quarter": "26Q2"}]),
        {}, status_incomplete)
    assert result["unknown_reasons"].get("budget_exhausted") == 1, result

    # 12. fetch_failed → unknown，reason 與 budget_exhausted 分開計數
    status_incomplete2 = {"incomplete_codes": {"1234": "fetch_failed"}}
    result = detect_margin_drops(
        nv([{"code": "1234", "name": "J", "market": "上市", "net_value": 9.0, "nv_quarter": "26Q2"}]),
        {}, status_incomplete2)
    assert result["unknown_reasons"].get("fetch_failed") == 1, result

    # 13. 同季多筆更正申報 → 取 date 最新一筆，不可當成兩季
    result = detect_margin_drops(
        nv([{"code": "1357", "name": "K", "market": "上市", "net_value": 9.0, "nv_quarter": "26Q2"}]),
        h("1357", [hrow("2026-03-31", "26Q1", 11.0),
                   hrow("2026-06-30", "26Q2", 15.0),   # 原始申報（錯誤，未使用）
                   hrow("2026-07-20", "26Q2", 9.0)]),  # 更正申報（date 較新）
        empty_status)
    assert result["counts"]["confirmed"] == 1, result   # 用更正後的 9.0，不是原始 15.0

    # 14. universe 只算 netvalue <10 的列，watch/safe(<10門檻以上) 不進母體
    result = detect_margin_drops(
        nv([{"code": "aaaa", "name": "L", "market": "上市", "net_value": 9.9, "nv_quarter": "26Q1"},
            {"code": "bbbb", "name": "M", "market": "上市", "net_value": 12.0, "nv_quarter": "26Q1"}]),
        {}, empty_status)
    assert result["universe"] == 1, result

    # === 14b. 面額非10元股：淨值10元跟信用交易資格無關，一律 not_applicable
    #     （2026-08-21，使用者發現「最近一季掉落」對非10元面額股誤判為真的信用交易事件）===
    result = detect_margin_drops(
        nv([{"code": "8422", "name": "可寧衛*", "market": "上市", "net_value": 7.57, "nv_quarter": "26Q2"}]),
        {"8422": {"rows": [hrow("2026-03-31", "26Q1", 10.04), hrow("2026-06-30", "26Q2", 7.57)]}},
        empty_status, par={"8422": {"par": 1.0, "shares": 100}})
    assert result["counts"]["not_applicable"] == 1, result
    assert result["counts"]["confirmed"] == 0, result   # 不可誤判成真的跌破信用交易門檻
    row = result["rows"][0]
    assert row["data_state"] == "not_applicable", row
    assert row["prev_nv"] is None and row["cur_nv"] is None, row   # 不適用時不給誤導性的數字

    # 14c. 面額10元股，同樣情境（同一組淨值）要正常判定 confirmed，證明修正只影響非10元股
    result = detect_margin_drops(
        nv([{"code": "9999", "name": "測試", "market": "上市", "net_value": 7.57, "nv_quarter": "26Q2"}]),
        {"9999": {"rows": [hrow("2026-03-31", "26Q1", 10.04), hrow("2026-06-30", "26Q2", 7.57)]}},
        empty_status, par={"9999": {"par": 10.0, "shares": 100}})
    assert result["counts"]["confirmed"] == 1, result

    # 14d. par 查不到面額 → 沿用舊行為當成面額10元（跟 full_delivery_threshold() 回退慣例一致）
    result = detect_margin_drops(
        nv([{"code": "8888", "name": "測試", "market": "上市", "net_value": 7.57, "nv_quarter": "26Q2"}]),
        {"8888": {"rows": [hrow("2026-03-31", "26Q1", 10.04), hrow("2026-06-30", "26Q2", 7.57)]}},
        empty_status, par={})
    assert result["counts"]["confirmed"] == 1, result

    # === 15. margin_risk_trend()：本季 vs 上季，非門檻達成判準（Phase D，2026-08-21）===
    # 15a. 回升中（同面額，factor=1）
    r = margin_risk_trend("m1", 7.5, "26Q2",
                          [hrow("2026-03-31", "26Q1", 6.5), hrow("2026-06-30", "26Q2", 7.5)])
    assert r == {"state": "up", "reason": None, "prev_q": "26Q1", "prev_nv": 6.5, "cur_nv": 7.5}, r

    # 15b. 下滑中
    r = margin_risk_trend("m2", 6.2, "26Q2",
                          [hrow("2026-03-31", "26Q1", 7.0), hrow("2026-06-30", "26Q2", 6.2)])
    assert r["state"] == "down", r

    # 15c. 持平
    r = margin_risk_trend("m3", 7.0, "26Q2",
                          [hrow("2026-03-31", "26Q1", 7.0), hrow("2026-06-30", "26Q2", 7.0)])
    assert r["state"] == "flat", r

    # 15d. 無歷史資料 → unknown/no_prev
    r = margin_risk_trend("m4", 7.0, "26Q2", [])
    assert r["state"] == "unknown" and r["reason"] == "no_prev", r

    # 15e. 只有當季一筆（無前季）→ unknown/no_prev
    r = margin_risk_trend("m5", 7.0, "26Q2", [hrow("2026-06-30", "26Q2", 7.0)])
    assert r["state"] == "unknown" and r["reason"] == "no_prev", r

    # 15f. 非相鄰季（中間缺季）→ unknown/not_adjacent
    r = margin_risk_trend("m6", 7.0, "26Q3",
                          [hrow("2026-03-31", "26Q1", 6.5), hrow("2026-09-30", "26Q3", 7.0)])
    assert r["state"] == "unknown" and r["reason"] == "not_adjacent", r

    # 15g. 面額非10元股：FinMind 值需按比例校準才能跟 goodinfo cur_nv 同基準比較
    r = margin_risk_trend("m7", 0.7, "26Q2",
                          [hrow("2026-03-31", "26Q1", 6.5), hrow("2026-06-30", "26Q2", 7.0)])
    # cur_nv=0.7 vs FinMind同季7.0 → ratio=10 → factor=10 → calibrated: 26Q1=0.65, 26Q2=0.70
    assert r["state"] == "up" and r["prev_nv"] == 0.65, r

    # 15h. 疑似異常（倍率超過 SUSPECT_RATIO）→ unknown/suspect，不可誤判方向
    r = margin_risk_trend("m8", 8.0, "26Q2",
                          [hrow("2026-03-31", "26Q1", 6.0), hrow("2026-06-30", "26Q2", 40.0)])
    assert r["state"] == "unknown" and r["reason"] == "suspect", r

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        print("crossings.py 無 CLI 主流程，供 gen_site.py 呼叫 detect_margin_drops()。"
              "用 --selftest 跑測試。")
