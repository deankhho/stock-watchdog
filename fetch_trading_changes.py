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
     的日期範圍只有「本日／一週內／一月內」三選一，查不到一個月以前），改成每天查，
     逐日累積寫進本檔，跑得夠久自然涵蓋一整季——架構跟 fetch_sbl.py／fetch_warrants.py
     同一套 state/history 模式。

2026-08-22 修正（發現 8/21 排程當天失敗，`type1=0`「本日公告」沒有補抓機制，那天的
公告永久漏掉，使用者發現「力麗/精金恢復信用交易」公告沒顯示在網站上）：改用
`type1=1`（實測回應涵蓋約 07/06~08/21，遠超過字面「一週內」，但範圍夠寬即可）
取代 `type1=0`，靠既有 `_dedup_key()` 去重機制自然吸收單日失敗——下次成功執行時，
之前漏掉那天的公告會在回應範圍內被重新看到、判斷是新資料、正常收錄，不需要額外的
補抓/回溯邏輯。多抓的資料量（171→804列，未篩選前）純粹是多一次解析，無額外外部
請求成本，dedup 保證不會重複累積。

資料源：mopsov.twse.com.tw/server-java/t39sb01（「臺灣證券交易所 & 證券櫃檯買賣中心 公告」，
Big5/cp950 編碼舊式系統，非正式 API，但實測可用；`type1=1`＝涵蓋近期較寬時間範圍，
非字面「本日」，見上方 2026-08-22 修正說明）。

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


# 2026-08-18 追加：從 content 抓「公司名（代號：XXXX）」＋所屬動作，讓使用者不必逐字讀
# 法規公文才知道是哪幾檔股票。真實公告常見「一、二、三、」編號複合列出多家公司、
# 各自不同動作（見 selftest 1b 的真實案例：同一則公告同時有停止買賣/列為變更交易方法/
# 恢復交易方法三種動作，分屬三檔不同股票，不能只抓第一個代號了事）。
#
# 2026-08-22 追加第二種真實格式（selftest 1c，9919康那香/2022聚亨案例）：信用交易類公告
# 常用阿拉伯數字編號「1.」「2.」而非中文數字「一、」，且股票用半形括號「名稱(代號)」，
# 沒有「代號：」關鍵字；來源文字換行轉空白還會把公司名切開（「聚亨」→「聚 亨」），
# 比對前要去空白。這種編號的段落標題常超過 20 字放不進 ACTION_HEADING_RE，改用關鍵字
# （暫停/停止→停止融資融券、恢復→恢復融資融券）判斷動作。
NUMBERED_ITEM_RE = re.compile(r"[一二三四五六七八九十]+、")
ARABIC_NUMBERED_ITEM_RE = re.compile(r"(?<=[：:、。\s])\d{1,2}\.(?!\d)")
ACTION_HEADING_RE = re.compile(r"^[一二三四五六七八九十]+、\s*([^：:]{2,20})[：:]")
CODE_NAME_RE = re.compile(r"([^、，。：:\s（）]{2,30})（代號[：:]\s*(\d{4})\s*）")
CODE_NAME_RE2 = re.compile(r"([^\s、，。：:()（）]{1,6}(?:\s[^\s、，。：:()（）]{1,6})?)\((\d{4})\)")


def _credit_action_from_heading(text: str):
    if "恢復" in text:
        return "恢復融資融券"
    if "暫停" in text or "停止" in text:
        return "停止融資融券"
    return None


def _refs_in_segment(seg: str, action) -> list:
    refs = []
    for name_m in CODE_NAME_RE.finditer(seg):
        refs.append({"code": name_m.group(2), "name": name_m.group(1).replace(" ", ""), "action": action})
    for name_m in CODE_NAME_RE2.finditer(seg):
        refs.append({"code": name_m.group(2), "name": name_m.group(1).replace(" ", ""), "action": action})
    return refs


def extract_stock_refs(content: str) -> list:
    """→ [{"code":.., "name":.., "action": str|None}, ...]。
    action 是該段落編號標題（如「恢復交易方法」）或關鍵字判斷出的動作（阿拉伯數字編號段落），
    沒有編號清單（單一公司單一動作的簡單公告）時一律 None——不強行從內文猜動作，避免誤判。"""
    refs = []
    markers = sorted(list(NUMBERED_ITEM_RE.finditer(content)) + list(ARABIC_NUMBERED_ITEM_RE.finditer(content)),
                      key=lambda m: m.start())
    if markers:
        bounds = [m.start() for m in markers] + [len(content)]
        for i, marker in enumerate(markers):
            seg = content[marker.start():bounds[i + 1]]
            m = ACTION_HEADING_RE.match(seg)
            action = m.group(1).strip() if m else _credit_action_from_heading(seg[:40])
            refs.extend(_refs_in_segment(seg, action))
    else:
        refs.extend(_refs_in_segment(content, None))
    return refs


def is_relevant(row: dict) -> bool:
    if row.get("department") in EXCLUDE_DEPARTMENTS:
        return False
    content = row.get("content", "")
    return any(kw in content for kw in KEYWORDS)


def fetch_recent_rows() -> list:
    """→ list[dict]（未篩選）。拋例外代表抓取失敗（HTTP/逾時/解析）。
    2026-08-22 起用 type1=1（實測涵蓋約近1.5個月）取代 type1=0（本日公告）——
    後者若當天執行失敗就永久漏抓，前者靠呼叫端 run() 既有 dedup 機制自然補回
    漏抓的日子，不需要額外邏輯，見檔案開頭 2026-08-22 修正說明。"""
    r = requests.post(URL, data={"type0": "0", "type1": "1"}, timeout=TIMEOUT)
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


def run(fetch_fn=fetch_recent_rows, prev: dict = None) -> dict:
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
    # 新項目與既有累積紀錄的舊項目都要有 stocks 欄位——自我修復，不需要另外的
    # 一次性 migration 腳本，沿用既有資料重跑一次就自動補上。
    for item in matched:
        item["stocks"] = extract_stock_refs(item.get("content", ""))

    return {"state": "ok", "fetched_at": today, "reason": None, "matched": matched}


def main():
    prev = load_prev()
    out = run(fetch_recent_rows, prev)
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

    # === 1b. extract_stock_refs()：從 content 抓「公司名（代號：XXXX）」＋所屬動作 ===
    # 2026-08-17 真實案例（第一筆真實抓到的公告，逐字複製，不是捏造的 fixture）：
    # 同一則公告用「一、二、三、」編號複合列出三家不同動作的公司，是本站目前
    # 唯一驗證過的真實格式——單一公司單一動作的簡單案例只是理論上也該支援，
    # 沒有真實樣本可對答案（同檔案開頭「尚未有真實案例驗證過」的既有教訓）。
    real_content = (
        "本公司經形式審閱國內上市公司115年第2季財務報告之相關處置如后 ，並已公告自115"
        "年8月19日起執行處置措施： 一、停止買賣： 凌巨科技股份有限公司（代號：8105）"
        "未依法令期限公告申報115年 第2季財務報告，核有本公司營業細則（下稱同細則）第50"
        "條第1項第 1款規定情事，爰將其上市有價證券停止買賣。 二、併案列為變更交易方法："
        "華冠通訊股份有限公司（代號：8101）前因有同細則第49條第1項第1 款規定情事，其上"
        "市有價證券經列為變更交易方法在案。嗣查該公司 最近期公告申報之財務報告，經會計"
        "師出具繼續經營能力存在重大不 確定性之核閱報告，符合同細則第49條第1項第3款規定，"
        "爰將其上市 有價證券併案列為變更交易方法。 三、恢復交易方法： 虹光精密工業股份有"
        "限公司（代號：2380）前因有同細則第49條第1 項第1款及第49條之2第1項第4款規定情事，"
        "其上市有價證券經列為變 更交易方法併採行分盤集合競價交易方式在案。嗣查該公司最近"
        "二期 公告申報之財務報告顯示，淨值均逾三億元並達所列示股本二分之一 以上，核已符"
        "合同細則第49條第2項第1款及第49條之2第2項第4款規 定，且無其他應列為變更交易方法"
        "及採行分盤集合競價交易方式情事 ，爰將其上市有價證券恢復交易方法。"
    )
    refs = extract_stock_refs(real_content)
    assert refs == [
        {"code": "8105", "name": "凌巨科技股份有限公司", "action": "停止買賣"},
        {"code": "8101", "name": "華冠通訊股份有限公司", "action": "併案列為變更交易方法"},
        {"code": "2380", "name": "虹光精密工業股份有限公司", "action": "恢復交易方法"},
    ], refs

    # 1c. 沒有編號清單的簡單案例（單一公司單一動作）——理論案例，尚無真實樣本
    refs = extract_stock_refs("測試股份有限公司（代號：1234）自115/08/19起變更交易方法為全額交割。")
    assert refs == [{"code": "1234", "name": "測試股份有限公司", "action": None}], refs

    # 1d. content 完全沒提到代號 → 空清單，不可拋例外
    assert extract_stock_refs("除權除息公告，無關代號") == []

    # 1e. 2026-08-22 真實案例（9919康那香/2022聚亨）：阿拉伯數字編號「1.」「2.」＋
    # 半形括號「名稱(代號)」（無「代號：」關鍵字）＋來源換行把公司名切開（「聚亨」→
    # 「聚 亨」）。修復前 extract_stock_refs() 對這則公告回傳空清單（真實 bug，見
    # data/trading_changes.json 該筆記錄曾經 stocks: []）。
    real_content_2 = (
        "公告本公司審核國內上市公司（不含金融業）公告並申報之115年第2 季財務報告每股淨值及累"
        "積虧損資料，符合暫停與恢復融資融券交易 之有價證券調整名單如下： 1.每股淨值低於票面，"
        "應暫停融資融券交易之有價證券共有2種：聚 亨(2022)、康那香(9919)。 2.每股淨值回復至票面"
        "以上，應恢復融資融券交易之有價證券共有4 種：力麗(1444)、千興(2025)、精金(3049)、晟鈦"
        "(3229)。 以上調整作業自本（115）年8月24日起實施，前開應暫停融資融券交 易之原融資買進"
        "及融券賣出之餘額，得於期限屆滿前了結。"
    )
    refs = extract_stock_refs(real_content_2)
    assert refs == [
        {"code": "2022", "name": "聚亨", "action": "停止融資融券"},
        {"code": "9919", "name": "康那香", "action": "停止融資融券"},
        {"code": "1444", "name": "力麗", "action": "恢復融資融券"},
        {"code": "2025", "name": "千興", "action": "恢復融資融券"},
        {"code": "3049", "name": "精金", "action": "恢復融資融券"},
        {"code": "3229", "name": "晟鈦", "action": "恢復融資融券"},
    ], refs

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
    assert out["matched"][0]["stocks"] == [], out       # 這筆 content 沒有「代號：」，空清單

    out2 = run(fetch_two_rows, prev=out)               # 同樣內容重跑一次
    assert len(out2["matched"]) == 1, out2              # 沒有重複累積

    # 5b. 既有累積紀錄裡缺 stocks 欄位的舊項目，重跑一次要自我修復補上（不需要另外
    # 的一次性 migration 腳本）
    prev_missing_stocks = {"state": "ok", "fetched_at": "2026-08-17", "reason": None, "matched": [
        {"content": "虹光精密工業股份有限公司（代號：2380）恢復交易方法。",
         "filed_date": "115/08/17", "filed_time": "16:25:19"}]}
    out3 = run(fetch_two_rows, prev=prev_missing_stocks)
    assert out3["matched"][0]["stocks"] == [{"code": "2380", "name": "虹光精密工業股份有限公司",
                                             "action": None}], out3

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

    # === 8. 本機 http.server：端到端測 fetch_recent_rows() 真的能解析 cp950 回應，
    #    且真的送出 type1=1（2026-08-22 修正核心，不能退回 type1=0 本日公告） ===
    fixture_big5 = fixture_html.encode("cp950")
    captured_body = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            captured_body["raw"] = self.rfile.read(length).decode("utf-8")
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
        rows = fetch_recent_rows()
        assert len(rows) == 2, rows
        assert "全額交割" in rows[0]["content"], rows[0]
        assert "type1=1" in captured_body["raw"], captured_body   # 不可退回 type1=0
    finally:
        URL = orig_url
        srv.shutdown()

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
