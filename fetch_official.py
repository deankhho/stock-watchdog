#!/usr/bin/env python3
"""
fetch_official.py — S2：官方名單與市場對照
資料源（2026-07-06 實測可用，記錄於 STATUS.md）：
- TWSE openapi /exchangeReport/TWT85U：集中市場證券變更交易（全額交割名單）
- TPEx openapi /tpex_cmode：上櫃變更交易/分盤/管理股票/停止交易
  （AlteredTrading="Ｙ"＝變更交易；TPEx SSL 憑證缺 SKI → verify=False）
- isin.twse.com.tw strMode=2/4：上市/上櫃全清單（市場別對照，Big5 編碼）
輸出：data/official.json
註：官方「停止融資融券」逐檔名單 openapi 無現成端點（只有餘額表），
信用警戒由 analyze.py 以淨值<10 推算；待補官方名單來源。
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import urllib3

urllib3.disable_warnings()
BASE = Path(__file__).parent
OUT = BASE / "data" / "official.json"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TPE = ZoneInfo("Asia/Taipei")

RETRIES = 3
BACKOFF = 2                  # 2 / 4 / 8 秒
MARKET_MAP_MIN_RATIO = 0.8   # 與前次比對的截斷偵測門檻
MARKET_MAP_FLOOR = 500       # 無舊檔可比時，單一 mode 的絕對下限
REASON_MAXLEN = 200


def fetch_url(url: str, *, verify: bool = True, timeout: int = 20,
              tries: int = RETRIES, as_json: bool = True):
    """帶重試的抓取。

    要涵蓋的失敗態（2026-08-10 實測）：
      - 半途斷線 `Response ended prematurely`（8/7 那次紅字的死因）
      - HTTP 200 但回非 JSON（TWSE openapi 間歇性維護頁）

    註：`r.json()` 拋的 requests.exceptions.JSONDecodeError 同時繼承
    RequestException 與 ValueError，光 catch 前者就接得到；這裡多列 ValueError
    是防後續若改用 json.loads() 直接解析，不是因為現在會漏。
    """
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers=H, verify=verify)
            r.raise_for_status()
            return r.json() if as_json else r.content
        except (requests.RequestException, ValueError) as e:
            last = e
            if i < tries - 1:
                time.sleep(BACKOFF * (2 ** i))
    raise RuntimeError(f"{url} 重試 {tries} 次仍失敗：{last}")


def fetch_twse_full_delivery() -> list:
    return [{"code": d["Code"].strip(), "name": d["Name"].strip(), "market": "上市"}
            for d in fetch_url("https://openapi.twse.com.tw/v1/exchangeReport/TWT85U")
            if d.get("Code", "").strip()]


def fetch_tpex_cmode() -> tuple:
    full, other = [], []
    for d in fetch_url("https://www.tpex.org.tw/openapi/v1/tpex_cmode", verify=False):
        code = d.get("SecuritiesCompanyCode", "").strip()
        name = d.get("CompanyName", "").strip()
        if not code:
            continue
        entry = {"code": code, "name": name, "market": "上櫃"}
        if d.get("AlteredTrading", "").strip() == "Ｙ":
            full.append(entry)
        else:  # 管理股票/停止交易等其他狀態，網站另欄提示
            flags = [k for k in ("ManagedStock", "SuspensionOfTrading", "PeriodicTrading")
                     if d.get(k, "").strip() == "Ｙ"]
            if flags:
                other.append({**entry, "flags": flags})
    return full, other


def fetch_disposal() -> dict:
    """處置股（雙市場）→ {code: {reason, period, market}}（濾掉權證等非 4-6 碼個股）"""
    out = {}
    for d in fetch_url("https://openapi.twse.com.tw/v1/announcement/punish"):
        code = d.get("Code", "").strip()
        if re.match(r"^\d{4}$", code):        # 個股（權證 6 碼濾掉）
            out[code] = {"reason": d.get("ReasonsOfDisposition", ""),
                         "period": d.get("DispositionPeriod", ""), "market": "上市"}
    for d in fetch_url("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
                       verify=False):
        code = d.get("SecuritiesCompanyCode", "").strip()
        if re.match(r"^\d{4}$", code):
            out[code] = {"reason": d.get("DispositionReasons", ""),
                         "period": d.get("DispositionPeriod", ""), "market": "上櫃"}
    return out


def fetch_margin_status() -> dict:
    """信用交易現況 → {code: {"in_universe": bool, "mark": "O/X/OX/..."}}
    官方註記（MI_MARGN notes 實測）：O=停止融資, X=停止融券, !=停止買賣；
    不在餘額表內（上市）＝非融資融券標的。上櫃全板都在表內，只看 Note。"""
    st = {}
    j = fetch_url("https://www.twse.com.tw/exchangeReport/MI_MARGN"
                  "?response=json&selectType=ALL", timeout=30)
    t = next(t for t in j.get("tables", []) if any("代號" in str(f) for f in t.get("fields", [])))
    for row in t["data"]:
        code = str(row[0]).strip()
        if re.match(r"^\d{4}$", code):
            st[code] = {"in_universe": True, "mark": str(row[-1]).strip()}
    for d in fetch_url("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance",
                       verify=False):
        code = d.get("SecuritiesCompanyCode", "").strip()
        if re.match(r"^\d{4}$", code):
            st[code] = {"in_universe": True, "mark": (d.get("Note") or "").strip()}
    return st


def fetch_market_map(prev_map: dict = None) -> dict:
    """isin.twse.com.tw 上市(2)/上櫃(4) 全清單 → {code: 市場}

    ⚠️ 這裡的門檻是**截斷偵測（failure detector）**，不是完整性驗證。
    它只能回答「這次是不是明顯少了一大截」，**不能**證明 market_map 正確。
    上市那支是 7.7MB Big5 大檔，正是 8/7 `Response ended prematurely` 的來源；
    而 decode(errors="replace") 會讓半途斷掉的內容安靜地少解出一批代號、不報錯，
    所以必須在解析後比對筆數，光靠 HTTP 狀態碼看不出來。
    """
    mp = {}
    for mode, market in [(2, "上市"), (4, "上櫃")]:
        floor = MARKET_MAP_FLOOR
        if prev_map:
            prev_n = sum(1 for v in prev_map.values() if v == market)
            if prev_n:
                floor = int(prev_n * MARKET_MAP_MIN_RATIO)
        last_n = None
        for i in range(RETRIES):
            raw = fetch_url(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}",
                            timeout=40, as_json=False)
            codes = re.findall(r">(\d{4,6})　", raw.decode("big5", errors="replace"))
            last_n = len(codes)
            if last_n >= floor:
                for code in codes:
                    mp.setdefault(code, market)
                break
            if i < RETRIES - 1:
                time.sleep(BACKOFF * (2 ** i))
        else:
            raise RuntimeError(f"{market}清單只解析出 {last_n} 檔（門檻 {floor}），"
                               f"疑似回應被截斷")
    return mp


def make_degradable(prev: dict, degraded: dict):
    """回傳 degradable(name, fn, restore)：次要來源失敗時沿用舊資料並標記降級。

    為什麼不留空：margin_status 若是空 dict，analyze.stock_status 的
    `m is None` 分支會把全部 1864 檔判成「非信用交易標的」——**靜默產出錯誤結論**，
    比顯示標記過的舊資料危險得多。舊檔也沒有該區塊時，升級為致命。
    """
    def degradable(name, fn, restore=None):
        try:
            return fn()
        except Exception as e:
            old = (restore or (lambda p: p.get(name)))(prev)
            if not old:
                raise SystemExit(f"{name} 抓取失敗且無舊資料可沿用：{e}")
            # 只記來源時間戳，不記 age_days——存下來的天數會隨管線停擺而凍結，
            # 年齡一律由讀取端（網站 JS／CI）當下計算
            degraded[name] = {"source_fetched_at": prev.get("fetched_at"),
                              "reason": f"{type(e).__name__}: {e}"[:REASON_MAXLEN]}
            print(f"⚠️  {name} 抓取失敗，沿用 {prev.get('fetched_at')} 的資料：{e}")
            return old
    return degradable


def main():
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    degraded = {}
    degradable = make_degradable(prev, degraded)

    # 致命來源：失敗就不產出（維持「不產出舊資料」原則）
    try:
        twse = fetch_twse_full_delivery()
        market_map = fetch_market_map(prev.get("market_map"))
    except Exception as e:
        sys.exit(f"官方資料抓取失敗（致命來源）：{e}（不產出舊資料）")

    # 可降級來源：失敗沿用舊區塊並標記
    tpex_full, tpex_other = degradable(
        "tpex_cmode", fetch_tpex_cmode,
        # 舊檔沒有獨立存 tpex_full，從 full_delivery 濾上櫃還原，不動 schema
        lambda p: (([x for x in p.get("full_delivery", []) if x.get("market") == "上櫃"],
                    p.get("tpex_other_flags", []))
                   if p.get("full_delivery") else None))
    disposal = degradable("disposal", fetch_disposal)
    margin_status = degradable("margin_status", fetch_margin_status)

    if not twse or not market_map:
        sys.exit("TWSE 名單或市場對照為空，視為失敗")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "fetched_at": datetime.now(TPE).isoformat(),   # 帶 +08:00，見 fetch_goodinfo 註解
        "full_delivery": twse + tpex_full,
        "tpex_other_flags": tpex_other,
        "market_map": market_map,
        "disposal": disposal,
        "margin_status": margin_status,
        "degraded": degraded,
    }, ensure_ascii=False))
    print(f"完成：全額交割 上市 {len(twse)} + 上櫃 {len(tpex_full)} 檔；"
          f"處置 {len(disposal)}；信用現況 {len(margin_status)} 檔 → {OUT}"
          + (f"；⚠️ 降級 {list(degraded)}" if degraded else ""))


def selftest():
    import http.server
    import threading

    state = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state["n"] += 1
            if self.path.startswith("/flaky"):        # 前兩次 500，第三次成功
                if state["n"] < 3:
                    self.send_error(500)
                    return
                body = b'[{"ok": 1}]'
            elif self.path.startswith("/always500"):
                self.send_error(500)
                return
            elif self.path.startswith("/notjson"):    # HTTP 200 但內容不是 JSON
                body = b'<html>maintenance</html>'
            else:
                body = b'[]'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    globals()["BACKOFF"] = 0            # 測試不需要真的等 2/4/8 秒
    try:
        # 重試到成功
        state["n"] = 0
        assert fetch_url(f"{base}/flaky") == [{"ok": 1}]
        assert state["n"] == 3, f"應該重試到第 3 次才成功，實際 {state['n']}"

        # 三次全失敗 → 拋出
        state["n"] = 0
        try:
            fetch_url(f"{base}/always500")
            raise AssertionError("全部失敗時應該拋出")
        except RuntimeError:
            pass

        # HTTP 200 但內容非 JSON：要重試滿次數再拋出，不可當成功（8/10 實測 TWSE 會這樣）
        state["n"] = 0
        try:
            fetch_url(f"{base}/notjson")
            raise AssertionError("非 JSON 應該視為失敗")
        except RuntimeError:
            pass
        assert state["n"] == RETRIES, f"非 JSON 應重試 {RETRIES} 次，實際 {state['n']}"
    finally:
        srv.shutdown()

    # --- degradable：舊檔有值→沿用並記結構化 degraded ---
    prev = {"fetched_at": "2026-08-06T12:28:24+08:00", "disposal": {"1234": {}}}
    deg = {}
    d = make_degradable(prev, deg)
    def boom():
        raise RuntimeError("x" * 500)
    assert d("disposal", boom) == {"1234": {}}
    assert deg["disposal"]["source_fetched_at"] == "2026-08-06T12:28:24+08:00"
    assert "age_days" not in deg["disposal"], "不得存會凍結的 age_days"
    assert len(deg["disposal"]["reason"]) <= REASON_MAXLEN, "reason 應截斷"

    # --- 舊檔沒有該區塊 → 升級為致命 ---
    deg2 = {}
    d2 = make_degradable({"fetched_at": "2026-08-06T12:28:24+08:00"}, deg2)
    try:
        d2("disposal", boom)
        raise AssertionError("無舊資料可沿用時應為致命")
    except SystemExit:
        pass

    # --- market_map 截斷偵測：相對門檻 ---
    prev_map = {str(i): "上市" for i in range(1000)}
    assert int(1000 * MARKET_MAP_MIN_RATIO) == 800
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
