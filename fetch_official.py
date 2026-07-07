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
from datetime import datetime
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()
BASE = Path(__file__).parent
OUT = BASE / "data" / "official.json"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch_twse_full_delivery() -> list:
    r = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/TWT85U",
                     timeout=20, headers=H)
    r.raise_for_status()
    return [{"code": d["Code"].strip(), "name": d["Name"].strip(), "market": "上市"}
            for d in r.json() if d.get("Code", "").strip()]


def fetch_tpex_cmode() -> tuple:
    r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_cmode",
                     timeout=20, headers=H, verify=False)
    r.raise_for_status()
    full, other = [], []
    for d in r.json():
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
    r = requests.get("https://openapi.twse.com.tw/v1/announcement/punish",
                     timeout=20, headers=H)
    for d in r.json():
        code = d.get("Code", "").strip()
        if re.match(r"^\d{4}$", code):        # 個股（權證 6 碼濾掉）
            out[code] = {"reason": d.get("ReasonsOfDisposition", ""),
                         "period": d.get("DispositionPeriod", ""), "market": "上市"}
    r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information",
                     timeout=20, headers=H, verify=False)
    for d in r.json():
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
    j = requests.get("https://www.twse.com.tw/exchangeReport/MI_MARGN"
                     "?response=json&selectType=ALL", timeout=30, headers=H).json()
    t = next(t for t in j.get("tables", []) if any("代號" in str(f) for f in t.get("fields", [])))
    for row in t["data"]:
        code = str(row[0]).strip()
        if re.match(r"^\d{4}$", code):
            st[code] = {"in_universe": True, "mark": str(row[-1]).strip()}
    r = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance",
                     timeout=20, headers=H, verify=False)
    for d in r.json():
        code = d.get("SecuritiesCompanyCode", "").strip()
        if re.match(r"^\d{4}$", code):
            st[code] = {"in_universe": True, "mark": (d.get("Note") or "").strip()}
    return st


def fetch_market_map() -> dict:
    """isin.twse.com.tw 上市(2)/上櫃(4) 全清單 → {code: 市場}"""
    mp = {}
    for mode, market in [(2, "上市"), (4, "上櫃")]:
        r = requests.get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}",
                         timeout=40, headers=H)
        txt = r.content.decode("big5", errors="replace")   # Big5（藍圖已註記）
        for code in re.findall(r">(\d{4,6})　", txt):
            mp.setdefault(code, market)
    return mp


def main():
    try:
        twse = fetch_twse_full_delivery()
        tpex_full, tpex_other = fetch_tpex_cmode()
        market_map = fetch_market_map()
        disposal = fetch_disposal()
        margin_status = fetch_margin_status()
    except Exception as e:
        sys.exit(f"官方資料抓取失敗：{e}（不產出舊資料）")

    if not twse or not market_map:
        sys.exit("TWSE 名單或市場對照為空，視為失敗")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "fetched_at": datetime.now().isoformat(),
        "full_delivery": twse + tpex_full,
        "tpex_other_flags": tpex_other,
        "market_map": market_map,
        "disposal": disposal,
        "margin_status": margin_status,
    }, ensure_ascii=False))
    print(f"完成：全額交割 上市 {len(twse)} + 上櫃 {len(tpex_full)} 檔；"
          f"處置 {len(disposal)}；信用現況 {len(margin_status)} 檔 → {OUT}")


if __name__ == "__main__":
    main()
