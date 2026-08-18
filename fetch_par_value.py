#!/usr/bin/env python3
"""
fetch_par_value.py — 上市/上櫃普通股每股面額 → data/par_value.json（面額門檻修正用）

2026-08-17 查證發現：證交所營業細則第49條全額交割觸發條件是「淨值已低於財務報告所列示
股本二分之一」，不是固定 5 元——只有面額 10 元的股票，股本二分之一才剛好等於每股淨值 5 元。
`analyze.py` 原本把 NET_VALUE_FULL_DELIVERY=5.0 當成全部個股共用的固定門檻，面額非 10 元
的個股（實測 TWSE 1095 檔中 20 檔、TPEx 890 檔中 15 檔，合計約 35 檔）門檻套錯。

資料源：
  TWSE openapi t187ap03_L（上市公司基本資料）欄位「普通股每股面額」
  TPEx openapi mopsfin_t187ap03_O（上櫃公司基本資料）欄位 ParValueOfCommonStock
兩者格式相同，都是「新台幣                 10.0000元」這種字串，非新台幣計價
（美元／泰銖／港幣，實測各僅 1 檔）或「無面額」「不適用」的一律標記為 None（無法判定），
不可用 5.0 硬套，交給 analyze.py 用預設值 fallback。

用法：python fetch_par_value.py [--selftest]
"""

import json
import os
import re
import sys
from pathlib import Path

import requests

from analyze import taipei_today

BASE = Path(__file__).parent
OUT = BASE / "data" / "par_value.json"

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TIMEOUT = 30

ABS_FLOOR = 1500       # 唯一觀測值 2026-08-17 為 1973 檔（TWSE+TPEx合併、濾掉非新台幣計價/
                        # 無面額後的實際可用筆數），這是保守猜的下限，待累積更多天數觀測後再校準
BASELINE = 1973
HISTORY_KEEP = 30

NTD_RE = re.compile(r"新台幣\s*([\d.]+)\s*元")


def _parse_ntd(raw: str):
    """「新台幣  10.0000元」→ 10.0；非新台幣計價／無面額／不適用一律回 None，不猜。"""
    if not raw:
        return None
    m = NTD_RE.search(raw)
    if not m:
        return None
    return float(m.group(1))


def fetch_par_twse() -> dict:
    """→ {code: par_value}，非新台幣計價/無面額的代號不收錄。"""
    r = requests.get(TWSE_URL, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    out = {}
    for x in data:
        code = x.get("公司代號")
        par = _parse_ntd(x.get("普通股每股面額", ""))
        if code and par is not None:
            out[code] = par
    return out


def fetch_par_tpex() -> dict:
    """→ {code: par_value}，非新台幣計價/無面額的代號不收錄。
    🔴 TPEx openapi SSL 憑證缺 SKI，比照 fetch_official.py 既有做法用 verify=False。"""
    r = requests.get(TPEX_URL, timeout=TIMEOUT, verify=False)
    r.raise_for_status()
    data = r.json()
    out = {}
    for x in data:
        code = x.get("SecuritiesCompanyCode")
        par = _parse_ntd(x.get("ParValueOfCommonStock", ""))
        if code and par is not None:
            out[code] = par
    return out


def fetch_par_values(fetch_twse=fetch_par_twse, fetch_tpex=fetch_par_tpex) -> dict:
    """→ {code: par_value}，兩個市場合併（代號不重複，直接合併即可）。"""
    merged = {}
    merged.update(fetch_twse())
    merged.update(fetch_tpex())
    return merged


def classify_fetch(count: int) -> tuple:
    """→ (accept: bool, severity: 'ok'|'reject', reason: str|None)"""
    if count == 0:
        return False, "reject", "空清單，視為失敗"
    if count < ABS_FLOOR:
        return False, "reject", f"筆數 {count} 低於絕對地板 {ABS_FLOOR}，疑似解析壞掉"
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
    par = prev.get("par") or {}
    state = "degraded" if par else "empty"
    fetched_at = prev.get("fetched_at") if state == "degraded" else taipei_today().isoformat()
    return {"state": state, "fetched_at": fetched_at, "reason": reason,
           "par": par, "count": prev.get("count", 0),
           "count_history": prev.get("count_history", [])}


def run(fetch_fn=fetch_par_values, prev: dict = None) -> dict:
    """核心邏輯，純函式方便 selftest 注入。"""
    prev = prev if prev is not None else load_prev()
    try:
        par = fetch_fn()
    except Exception as e:
        return build_degraded(prev, f"抓取失敗：{type(e).__name__}: {e}")

    count = len(par)
    accept, severity, reason = classify_fetch(count)
    if not accept:
        return build_degraded(prev, reason)

    today = taipei_today().isoformat()
    history = (prev.get("count_history") or [])[-(HISTORY_KEEP - 1):] + [{"date": today, "count": count}]
    return {"state": "ok", "fetched_at": today, "reason": reason,
           "par": par, "count": count, "count_history": history}


def main():
    prev = load_prev()
    out = run(fetch_par_values, prev)
    write_atomic(out)
    if out["state"] != "ok":
        print(f"::warning::{out['reason']}，state={out['state']}")
    print(f"完成：面額資料 {out.get('count', 0)} 檔，state={out['state']} → {OUT}")


def selftest():
    import http.server
    import threading

    global TWSE_URL, TPEX_URL

    # === 1. _parse_ntd()：新台幣抓數字，其他一律 None ===
    assert _parse_ntd("新台幣                 10.0000元") == 10.0
    assert _parse_ntd("新台幣                  1.0000元") == 1.0
    assert _parse_ntd("新台幣                  2.5000元") == 2.5
    assert _parse_ntd("無面額") is None
    assert _parse_ntd("不適用") is None
    assert _parse_ntd("美金0.05元") is None
    assert _parse_ntd("泰銖1元") is None
    assert _parse_ntd("") is None
    assert _parse_ntd(None) is None

    # === 2. fetch_par_values()：TWSE／TPEx 合併，非新台幣個股不收錄 ===
    def fake_twse():
        return {"2330": 10.0, "7835": 10.0}

    def fake_tpex():
        return {"6488": 10.0, "1259": 1.0}

    merged = fetch_par_values(fake_twse, fake_tpex)
    assert merged == {"2330": 10.0, "7835": 10.0, "6488": 10.0, "1259": 1.0}, merged

    # === 3. classify_fetch() ===
    assert classify_fetch(0) == (False, "reject", "空清單，視為失敗")
    accept, sev, reason = classify_fetch(100)
    assert not accept and "絕對地板" in reason, (accept, sev, reason)
    accept, sev, reason = classify_fetch(1985)
    assert accept and sev == "ok", (accept, sev, reason)

    # === 4. run()：截斷 → 沿用舊清單，不可寫入殘缺資料 ===
    prev_good = {"state": "ok", "fetched_at": "2026-08-10", "reason": None,
                "par": {"2330": 10.0, "1259": 1.0}, "count": 2, "count_history": []}

    def fetch_truncated():
        return {f"{1000+i}": 10.0 for i in range(100)}

    out = run(fetch_truncated, prev=prev_good)
    assert out["state"] == "degraded", out
    assert out["par"] == {"2330": 10.0, "1259": 1.0}, out       # 🔴 舊資料原封不動
    assert out["fetched_at"] == "2026-08-10", out

    # === 5. run()：正常情況 + 抓取拋例外都測過 ===
    def fetch_ok():
        return {f"{1000+i}": 10.0 for i in range(1985)}

    out = run(fetch_ok, prev={})
    assert out["state"] == "ok" and out["count"] == 1985, out

    def fetch_boom():
        raise RuntimeError("timeout")

    out = run(fetch_boom, prev=prev_good)
    assert out["state"] == "degraded" and "抓取失敗" in out["reason"], out
    assert out["par"] == {"2330": 10.0, "1259": 1.0}, out

    # === 6. 首次執行 + 失敗 → empty 不是 degraded ===
    out = run(fetch_truncated, prev={})
    assert out["state"] == "empty", out

    # === 7. 本機 http.server：端到端測 fetch_par_twse() 真的能解析真實 API 回應格式 ===
    fixture = json.dumps([
        {"公司代號": "2330", "公司名稱": "台積電", "普通股每股面額": "新台幣                 10.0000元"},
        {"公司代號": "9999", "公司名稱": "測試無面額", "普通股每股面額": "無面額"},
    ]).encode("utf-8")

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
    orig_url = TWSE_URL
    TWSE_URL = f"http://127.0.0.1:{port}"
    try:
        par = fetch_par_twse()
        assert par == {"2330": 10.0}, par         # 9999(無面額) 不收錄
    finally:
        TWSE_URL = orig_url
        srv.shutdown()

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
