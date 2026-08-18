#!/usr/bin/env python3
"""
fetch_trading_changes.py — 每日抓「變更交易方法／信用交易」公告 → data/trading_changes.json（新頁籤用）

2026-08-17 查證：
  1. 沒有查到「證交所/櫃買中心幾天內一定公告」的明文規定，只查到《審閱上市公司財務報告
     作業程序》第9條「應於檢送財務報告期限日後三個工作日內彙報主管機關」（彙報金管會，
     不是對市場公告的期限）。
  2. 用本站自己的歷史資料（data/listing_dates.json 裡精確到日的 13 筆列入紀錄）反推，
     發現乾淨的規律：季報截止日（5/15、8/14、11/14）後 5 個日曆日／3 個營業日、
     年報截止日（3/31）後 7 個日曆日／5 個營業日，同一批股票精確落在同一天
     （TWSE 分批作業的證據）。跟第9條的「三個工作日」大致吻合。
  3. 因此不做一次性「查本季」的歷史回溯（來源系統 mopsov.twse.com.tw/server-java/t39sb01
     的日期範圍只有「本日／一週內／一月內」三選一，查不到一個月以前），改成**每天查
     「本日公告」**，逐日累積寫進本檔，跑得夠久自然涵蓋一整季——架構跟 fetch_sbl.py／
     fetch_warrants.py 同一套 state/history 模式。

資料源：mopsov.twse.com.tw/server-java/t39sb01（「臺灣證券交易所 & 證券櫃檯買賣中心 公告」，
Big5/cp950 編碼舊式系統，非正式 API，但實測可用；type1=0 為「本日公告」）。

篩選規則（使用者明確要求：不要處置股／注意股，只要因財報而變更交易方法及信用交易的變化）：
  - 排除 部門='監視部'（處置有價證券、注意交易資訊等都在這個部門）
  - 部門='交易部' 且內容含關鍵字（變更交易方法／信用交易／停止融資融券／恢復融資融券）才收錄
  - 🔴 關鍵字清單基於營業細則第49條原文用字＋既有規則頁面用語推導，**尚未見過真實案例驗證
    過**（過去一個月查無真正的列入/解除全額交割公告，見上面第2點的規律：預期本季案例落在
    8/19 附近，屆時才有真實資料可以校準關鍵字，見下方 KEYWORDS 註解）。

健檢邏輯刻意跟 fetch_sbl.py／fetch_warrants.py 不同：那兩支資料量接近常數（借券標的、
有效權證數），筆數驟降代表解析壞掉；這裡「今天有幾則變更交易公告」天生就大幅波動、
0 則是完全正常的常態（真正的全額交割列入/解除一季可能就那幾次），所以**不能用筆數地板
判斷健康**。只要 HTTP 抓取成功、頁面能解析出表格結構，就算 state=ok，即使 matched 是空的。

用法：python fetch_trading_changes.py [--selftest]
"""

import json
import os
import re
import sys
from pathlib import Path

import lxml.html
import requests

from analyze import taipei_today

BASE = Path(__file__).parent
OUT = BASE / "data" / "trading_changes.json"

URL = "https://mopsov.twse.com.tw/server-java/t39sb01"     # selftest 會覆寫成本機 http.server
TIMEOUT = 30
HISTORY_KEEP = 200      # 累積上限（一季案例數遠低於此，純防呆）

EXCLUDE_DEPARTMENTS = {"監視部"}   # 處置有價證券、注意交易資訊都在這個部門，使用者明確要求排除
# 🔴 尚未有真實案例驗證過，見檔案開頭說明；預期 26Q2 案例落在 2026-08-19 附近，
# 屆時要回頭核對這份清單有沒有漏抓或抓錯。
KEYWORDS = ("變更交易方法", "停止信用交易", "恢復信用交易", "停止融資融券", "恢復融資融券")

BR_RE = re.compile(r"<br\s*/?>", re.I)


def _cell_text(td) -> str:
    """<td> → 純文字，<br> 轉成空白（避免跨行的關鍵字被無縫黏成不同的字）。"""
    raw = lxml.html.tostring(td, encoding="unicode")
    raw = BR_RE.sub(" ", raw)
    node = lxml.html.fromstring(raw)
    return re.sub(r"\s+", " ", node.text_content()).strip()


def parse_bulletin(html_text: str) -> list:
    """純函式：解碼後的 HTML 字串 → list[dict]，每筆是一則公告。頁面結構跟預期不符
    （找不到任何 <table border='1'>）會拋 ValueError，呼叫端視同抓取失敗處理。"""
    tree = lxml.html.fromstring(html_text)
    tables = tree.xpath("//table[@border='1']")
    if not tables:
        raise ValueError("找不到公告表格，頁面結構可能已變更")
    rows = []
    for tr in tables[0].xpath("./tr")[1:]:            # 第一列是表頭，跳過
        tds = tr.xpath("./td")
        if len(tds) < 8:
            continue
        rows.append({
            "category": _cell_text(tds[1]),
            "department": _cell_text(tds[2]),
            "content": _cell_text(tds[3]),
            "announce_date": _cell_text(tds[4]),
            "deadline_date": _cell_text(tds[5]),
            "filed_date": _cell_text(tds[6]),
            "filed_time": _cell_text(tds[7]),
        })
    return rows


def is_relevant(row: dict) -> bool:
    if row.get("department") in EXCLUDE_DEPARTMENTS:
        return False
    content = row.get("content", "")
    return any(kw in content for kw in KEYWORDS)


def fetch_today_rows() -> list:
    """→ list[dict]（未篩選）。拋例外代表抓取失敗（HTTP/逾時/解析）。"""
    r = requests.post(URL, data={"type0": "0", "type1": "0"}, timeout=TIMEOUT)
    r.raise_for_status()
    text = r.content.decode("cp950", errors="replace")
    return parse_bulletin(text)


def _dedup_key(row: dict) -> str:
    """同一天重跑不可產生重複紀錄；用建檔日期+時間+內容前30字當去重鍵，
    足夠區分同一天的不同公告，不需要真正的唯一 ID（來源沒提供）。"""
    return f"{row.get('filed_date','')}|{row.get('filed_time','')}|{row.get('content','')[:30]}"


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
    """抓取/解析失敗：沿用舊的累積紀錄，不可假裝今天檢查過。"""
    matched = prev.get("matched") or []
    state = "degraded" if matched else "empty"
    fetched_at = prev.get("fetched_at") if state == "degraded" else taipei_today().isoformat()
    return {"state": state, "fetched_at": fetched_at, "reason": reason,
           "matched": matched}


def run(fetch_fn=fetch_today_rows, prev: dict = None) -> dict:
    """核心邏輯，純函式方便 selftest 注入。0 筆符合關鍵字是正常狀態，不是失敗
    （跟 fetch_sbl.py／fetch_warrants.py 的「空清單=失敗」刻意不同，見檔案開頭說明）。"""
    prev = prev if prev is not None else load_prev()
    try:
        rows = fetch_fn()
    except Exception as e:
        return build_degraded(prev, f"抓取失敗：{type(e).__name__}: {e}")

    today = taipei_today().isoformat()
    matched = list(prev.get("matched") or [])
    seen_keys = {_dedup_key(m) for m in matched}
    new_hits = [r for r in rows if is_relevant(r)]
    for r in new_hits:
        key = _dedup_key(r)
        if key not in seen_keys:
            item = dict(r)
            item["seen_at"] = today
            matched.append(item)
            seen_keys.add(key)
    matched = matched[-HISTORY_KEEP:]

    return {"state": "ok", "fetched_at": today, "reason": None, "matched": matched}


def main():
    prev = load_prev()
    out = run(fetch_today_rows, prev)
    write_atomic(out)
    if out["state"] != "ok":
        print(f"::warning::{out['reason']}，state={out['state']}")
    print(f"完成：累積 {len(out.get('matched', []))} 則變更交易/信用交易公告，"
          f"state={out['state']} → {OUT}")


def selftest():
    import http.server
    import threading

    global URL

    # === 1. _cell_text()：<br> 轉空白，不黏字 ===
    frag = lxml.html.fromstring("<td>變更交易<br>方法為全額交割</td>")
    assert _cell_text(frag) == "變更交易 方法為全額交割", _cell_text(frag)

    # === 2. is_relevant()：部門排除＋關鍵字判斷 ===
    assert is_relevant({"department": "交易部", "content": "OO自115/08/19起變更交易方法為全額交割"})
    assert is_relevant({"department": "交易部", "content": "OO自115/08/19起恢復信用交易"})
    assert not is_relevant({"department": "監視部", "content": "變更交易方法為全額交割者，以人工管制"})  # 處置公告誤中關鍵字，仍要排除
    assert not is_relevant({"department": "交易部", "content": "上市開始買賣之認購權證上市掛牌參考價"})  # 無關鍵字

    # === 3. parse_bulletin()：真實頁面結構（截斷版）能正確解析出 8 欄 ===
    fixture_html = """<html><body><table border='1'>
<tr bgcolor='#BACDFF'><th>等級</th><th>類別</th><th>部門</th><th>內容</th><th>公告日期</th><th>截止日期</th><th>建檔日期</th><th>建檔時間</th></tr>
<tr bgcolor='#D0D0FF'>
<td align='center'>一般</td>
<td>上下市櫃、停止及暫停交易</td><td>交易部</td><td>測試股(1234)自115/08/19起<br>變更交易方法為全額交割。</td>
<td align='center'>115/08/19</td>
<td align='center'>115/08/19</td>
<td align='center'>115/08/19</td>
<td align='center'>09:00:00</td>
</tr>
<tr bgcolor='#BACDFF'>
<td align='center'>一般</td>
<td>處置有價證券</td><td>監視部</td><td>某股處置公告，內容含變更交易方法為全額交割者字樣</td>
<td align='center'>115/08/19</td><td align='center'>115/08/26</td>
<td align='center'>115/08/19</td><td align='center'>10:00:00</td>
</tr>
</table></body></html>"""
    rows = parse_bulletin(fixture_html)
    assert len(rows) == 2, rows
    assert rows[0]["department"] == "交易部", rows[0]
    assert "變更交易方法為全額交割" in rows[0]["content"], rows[0]
    assert rows[1]["department"] == "監視部", rows[1]

    # === 4. parse_bulletin()：找不到表格 → 拋例外 ===
    try:
        parse_bulletin("<html><body>無表格</body></html>")
        assert False, "應該要拋例外"
    except ValueError:
        pass

    # === 5. run()：篩選＋累積＋去重（同一天重跑不可產生重複紀錄） ===
    def fetch_two_rows():
        return [
            {"department": "交易部", "content": "測試股A變更交易方法為全額交割",
             "category": "上下市櫃、停止及暫停交易", "announce_date": "115/08/19",
             "deadline_date": "115/08/19", "filed_date": "115/08/19", "filed_time": "09:00:00"},
            {"department": "監視部", "content": "處置公告", "category": "處置有價證券",
             "announce_date": "115/08/19", "deadline_date": "115/08/26",
             "filed_date": "115/08/19", "filed_time": "10:00:00"},
        ]

    out = run(fetch_two_rows, prev={})
    assert out["state"] == "ok", out
    assert len(out["matched"]) == 1, out              # 監視部那筆被排除
    assert out["matched"][0]["content"] == "測試股A變更交易方法為全額交割", out

    out2 = run(fetch_two_rows, prev=out)               # 同樣內容重跑一次
    assert len(out2["matched"]) == 1, out2              # 沒有重複累積

    # === 6. run()：0 筆符合關鍵字＝正常，state 仍是 ok（不是失敗！） ===
    def fetch_none_relevant():
        return [{"department": "交易部", "content": "除權除息公告",
                 "category": "除權除息", "announce_date": "115/08/19",
                 "deadline_date": "115/08/19", "filed_date": "115/08/19", "filed_time": "09:00:00"}]

    out = run(fetch_none_relevant, prev={})
    assert out["state"] == "ok", out                    # 🔴 不是 degraded/empty
    assert out["matched"] == [], out

    # === 7. run()：抓取失敗 → 沿用舊累積紀錄，不可假裝檢查過 ===
    prev_good = {"state": "ok", "fetched_at": "2026-08-18",
                "matched": [{"content": "舊紀錄", "filed_date": "115/08/18", "filed_time": "09:00:00"}]}

    def fetch_boom():
        raise RuntimeError("timeout")

    out = run(fetch_boom, prev=prev_good)
    assert out["state"] == "degraded" and "抓取失敗" in out["reason"], out
    assert out["matched"] == prev_good["matched"], out
    assert out["fetched_at"] == "2026-08-18", out         # 舊時間戳，不可假裝今天抓過

    # === 8. 本機 http.server：端到端測 fetch_today_rows() 真的能解析 cp950 回應 ===
    fixture_big5 = fixture_html.encode("cp950")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=big5")
            self.send_header("Content-Length", str(len(fixture_big5)))
            self.end_headers()
            self.wfile.write(fixture_big5)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig_url = URL
    URL = f"http://127.0.0.1:{port}"
    try:
        rows = fetch_today_rows()
        assert len(rows) == 2, rows
        assert "全額交割" in rows[0]["content"], rows[0]
    finally:
        URL = orig_url
        srv.shutdown()

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
