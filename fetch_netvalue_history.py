#!/usr/bin/env python3
"""
fetch_netvalue_history.py — crossings.py 專用的淨值歷史抓取器

🔴 與 backtest.py 完全解耦：不共用抓取邏輯、不共用快取路徑（data/netvalue_history/
不是 data/history/）、不寫進 backtest.json（寫 data/netvalue_history_status.json）。
這樣「掉落偵測出錯」與「backtest 原本的交易預測回測出錯」才能互不牽連、可分開歸因。

股池＝ data/netvalue.json 中 net_value <10 的全部代號（與 crossings.py 母體同一定義，
不經 report groups，避免 KY `*` 股被 analyze.py 排除掉造成的落差，見計畫發現 T）
∪ data/official.json 的 full_delivery 清單代號（2026-08-18 追加：recover 分類本身
沒有淨值上限，只收低淨值股會系統性漏抓 nv>=10 的 recover 候選，見 build_pool()）。

只保留最新 4 季（判定用 2 季 + 2 季供 crossings.py 的多季回溯 trail 顯示），
不比照 backtest.py 留 8 季——面額 factor 只在同季比對時計算並只套用最新一個季度跨距，
判定（confirmed/no_drop 等）邏輯不變，多出的較舊季度只供 trail 顯示，逐季標
confidence（見 crossings.py），不可讓使用者誤讀成整條 trail 都經過同等驗證。

預算：每檔最多 1 次請求 + 1 次重試（最多 2 requests/檔）；MAX_REQ=250（300/hr 留餘裕）。
優先序：P1（季別落後，真的缺資料，必抓）> P2（公告期內補早鳥，依 fetched_at 舊到新）。
🔴 P1 超額不可 hard fail——latest_expected_quarter() 在每季截止日當天跳季，
那一刻全部快取可能同時變成 P1（發現 Q）。P1 也採輪替，吃滿預算為止，數天內輪完；
沒抓到的一律進 incomplete_codes，不 fail 整個流程。

不需要另外的續跑 state 檔：續跑狀態就是各快取檔自己的 fetched_at（發現 S）。

用法：python fetch_netvalue_history.py [--selftest]
"""

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

from analyze import in_filing_window, latest_expected_quarter, taipei_today
from crossings import low_netvalue_pool

BASE = Path(__file__).parent
NV_FILE = BASE / "data" / "netvalue.json"
OF_FILE = BASE / "data" / "official.json"
CACHE = BASE / "data" / "netvalue_history"
STATUS_OUT = BASE / "data" / "netvalue_history_status.json"

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
KEEP_QUARTERS = 4            # 判定用最新 2 季 + 2 季供多季回溯顯示，不比照 backtest.py 留 8 季
MAX_REQ = 250                 # FinMind 免 token 300/hr，留 50 餘裕


class FetchFailed(Exception):
    """抓取失敗，携帶已消耗的 request 數（重試也計入預算）"""
    def __init__(self, used: int, msg: str):
        super().__init__(msg)
        self.used = used


def read_cache(fp: Path) -> tuple:
    """→ (rows, fetched_at)。無快取檔視為 (?, None)"""
    if not fp.exists():
        return [], None
    d = json.loads(fp.read_text())
    return d.get("rows", []), d.get("fetched_at")


def write_cache(fp: Path, rows: list, today: date = None) -> None:
    """原子寫入（同 backtest.py 手法：先寫暫存再 os.replace）"""
    today = today or taipei_today()
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": today.isoformat(), "rows": rows},
                              ensure_ascii=False))
    os.replace(tmp, fp)


def classify_priority(rows: list, fetched_at, today: date = None) -> str:
    """→ 'skip' / 'P1' / 'P2'

    🔴 rows=[]（空 rows 快取）一律視為完全沒有歷史 → P1。
    這不是理論案例——data/history/9110.json 現況就是空 rows（發現 X），
    新抓取器必須一樣正確處理，不可假設 rows 非空。
    """
    today = today or taipei_today()
    if fetched_at == today.isoformat():
        return "skip"
    if not rows:
        return "P1"
    if rows[-1]["quarter"] < latest_expected_quarter(today):
        return "P1"
    if in_filing_window(today):
        return "P2"
    return "skip"


def _quarter_str(iso_date: str) -> str:
    m = int(iso_date[5:7])
    q = {3: 1, 6: 2, 9: 3, 12: 4}.get(m, (m - 1) // 3 + 1)
    return f"{iso_date[2:4]}Q{q}"


def fetch_one(code: str) -> tuple:
    """FinMind 資產負債表 → 最新 KEEP_QUARTERS 季每股淨值。
    → (rows, requests_used)；失敗時拋 FetchFailed(used, msg)。
    每檔最多 1 次請求 + 1 次重試，requests_used 恆 <= 2。"""
    used = 0
    last_exc = None
    for _attempt in range(2):
        used += 1
        try:
            start = f"{taipei_today().year - 1}-01-01"
            r = requests.get(FINMIND_URL, params={
                "dataset": "TaiwanStockBalanceSheet",
                "data_id": code, "start_date": start}, timeout=20)
            # 🔴 FinMind 配額用完時回 HTTP 402、data 是空陣列——r.json() 不會拋例外，
            # 沒有 raise_for_status() 會把「額度用完」誤判成「這檔真的沒有歷史資料」，
            # 靜默寫成 rows=[] 快取（2026-08-15 實測抓到：6210 等 58 檔真實遇到 402，
            # data 筆數 0，若不擋下來就會被 crossings.py 當成 no_prev，混進正常的空歷史）
            r.raise_for_status()
            data = r.json().get("data", [])
            by_date = {}
            for x in data:
                by_date.setdefault(x["date"], {})[x["type"]] = x["value"]
            out = []
            for d, vals in sorted(by_date.items()):
                equity = vals.get("EquityAttributableToOwnersOfParent") or vals.get("Equity")
                capital = (vals.get("OrdinaryShare") or vals.get("Share_capital")
                           or vals.get("CapitalStock"))
                if not equity or not capital:
                    continue
                nv = round(equity / capital * 10, 2)     # 面額 10 元假設，見 crossings.py 校準
                out.append({"date": d, "quarter": _quarter_str(d), "net_value": nv})
            return out[-KEEP_QUARTERS:], used
        except Exception as e:
            last_exc = e
    raise FetchFailed(used, f"{code} 抓取失敗（已重試1次）：{last_exc}")


def run_budgeted_fetch(pool: list, cache_lookup: dict, today: date,
                       fetch_fn=fetch_one, write_fn=None, max_req: int = MAX_REQ) -> dict:
    """核心預算/優先序邏輯，抽成純函式方便 selftest 注入 stub，不碰真實檔案系統。

    pool：代號清單
    cache_lookup：{code: (rows, fetched_at)}
    fetch_fn：callable(code) -> (rows, requests_used)，失敗拋 FetchFailed(used, msg)
    write_fn：callable(code, rows, today)，預設不寫（selftest 用）；main() 會傳真正的寫檔函式
    → status dict（不含 generated_at，由呼叫端補）
    """
    p1, p2 = [], []
    for code in pool:
        rows, fetched_at = cache_lookup.get(code, ([], None))
        pri = classify_priority(rows, fetched_at, today)
        if pri == "P1":
            p1.append(code)
        elif pri == "P2":
            p2.append(code)

    # P1：季別落後程度（quarter 字串小者更落後）優先，其次 fetched_at 舊到新
    def p1_key(c):
        rows, fa = cache_lookup.get(c, ([], None))
        last_q = rows[-1]["quarter"] if rows else ""
        return (last_q, fa or "")
    p1.sort(key=p1_key)
    # P2：依 fetched_at 由舊到新輪替
    p2.sort(key=lambda c: cache_lookup.get(c, ([], None))[1] or "")

    req_count = 0
    fetched_count = 0
    incomplete = {}

    for code in p1:
        if req_count + 2 > max_req:          # 保留這檔最壞情況（1次+1重試）的預算空間
            incomplete[code] = "budget_exhausted"
            continue
        try:
            rows, used = fetch_fn(code)
            req_count += used
            fetched_count += 1
            if write_fn:
                write_fn(code, rows, today)
        except FetchFailed as e:
            req_count += e.used
            incomplete[code] = "fetch_failed"

    p2_fetched = 0
    for code in p2:
        if req_count + 2 > max_req:
            break                            # P2 沒輪到不算失敗，下次由 fetched_at 排序自然補上
        try:
            rows, used = fetch_fn(code)
            req_count += used
            p2_fetched += 1
            if write_fn:
                write_fn(code, rows, today)
        except FetchFailed as e:
            req_count += e.used               # P2 早鳥失敗沿用舊資料，不進 incomplete_codes

    return {
        "pool_size": len(pool), "p1_count": len(p1), "p2_count": len(p2),
        "fetched_count": fetched_count, "p2_fetched_count": p2_fetched,
        "req_count": req_count, "incomplete_codes": incomplete,
    }


def build_pool(nv_data: dict, official_data: dict) -> list:
    """抓取母體 = low_netvalue_pool(nv_data) ∪ official.json 的 full_delivery 清單代號。

    🔴 `recover` 分類本身沒有淨值上限（classify() 對 in_official=True 的分支只要求
    nv>=threshold，不設上限），但這裡的母體原本只收 net_value<CROSS_NV(10.0) 的股票——
    兩者不是子集關係。2026-08-18 用真實資料驗證過：1213 大飲 nv=10.98 是當時 6 檔
    recover 候選之一，因為母體定義從一開始就沒把它算進去，`1213.json` 確實不存在，
    會被下游 `recover_eligibility()` 系統性判成 unknown（不是真的缺資料）。

    official_data 讀取失敗、缺 `full_delivery` 或整包為 None/{}（降級/缺檔）時，
    優雅退回只用 low_netvalue_pool()——多抓是加分，不是必要條件，不可讓整支腳本掛掉。"""
    codes = {r["code"] for r in low_netvalue_pool(nv_data)}
    full_delivery = (official_data or {}).get("full_delivery")
    if full_delivery:
        codes |= {x["code"] for x in full_delivery}
    return sorted(codes)


def main():
    nv_data = json.loads(NV_FILE.read_text())
    of_data = json.loads(OF_FILE.read_text()) if OF_FILE.exists() else {}
    pool = build_pool(nv_data, of_data)
    today = taipei_today()

    cache_lookup = {}
    for code in pool:
        fp = CACHE / f"{code}.json"
        cache_lookup[code] = read_cache(fp)

    def write_fn(code, rows, today_):
        write_cache(CACHE / f"{code}.json", rows, today_)

    def fetch_throttled(code):
        # FinMind 免 token 限速保守值（同 backtest.py 慣例）；只在真實抓取路徑節流，
        # 不放進 fetch_one()／run_budgeted_fetch() 本體，避免拖慢 selftest
        result = fetch_one(code)
        time.sleep(0.6)
        return result

    status = run_budgeted_fetch(pool, cache_lookup, today, fetch_throttled, write_fn)
    status["generated_at"] = taipei_today().isoformat()

    STATUS_OUT.parent.mkdir(exist_ok=True)
    STATUS_OUT.write_text(json.dumps(status, ensure_ascii=False, indent=1))

    print(f"淨值歷史抓取股池 {status['pool_size']} 檔（netvalue <10 ∪ 官方全額交割清單，"
          f"後者確保 nv>=10 的 recover 候選不被漏抓）")
    print(f"P1（缺資料，必抓）{status['p1_count']} 檔／P2（補早鳥）{status['p2_count']} 檔／"
          f"實際抓取 P1 {status['fetched_count']}＋P2 {status['p2_fetched_count']} 檔／"
          f"request {status['req_count']}（上限 {MAX_REQ}）／未完成 {len(status['incomplete_codes'])} 檔")
    if status["p1_count"] and len(status["incomplete_codes"]) / status["p1_count"] > 0.5:
        print(f"::warning::P1 未完成比例 {len(status['incomplete_codes'])}/{status['p1_count']} "
              f"超過 50%（季度切換日附近為預期狀態，僅供維運觀察，不代表資料異常，見計畫已知限制）")


def selftest():
    today = date(2026, 8, 14)

    # 0. build_pool()：母體 = low_netvalue_pool() ∪ official.json 的 full_delivery 清單
    #    （2026-08-18 用真實資料驗證：1213 大飲 nv=10.98 曾因母體只收低淨值股被漏抓）
    nv_data_0 = {"rows": [
        {"code": "1111", "name": "低淨值股", "net_value": 5.0},
        {"code": "2222", "name": "已恢復但仍偏低", "net_value": 9.0},
    ]}
    official_0 = {"full_delivery": [
        {"code": "2222", "name": "已恢復但仍偏低", "market": "上市"},
        {"code": "3333", "name": "大飲型（nv>=10）", "market": "上市"},
    ]}
    assert build_pool(nv_data_0, official_0) == ["1111", "2222", "3333"], build_pool(nv_data_0, official_0)

    # 0b. official.json 缺失/降級（無 full_delivery）→ 優雅退回只用 low_netvalue_pool()
    assert build_pool(nv_data_0, {}) == ["1111", "2222"]
    assert build_pool(nv_data_0, {"state": "degraded"}) == ["1111", "2222"]
    assert build_pool(nv_data_0, None) == ["1111", "2222"]

    # 1. classify_priority：空 rows 一律 P1（發現 X 的回歸測試）
    assert classify_priority([], None, today) == "P1"
    assert classify_priority([], "2026-08-13", today) == "P1"

    # 2. 今天已抓過 → skip
    assert classify_priority([{"quarter": "26Q2"}], "2026-08-14", today) == "skip"

    # 3. 季別落後（截止日已過但快取還在 26Q1）→ P1
    assert classify_priority([{"quarter": "26Q1"}], "2026-08-13", today) == "P1"

    # 4. 季別沒落後、在公告期內 → P2（8/14 屬公告期）
    assert classify_priority([{"quarter": "26Q2"}], "2026-08-10", today) == "P2"

    # 5. 季別沒落後、不在公告期 → skip
    d0925 = date(2026, 9, 25)
    assert classify_priority([{"quarter": "26Q2"}], "2026-09-20", d0925) == "skip"

    # --- run_budgeted_fetch：185 檔全部落後（截止日當天，發現 Q 的真實情境）---
    pool = [f"{i:04d}" for i in range(185)]
    cache_lookup = {c: ([], None) for c in pool}   # 全部無快取 → 全部 P1

    def fetch_ok(code):
        return [{"date": "2026-06-30", "quarter": "26Q2", "net_value": 8.0}], 1

    status = run_budgeted_fetch(pool, cache_lookup, today, fetch_fn=fetch_ok, max_req=100)
    assert status["p1_count"] == 185, status
    assert status["req_count"] <= 100, status                    # 不可超過預算
    assert len(status["incomplete_codes"]) > 0, status            # 185 檔 > 預算 100，必有未完成
    assert all(v == "budget_exhausted" for v in status["incomplete_codes"].values()), status
    assert status["fetched_count"] + len(status["incomplete_codes"]) == 185, status
    # 🔴 不可 hard fail：跑到這裡沒有拋例外，就是「不 fail」的直接證明

    # --- 185 檔全部落後 + 全部抓取失敗（每檔耗滿 2 requests）---
    def fetch_fail(code):
        raise FetchFailed(2, "boom")

    status2 = run_budgeted_fetch(pool, cache_lookup, today, fetch_fn=fetch_fail, max_req=250)
    assert status2["req_count"] <= 250, status2
    assert status2["fetched_count"] == 0, status2
    reasons = set(status2["incomplete_codes"].values())
    assert reasons <= {"fetch_failed", "budget_exhausted"}, status2
    assert "fetch_failed" in reasons, status2       # 前面幾檔應該真的打了 request 才失敗

    # --- 預算充足時（大 max_req）：全部成功，無 incomplete ---
    status3 = run_budgeted_fetch(pool, cache_lookup, today, fetch_fn=fetch_ok, max_req=10000)
    assert status3["fetched_count"] == 185, status3
    assert len(status3["incomplete_codes"]) == 0, status3

    # --- P2 沒輪到不算失敗，不進 incomplete_codes ---
    pool_p2 = [f"p{i:04d}" for i in range(5)]
    cache_lookup_p2 = {c: ([{"quarter": "26Q2"}], "2026-08-01") for c in pool_p2}  # 季別沒落後、公告期內
    status4 = run_budgeted_fetch(pool_p2, cache_lookup_p2, today, fetch_fn=fetch_ok, max_req=0)
    assert status4["p2_count"] == 5, status4
    assert status4["p2_fetched_count"] == 0, status4
    assert len(status4["incomplete_codes"]) == 0, status4   # P2 沒輪到，不是「缺資料」

    # --- 空 rows 快取（發現 X）→ P1，且抓取成功後正確覆蓋 ---
    pool_x = ["9110"]
    cache_lookup_x = {"9110": ([], "2026-08-13")}
    status5 = run_budgeted_fetch(pool_x, cache_lookup_x, today, fetch_fn=fetch_ok, max_req=250)
    assert status5["p1_count"] == 1, status5
    assert status5["fetched_count"] == 1, status5

    # --- 只留最新 4 季：fetch_one 的裁切邏輯（純函式部分，不含網路）---
    class _FakeResp:
        def __init__(self, rows):
            self._rows = rows
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": self._rows}
    import fetch_netvalue_history as m
    orig_get = m.requests.get
    fake_rows = []
    for q, d_ in [("2025-03-31", 1), ("2025-06-30", 1), ("2025-09-30", 1),
                  ("2025-12-31", 1), ("2026-03-31", 1)]:
        fake_rows.append({"date": q, "type": "EquityAttributableToOwnersOfParent", "value": 100})
        fake_rows.append({"date": q, "type": "OrdinaryShare", "value": 10})
    m.requests.get = lambda *a, **kw: _FakeResp(fake_rows)
    try:
        rows, used = fetch_one("TEST")
        assert used == 1, used
        assert len(rows) == 4, rows                 # 5 季資料只留最新 4 季
        assert rows[-1]["quarter"] == "26Q1", rows
        assert rows[0]["quarter"] == "25Q2", rows    # 確認裁掉的是最舊那季（25Q1），不是隨意砍
    finally:
        m.requests.get = orig_get

    # --- 🔴 配額用完（HTTP 402，data 空陣列）必須視為失敗，不可靜默當成「無歷史資料」---
    # 2026-08-15 實測：FinMind 額度用完時回 402、data: []，r.json() 不拋例外，
    # 沒有 raise_for_status() 就會把「額度用完」誤判成「這檔真的沒有歷史資料」
    class _Fake402:
        status_code = 402
        def raise_for_status(self):
            import requests as _rq
            raise _rq.HTTPError("402 Requests reach the upper limit")
        def json(self):
            return {"data": [], "msg": "Requests reach the upper limit"}
    m.requests.get = lambda *a, **kw: _Fake402()
    try:
        try:
            fetch_one("TEST")
            raise AssertionError("402 應該要拋 FetchFailed，不可靜默回傳空 rows")
        except FetchFailed as e:
            assert e.used == 2, e.used   # 重試過一次，兩次都 402
    finally:
        m.requests.get = orig_get

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
