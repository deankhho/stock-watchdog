#!/usr/bin/env python3
"""
fetch_goodinfo.py — S1：抓 goodinfo.tw 每股淨值最低排行（上市＋上櫃）
輸出：data/netvalue.json

已實證（BLUEPRINT）：goodinfo 擋非瀏覽器抓取 → Playwright + 系統 Chrome。
策略：淨值由低到高逐頁抓，抓到淨值 ≥ 17 元即停（門檻 15 元留 buffer，
供「最近一季掉落」判定保留前季基準，見 crossings.py）。
失敗即報錯退出，不得用舊資料靜默頂替。
"""

import io
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
OUT = BASE / "data" / "netvalue.json"

CHROME = "/usr/bin/google-chrome"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LAUNCH_ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]

STOP_NET_VALUE = 17.0     # 抓到這個淨值就停
MAX_PAGES = 30            # 保險上限

# goodinfo 排行頁：MARKET_CAT=熱門排行 + INDUSTRY_CAT=每股淨值最低
# RANK 參數控制分頁（0 起算，每頁 300 檔）；市場用 FL_MARKET 過濾不可靠，
# 排行榜本身混合上市/上櫃，表格內有「市場」欄，直接從欄位取
LIST_URL = ("https://goodinfo.tw/tw/StockList.asp?RPT_TIME=&MARKET_CAT=熱門排行"
            "&INDUSTRY_CAT=每股淨值最低@@每股淨值@@每股淨值最低&RANK={rank}")


def parse_tables(html: str) -> pd.DataFrame | None:
    """從頁面 HTML 找出含代號/名稱/每股淨值的表格"""
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return None
    for df in tables:
        # goodinfo 欄名含空格（「每股 淨值 (元)」，2026-07-06 實測）→ 去空格再比對
        cols = [("".join(dict.fromkeys(map(str, c))) if isinstance(c, tuple) else str(c))
                .replace(" ", "") for c in df.columns]
        joined = "|".join(cols)
        if "代號" in joined and "每股淨值" in joined:
            df.columns = cols
            return df
    return None


def pick_col(cols, *keywords):
    """找出欄名含全部關鍵字的第一個欄位"""
    for c in cols:
        if all(k in c for k in keywords):
            return c
    return None


TPE = ZoneInfo("Asia/Taipei")
MIN_ROWS = 250              # 正常 300 檔；低於此視為抓取不完整
MAX_BLANK_RATIO = 0.05      # 關鍵欄位空白比例上限


def check_rows_health(rows: list) -> tuple:
    """寫檔前的內容健全性檢查 → (致命訊息, 警告訊息)。

    為什麼需要：時間戳只證明「程序跑完了」，不證明「抓到好資料」。
    若 goodinfo 改版讓某欄解析不到，流程仍會走完並寫下新的 fetched_at，
    網站上的「資料年齡」就會顯示 0 天——用來監控靜默失敗的指標本身被繞過。

    只檢查「會安靜壞掉」的欄位：
      - nv_quarter：欄位改名時 pick_col 回 None → 全部變 ""，
        analyze.detect_new_reports 的 `q != prev["quarter"]` 永遠不成立，
        新財報偵測整組靜默死亡（不會有任何錯誤訊息）
      - price：欄位改名 → 全部 None，不影響分級，故僅警告
    code/name/net_value 欄位若消失會直接 KeyError 崩掉，屬吵鬧失敗，不需在此攔。
    """
    fatal, warn = [], []
    n = len(rows)
    if n < MIN_ROWS:
        fatal.append(f"僅 {n} 筆（< {MIN_ROWS}），抓取不完整")
    if n:
        blank_q = sum(1 for r in rows if not r.get("nv_quarter"))
        if blank_q / n > MAX_BLANK_RATIO:
            fatal.append(f"財報季度欄空白 {blank_q}/{n}——欄位可能改名，"
                         f"新財報偵測會靜默失效")
        if all(r.get("price") is None for r in rows):
            warn.append("股價欄全為 None——欄位可能改名（不影響分級）")
    return fatal, warn


def selftest():
    ok = [{"code": "1234", "name": "測試", "price": 5.0,
           "net_value": 4.0, "nv_quarter": "26Q2", "market": ""}] * 300
    assert check_rows_health(ok) == ([], [])

    assert check_rows_health(ok[:200])[0], "筆數不足應為致命"

    bad_q = [dict(r) for r in ok]
    for r in bad_q[:30]:                       # 10% 空白 > 5% 門檻
        r["nv_quarter"] = ""
    assert bad_q is not ok and check_rows_health(bad_q)[0], "季度欄大量空白應為致命"

    edge_q = [dict(r) for r in ok]
    for r in edge_q[:12]:                      # 4% < 5% 門檻，不該觸發
        r["nv_quarter"] = ""
    assert check_rows_health(edge_q)[0] == [], "4% 空白不應致命"

    no_price = [dict(r, price=None) for r in ok]
    f, w = check_rows_health(no_price)
    assert f == [] and w, "股價欄全空應只警告不致命"

    print("selftest OK")


def main():
    rows, seen = [], set()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True,
                                    args=LAUNCH_ARGS)
        page = browser.new_page(user_agent=UA,
                                viewport={"width": 1400, "height": 900})
        stop = False
        for pg_no in range(MAX_PAGES):
            url = LIST_URL.format(rank=pg_no)
            for attempt in range(3):
                try:
                    # networkidle 等不到（goodinfo 廣告持續載入，2026-07-06 實測 timeout）
                    # → domcontentloaded + 輪詢等 XHR 把資料填進 #tblStockList
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_selector("#tblStockList", timeout=20000)
                    break
                except Exception as e:
                    print(f"第 {pg_no} 頁載入失敗（{attempt+1}/3）：{e}")
                    time.sleep(8)
            else:
                browser.close()
                sys.exit("連續載入失敗，中止（不產出舊資料）")

            # 輪詢直到表格有資料（selector 出現≠資料已填，2026-07-06 實測）
            df = None
            for _ in range(10):
                df = parse_tables(page.content())
                if df is not None and len(df) > 5:
                    break
                time.sleep(3)
            if df is None or df.empty:
                # 第一頁就沒表格＝被擋；印 title 供診斷
                print(f"第 {pg_no} 頁無資料表。頁面 title={page.title()!r}")
                if pg_no == 0:
                    browser.close()
                    sys.exit("疑似被 goodinfo 擋下，中止。可改 headless=False 重試")
                break

            cols = list(df.columns)
            c_code = pick_col(cols, "代號")
            c_name = pick_col(cols, "名稱")
            c_nv = pick_col(cols, "每股淨值")
            c_price = pick_col(cols, "成交") or pick_col(cols, "股價")
            c_quarter = pick_col(cols, "財報季度")   # 淨值出自哪一季財報

            added = 0
            for _, r in df.iterrows():
                code = str(r[c_code]).strip()
                if not re.match(r"^\d{4,6}$", code) or code in seen:
                    continue
                try:
                    nv = float(r[c_nv])
                except (ValueError, TypeError):
                    continue
                try:
                    price = float(r[c_price]) if c_price else None
                except (ValueError, TypeError):
                    price = None
                quarter = str(r[c_quarter]).strip() if c_quarter else ""
                seen.add(code)
                # market 此表無欄位，S3 以官方 ISIN 清單補上
                rows.append({"code": code, "name": str(r[c_name]).strip(),
                             "price": price, "net_value": nv,
                             "nv_quarter": quarter, "market": ""})
                added += 1
                if nv >= STOP_NET_VALUE:
                    stop = True
            print(f"第 {pg_no} 頁：+{added} 檔（累計 {len(rows)}），"
                  f"最後淨值 {rows[-1]['net_value'] if rows else '-'}")
            if stop:
                break
            time.sleep(random.uniform(3, 6))
        browser.close()

    # 健全性檢查：不通過就非零退出、不覆寫 netvalue.json
    # （沿用本檔既有「失敗不頂替」原則——這也是網站「資料年齡」指標可信的前提）
    fatal, warn = check_rows_health(rows)
    for w in warn:
        print(f"⚠️  {w}")
    if fatal:
        sys.exit("內容健全性檢查未過，不輸出：" + "；".join(fatal))

    rows.sort(key=lambda r: r["net_value"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        # 帶 +08:00：runner 跑 UTC、家用機跑 Taipei，無時區字串會讓兩邊語意差 8 小時，
        # 前端 Date.parse() 又當本地時間解讀 → 資料年齡無法精確計算
        "fetched_at": datetime.now(TPE).isoformat(),
        "source": "goodinfo.tw 每股淨值最低排行",
        "count": len(rows),
        "rows": rows,
    }, ensure_ascii=False, indent=1))
    # 註：market 欄本表無資料，一律留空由 analyze.py 以官方 ISIN 清單補（見上方 rows.append）。
    # 舊版這裡印「市場分佈 {'': 300}」，看起來像解析壞掉，實際是設計行為——改印真正有意義的欄位。
    qs = {}
    for r in rows:
        qs[r["nv_quarter"]] = qs.get(r["nv_quarter"], 0) + 1
    print(f"完成：{len(rows)} 檔 → {OUT}；財報季度分佈 {qs}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
