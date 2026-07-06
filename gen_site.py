#!/usr/bin/env python3
"""
gen_site.py — S4：靜態網站（docs/index.html + docs/rules.html）
單檔、vanilla JS、深色儀表板風、RWD（使用者主要手機看）。
"""

import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
REPORT = BASE / "data" / "report.json"
BACKTEST = BASE / "data" / "backtest.json"
DOCS = BASE / "docs"

GROUP_LABEL = {"predict_in": "🔴 預測打入", "recover": "🟢 恢復候選",
               "edge": "🟠 危險邊緣", "official": "⚪ 全額交割中"}


def gen_backtest_page():
    """docs/backtest.html — 近兩年每季淨值時間線（<5 紅、<10 黃、其餘綠）"""
    if not BACKTEST.exists():
        return False
    bt = json.loads(BACKTEST.read_text())
    rows = []
    stocks = sorted(bt["stocks"].items(),
                    key=lambda kv: (kv[1]["group"], kv[1]["current_nv"] or 0))
    for code, s in stocks:
        cells = "".join(
            f'<div class="q {"r" if h["hit5"] else "y" if h["hit10"] else "g"}" '
            f'title="{h["quarter"]} 淨值 {h["net_value"]}">'
            f'<span>{h["quarter"]}</span>{h["net_value"]:.1f}</div>'
            for h in s["history"])
        rows.append(f"""<div class="row">
  <div class="head"><a href="https://goodinfo.tw/tw/StockDetail.asp?STOCK_ID={code}"
    target="_blank">{code}</a> {s['name']} <span class="mk">{s.get('market','')}</span>
  <span class="grp">{GROUP_LABEL.get(s['group'], s['group'])}</span></div>
  <div class="tl">{cells or '（無資料）'}</div></div>""")
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>歷史驗證（近兩年淨值）</title>
<style>
body {{ font-family:"Noto Sans TC","Microsoft JhengHei",sans-serif; background:#0B0C10;
  color:#F2F4F8; padding:20px; max-width:900px; margin:0 auto; }}
h1 {{ font-size:20px; }} a {{ color:#60a5fa; text-decoration:none; }}
.meta {{ font-size:12px; color:#9BA3B4; margin:6px 0 16px; }}
.row {{ background:#13151B; border-radius:12px; padding:12px 14px; margin-bottom:10px; }}
.head {{ font-size:14px; font-weight:600; margin-bottom:8px; }}
.mk {{ font-size:11px; color:#9BA3B4; font-weight:400; }}
.grp {{ float:right; font-size:12px; font-weight:400; }}
.tl {{ display:flex; gap:4px; flex-wrap:wrap; }}
.q {{ flex:1; min-width:64px; text-align:center; padding:6px 2px; border-radius:6px;
  font-size:13px; font-weight:700; font-variant-numeric:tabular-nums; }}
.q span {{ display:block; font-size:10px; font-weight:400; opacity:.75; }}
.q.r {{ background:#7f1d1d; color:#fecaca; }}
.q.y {{ background:#713f12; color:#fde68a; }}
.q.g {{ background:#14532d; color:#bbf7d0; }}
</style></head><body>
<h1>歷史驗證：近兩年每季淨值</h1>
<div class="meta"><a href="index.html">← 回預警表</a>・紅=淨值&lt;5（全額交割門檻）・
黃=&lt;10（停信用門檻）・綠=安全・資料 FinMind（{bt['generated_at'][:16].replace('T',' ')}）・
用途：對照各股跌破門檻的季度與官方列入時點，驗證規則有效性</div>
{''.join(rows)}
</body></html>"""
    (DOCS / "backtest.html").write_text(html)
    return True

TABS = [
    ("predict_in", "🔴 預測打入", "淨值<5、尚未列全額交割——下次財報後恐公告，提前留意"),
    ("recover", "🟢 恢復候選", "已全額交割但最新淨值≥5——連兩季達標可恢復普通交易"),
    ("edge", "🟠 危險邊緣", "淨值 5~6——再虧一季恐跌破 5 元門檻"),
    ("margin_risk", "🟡 信用警戒", "淨值 6~10——低於 10 元將停止融資融券"),
    ("official", "⚪ 全額交割中", "官方現行變更交易方法名單"),
]


def fmt(v, nd=2):
    return "-" if v is None else f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def main():
    rep = json.loads(REPORT.read_text())
    g = rep["groups"]

    tab_btns, panels = [], []
    for key, label, desc in TABS:
        rows = g.get(key, [])
        tab_btns.append(f'<button class="tab" data-t="{key}">{label}'
                        f'<span class="n">{len(rows)}</span></button>')
        trs = "".join(f"""<tr>
  <td><a href="{r['goodinfo_url']}" target="_blank">{r['code']}</a></td>
  <td>{r['name']}</td><td>{r.get('market','')}</td>
  <td class="num">{fmt(r.get('price'))}</td>
  <td class="num nv">{fmt(r.get('net_value'))}</td>
  <td class="num {'neg' if (r.get('gap') or 0) < 0 else 'pos'}">{fmt(r.get('gap'))}</td>
  <td>{r.get('nv_quarter','')}{('<span class=note>' + r['note'] + '</span>') if r.get('note') else ''}</td>
</tr>""" for r in rows)
        panels.append(f"""<section class="panel" data-t="{key}">
  <p class="desc">{desc}</p>
  <table><thead><tr><th>代號</th><th>名稱</th><th>市場</th><th>股價</th>
    <th>每股淨值</th><th>距5元</th><th>財報季度</th></tr></thead>
  <tbody>{trs or '<tr><td colspan=7 class=empty>（目前無）</td></tr>'}</tbody></table>
</section>""")

    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>全額交割／信用交易預警</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:"Noto Sans TC","Microsoft JhengHei",sans-serif; background:#0B0C10;
  color:#F2F4F8; padding:20px; max-width:1080px; margin:0 auto; }}
h1 {{ font-size:20px; letter-spacing:-.01em; }}
.meta {{ font-size:12px; color:#9BA3B4; margin:6px 0 4px; line-height:1.8; }}
.deadline {{ display:inline-block; background:#7f1d1d; color:#fecaca; padding:2px 10px;
  border-radius:8px; font-size:12px; font-weight:600; }}
.meta a {{ color:#60a5fa; }}
.tabs {{ display:flex; gap:6px; margin:16px 0 12px; flex-wrap:wrap; }}
.tab {{ padding:8px 12px; border:1px solid rgba(255,255,255,.1); background:#13151B;
  color:#9BA3B4; border-radius:10px; font-size:13px; cursor:pointer; }}
.tab.on {{ background:#F2F4F8; color:#0B0C10; font-weight:700; }}
.tab .n {{ margin-left:6px; font-size:11px; opacity:.7; }}
.desc {{ font-size:13px; color:#9BA3B4; margin-bottom:10px; }}
.panel {{ display:none; }} .panel.on {{ display:block; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; background:#13151B;
  border-radius:12px; overflow:hidden; }}
th {{ text-align:left; padding:10px 8px; color:#5C6474; font-size:12px;
  border-bottom:1px solid rgba(255,255,255,.08); cursor:pointer; white-space:nowrap; }}
td {{ padding:9px 8px; border-bottom:1px solid rgba(255,255,255,.05); }}
td a {{ color:#60a5fa; text-decoration:none; font-family:ui-monospace,monospace; }}
.num {{ font-variant-numeric:tabular-nums; }}
.nv {{ font-weight:700; }}
.neg {{ color:#f87171; }} .pos {{ color:#34d399; }}
.empty {{ color:#5C6474; text-align:center; padding:24px; }}
.note {{ display:block; font-size:11px; color:#f59e0b; }}
@media (max-width:640px) {{
  body {{ padding:12px; }}
  th:nth-child(7), td:nth-child(7) {{ display:none; }}
  table {{ font-size:12px; }}
}}
</style></head><body>
<h1>全額交割／信用交易預警</h1>
<div class="meta">淨值資料：{rep['nv_fetched_at'][:16].replace('T',' ')}（goodinfo）・
官方名單：{rep['official_fetched_at'][:16].replace('T',' ')}（證交所/櫃買中心）・
<a href="rules.html">分級規則與法規依據</a>・<a href="backtest.html">歷史驗證</a><br>
<span class="deadline">下一財報截止：{rep['next_report_deadline']}（{rep['days_to_report']} 天後）</span></div>
<div class="tabs">{''.join(tab_btns)}</div>
{''.join(panels)}
<script>
const tabs=document.querySelectorAll('.tab'), panels=document.querySelectorAll('.panel');
function show(t){{tabs.forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 panels.forEach(p=>p.classList.toggle('on',p.dataset.t===t));}}
tabs.forEach(b=>b.onclick=()=>show(b.dataset.t));
show('predict_in');
// 點表頭排序
document.querySelectorAll('th').forEach((th)=>th.onclick=()=>{{
  const tb=th.closest('table').querySelector('tbody');
  const i=[...th.parentNode.children].indexOf(th);
  const rows=[...tb.querySelectorAll('tr')];
  const asc=th.dataset.asc!=='1'; th.dataset.asc=asc?'1':'0';
  rows.sort((a,b)=>{{
    const x=a.children[i]?.textContent.trim(), y=b.children[i]?.textContent.trim();
    const nx=parseFloat(x), ny=parseFloat(y);
    const c=(isNaN(nx)||isNaN(ny))?x.localeCompare(y):nx-ny;
    return asc?c:-c;}});
  rows.forEach(r=>tb.appendChild(r));}});
</script></body></html>"""

    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html)

    rules = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>分級規則與法規依據</title>
<style>
body {{ font-family:"Noto Sans TC","Microsoft JhengHei",sans-serif; background:#0B0C10;
  color:#F2F4F8; padding:20px; max-width:800px; margin:0 auto; line-height:1.9; font-size:14px; }}
h1 {{ font-size:20px; }} h2 {{ font-size:16px; margin:20px 0 8px; color:#60a5fa; }}
a {{ color:#60a5fa; }} .src {{ font-size:12px; color:#9BA3B4; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; margin:8px 0; }}
th,td {{ border:1px solid rgba(255,255,255,.12); padding:8px; text-align:left; }}
</style></head><body>
<h1>分級規則與法規依據</h1><p><a href="index.html">← 回預警表</a></p>

<h2>本站分級邏輯</h2>
<table>
<tr><th>分級</th><th>條件</th><th>意涵</th></tr>
<tr><td>🔴 預測打入</td><td>每股淨值 &lt; 5 元且未列官方名單</td><td>下次財報公布後恐被公告變更交易方法（全額交割），公告常伴隨連續跌停</td></tr>
<tr><td>🟢 恢復候選</td><td>已列名單但最新淨值 ≥ 5 元</td><td>連續兩次財報淨值 ≥5 可恢復普通交易，恢復常伴隨行情</td></tr>
<tr><td>🟠 危險邊緣</td><td>淨值 5 ~ 6 元</td><td>再虧損一季可能跌破門檻</td></tr>
<tr><td>🟡 信用警戒</td><td>淨值 6 ~ 10 元</td><td>低於 10 元將停止融資融券（融資斷頭賣壓）</td></tr>
<tr><td>⚪ 全額交割中</td><td>官方現行名單</td><td>買賣需預收全額款券</td></tr>
</table>

<h2>法規依據</h2>
<p><b>變更交易方法（全額交割）</b>：臺灣證券交易所營業細則第 49 條——上市公司最近期財務報告
顯示每股淨值低於 5 元者，列為變更交易方法股票；其後連續兩次財務報告每股淨值達 5 元以上，
得恢復普通交易。上櫃公司依櫃買中心「證券商營業處所買賣有價證券業務規則」相關條文辦理。<br>
<span class="src">出處：<a href="https://twse-regulation.twse.com.tw/" target="_blank">證交所法規知識庫</a>／
<a href="https://www.tpex.org.tw/" target="_blank">櫃買中心</a>（條文全文請以官方最新版本為準）</span></p>
<p><b>停止融資融券</b>：「有價證券得為融資融券標準」——每股淨值低於 10 元者停止融資融券；
回升達 10 元以上恢復。<br>
<span class="src">出處：<a href="https://law.moj.gov.tw/" target="_blank">全國法規資料庫</a></span></p>
<p><b>財報申報期限</b>（一般上市櫃公司）：年報 3/31、Q1 5/15、Q2 8/14、Q3 11/14；
金控、銀行、保險等另有規定。<br>
<span class="src">出處：<a href="https://www.fsc.gov.tw/" target="_blank">金管會</a>「公開發行公司財務報告及營運情形公告申報特殊適用範圍辦法」</span></p>

<h2>資料來源</h2>
<p>每股淨值：goodinfo.tw 每股淨值排行（含財報季度標記）<br>
官方名單：證交所 openapi TWT85U（變更交易）＋櫃買中心 openapi tpex_cmode<br>
產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<p class="src">⚠️ 本站僅供研究參考，非投資建議。資料可能延遲或有誤，交易前請以官方公告為準。</p>
</body></html>"""
    (DOCS / "rules.html").write_text(rules)
    bt = gen_backtest_page()
    print(f"已生成 docs/index.html + rules.html" + ("+ backtest.html" if bt else "（backtest.json 未就緒，略過歷史頁）"))


if __name__ == "__main__":
    main()
