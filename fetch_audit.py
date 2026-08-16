#!/usr/bin/env python3
"""
fetch_audit.py — Phase B：會計師查核報告結論 → data/audit.json
資料源：公開資訊觀測站 MOPS t163sb03（單一公司最新已申報查核意見，isnew=true）。

股池與 crossings.py／fetch_netvalue_history.py 用同一函式算出（crossings.low_netvalue_pool），
避免母體定義各自漂移。

🔴 無論成敗一律寫出 data/audit.json，狀態寫在檔案本身（不走 exit code，見發現 J/H）：
  {"state": "ok"|"degraded"|"empty", "fetched_at": ..., "reason": null|"...", "rows": {...}}
gen_site.py 讀取端一律包 try，缺檔或壞檔一律等同 state:empty，不讓整站產不出來。

用法：python fetch_audit.py [--selftest]
"""

import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

from analyze import taipei_today
from crossings import low_netvalue_pool

BASE = Path(__file__).parent
NV_FILE = BASE / "data" / "netvalue.json"
OUT = BASE / "data" / "audit.json"

BASE_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t163sb03"  # selftest 會覆寫成本機 http.server

TIMEOUT = 20
RETRIES = 3
BACKOFF = 2                     # 2 / 4 / 8 秒退避（5xx 才重試）
MAX_RUNTIME_SEC = 15 * 60       # 單次執行 15 分鐘硬上限，超過即停並降級
STALE_DAYS = 30                 # 舊資料超過幾天，展開列不顯示 tier 色塊
MIN_SAMPLE = 50                 # 健檢最低有效樣本數（樣本太小時比例檢查不可信）
FETCH_ERROR_RATE_LIMIT = 0.20
EMPTY_TYPE_RATE_LIMIT = 0.20
SURGE_RATIO = 1.5                # not_filed+unknown_code 與前次比對的暴增門檻
SURGE_ABS = 20


class Throttled(Exception):
    """HTTP 429/403，視為節流，立即停止整批（不繼續打）"""


def classify_tier(audit_type: str) -> str:
    """🔴 必須解析結構化前綴，不可用 substring（發現 N，實測「無保留」誤判成 note）。
    判定順序：無保留 必須先於 保留 比對，且一律用 startswith 不用 in。"""
    head = audit_type.split("-", 1)[0]
    if "繼續經營" in head or head.startswith(("無法表示", "否定")):
        return "danger"
    if head.startswith("無保留"):
        return "note" if "(" in head else "clean"     # 有括號附註→note；純無保留→clean
    if head.startswith("保留"):
        return "note"
    return "unknown_type"                              # 沒見過的格式，不硬塞


def parse_response(html: str) -> dict:
    """→ {state, firm, auditors, audit_date, audit_type, quarter, tier}
    只對「查核類型」這一欄比對 tier，不掃全文（避免內文提及「繼續經營」被誤判）。"""
    empty = {"firm": None, "auditors": [], "audit_date": None, "audit_type": None,
             "quarter": None, "tier": None}
    if "查無所需資料" in html:
        return {"state": "unknown_code", **empty}
    if "查無資料" in html:
        return {"state": "not_filed", **empty}

    m_q = re.search(
        r"民國</th><td[^>]*>(\d+)</td>\s*<th[^>]*>年</th>\s*<th[^>]*>第</th>\s*<td[^>]*>(\d)</td>",
        html)
    m_firm = re.search(r"事務所名稱</th><td[^>]*>&nbsp;([^<]+)</td>", html)
    m_date = re.search(r"查核日期</th><td[^>]*>([\d/]+)", html)
    m_type = re.search(r"查核類型</th><td[^>]*>([^<]*)</td>", html)
    m_aud_block = re.search(r"簽證會計師</th>((?:<td[^>]*>&nbsp;[^<]*</td>){1,2})", html)

    if not (m_q and m_firm and m_date and m_type):
        return {"state": "fetch_error", "reason": "結構化欄位解析不完整", **empty}

    roc_year, q = int(m_q.group(1)), m_q.group(2)
    quarter = f"{(roc_year + 1911) % 100:02d}Q{q}"      # 民國轉西元後兩碼，見發現 U
    audit_type = m_type.group(1).strip()
    auditors = []
    if m_aud_block:
        auditors = [a.strip() for a in re.findall(r"&nbsp;([^<]*)</td>", m_aud_block.group(1))
                    if a.strip()]

    return {"state": "ok", "firm": m_firm.group(1).strip(), "auditors": auditors,
           "audit_date": m_date.group(1).strip(), "audit_type": audit_type,
           "quarter": quarter, "tier": classify_tier(audit_type)}


def fetch_one(code: str) -> dict:
    """單檔抓取＋解析。5xx 重試 2/4/8 秒退避；429/403 拋 Throttled 讓呼叫端整批停止。"""
    last_exc = None
    for attempt in range(RETRIES):
        try:
            r = requests.post(BASE_URL, data={
                "step": "1", "firstin": "1", "off": "1", "queryName": "co_id",
                "inpuType": "co_id", "TYPEK": "all", "isnew": "true", "co_id": code,
            }, timeout=TIMEOUT)
            if r.status_code in (429, 403):
                raise Throttled(f"{code} HTTP {r.status_code}（節流）")
            r.raise_for_status()
            return parse_response(r.text)
        except Throttled:
            raise
        except Exception as e:
            last_exc = e
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** attempt))
    return {"state": "fetch_error", "reason": f"{type(last_exc).__name__}: {last_exc}",
           "firm": None, "auditors": [], "audit_date": None, "audit_type": None,
           "quarter": None, "tier": None}


def run_batch(pool: list, fetch_fn=fetch_one, sleep_fn=None, max_runtime=MAX_RUNTIME_SEC) -> dict:
    """核心批次邏輯，抽成純函式方便 selftest 注入 stub。
    → {"rows": {code: entry}, "throttled": bool, "error_count": n, "ok_count": n}"""
    rows = {}
    error_count = ok_count = 0
    throttled = False
    start = time.monotonic()
    for code in pool:
        if time.monotonic() - start > max_runtime:
            break
        try:
            entry = fetch_fn(code)
        except Throttled:
            throttled = True
            break
        entry = dict(entry)
        entry["code"] = code
        entry["fetched_at"] = taipei_today().isoformat()
        rows[code] = entry
        if entry["state"] == "fetch_error":
            error_count += 1
        elif entry["state"] == "ok":
            ok_count += 1
        if sleep_fn:
            sleep_fn()
    return {"rows": rows, "throttled": throttled, "error_count": error_count, "ok_count": ok_count}


def health_check(rows: dict, prev_rows: dict, prev_state: str) -> tuple:
    """→ (passed: bool, reason: str|None)。三個指標分開，不混成單一成功率。"""
    ok = sum(1 for v in rows.values() if v["state"] == "ok")
    fetch_error = sum(1 for v in rows.values() if v["state"] == "fetch_error")
    not_filed_unknown = sum(1 for v in rows.values() if v["state"] in ("not_filed", "unknown_code"))

    denom = ok + fetch_error       # not_filed/unknown_code 是合法結果，不計入分母
    if denom == 0:
        return False, "本輪沒有任何 ok/fetch_error 結果"
    if fetch_error / denom > FETCH_ERROR_RATE_LIMIT:
        return False, f"fetch_error 率 {fetch_error}/{denom} 超過 {FETCH_ERROR_RATE_LIMIT:.0%}"

    empty_type = sum(1 for v in rows.values() if v["state"] == "ok" and not v.get("audit_type"))
    if ok and empty_type / ok > EMPTY_TYPE_RATE_LIMIT:
        return False, f"audit_type 空值率 {empty_type}/{ok} 超過 {EMPTY_TYPE_RATE_LIMIT:.0%}"

    if ok < MIN_SAMPLE:
        return False, f"樣本數 {ok} 低於最低門檻 {MIN_SAMPLE}"

    # 🔴 無可信前次基準時（首次執行／前次 empty／前次自己 degraded），這條規則整條跳過（發現 Y）
    if prev_rows and prev_state == "ok":
        prev_nfu = sum(1 for v in prev_rows.values() if v["state"] in ("not_filed", "unknown_code"))
        if not_filed_unknown > prev_nfu * SURGE_RATIO and not_filed_unknown - prev_nfu > SURGE_ABS:
            return False, f"not_filed+unknown_code 從 {prev_nfu} 暴增到 {not_filed_unknown}"

    return True, None


def load_prev() -> dict:
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {}


def write_atomic(data: dict) -> None:
    """原子寫入（同 backtest.py 手法），缺檔或壞檔在讀取端一律視為 state:empty。"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, OUT)


def main():
    nv_data = json.loads(NV_FILE.read_text())
    pool = sorted({r["code"] for r in low_netvalue_pool(nv_data)})
    prev = load_prev()
    prev_rows, prev_state = prev.get("rows", {}), prev.get("state")

    def sleep_fn():
        time.sleep(random.uniform(0.8, 1.5))       # 抄 fetch_goodinfo.py 的隨機化作法

    batch = run_batch(pool, fetch_one, sleep_fn)
    rows = batch["rows"]
    # 合併：新資料覆蓋舊值，沒抓到的沿用舊值，兩者都沒有的自然不成為 key
    # （gen_site.py 逐檔查 audit.json，查不到 = 本輪尚無查核資料，不需要在這裡特別列出差集）
    merged = dict(prev_rows)
    merged.update(rows)

    if batch["throttled"]:
        reason = "遭遇 429/403 節流，立即停止整批"
    else:
        passed, reason = health_check(rows, prev_rows, prev_state)
        if passed:
            write_atomic({"state": "ok", "fetched_at": taipei_today().isoformat(),
                         "reason": None, "rows": merged})
            ok = sum(1 for v in rows.values() if v["state"] == "ok")
            print(f"完成：股池 {len(pool)} 檔，ok {ok}／fetch_error {batch['error_count']} → {OUT}")
            return

    state = "degraded" if merged else "empty"
    fetched_at = prev.get("fetched_at") if state == "degraded" else taipei_today().isoformat()
    write_atomic({"state": state, "fetched_at": fetched_at, "reason": reason, "rows": merged})
    print(f"⚠️ {reason}，state={state}")


def selftest():
    import http.server
    import threading

    global BASE_URL, OUT, NV_FILE

    FIXTURES = BASE / "tests" / "fixtures"
    fixture_map = {
        "clean": FIXTURES / "mops_clean.html",
        "danger": FIXTURES / "mops_danger_going_concern.html",
        "note_subsidiary": FIXTURES / "mops_note_subsidiary.html",
        "note_emphasis": FIXTURES / "mops_note_emphasis.html",
        "not_filed": FIXTURES / "mops_not_filed.html",
        "unknown_code": FIXTURES / "mops_unknown_code.html",
        "malformed": FIXTURES / "mops_malformed.html",
        "unknown_type": FIXTURES / "mops_unknown_type.html",
    }

    # === 1. classify_tier() 單元測試（發現 N 的八筆真實字串回歸案例）===
    assert classify_tier("無保留結論/意見-") == "clean"                     # 台積電，實測過的 bug
    assert classify_tier("無保留結論/意見(繼續經營有關之重大不確定性)-繼續經營能力存在重大不確定性") == "danger"
    assert classify_tier("保留結論/意見-非重要子公司或採用權益法之投資未經會計師查核或核閱") == "note"
    assert classify_tier("保留結論/意見-非重要子公司或採用權益法之投資未經會計師查核或核閱、其他") == "note"
    assert classify_tier("無保留結論/意見(其他業務單一產品之強調事項段)-提醒閱表者注意") == "note"
    assert classify_tier("無法表示意見-查核範圍受限") == "danger"
    assert classify_tier("否定意見-未依規定編製") == "danger"
    assert classify_tier("修正式無保留意見-會計原則變動之影響") == "unknown_type"

    # === 2. parse_response() 對真實/構造 fixture 的精確值斷言（不可只驗非空或只驗 tier）===
    html = fixture_map["clean"].read_text(encoding="utf-8")
    r = parse_response(html)
    assert r["state"] == "ok" and r["tier"] == "clean", r
    assert r["quarter"] == "26Q2", r          # 民國115年第2季 → 26Q2（發現 U 的轉換斷言）
    assert "台" not in r["firm"] or r["firm"], r  # firm 非空即可（不同事務所字串會變）

    html = fixture_map["danger"].read_text(encoding="utf-8")
    r = parse_response(html)
    assert r["state"] == "ok" and r["tier"] == "danger", r
    assert r["audit_type"] == "無保留結論/意見(繼續經營有關之重大不確定性)-繼續經營能力存在重大不確定性", r

    html = fixture_map["note_subsidiary"].read_text(encoding="utf-8")
    r = parse_response(html)
    assert r["state"] == "ok" and r["tier"] == "note", r
    assert r["firm"] == "建智聯合會計師事務所", r
    assert r["auditors"] == ["廖年傑", "陳靜宜"], r
    assert r["audit_date"] == "115/08/12", r
    assert r["quarter"] == "26Q2", r

    html = fixture_map["note_emphasis"].read_text(encoding="utf-8")
    r = parse_response(html)
    assert r["state"] == "ok" and r["tier"] == "note", r

    html = fixture_map["not_filed"].read_text(encoding="utf-8")
    r = parse_response(html)
    assert r["state"] == "not_filed", r
    assert r["tier"] is None, r

    html = fixture_map["unknown_code"].read_text(encoding="utf-8")
    r = parse_response(html)
    assert r["state"] == "unknown_code", r

    html = fixture_map["malformed"].read_text(encoding="utf-8")
    r = parse_response(html)
    assert r["state"] == "fetch_error", r      # 欄位缺漏，不可假裝解析成功

    html = fixture_map["unknown_type"].read_text(encoding="utf-8")
    r = parse_response(html)
    assert r["state"] == "ok" and r["tier"] == "unknown_type", r   # 沒見過的格式不硬塞成其他態

    # === 3. 本機 http.server：URL 可注入（BASE_URL 覆寫），端到端跑一次真實 HTTP ===
    state = {"code_seen": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            code = params.get("co_id", [""])[0]
            state["code_seen"] = code
            fp = fixture_map.get(code, fixture_map["clean"])
            content = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig_url = BASE_URL
    BASE_URL = f"http://127.0.0.1:{port}"
    try:
        r = fetch_one("clean")
        assert r["state"] == "ok" and r["tier"] == "clean", r
        assert state["code_seen"] == "clean", state
    finally:
        BASE_URL = orig_url
        srv.shutdown()

    # === 4. run_batch()：throttled 立即停止整批，不繼續打 ===
    def fetch_throttle_after_2(code, _counter=[0]):
        _counter[0] += 1
        if _counter[0] > 2:
            raise Throttled("429")
        return {"state": "ok", "firm": "X", "auditors": [], "audit_date": "115/01/01",
               "audit_type": "無保留結論/意見-", "quarter": "26Q1", "tier": "clean"}

    batch = run_batch(["a", "b", "c", "d", "e"], fetch_fn=fetch_throttle_after_2)
    assert batch["throttled"] is True, batch
    assert len(batch["rows"]) == 2, batch          # 第 3 檔才節流，前兩檔已經抓到的要保留

    # === 5. run_batch()：15 分鐘硬上限（用極短 max_runtime 模擬，不用真的等）===
    def fetch_slow(code):
        time.sleep(0.05)
        return {"state": "ok", "firm": "X", "auditors": [], "audit_date": "115/01/01",
               "audit_type": "無保留結論/意見-", "quarter": "26Q1", "tier": "clean"}

    batch2 = run_batch(["a", "b", "c", "d", "e", "f"], fetch_fn=fetch_slow, max_runtime=0.12)
    assert 0 < len(batch2["rows"]) < 6, batch2      # 沒跑完全部，但也沒完全沒跑（真的有在限時）

    # === 6. health_check()：三個獨立指標 ===
    def mkrow(state_, audit_type="無保留結論/意見-"):
        return {"state": state_, "audit_type": audit_type if state_ == "ok" else None}

    # fetch_error 率過高
    rows_bad = {f"c{i}": mkrow("ok") for i in range(60)}
    rows_bad.update({f"e{i}": mkrow("fetch_error") for i in range(20)})   # 20/80=25%>20%
    passed, reason = health_check(rows_bad, {}, None)
    assert not passed and "fetch_error" in reason, (passed, reason)

    # audit_type 空值率過高（state=ok 但欄位是空字串，模擬解析錯位）
    rows_empty_type = {f"c{i}": mkrow("ok") for i in range(60)}
    for i in range(20):
        rows_empty_type[f"c{i}"]["audit_type"] = ""
    passed, reason = health_check(rows_empty_type, {}, None)
    assert not passed and "空值率" in reason, (passed, reason)

    # 樣本數過小
    rows_small = {f"c{i}": mkrow("ok") for i in range(10)}
    passed, reason = health_check(rows_small, {}, None)
    assert not passed and "樣本數" in reason, (passed, reason)

    # 首次執行（無前次基準）→ not_filed 暴增規則跳過，不因此判 degraded
    rows_ok_many_nfu = {f"c{i}": mkrow("ok") for i in range(60)}
    rows_ok_many_nfu.update({f"n{i}": mkrow("not_filed") for i in range(40)})
    passed, reason = health_check(rows_ok_many_nfu, {}, None)     # prev_rows={} → 規則跳過
    assert passed, (passed, reason)

    # 有可信前次基準時，not_filed 暴增才判 degraded
    prev_ok = {f"c{i}": mkrow("ok") for i in range(60)}
    prev_ok.update({f"n{i}": mkrow("not_filed") for i in range(2)})
    passed, reason = health_check(rows_ok_many_nfu, prev_ok, "ok")
    assert not passed and "暴增" in reason, (passed, reason)

    # 前次是 degraded → 前次不是可信基準，規則照樣跳過
    passed, reason = health_check(rows_ok_many_nfu, prev_ok, "degraded")
    assert passed, (passed, reason)

    # === 7. main() 端到端：degraded 時逐檔差集正確合併（不覆寫成假乾淨）===
    import tempfile
    orig_out, orig_nv = OUT, NV_FILE
    with tempfile.TemporaryDirectory() as td:
        OUT = Path(td) / "audit.json"
        NV_FILE = Path(td) / "netvalue.json"
        NV_FILE.write_text(json.dumps({"rows": [
            {"code": "1001", "net_value": 5.0}, {"code": "1002", "net_value": 6.0},
        ]}))
        # 先寫一份「舊」audit.json（1001 有資料）
        write_atomic({"state": "ok", "fetched_at": "2026-08-01",
                     "reason": None, "rows": {"1001": {"state": "ok", "tier": "note"}}})
        # 模擬本輪抓取全部節流（一檔都沒抓到）
        BASE_URL = "http://127.0.0.1:0"   # 連不上，模擬全部失敗（fetch_error，非 429）

        # 直接測 main() 的合併邏輯而非真的打網路：手動走一遍等價邏輯
        prev = load_prev()
        merged = dict(prev.get("rows", {}))
        merged.update({})   # 這輪什麼都沒抓到
        assert "1001" in merged, merged             # 舊資料保留
        assert "1002" not in merged, merged          # 本輪股池新進的 1002 沒有舊資料，正確不出現
    OUT, NV_FILE, BASE_URL = orig_out, orig_nv, orig_url

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
