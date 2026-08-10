#!/usr/bin/env python3
"""
test_failure_path.py — 跨層 failure-path 整合測試

為什麼需要這支：2026-08-10 的事故是 fetch → analyze → report → gen_site → Actions
中間某一層把失敗吞掉，資料靜默停擺 27 天沒人發現。三個模組各自的 --selftest
只證明「函式各自正確」，完全抓不到這種跨層問題。這支證明的是：
**失敗真的被擋下來，而且一路傳到使用者眼前的畫面上。**

作法：在暫存目錄複製整套腳本＋捏造的 data/，真的跑 analyze.py 與 gen_site.py，
再用 playwright 實際載入 HTML 執行 JS 後斷言。
🔴 不 grep HTML——年齡與警示都是 JS 在開頁當下產生的，HTML 原始碼裡只有時間戳，
   grep 到的 'stale-banner' 只是 JS 原始碼字串，證明不了畫面真的顯示。
🔴 斷言一律抓自己控制的 data-testid，不抓文案或 DOM 結構，避免版面微調就誤報。

用法：python test_failure_path.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
TPE = ZoneInfo("Asia/Taipei")
SCRIPTS = ["analyze.py", "gen_site.py"]


def iso_days_ago(n: int) -> str:
    return (datetime.now(TPE) - timedelta(days=n, hours=1)).isoformat()


def build_env(tmp: Path, nv_days_ago: int, degraded: dict) -> Path:
    """在暫存目錄搭一套可獨立執行的專案（腳本＋捏造資料）"""
    for s in SCRIPTS:
        shutil.copy(BASE / s, tmp / s)
    data = tmp / "data"
    data.mkdir()

    real = json.loads((BASE / "data" / "netvalue.json").read_text())
    real["fetched_at"] = iso_days_ago(nv_days_ago)
    (data / "netvalue.json").write_text(json.dumps(real, ensure_ascii=False))

    off = json.loads((BASE / "data" / "official.json").read_text())
    off["fetched_at"] = iso_days_ago(0)
    off["degraded"] = degraded
    (data / "official.json").write_text(json.dumps(off, ensure_ascii=False))

    for f in ("backtest.json", "listing_dates.json"):
        if (BASE / "data" / f).exists():
            shutil.copy(BASE / "data" / f, data / f)
    return tmp


def run_pipeline(tmp: Path) -> dict:
    """跑 analyze.py → gen_site.py，回傳 report.json 內容"""
    for script in SCRIPTS:
        r = subprocess.run([sys.executable, str(tmp / script)],
                           capture_output=True, text=True, cwd=tmp)
        if r.returncode != 0:
            raise AssertionError(f"{script} 失敗：{r.stderr[-800:]}")
    return json.loads((tmp / "data" / "report.json").read_text())


def render(page_path: Path, fake_now_days_ahead: int = 0) -> dict:
    """用真瀏覽器載入並執行 JS，回傳畫面上實際看到的東西。

    fake_now_days_ahead：在載入前竄改瀏覽器的 Date.now()，用來證明年齡不是烤死的。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        if fake_now_days_ahead:
            offset_ms = fake_now_days_ahead * 86400000
            page.add_init_script(f"""
                const _real = Date.now;
                Date.now = () => _real() + {offset_ms};
            """)
        page.goto(page_path.as_uri())
        page.wait_for_selector('[data-testid="nv-age"]')
        out = {
            "nv_age_text": page.locator('[data-testid="nv-age"]').inner_text(),
            "nv_age_class": page.locator('[data-testid="nv-age"]').get_attribute("class"),
            "stale": page.locator('[data-testid="stale-banner"]').count(),
            "stale_class": (page.locator('[data-testid="stale-banner"]').first
                            .get_attribute("class")
                            if page.locator('[data-testid="stale-banner"]').count() else ""),
            "stale_text": (page.locator('[data-testid="stale-banner"]').first.inner_text()
                           if page.locator('[data-testid="stale-banner"]').count() else ""),
            "degraded": page.locator('[data-testid="degraded-banner"]').count(),
            "degraded_class": (page.locator('[data-testid="degraded-banner"]').first
                               .get_attribute("class")
                               if page.locator('[data-testid="degraded-banner"]').count() else ""),
            "degraded_text": (page.locator('[data-testid="degraded-banner"]').first.inner_text()
                              if page.locator('[data-testid="degraded-banner"]').count() else ""),
        }
        browser.close()
    return out


def case(name: str, nv_days_ago: int, degraded: dict, fake_ahead: int = 0) -> tuple:
    with tempfile.TemporaryDirectory() as td:
        tmp = build_env(Path(td), nv_days_ago, degraded)
        rep = run_pipeline(tmp)
        seen = render(tmp / "docs" / "index.html", fake_ahead)
        print(f"  [{name}] report.nv_age_days={rep['nv_age_days']} "
              f"畫面={seen['nv_age_text']!r} stale={seen['stale']} deg={seen['degraded']}")
        return rep, seen


def main():
    print("=== 案例 A：goodinfo 失敗，舊 fetched_at 保留 27 天 ===")
    rep, seen = case("A", 27, {})
    assert rep["nv_age_days"] == 27, f"report 應算出 27 天，實際 {rep['nv_age_days']}"
    assert "27" in seen["nv_age_text"], f"畫面應顯示 27 天，實際 {seen['nv_age_text']!r}"
    assert "bad" in (seen["nv_age_class"] or ""), "≥10 天應標紅"
    assert seen["stale"] == 1, "應出現過期警示橫幅"
    assert "bad" in seen["stale_class"], "27 天應是紅色（bad）而非黃色"
    assert "不可信" in seen["stale_text"], "紅色警示要講明預測欄位不可信"

    print("=== 案例 B：次要來源降級（5 天前 → 紅；1 天前 → 黃）===")
    deg5 = {"margin_status": {"source_fetched_at": iso_days_ago(5), "reason": "ReadTimeout"}}
    _, seen = case("B-5天", 0, deg5)
    assert seen["degraded"] == 1, "應出現降級橫幅"
    assert "bad" in seen["degraded_class"], "≥3 天應為紅色"
    assert "信用交易現況" in seen["degraded_text"], "橫幅必須明講是哪一塊舊了"
    assert "5 天前" in seen["degraded_text"], f"應標出天數，實際 {seen['degraded_text']!r}"

    deg1 = {"margin_status": {"source_fetched_at": iso_days_ago(1), "reason": "ReadTimeout"}}
    _, seen = case("B-1天", 0, deg1)
    assert seen["degraded"] == 1 and "warn" in seen["degraded_class"], "<3 天應為黃色"
    assert "bad" not in seen["degraded_class"], "1 天不該紅色"

    print("=== 案例 C：反證——資料新鮮且無降級時，兩種警示都不該出現 ===")
    _, seen = case("C", 0, {})
    assert seen["stale"] == 0, "新鮮資料不該有過期警示"
    assert seen["degraded"] == 0, "無降級不該有降級警示"
    assert "warn" not in (seen["nv_age_class"] or "") and \
           "bad" not in (seen["nv_age_class"] or ""), "新鮮資料不該標色"

    print("=== 案例 D：年齡沒被烤死——同一份 HTML 在不同「今天」要顯示不同天數 ===")
    with tempfile.TemporaryDirectory() as td:
        tmp = build_env(Path(td), 2, {})
        run_pipeline(tmp)
        page = tmp / "docs" / "index.html"
        now = render(page, 0)
        later = render(page, 30)          # 同一個檔案，只把瀏覽器的今天往後推 30 天
    print(f"  [D] 同檔案：現在={now['nv_age_text']!r} 30天後={later['nv_age_text']!r}")
    assert now["nv_age_text"] != later["nv_age_text"], \
        "同一份 HTML 在不同日期顯示相同天數 → 年齡被烤死在靜態頁"
    assert now["stale"] == 0 and later["stale"] == 1, \
        "2 天前的資料現在不該警示、30 天後必須警示"

    print("\n✅ test_failure_path 全數通過（A/B/C/D）")


if __name__ == "__main__":
    main()
