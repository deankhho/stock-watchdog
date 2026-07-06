#!/usr/bin/env python3
"""
fetch_goodinfo.py — S1：抓 goodinfo.tw 每股淨值最低排行（上市＋上櫃）
輸出：data/netvalue.json

已實證（BLUEPRINT）：goodinfo 擋非瀏覽器抓取 → Playwright + 系統 Chrome。
策略：淨值由低到高逐頁抓，抓到淨值 ≥ 12 元即停（門檻 10 元留 buffer）。
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

import pandas as pd
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
OUT = BASE / "data" / "netvalue.json"

CHROME = "/usr/bin/google-chrome"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LAUNCH_ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]

STOP_NET_VALUE = 12.0     # 抓到這個淨值就停
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

    if len(rows) < 50:
        sys.exit(f"僅抓到 {len(rows)} 筆（<50），視為失敗，不輸出")

    rows.sort(key=lambda r: r["net_value"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "fetched_at": datetime.now().isoformat(),
        "source": "goodinfo.tw 每股淨值最低排行",
        "count": len(rows),
        "rows": rows,
    }, ensure_ascii=False, indent=1))
    mk = {}
    for r in rows:
        mk[r["market"]] = mk.get(r["market"], 0) + 1
    print(f"完成：{len(rows)} 檔 → {OUT}；市場分佈 {mk}")


if __name__ == "__main__":
    main()
