#!/usr/bin/env python3
"""
backtest.py — S7：近兩年（8 季）淨值歷史，驗證規則有效性
股池：report.json 的 predict_in + edge + recover + official（約 50-60 檔，
控制在 FinMind 免 token 300 次/小時限制內；margin_risk 134 檔不納入 v1）
資料源：FinMind TaiwanStockBalanceSheet（權益總額/股本 → 每股淨值）
快取：data/history/<code>.json（重跑跳過）
輸出：data/backtest.json：每檔 8 季 [{quarter, net_value, hit5, hit10}]
"""

import json
import time
from datetime import date, datetime
from pathlib import Path

import requests

BASE = Path(__file__).parent
REPORT = BASE / "data" / "report.json"
CACHE = BASE / "data" / "history"
OUT = BASE / "data" / "backtest.json"

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
START = f"{date.today().year - 2}-01-01"


def fetch_balance(code: str) -> list:
    """FinMind 資產負債表 → 每季每股淨值（快取）"""
    fp = CACHE / f"{code}.json"
    if fp.exists():
        return json.loads(fp.read_text())
    r = requests.get(FINMIND_URL, params={
        "dataset": "TaiwanStockBalanceSheet",
        "data_id": code, "start_date": START}, timeout=20)
    rows = r.json().get("data", [])
    # 整理成 {date: {type: value}}
    by_date = {}
    for x in rows:
        by_date.setdefault(x["date"], {})[x["type"]] = x["value"]
    out = []
    for d, vals in sorted(by_date.items()):
        equity = vals.get("EquityAttributableToOwnersOfParent") or vals.get("Equity")
        capital = vals.get("OrdinaryShare") or vals.get("Share_capital") or vals.get("CapitalStock")
        if not equity or not capital:
            continue
        nv = round(equity / capital * 10, 2)      # 面額 10 元
        m = int(d[5:7])
        quarter = f"{d[2:4]}Q{ {3: 1, 6: 2, 9: 3, 12: 4}.get(m, (m - 1) // 3 + 1) }"
        out.append({"date": d, "quarter": quarter, "net_value": nv,
                    "hit5": nv < 5, "hit10": nv < 10})
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(out, ensure_ascii=False))
    return out


def main():
    rep = json.loads(REPORT.read_text())
    pool = []
    for k in ("predict_in", "edge", "recover", "official"):
        pool.extend(rep["groups"][k])
    print(f"回測股池 {len(pool)} 檔（predict_in/edge/recover/official）")

    result, failed = {}, []
    for i, s in enumerate(pool, 1):
        code = s["code"]
        try:
            hist = fetch_balance(code)
        except Exception as e:
            print(f"  ✗ {code} {e}")
            failed.append(code)
            continue
        result[code] = {"name": s["name"], "market": s.get("market", ""),
                        "current_nv": s.get("net_value"),
                        "group": next(k for k in ("predict_in","edge","recover","official")
                                      if s in rep["groups"][k]),
                        "history": hist[-8:]}
        if i % 10 == 0:
            print(f"  {i}/{len(pool)}")
        time.sleep(0.6)          # FinMind 免 token 限速保守值

    OUT.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "start": START, "count": len(result), "failed": failed,
        "stocks": result,
    }, ensure_ascii=False))
    ok8 = sum(1 for v in result.values() if len(v["history"]) >= 6)
    print(f"完成 {len(result)} 檔（≥6季資料:{ok8}，失敗:{len(failed)}）→ {OUT}")


if __name__ == "__main__":
    main()
