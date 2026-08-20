#!/usr/bin/env python3
"""
fetch_balance_sheet.py — 上市/上櫃資產負債表「保留盈餘」→ data/balance_sheet.json
（Phase 0 累積虧損資料源，供面額非10元股信用交易資格判斷用；[有價證券得為融資融券標準]
第2/4條：面額非10元股門檻是「最近一個會計年度決算無累積虧損」，不是看淨值）

2026-08-20 Phase 0 查證：
- 一開始以為的端點 t187ap06 其實是「綜合損益表」，不是資產負債表——正確端點是 t187ap07。
- t187ap07 沒有獨立的「累積虧損」欄位，只有「保留盈餘」（淨額，正負併記）；查台積電(2330,
  +60億)/大飲(1213,+3.3萬)/華義(3086,-53.6萬,上櫃)三檔真實資料交叉驗證：負值＝有累積虧損，
  正值/零＝無累積虧損，本站以此判定，不是猜的。
- 依產業別分 6 個 schema（一般業/金融業/金控業/保險業/證券期貨業/異業），每個市場各查 6 個
  端點（TWSE `_L_*` 6個＋TPEx `_O_*` 6個，共12個），全部合併。不查 `_U_`（興櫃，本站不涵蓋）。
- KY 股（境外公司來台上市）有正常收錄（實測 TWSE 一般業一次查到15檔KY公司），不是結構性缺漏；
  單一個股查無資料視為該股本季未申報/申報延遲的個案，不代表整個 API 排除某類公司。
- 只解決「欄位有沒有」跟「語義對不對」，不解決「哪些個股需要看這個欄位」（面額分岐判斷在
  analyze.py，這裡只負責把 raw 保留盈餘資料備妥）。

用法：python fetch_balance_sheet.py [--selftest]
"""

import json
import os
import sys
from pathlib import Path

import requests

from analyze import taipei_today

BASE = Path(__file__).parent
OUT = BASE / "data" / "balance_sheet.json"

# 6 個產業別 schema 代碼；basi=金融業／bd=證券期貨業／ci=一般業／fh=金控業／ins=保險業／mim=異業
CATEGORIES = ["basi", "bd", "ci", "fh", "ins", "mim"]
TWSE_BASE = "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_"
TIMEOUT = 30

# 唯一觀測值 2026-08-20：TWSE 6 類合計約 979 檔、TPEx 6 類合計約 862 檔，共約 1841 檔
# （跟 par_value.json 的 1973 檔量級相近，但不完全相等——資產負債表要求已正式申報財報，
# 面額/基本資料是註冊資訊，永遠有；此為保守猜的下限，待多天觀測後再校準）
ABS_FLOOR = 1300
BASELINE = 1841
HISTORY_KEEP = 30


def _parse_num(raw):
    """財報數字欄位（可能帶千分位逗號、空字串、null）→ float；解析不了一律 None，不可猜 0。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _quarter(roc_year_raw, q_raw):
    """民國年+季別 → '26Q2' 格式（同 fetch_audit.py 既有轉換慣例）；解析不了回 None。"""
    try:
        roc_year = int(str(roc_year_raw).strip())
        q = str(q_raw).strip()
        if q not in ("1", "2", "3", "4"):
            return None
        return f"{(roc_year + 1911) % 100:02d}Q{q}"
    except (ValueError, TypeError):
        return None


def _fetch_one(url: str, code_key: str, name_key: str, retained_key: str,
               year_key: str, quarter_key: str, verify=True) -> dict:
    """單一產業別端點 → {code: {retained_earnings, quarter, name}}；
    解析失敗的個別列直接跳過（不讓一列壞資料拖垮整個產業別），不是整批放棄。"""
    r = requests.get(url, timeout=TIMEOUT, verify=verify)
    r.raise_for_status()
    data = r.json()
    out = {}
    for x in data:
        code = x.get(code_key)
        retained = _parse_num(x.get(retained_key))
        quarter = _quarter(x.get(year_key), x.get(quarter_key))
        if code and retained is not None and quarter:
            out[code] = {"retained_earnings": retained, "quarter": quarter,
                        "name": x.get(name_key) or ""}
    return out


def fetch_twse_category(cat: str) -> dict:
    return _fetch_one(TWSE_BASE + cat, "公司代號", "公司名稱", "保留盈餘", "年度", "季別")


def fetch_tpex_category(cat: str) -> dict:
    """🔴 TPEx openapi SSL 憑證缺 SKI，比照 fetch_par_value.py 既有做法用 verify=False。"""
    return _fetch_one(TPEX_BASE + cat, "SecuritiesCompanyCode", "CompanyName",
                      "保留盈餘", "年度", "季別", verify=False)


def fetch_balance_sheet(fetch_twse_cat=fetch_twse_category,
                        fetch_tpex_cat=fetch_tpex_category) -> dict:
    """12 個端點（6 產業別 × 2 市場）合併 → {code: {retained_earnings, quarter, name}}。
    單一產業別端點失敗只跳過該類、記警告，不讓整批因為一個小類別（如金控業檔數本來就少）
    的暫時性錯誤而全部作廢——跟 fetch_par_value.py 的 2 端點全有全無不同，因為端點數變多，
    單點故障率會累加，過度嚴格反而常態性觸發 degraded。"""
    merged = {}
    errors = []
    for cat in CATEGORIES:
        try:
            merged.update(fetch_twse_cat(cat))
        except Exception as e:
            errors.append(f"TWSE/{cat}: {type(e).__name__}: {e}")
        try:
            merged.update(fetch_tpex_cat(cat))
        except Exception as e:
            errors.append(f"TPEx/{cat}: {type(e).__name__}: {e}")
    if len(errors) >= 8:  # 12 端點裡壞掉太多（門檻抓大概，避免單一市場整個掛掉還被當成小問題）
        raise RuntimeError(f"{len(errors)}/12 端點失敗：" + " | ".join(errors[:3]))
    return merged


def classify_fetch(count: int) -> tuple:
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
    rows = prev.get("rows") or {}
    state = "degraded" if rows else "empty"
    fetched_at = prev.get("fetched_at") if state == "degraded" else taipei_today().isoformat()
    return {"state": state, "fetched_at": fetched_at, "reason": reason,
           "rows": rows, "count": prev.get("count", 0),
           "count_history": prev.get("count_history", [])}


def run(fetch_fn=fetch_balance_sheet, prev: dict = None) -> dict:
    prev = prev if prev is not None else load_prev()
    try:
        rows = fetch_fn()
    except Exception as e:
        return build_degraded(prev, f"抓取失敗：{type(e).__name__}: {e}")

    count = len(rows)
    accept, severity, reason = classify_fetch(count)
    if not accept:
        return build_degraded(prev, reason)

    today = taipei_today().isoformat()
    history = (prev.get("count_history") or [])[-(HISTORY_KEEP - 1):] + [{"date": today, "count": count}]
    return {"state": "ok", "fetched_at": today, "reason": reason,
           "rows": rows, "count": count, "count_history": history}


def main():
    prev = load_prev()
    out = run(fetch_balance_sheet, prev)
    write_atomic(out)
    if out["state"] != "ok":
        print(f"::warning::{out['reason']}，state={out['state']}")
    print(f"完成：資產負債表資料 {out.get('count', 0)} 檔，state={out['state']} → {OUT}")


def selftest():
    import http.server
    import threading

    global TWSE_BASE, TPEX_BASE

    # === 1. _parse_num() ===
    assert _parse_num("62,929,089.00") == 62929089.0
    assert _parse_num("-536.00") == -536.0
    assert _parse_num("0.00") == 0.0
    assert _parse_num("") is None
    assert _parse_num(None) is None
    assert _parse_num("N/A") is None

    # === 2. _quarter()：民國轉西元後兩碼，同 fetch_audit.py 慣例 ===
    assert _quarter("115", "2") == "26Q2"
    assert _quarter("114", "4") == "25Q4"
    assert _quarter("115", "5") is None       # 非法季別
    assert _quarter("abc", "2") is None       # 非法年度

    # === 3. fetch_balance_sheet()：12 端點合併，單一端點失敗只跳過不影響其他 ===
    def fake_twse_cat(cat):
        if cat == "ci":
            return {"2330": {"retained_earnings": 6051113099.0, "quarter": "26Q2", "name": "台積電"}}
        if cat == "bd":
            raise RuntimeError("timeout")     # 模擬單一產業別端點失敗
        return {}

    def fake_tpex_cat(cat):
        if cat == "ci":
            return {"3086": {"retained_earnings": -536.0, "quarter": "26Q2", "name": "華義*"}}
        return {}

    merged = fetch_balance_sheet(fake_twse_cat, fake_tpex_cat)
    assert merged == {
        "2330": {"retained_earnings": 6051113099.0, "quarter": "26Q2", "name": "台積電"},
        "3086": {"retained_earnings": -536.0, "quarter": "26Q2", "name": "華義*"},
    }, merged

    # === 3b. 太多端點失敗（>=8/12）要整批放棄，不能悶著只回少少幾檔當成正常 ===
    def all_fail(cat):
        raise RuntimeError("boom")

    try:
        fetch_balance_sheet(all_fail, all_fail)
        assert False, "應該要拋例外"
    except RuntimeError as e:
        assert "端點失敗" in str(e), e

    # === 4. classify_fetch() ===
    assert classify_fetch(0) == (False, "reject", "空清單，視為失敗")
    accept, sev, reason = classify_fetch(100)
    assert not accept and "絕對地板" in reason, (accept, sev, reason)
    accept, sev, reason = classify_fetch(1841)
    assert accept and sev == "ok", (accept, sev, reason)

    # === 5. run()：截斷 → 沿用舊資料，不可寫入殘缺結果 ===
    prev_good = {"state": "ok", "fetched_at": "2026-08-10", "reason": None,
                "rows": {"2330": {"retained_earnings": 1.0, "quarter": "26Q1", "name": "台積電"}},
                "count": 1, "count_history": []}

    def fetch_truncated():
        return {f"{1000+i}": {"retained_earnings": 1.0, "quarter": "26Q2", "name": "x"}
                for i in range(50)}

    out = run(fetch_truncated, prev=prev_good)
    assert out["state"] == "degraded", out
    assert out["rows"] == prev_good["rows"], out
    assert out["fetched_at"] == "2026-08-10", out

    # === 6. run()：正常情況 + 抓取拋例外 ===
    def fetch_ok():
        return {f"{1000+i}": {"retained_earnings": 1.0, "quarter": "26Q2", "name": "x"}
                for i in range(1841)}

    out = run(fetch_ok, prev={})
    assert out["state"] == "ok" and out["count"] == 1841, out

    def fetch_boom():
        raise RuntimeError("timeout")

    out = run(fetch_boom, prev=prev_good)
    assert out["state"] == "degraded" and "抓取失敗" in out["reason"], out
    assert out["rows"] == prev_good["rows"], out

    # === 7. 首次執行 + 失敗 → empty 不是 degraded ===
    out = run(fetch_truncated, prev={})
    assert out["state"] == "empty", out

    # === 8. 本機 http.server：端到端測 fetch_twse_category() 真的能解析真實 API 回應格式 ===
    fixture = json.dumps([
        {"公司代號": "2330", "公司名稱": "台積電", "年度": "115", "季別": "2",
         "保留盈餘": "6,051,113,099.00"},
        {"公司代號": "9999", "公司名稱": "測試缺季別", "年度": "115", "季別": "",
         "保留盈餘": "100.00"},
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
    orig = TWSE_BASE
    TWSE_BASE = f"http://127.0.0.1:{port}/"
    try:
        result = fetch_twse_category("ci")
        # 9999 因季別解析失敗被跳過，不是硬塞一個猜測值
        assert result == {
            "2330": {"retained_earnings": 6051113099.0, "quarter": "26Q2", "name": "台積電"},
        }, result
    finally:
        TWSE_BASE = orig
        srv.shutdown()

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
