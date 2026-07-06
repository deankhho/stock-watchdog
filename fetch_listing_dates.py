#!/usr/bin/env python3
"""
fetch_listing_dates.py — 全額交割「列入日期」偵測（上市＋上櫃）
方法：歷史名單按月取樣，每檔首次出現的月份區間再以二分法收斂至「日」。
- 上市：TWT85U?date=YYYYMMDD（2026-07-07 實測可用）
- 上櫃：/www/zh-tw/afterTrading/chtm?date=YYYY/MM/DD（playwright 攔 XHR 找到；
  openapi 無歷史端點、猜測路徑全 404——調查先於設計的實證）
  欄位[2]=變更交易（Ｙ），SSL 缺 SKI → verify=False
（官方 API 無列入原因欄；本日期為「首次見於名單日」，即生效日）
輸出：data/listing_dates.json {code: {"since": "YYYY-MM-DD", "precision": "day|before_window"}}
快取：已收斂的檔不重查。
"""

import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()
BASE = Path(__file__).parent
OF_FILE = BASE / "data" / "official.json"
OUT = BASE / "data" / "listing_dates.json"
H = {"User-Agent": "Mozilla/5.0"}
SLEEP = 0.4


def twt85u_codes(d: date) -> set | None:
    """某日的上市全額交割名單（非交易日 stat 非 OK → 回 None）"""
    r = requests.get("https://www.twse.com.tw/exchangeReport/TWT85U"
                     f"?response=json&date={d:%Y%m%d}", timeout=15, headers=H)
    time.sleep(SLEEP)
    j = r.json()
    if j.get("stat") != "OK":
        return None
    codes = {row[0] for row in j.get("data", [])}
    # 假日/異常日 TWSE 會回 OK+空資料（2026-07-07 實測：多個月初 0 檔）
    # 全額交割名單不可能為空 → 空集合視為無效樣本
    if not codes:
        return None
    # 異常爆量（實測 2026-04-01 回 39 檔，其他日都 12-14）也視為無效
    if len(codes) > 25:
        return None
    return codes


def tpex_codes(d: date) -> set | None:
    """某日的上櫃變更交易名單（欄位[2]=變更交易 Ｙ）"""
    r = requests.get("https://www.tpex.org.tw/www/zh-tw/afterTrading/chtm",
                     params={"date": f"{d:%Y/%m/%d}"}, timeout=15,
                     headers={"User-Agent": "Mozilla/5.0"}, verify=False)
    time.sleep(SLEEP)
    try:
        t = r.json()["tables"][0]
    except Exception:
        return None
    rows = t.get("data", [])
    codes = {row[0] for row in rows if len(row) > 2 and row[2].strip() == "Ｙ"}
    return codes or None       # 空=假日/異常，視為無效（同 TWSE 實測教訓）


def nearest_trading(d: date, fetcher) -> tuple:
    """往後找最近有資料的交易日"""
    for i in range(10):
        dd = d + timedelta(days=i)
        codes = fetcher(dd)
        if codes is not None:
            return dd, codes
    return None, None


def detect_market(targets: list, fetcher, cache: dict, label: str):
    todo = [c for c in targets if c not in cache]
    print(f"{label}名單 {len(targets)} 檔，待查 {len(todo)} 檔")
    if not todo:
        return
    today = date.today()
    samples = []
    for k in range(24, -1, -1):
        y, m = today.year, today.month - k
        while m <= 0:
            y, m = y - 1, m + 12
        d0 = date(y, m, 1)
        if d0 > today:
            continue
        dd, codes = nearest_trading(d0, fetcher)
        if codes is not None:
            samples.append((dd, codes))
    print(f"  有效取樣 {len(samples)} 個月")

    for code in todo:
        first_i = next((i for i, (_, cs) in enumerate(samples) if code in cs), None)
        if first_i is None:
            cache[code] = {"since": None, "precision": "not_found_in_window"}
            continue
        if first_i == 0:
            cache[code] = {"since": samples[0][0].isoformat(),
                           "precision": "before_window"}
            continue
        lo, hi = samples[first_i - 1][0], samples[first_i][0]
        while (hi - lo).days > 1:
            mid = lo + (hi - lo) / 2
            dd, codes = nearest_trading(mid, fetcher)
            if dd is None or dd >= hi:
                break
            if code in codes:
                hi = dd
            else:
                lo = dd
        cache[code] = {"since": hi.isoformat(), "precision": "day"}
        print(f"  {code} 列入日 ≈ {hi}")


def main():
    official = json.loads(OF_FILE.read_text())
    cache = json.loads(OUT.read_text()) if OUT.exists() else {}
    twse = [x["code"] for x in official["full_delivery"] if x["market"] == "上市"]
    tpex = [x["code"] for x in official["full_delivery"] if x["market"] == "上櫃"]
    detect_market(twse, twt85u_codes, cache, "上市")
    OUT.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    detect_market(tpex, tpex_codes, cache, "上櫃")
    OUT.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    print(f"完成 → {OUT}")


if __name__ == "__main__":
    main()
