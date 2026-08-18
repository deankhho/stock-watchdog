#!/usr/bin/env python3
"""
analyze.py — S3：分級引擎（純本地）
讀 data/netvalue.json + data/official.json → data/report.json

五分級（互斥，判定順序：recover → official → predict_in → edge → margin_risk）：
  recover     在官方全額交割名單 且 最新淨值 >= 5   → 恢復候選（連兩季達標即恢復）
  official    在官方全額交割名單（淨值仍 <5）        → 現況
  predict_in  不在名單 且 淨值 < 5                  → 預測下次財報後打入
  edge        5 <= 淨值 < 6                         → 危險邊緣
  margin_risk 6 <= 淨值 < 10                        → 信用交易警戒（<10 停融資融券）

用法：python analyze.py [--selftest]
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
NV_FILE = BASE / "data" / "netvalue.json"
OF_FILE = BASE / "data" / "official.json"
PAR_FILE = BASE / "data" / "par_value.json"
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
    佔母體98%以上），不可用 0 或 None 之類的假值硬套。"""
    p = par.get(code)
    return p / 2 if p else NET_VALUE_FULL_DELIVERY


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
    assert full_delivery_threshold("3086", {"3086": 1.0}) == 0.5
    assert full_delivery_threshold("2330", {"3086": 1.0}) == 5.0    # 沒收錄，回退固定值
    assert full_delivery_threshold("2330", {}) == 5.0                # par 資料整包缺失，回退固定值
    d, s = days_to_next_report(date(2026, 7, 6))
    assert s == "2026-08-14" and d == 39, (d, s)

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
        item["goodinfo_url"] = f"https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={r['code']}"
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
