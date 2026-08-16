#!/usr/bin/env python3
"""
fetch_sbl.py — Phase B：借券標的清單 → data/sbl.json（發現 K／§8）
資料源：TWSE exchangeReport/TWT60U「標的證券明細」（僅供做否定判斷：不在清單→確定不能借券，
不代表在清單內就「目前有券可借」，見已知限制）。

🔴 獨立成 data/sbl.json，不塞進 official.json——後者的 fetched_at／degraded 語意不該被
SBL 這個次要來源污染，兩者互不影響新鮮度判定。

🔴 截斷偵測（借券「不在清單」是否定判斷，資料被截斷會讓系統對大量股票錯報 ✗）：
  絕對地板：筆數 < 1000 → 立即拒絕覆寫、標降級（唯一觀測值 2074，腰斬以上必是解析問題）
  相對門檻（觀察期，只 warning 不擋寫入）：1000 ~ 2074*0.8=1659 之間 → 接受寫入但發 warning
  空清單 → 一律視為失敗，不可寫入
  基準值：2026-08-13 實測 2074 檔（2026-08-16 複測仍是 2074，穩定）

用法：python fetch_sbl.py [--selftest]
"""

import json
import os
import sys
from pathlib import Path

import requests

from analyze import taipei_today

BASE = Path(__file__).parent
OUT = BASE / "data" / "sbl.json"

SBL_URL = "https://www.twse.com.tw/exchangeReport/TWT60U"   # selftest 會覆寫成本機 http.server
TIMEOUT = 20

ABS_FLOOR = 1000            # 絕對地板，立即生效，不等觀察期
BASELINE = 2074              # 2026-08-13 實測基準值
REL_RATIO = 0.8              # 相對門檻＝BASELINE * REL_RATIO（觀察期，只 warning）
HISTORY_KEEP = 30            # count_history 保留天數，供事後校準門檻


def fetch_sbl_list() -> tuple:
    """→ (codes: set[str], count: int)。拋例外代表抓取失敗（HTTP/逾時/非 JSON）。"""
    r = requests.get(SBL_URL, params={"response": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    data = j.get("data", [])
    codes = {row[0].strip() for row in data if row and row[0]}
    return codes, len(codes)


def classify_fetch(count: int) -> tuple:
    """→ (accept: bool, severity: 'ok'|'warn', reason: str|None)"""
    if count == 0:
        return False, "reject", "空清單，視為失敗"
    if count < ABS_FLOOR:
        return False, "reject", f"筆數 {count} 低於絕對地板 {ABS_FLOOR}，疑似截斷"
    threshold = BASELINE * REL_RATIO
    if count < threshold:
        return True, "warn", f"筆數 {count} 低於觀察期相對門檻 {threshold:.0f}（僅 warning，接受寫入）"
    return True, "ok", None


def load_prev() -> dict:
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {}


def write_atomic(data: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, OUT)


def build_degraded(prev: dict, reason: str) -> dict:
    """截斷／失敗時：沿用舊清單，不可用本輪殘缺資料覆寫（發現：否定判斷在資料不可信時必須收回）。"""
    codes = prev.get("codes") or []
    state = "degraded" if codes else "empty"
    fetched_at = prev.get("fetched_at") if state == "degraded" else taipei_today().isoformat()
    return {"state": state, "fetched_at": fetched_at, "reason": reason,
           "codes": codes, "count": prev.get("count", 0),
           "count_history": prev.get("count_history", [])}


def run(fetch_fn=fetch_sbl_list, prev: dict = None) -> dict:
    """核心邏輯，純函式方便 selftest 注入，不直接寫檔。→ 要寫入的完整 dict。"""
    prev = prev if prev is not None else load_prev()
    try:
        codes, count = fetch_fn()
    except Exception as e:
        return build_degraded(prev, f"抓取失敗：{type(e).__name__}: {e}")

    accept, severity, reason = classify_fetch(count)
    if not accept:
        return build_degraded(prev, reason)

    today = taipei_today().isoformat()
    history = (prev.get("count_history") or [])[-(HISTORY_KEEP - 1):] + [{"date": today, "count": count}]
    out = {"state": "ok", "fetched_at": today, "reason": (reason if severity == "warn" else None),
          "codes": sorted(codes), "count": count, "count_history": history}
    return out


def main():
    prev = load_prev()
    out = run(fetch_sbl_list, prev)
    write_atomic(out)
    if out["state"] != "ok":
        print(f"::warning::{out['reason']}，state={out['state']}")
    elif out.get("reason"):
        print(f"::warning::{out['reason']}")
    print(f"完成：借券標的 {out.get('count', 0)} 檔，state={out['state']} → {OUT}")


def selftest():
    import http.server
    import threading

    global SBL_URL

    # === 1. classify_fetch()：三段門檻 ===
    assert classify_fetch(0) == (False, "reject", "空清單，視為失敗")
    accept, sev, reason = classify_fetch(500)
    assert not accept and sev == "reject" and "絕對地板" in reason, (accept, sev, reason)
    accept, sev, reason = classify_fetch(1200)          # 1000~1659 之間 → warn 但接受
    assert accept and sev == "warn" and "觀察期" in reason, (accept, sev, reason)
    accept, sev, reason = classify_fetch(2074)
    assert accept and sev == "ok" and reason is None, (accept, sev, reason)
    accept, sev, reason = classify_fetch(999)            # 剛好卡在地板下
    assert not accept and sev == "reject", (accept, sev, reason)
    accept, sev, reason = classify_fetch(1000)            # 剛好在地板上（含）
    assert accept, (accept, sev, reason)

    # === 2. run()：正常情況 ===
    def fetch_ok():
        return {f"{1000+i}" for i in range(2074)}, 2074

    out = run(fetch_ok, prev={})
    assert out["state"] == "ok" and out["count"] == 2074, out
    assert len(out["codes"]) == 2074, out
    assert out["codes"] == sorted(out["codes"]), out          # 排序確定性
    assert len(out["count_history"]) == 1, out

    # === 3. run()：截斷（低於絕對地板）→ 沿用舊清單，不可寫入殘缺資料 ===
    prev_good = {"state": "ok", "fetched_at": "2026-08-10", "reason": None,
                "codes": ["2330", "2317"], "count": 2, "count_history": []}

    def fetch_truncated():
        return {"2330"}, 1              # 只剩 1 檔，遠低於 1000

    out = run(fetch_truncated, prev=prev_good)
    assert out["state"] == "degraded", out
    assert out["codes"] == ["2330", "2317"], out       # 🔴 舊清單原封不動，不可被 1 檔殘缺資料覆蓋
    assert out["fetched_at"] == "2026-08-10", out       # 舊時間戳，不可假裝今天抓到

    # === 4. run()：空清單 → 一律視為失敗 ===
    def fetch_empty():
        return set(), 0

    out = run(fetch_empty, prev=prev_good)
    assert out["state"] == "degraded", out
    assert out["codes"] == ["2330", "2317"], out

    # === 5. run()：首次執行（無舊資料）+ 截斷 → empty，不是 degraded ===
    out = run(fetch_truncated, prev={})
    assert out["state"] == "empty", out
    assert out["codes"] == [], out

    # === 6. run()：抓取拋例外（HTTP失敗/逾時）→ 視同截斷處理，沿用舊資料 ===
    def fetch_boom():
        raise RuntimeError("timeout")

    out = run(fetch_boom, prev=prev_good)
    assert out["state"] == "degraded" and "抓取失敗" in out["reason"], out
    assert out["codes"] == ["2330", "2317"], out

    # === 7. run()：觀察期相對門檻（1000~1659）→ 接受寫入但帶 warning reason ===
    def fetch_borderline():
        return {f"{i}" for i in range(1200)}, 1200

    out = run(fetch_borderline, prev={})
    assert out["state"] == "ok", out           # 接受寫入，不是 degraded
    assert out["count"] == 1200, out
    assert out["reason"] and "觀察期" in out["reason"], out    # 但要留痕跡

    # === 8. count_history 累積上限 ===
    prev_long_history = {"count_history": [{"date": f"d{i}", "count": i} for i in range(HISTORY_KEEP)]}
    out = run(fetch_ok, prev=prev_long_history)
    assert len(out["count_history"]) == HISTORY_KEEP, out   # 不會無限增長

    # === 9. 本機 http.server：URL 可注入，端到端跑一次真實 HTTP（含真實 TWSE 回應格式）===
    fixture = json.dumps({
        "stat": "OK",
        "fields": ["代號", "名稱", "市場別", "開盤參考價", "限制出借(詳說明2)"],
        "data": [[f"{2000+i}", "測試股" + str(i), "集中市場", "10.0", ""] for i in range(2074)],
    }).encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(fixture)))
            self.end_headers()
            self.wfile.write(fixture)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig_url = SBL_URL
    SBL_URL = f"http://127.0.0.1:{port}"
    try:
        codes, count = fetch_sbl_list()
        assert count == 2074, count
        assert "2000" in codes, codes
    finally:
        SBL_URL = orig_url
        srv.shutdown()

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
