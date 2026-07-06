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


def detect_events(hist: list, market: str) -> list:
    """從淨值序列偵測 打入/恢復 事件（含依據說明，上市/上櫃分別引用規定）
    全額交割：跌破5→打入；之後連續兩季≥5→第二季恢復
    信用交易：跌破10→停止；回到10以上→恢復（單季即可）"""
    rule_full = ("證交所營業細則第49條" if market == "上市"
                 else "櫃買中心業務規則（上櫃）")
    rule_margin = "有價證券得為融資融券標準"
    ev = []
    prev5 = prev10 = None
    streak5 = 0                       # 連續 ≥5 的季數（處於全額交割狀態中）
    in_full = False
    for h in hist:
        nv, q = h["net_value"], h["quarter"]
        if prev5 is not None:
            if not prev5 and h["hit5"]:          # ≥5 → <5
                ev.append({"q": q, "type": "full_in",
                           "text": f"{q} 淨值 {nv} 跌破 5 元 → 依{rule_full}應列為全額交割"})
                in_full, streak5 = True, 0
            if not prev10 and h["hit10"] and not h["hit5"]:
                ev.append({"q": q, "type": "margin_stop",
                           "text": f"{q} 淨值 {nv} 跌破 10 元 → 依{rule_margin}應停止融資融券"})
            if prev10 and not h["hit10"]:
                ev.append({"q": q, "type": "margin_recover",
                           "text": f"{q} 淨值 {nv} 回到 10 元以上 → 依{rule_margin}恢復融資融券（單季即可）"})
        else:
            in_full = h["hit5"]                  # 序列起點已在門檻下
        if in_full:
            streak5 = streak5 + 1 if not h["hit5"] else 0
            if streak5 >= 2:
                # 注意：49條有多款列入事由，淨值只是其一（2026-07-07 大飲案例實證：
                # 淨值11.16仍在名單）。僅能說「符合淨值款恢復條件」，不能斷言恢復。
                ev.append({"q": q, "type": "full_recover",
                           "text": f"{q} 已連續兩季淨值 ≥5（{hist[hist.index(h)-1]['quarter']}→{q}）→ 符合{rule_full}的淨值款恢復條件；"
                                   f"⚠️ 若係因其他事由列入（會計師意見/財報未依限公告/重整等），需該原因消滅才恢復"})
                in_full = False
        prev5, prev10 = h["hit5"], h["hit10"]
    return ev


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
        # 面額校準：公式假設面額10元，但台股有面額1元/5元股（如華義面額1元，
        # 算出來會高10倍——2026-07-06 實測發現）。用 goodinfo 現值比對最新一季校準。
        hist8 = hist[-8:]
        cur_nv = s.get("net_value")
        factor, unreliable = 1, False
        if cur_nv and hist8:
            ratio = hist8[-1]["net_value"] / cur_nv
            if ratio > 1.5 or ratio < 0.67:      # 明顯偏離 → 面額非 10 或資料異常
                snapped = round(ratio)
                # 合理面額倍率：面額5→2倍、2.5→4、1→10、0.5→20
                if snapped in (2, 4, 5, 10, 20):
                    factor = snapped
                    for h in hist8:
                        h["net_value"] = round(h["net_value"] / factor, 2)
                        h["hit5"] = h["net_value"] < 5
                        h["hit10"] = h["net_value"] < 10
                else:
                    # 非面額問題（如 KY 股 FinMind 欄位單位異常，4157 實測比值 343）
                    # → 標記不可靠，不顯示錯誤歷史誤導判斷
                    unreliable = True
                    hist8 = []
        result[code] = {"name": s["name"], "market": s.get("market", ""),
                        "current_nv": cur_nv,
                        "par_factor": factor, "unreliable": unreliable,
                        "group": next(k for k in ("predict_in","edge","recover","official")
                                      if s in rep["groups"][k]),
                        "history": hist8,
                        "events": detect_events(hist8, s.get("market", ""))}
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
