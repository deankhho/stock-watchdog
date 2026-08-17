#!/usr/bin/env python3
"""
fetch_warrants.py — 認售／認購權證標的清單 → data/warrants.json（§8 追加項目）

原計畫（§8）判定「認售權證沒有現成的逐檔端點，不猜、不寫死」，一律顯示「需自行查詢」。
2026-08-16 使用者追問「只要有或沒有就好不行？」，重新查證後找到可用組合：

  1. TWSE openapi t187ap37_L（權證發行資訊）：含「標的證券/指數」欄，但只有名稱、
     是全部歷史發行（含已到期），且沒有代號欄。過濾「履約截止日 >= 今天」才是目前有效的，
     再依「權證類型」分成認售（put_codes）／認購（call_codes）兩組：
     認售跟放空同方向（買認售權證≈看跌的替代工具），認購跟作多同方向（買認購權證≈看漲的
     替代工具）。predict_in／edge（預測打入全額交割）用認售；recover（恢復候選）用認購
     （2026-08-17 使用者確認：了解進出全額交割股時，除了直接買賣股票，還有哪些工具可用）。
  2. ISIN 上市/上櫃全清單（isin.twse.com.tw）：拿來把①的「標的名稱」轉成代號。
     🔴 必須用 cp950 解碼，不可用 big5——big5 對「碁」等字元解碼失敗變亂碼，
     會讓 6285 啟碁／2353 宏碁這類真實股票的名稱比對失敗、被誤判成「查無此權證標的」
     （2026-08-16 實測抓到；fetch_official.py 現有的 big5 解碼只取數字代號不受影響，
     這裡因為要比對「名稱」文字，同一個 bug 才會真的咬人）。

只保留展開列用得到的判斷：該代號目前是否有仍在有效期間內的認售／認購權證。
跟 SBL 一樣只做「有／沒有」的資格判斷，不代表流動性或實際能不能成交。

用法：python fetch_warrants.py [--selftest]
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

from analyze import taipei_today

BASE = Path(__file__).parent
OUT = BASE / "data" / "warrants.json"

ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp"       # selftest 會覆寫成本機 http.server
WARRANT_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap37_L"
TIMEOUT = 30

ABS_FLOOR_PUT = 50           # 認售：唯一觀測值 2026-08-16 為 180 檔，這是保守猜的下限，
                              # 待累積更多天數觀測後再校準（同 fetch_sbl.py 的作法）
BASELINE_PUT = 180
ABS_FLOOR_CALL = 100         # 認購：唯一觀測值 2026-08-17 為 468 檔，這是保守猜的下限，
                              # 待累積更多天數觀測後再校準
BASELINE_CALL = 468
HISTORY_KEEP = 30


def _roc_to_date(s: str):
    """民國年月日字串（如 '1150730'）→ date；格式不對回 None，不拋例外。"""
    if not s or len(s) < 6:
        return None
    try:
        y = int(s[:-4]) + 1911
        m = int(s[-4:-2])
        d = int(s[-2:])
        return date(y, m, d)
    except ValueError:
        return None


def fetch_isin_name_to_code() -> dict:
    """上市(2)+上櫃(4) 全清單 → {name: code}。
    🔴 用 cp950 不是 big5（見檔案開頭說明），否則「啟碁」「宏碁」等字會解碼失敗。"""
    mapping = {}
    for mode in (2, 4):
        r = requests.get(ISIN_URL, params={"strMode": mode}, timeout=TIMEOUT)
        r.raise_for_status()
        text = r.content.decode("cp950", errors="replace")
        for m in re.finditer(r">(\d{4})　([^<]+?)</td><td[^>]*>[A-Z0-9]{12}<", text):
            code, name = m.group(1), m.group(2).strip()
            mapping[name] = code
    return mapping


def fetch_active_underlyings() -> tuple:
    """→ (put_names, call_names)：目前仍在履約期間內的認售／認購權證標的名稱集合。"""
    r = requests.get(WARRANT_API_URL, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    today = taipei_today()
    put_names, call_names = set(), set()
    for x in data:
        wtype = x.get("權證類型")
        if wtype not in ("認售", "認購"):
            continue
        exp = _roc_to_date(x.get("履約截止日", ""))
        if not (exp and exp >= today):
            continue
        name = x.get("標的證券/指數", "")
        if not name:
            continue
        (put_names if wtype == "認售" else call_names).add(name)
    return put_names, call_names


def resolve_codes(fetch_isin=fetch_isin_name_to_code, fetch_underlyings=fetch_active_underlyings) -> tuple:
    """→ (put_codes, call_codes, count_put, count_call, unmatched_names)。
    unmatched 多半是指數／ETF／期貨等非個股標的，不是 bug，但保留供健檢／除錯用。"""
    name_to_code = fetch_isin()
    put_names, call_names = fetch_underlyings()
    put_codes = {name_to_code[n] for n in put_names if n in name_to_code}
    call_codes = {name_to_code[n] for n in call_names if n in name_to_code}
    unmatched = (put_names | call_names) - set(name_to_code.keys())
    return put_codes, call_codes, len(put_codes), len(call_codes), unmatched


def classify_fetch(count_put: int, count_call: int) -> tuple:
    """→ (accept: bool, severity: 'ok'|'reject', reason: str|None)。
    認售／認購任一邊過低都拒收（同一支 API 一次抓回來，任一邊壞掉多半代表解析壞了，
    不是那一邊真的沒有權證），避免半套資料寫進站上。"""
    if count_put == 0 or count_call == 0:
        return False, "reject", f"認售 {count_put} 檔／認購 {count_call} 檔，其中一邊是空清單，視為失敗"
    if count_put < ABS_FLOOR_PUT:
        return False, "reject", f"認售筆數 {count_put} 低於絕對地板 {ABS_FLOOR_PUT}，疑似解析壞掉"
    if count_call < ABS_FLOOR_CALL:
        return False, "reject", f"認購筆數 {count_call} 低於絕對地板 {ABS_FLOOR_CALL}，疑似解析壞掉"
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
    put_codes = prev.get("put_codes") or []
    call_codes = prev.get("call_codes") or []
    state = "degraded" if (put_codes or call_codes) else "empty"
    fetched_at = prev.get("fetched_at") if state == "degraded" else taipei_today().isoformat()
    return {"state": state, "fetched_at": fetched_at, "reason": reason,
           "put_codes": put_codes, "call_codes": call_codes,
           "count_put": prev.get("count_put", 0), "count_call": prev.get("count_call", 0),
           "count_history": prev.get("count_history", [])}


def run(resolve_fn=resolve_codes, prev: dict = None) -> dict:
    """核心邏輯，純函式方便 selftest 注入。"""
    prev = prev if prev is not None else load_prev()
    try:
        put_codes, call_codes, count_put, count_call, _unmatched = resolve_fn()
    except Exception as e:
        return build_degraded(prev, f"抓取失敗：{type(e).__name__}: {e}")

    accept, severity, reason = classify_fetch(count_put, count_call)
    if not accept:
        return build_degraded(prev, reason)

    today = taipei_today().isoformat()
    history = (prev.get("count_history") or [])[-(HISTORY_KEEP - 1):] + [
        {"date": today, "count_put": count_put, "count_call": count_call}]
    return {"state": "ok", "fetched_at": today, "reason": reason,
           "put_codes": sorted(put_codes), "call_codes": sorted(call_codes),
           "count_put": count_put, "count_call": count_call, "count_history": history}


def main():
    prev = load_prev()
    out = run(resolve_codes, prev)
    write_atomic(out)
    if out["state"] != "ok":
        print(f"::warning::{out['reason']}，state={out['state']}")
    print(f"完成：認售權證標的 {out.get('count_put', 0)} 檔／認購權證標的 {out.get('count_call', 0)} 檔，"
          f"state={out['state']} → {OUT}")


def selftest():
    import http.server
    import threading

    global ISIN_URL, WARRANT_API_URL

    today = date(2026, 8, 16)

    # === 1. _roc_to_date()：格式轉換與防呆 ===
    assert _roc_to_date("1150730") == date(2026, 7, 30)
    assert _roc_to_date("") is None
    assert _roc_to_date("abc") is None
    assert _roc_to_date("123") is None            # 太短

    # === 2. classify_fetch()：認售／認購各自有地板，任一邊過低都拒收 ===
    assert classify_fetch(0, 999) == (False, "reject",
        "認售 0 檔／認購 999 檔，其中一邊是空清單，視為失敗")
    assert classify_fetch(999, 0)[0] is False
    accept, sev, reason = classify_fetch(10, 999)
    assert not accept and "認售" in reason and "絕對地板" in reason, (accept, sev, reason)
    accept, sev, reason = classify_fetch(999, 10)
    assert not accept and "認購" in reason and "絕對地板" in reason, (accept, sev, reason)
    accept, sev, reason = classify_fetch(180, 999)
    assert accept and sev == "ok", (accept, sev, reason)

    # === 3. fetch_active_underlyings()：認售／認購分開收，都只認履約截止日未過 ===
    fake_data = [
        {"權證類型": "認售", "標的證券/指數": "A股", "履約截止日": "1150901"},   # 有效認售
        {"權證類型": "認購", "標的證券/指數": "B股", "履約截止日": "1150901"},   # 有效認購
        {"權證類型": "認售", "標的證券/指數": "C股", "履約截止日": "1140101"},   # 已過期認售，排除
        {"權證類型": "認購", "標的證券/指數": "D股", "履約截止日": "1140101"},   # 已過期認購，排除
        {"權證類型": "認售", "標的證券/指數": "A股", "履約截止日": "1150801"},   # 同標的重複，去重
    ]

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    orig_get = requests.get
    requests.get = lambda *a, **kw: _FakeResp(fake_data)
    try:
        # taipei_today 由 analyze 提供，這裡直接測邏輯用固定日期比較，改用內部函式手動驗證
        put_names, call_names = set(), set()
        for x in fake_data:
            exp = _roc_to_date(x["履約截止日"])
            if not (exp and exp >= today):
                continue
            (put_names if x["權證類型"] == "認售" else call_names).add(x["標的證券/指數"])
        assert put_names == {"A股"}, put_names      # C股(過期) 排除
        assert call_names == {"B股"}, call_names    # D股(過期) 排除
    finally:
        requests.get = orig_get

    # === 4. resolve_codes()：name→code 橋接，認售／認購分開算，無法比對的名稱不拋例外 ===
    def fake_isin():
        return {"A股": "1111", "B股": "2222"}

    def fake_underlyings():
        return {"A股", "C股（查無此代號，指數型）"}, {"B股"}

    put_codes, call_codes, count_put, count_call, unmatched = resolve_codes(fake_isin, fake_underlyings)
    assert put_codes == {"1111"}, put_codes
    assert call_codes == {"2222"}, call_codes
    assert count_put == 1, count_put
    assert count_call == 1, count_call
    assert unmatched == {"C股（查無此代號，指數型）"}, unmatched

    # === 5. run()：截斷 → 沿用舊清單，不可寫入殘缺資料 ===
    prev_good = {"state": "ok", "fetched_at": "2026-08-10", "reason": None,
                "put_codes": ["1234", "5678"], "call_codes": ["9999"],
                "count_put": 2, "count_call": 1, "count_history": []}

    def resolve_truncated():
        return {"1111"}, set(), 1, 0, set()

    out = run(resolve_truncated, prev=prev_good)
    assert out["state"] == "degraded", out
    assert out["put_codes"] == ["1234", "5678"], out    # 🔴 舊清單原封不動
    assert out["call_codes"] == ["9999"], out
    assert out["fetched_at"] == "2026-08-10", out

    # === 6. run()：正常情況 + 抓取拋例外都測過 ===
    def resolve_ok():
        return ({f"{1000+i}" for i in range(180)}, {f"{2000+i}" for i in range(300)}, 180, 300, set())

    out = run(resolve_ok, prev={})
    assert out["state"] == "ok" and out["count_put"] == 180 and out["count_call"] == 300, out
    assert out["put_codes"] == sorted(out["put_codes"]), out
    assert out["call_codes"] == sorted(out["call_codes"]), out

    def resolve_boom():
        raise RuntimeError("timeout")

    out = run(resolve_boom, prev=prev_good)
    assert out["state"] == "degraded" and "抓取失敗" in out["reason"], out
    assert out["put_codes"] == ["1234", "5678"], out
    assert out["call_codes"] == ["9999"], out

    # === 7. 首次執行 + 失敗 → empty 不是 degraded ===
    out = run(resolve_truncated, prev={})
    assert out["state"] == "empty", out

    # === 8. 本機 http.server：端到端測 fetch_isin_name_to_code() 的 cp950 解碼
    #    （這是這次真正抓到的 bug，回歸測試必須釘住「碁」這種 big5 解不動的字）===
    isin_html = ('<html><body><table>'
                '<tr><td>6285　啟碁</td><td>TW0006285000</td></tr>'
                '</table></body></html>').encode("cp950")

    class ISINHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")   # 故意不帶 charset，逼真實情境
            self.send_header("Content-Length", str(len(isin_html)))
            self.end_headers()
            self.wfile.write(isin_html)
        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), ISINHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig_isin_url = ISIN_URL
    ISIN_URL = f"http://127.0.0.1:{port}"
    try:
        mapping = fetch_isin_name_to_code()
        assert mapping.get("啟碁") == "6285", mapping   # 🔴 big5 解碼會讓這個字變亂碼比對失敗
    finally:
        ISIN_URL = orig_isin_url
        srv.shutdown()

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
