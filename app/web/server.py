"""Web 仪表盘:走势预测 + 实时操作建议。

启动: python run_web.py  (默认 http://127.0.0.1:8000)
"""
import io
import json
import os
import re as _re
import sys
import time as _time
import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
from flask import Flask, jsonify, render_template_string, request, redirect
from markupsafe import Markup, escape

from app.analysis import analyze, analyze_light
from app import config

app = Flask(__name__)


def _boldify(s) -> Markup:
    """将文本中的 **加粗** 标记渲染为 <b>,先转义防注入。"""
    esc = escape(str(s))
    esc = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    return Markup(esc)


app.jinja_env.filters["boldify"] = _boldify


# ============================================================ 五大模块 Web(新增)
import html as _html
from markupsafe import Markup as _Markup

_SIDE_MENU = [("/decision", "decision", "🎯 今日决策"),
              ("/analyze", "home", "📈 走势预测"),
              ("/report", "report", "📄 复盘报告"),
              ("/portfolio", "portfolio", "💼 持仓诊断"),
              ("/alerts", "alerts", "🚨 盘中预警"), ("/settings", "settings", "⚙️ 系统设置")]


def _SIDE(active: str) -> str:
    links = "\n".join(
        f'  <a href="{u}" class="{"on" if a == active else ""}">{t}</a>' for u, a, t in _SIDE_MENU)
    return (
        '<style>'
        '.side{position:fixed;left:0;top:0;bottom:0;width:206px;background:#131a28;'
        'border-right:1px solid var(--line);padding:16px 10px;z-index:50;overflow-y:auto;}'
        '.side .logo{font-size:16px;font-weight:700;margin:0 10px 16px;color:#e6e9f0;}'
        '.side a{display:block;padding:9px 12px;margin:2px 0;border-radius:8px;color:#b9c2d4;'
        'text-decoration:none;font-size:14px;white-space:nowrap;}'
        '.side a:hover{background:#22304a;color:#7aa2ff;}'
        '.side a.on{background:#2f6fed;color:#fff;}'
        '.wrap{margin-left:210px!important;max-width:1320px!important;}'
        '@media(max-width:900px){.side{position:static;width:auto;display:flex;flex-wrap:wrap;padding:10px;}'
        '.side .logo{flex-basis:100%;margin-bottom:6px;}.side a{display:inline-block;margin:2px 4px;}'
        '.wrap{margin-left:0!important;}}'
        '</style>'
        '<aside class="side"><div class="logo">📊 量化决策台</div>' + links
        + '<div style="margin-top:18px;padding:8px;border:1px solid #5b4231;border-radius:8px;'
          'background:#2a2017;color:#e0a860;font-size:11px;line-height:1.6">⚠️ 本站全部内容仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>'
        + '</aside>')


def _h(v) -> str:
    if v is None:
        return ""
    return _html.escape(str(v))


def _fmt(v, fmt="{:,.2f}"):
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return _h(v or "")


def _md_to_html(md: str) -> str:
    """轻量 Markdown → HTML(标题/表格/列表/加粗)。"""
    lines = (md or "").split("\n")
    out, in_tbl = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|"):
            in_tbl.append(line)
            i += 1
            if i >= len(lines) or not lines[i].startswith("|"):
                out.append(_tbl_block(in_tbl))
                in_tbl = []
            continue
        if in_tbl:
            out.append(_tbl_block(in_tbl)); in_tbl = []
        s = line.strip()
        if not s:
            out.append(""); i += 1; continue
        if s.startswith("### "):
            out.append(f"<h3>{_bold(s[4:])}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2>{_bold(s[3:])}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1>{_bold(s[2:])}</h1>")
        elif s.startswith("- "):
            out.append(f'<div class="line">• {_bold(s[2:])}</div>')
        elif s.startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
            out.append(f'<div class="line">{_bold(s.split(". ", 1)[1] if ". " in s else s)}</div>')
        elif s.startswith("> "):
            out.append(f'<div class="mut" style="margin:8px 0">📌 {_bold(s[2:])}</div>')
        elif s.startswith("**"):
            out.append(f'<div class="line"><b>{_bold(s)}</b></div>')
        else:
            out.append(f'<div class="line">{_bold(s)}</div>')
        i += 1
    if in_tbl:
        out.append(_tbl_block(in_tbl))
    return _linkify("\n".join(out))


def _bold(s):
    return _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _html.escape(s))


def _linkify(s):
    """6 位股票代码 → 走势预测详情页跳转链接(P2 报告体验)。"""
    return _re.sub(r"(?<!code=)\b((?:60|00|30|68)\d{4})\b",
                   r'<a href="/analyze?code=\1">\1</a>', s)


def _tbl_block(rows) -> str:
    parsed = []
    for ln in rows:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cells and set(cells) <= {"-", "---", "---|"}:
            continue
        parsed.append(cells)
    if not parsed:
        return ""
    head = parsed[0]
    body = parsed[1:]
    html_ = ["<div class='tbl'><table><tr>" + "".join(f"<th>{_h(c)}</th>" for c in head) + "</tr>"]
    for r in body:
        cells = (r + [""] * (len(head) - len(r)))[: len(head)]
        html_.append("<tr>" + "".join(f"<td>{_bold(c)}</td>" for c in cells) + "</tr>")
    html_.append("</table></div>")
    return "\n".join(html_)


def _shell(active: str, title: str, content: str) -> str:
    return ("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>""" + _h(title) + """ - 量化决策台</title>
<style>
  :root { --bg:#0f1420; --card:#1a2130; --line:#2a3350; --txt:#e6e9f0; --mut:#8a94a8; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Microsoft YaHei",system-ui,sans-serif; background:var(--bg); color:var(--txt); }
  .wrap { max-width:1180px; margin:0 auto; padding:18px; }
  header { display:flex; align-items:center; gap:12px; padding:14px 0; flex-wrap:wrap; }
  header h1 { font-size:20px; margin:0; }
  .mut { color:var(--mut); font-size:13px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin:14px 0; }
  .card h3 { margin:0 0 10px; font-size:15px; color:#7aa2ff; }
  .line { font-size:14px; line-height:1.9; padding:3px 0; }
  .line b { color:#ffca28; }
  h1, h2, h3 { color:#e6e9f0; }
  .card h1, .card h2, .card h3 { margin:0 0 8px; }
  .up{color:#ef5350;} .down{color:#26a69a;} .flat{color:#ffca28;} .mut{color:var(--mut);}
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td,th { padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; }
  th { color:#b9c2d4; font-weight:600; background:#151c2b; }
  tr:hover td { background:#1d2638; }
  .tbl { margin:10px 0 14px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .grid3 { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
  .badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:13px; font-weight:600; }
  .b-core{background:#e5393522;color:#ef5350;border:1px solid #ef535055;}
  .b-branch{background:#fbc02d22;color:#ffca28;border:1px solid #fbc02d55;}
  .b-watch{background:#546e7a22;color:#90a4ae;border:1px solid #90a4ae55;}
  .b-buy{background:#e5393522;color:#ef5350;border:1px solid #ef535055;}
  .b-sell{background:#00897b22;color:#26a69a;border:1px solid #26a69a55;}
  .b-hold{background:#fbc02d22;color:#ffca28;border:1px solid #fbc02d55;}
  .b-wait{background:#546e7a22;color:#90a4ae;border:1px solid #90a4ae55;}
  .pill { display:inline-block; background:#0b1018; border:1px solid var(--line); border-radius:8px; padding:6px 12px; font-size:13px; margin:2px 4px 2px 0; }
  .pill b { font-size:16px; }
  .err { color:#ff6e6e; padding:12px; }
  .ok { color:#26a69a; padding:12px; }
  .btn { padding:8px 18px; border-radius:8px; border:0; background:#2f6fed; color:#fff; cursor:pointer; font-size:14px; margin:2px 4px 2px 0; text-decoration:none; display:inline-block; }
  .btn:hover { background:#3f7ffd; }
  .btn.red { background:#c62828; } .btn.gray { background:#455a64; }
  input, select, textarea { padding:8px 12px; border-radius:8px; border:1px solid var(--line); background:#0b1018; color:var(--txt); font-size:14px; margin:2px 0; }
  .footer { color:var(--mut); font-size:12px; margin-top:16px; line-height:1.8; }
  .tag { display:inline-block; padding:2px 8px; border-radius:6px; background:#22304a; color:#7aa2ff; font-size:12px; margin-right:6px; }
  .toast { position:fixed; top:18px; left:50%; transform:translateX(-50%); z-index:999; display:none;
           padding:11px 22px; border-radius:10px; font-size:14px; font-weight:600;
           background:#1a2130; border:1px solid var(--line); box-shadow:0 6px 24px rgba(0,0,0,.45); }
  .toast.ok { color:#26a69a; border-color:#26a69a66; }
  .toast.err { color:#ef5350; border-color:#ef535066; }
  /* ---- 第三轮:前端体验 ---- */
  details.card details, details.card > summary { outline:none; }
  details > summary { cursor:pointer; user-select:none; }
  .delta { color:var(--mut); }
  .delta.up{color:#ef5350;} .delta.down{color:#26a69a;}
  .target-tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }
  .tabs-btn { padding:7px 16px; border-radius:8px; border:1px solid var(--line); background:#0b1018;
              color:var(--txt); cursor:pointer; font-size:14px; }
  .tabs-btn.on { background:#2f6fed; border-color:#2f6fed; color:#fff; }
  .tab-pane { display:none; }
  .tab-pane.on { display:block; }
  .modal-mask { position:fixed; inset:0; z-index:1000; background:rgba(8,12,20,.72);
                display:flex; align-items:center; justify-content:center; padding:20px; }
  .modal-box { background:var(--card); border:1px solid var(--line); border-radius:14px;
               width:100%; max-width:980px; max-height:92vh; overflow:auto; padding:18px; }
  .modal-head { display:flex; justify-content:space-between; align-items:center;
                border-bottom:1px solid var(--line); padding-bottom:10px; margin-bottom:12px; }
  .modal-head h3 { margin:0; color:#7aa2ff; }
  .modal-x { cursor:pointer; font-size:18px; color:var(--mut); }
  .modal-x:hover { color:#fff; }
  @media(max-width:900px){ .grid,.grid3{grid-template-columns:1fr;} }
</style>
</head>
<body>""" + _SIDE(active) +
            '<div class="wrap">' + content + '</div></body></html>')


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>量化预测分析 - SilverQuant + Akshare + VectorBT</title>
<style>
  :root { --bg:#0f1420; --card:#1a2130; --line:#2a3350; --txt:#e6e9f0; --mut:#8a94a8; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Microsoft YaHei",system-ui,sans-serif; background:var(--bg); color:var(--txt); }
  .wrap { max-width:1180px; margin:0 auto; padding:18px; }
  header { display:flex; align-items:center; gap:12px; padding:14px 0; flex-wrap:wrap; }
  header h1 { font-size:20px; margin:0; }
  .mut { color:var(--mut); font-size:13px; }
  form { display:flex; gap:8px; }
  input { padding:8px 12px; border-radius:8px; border:1px solid var(--line);
          background:#0b1018; color:var(--txt); font-size:15px; width:150px; }
  button { padding:8px 18px; border-radius:8px; border:0; background:#2f6fed; color:#fff; cursor:pointer; font-size:15px; }
  button:hover { background:#3f7ffd; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:14px 0; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .card h3 { margin:0 0 10px; font-size:15px; }
  .big { font-size:30px; font-weight:700; }
  .up{color:#ef5350;} .down{color:#26a69a;} .flat{color:#ffca28;} .mut{color:var(--mut);}
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td,th { padding:6px 8px; border-bottom:1px solid var(--line); text-align:left; }
  th { color:var(--mut); font-weight:400; }
  .levels { display:flex; flex-wrap:wrap; gap:10px; }
  .pill { background:#0b1018; border:1px solid var(--line); border-radius:8px; padding:6px 12px; font-size:13px; }
  .pill b { font-size:16px; }
  .reason { font-size:13px; padding:4px 0; }
  .risk { font-size:13px; padding:4px 0; color:#ffb74d; }
  .err { color:#ff6e6e; padding:12px; }
  .badge { display:inline-block; padding:4px 12px; border-radius:20px; font-size:14px; font-weight:600; }
  .b-buy{background:#e5393522;color:#ef5350;border:1px solid #ef535055;}
  .b-sell{background:#00897b22;color:#26a69a;border:1px solid #26a69a55;}
  .b-hold{background:#fbc02d22;color:#ffca28;border:1px solid #fbc02d55;}
  .b-wait{background:#546e7a22;color:#90a4ae;border:1px solid #90a4ae55;}
  .footer { color:var(--mut); font-size:12px; margin-top:16px; line-height:1.8; }
  nav a { display:inline-block; margin-left:8px; padding:7px 14px; border-radius:8px;
          border:1px solid var(--line); color:var(--txt); text-decoration:none; font-size:14px; }
  nav a:hover { border-color:#2f6fed; color:#7aa2ff; }
  .rank td:nth-child(1){color:var(--mut);} .rank .top3 td:nth-child(1){color:#ffca28;font-weight:600;}
  .rank .neg td:nth-child(5){color:#26a69a;} .rank .pos td:nth-child(5){color:#ef5350;}
  @media(max-width:820px){ .grid{grid-template-columns:1fr;} }
</style>
</head>
<body>
{{ SIDE | safe }}
<div class="wrap">
  <header>
    <h1>📈 量化走势预测与操作建议</h1>
    <span class="mut">SilverQuant · Akshare · VectorBT · LightGBM</span>
  </header>
  <form method="get" action="/">
    <input name="code" placeholder="股票代码, 如 600519" value="{{ code or '' }}" autofocus>
    <button type="submit">分析</button>
  </form>

  {% if error %}<div class="card err">{{ error }}</div>{% endif %}
  {% if not error and r %}
  <div class="grid">
    <div class="card">
      <h3>{{ r.name }} ({{ r.code }}) · 未来 {{ model.horizon }} 日走势预测
          <span class="mut">{{ r.analyzed_at }}</span></h3>
      <span class="big {{ {'up':'up','down':'down','flat':'flat'}[r.prediction.direction] }}">
        {{ r.prediction.direction_cn }}</span>
      <span class="badge {{ 'b-buy' if r.advice.action=='buy' else ('b-sell' if r.advice.action=='sell' else ('b-hold' if r.advice.action=='hold' else 'b-wait')) }}">
        建议: {{ r.advice.action_cn }}</span>
      <p class="mut">上涨 {{ '%0.1f'|format(r.prediction.p_up*100) }}% · 震荡 {{ '%0.1f'|format(r.prediction.p_flat*100) }}% · 下跌 {{ '%0.1f'|format(r.prediction.p_down*100) }}%
         · 置信度 {{ '%0.1f'|format(r.advice.confidence*100) }}%{% if r.advice.strong %} · 强信号{% endif %}</p>
      <p>预期涨跌幅
        {% if r.prediction.expected_return is not none %}
          <b class="{{ 'up' if r.prediction.expected_return>=0 else 'down' }}">{{ '%+.2f'|format(r.prediction.expected_return*100) }}%</b>
        {% else %}<b>-</b>{% endif %}
        &nbsp;·&nbsp; 盈亏比
        {% if r.prediction.reward_risk is not none %}
          <b class="{{ 'up' if r.prediction.reward_risk>=1 else 'down' }}">{{ '%0.2f'|format(r.prediction.reward_risk) }}</b>
          <span class="mut">(期望盈利/期望亏损)</span>
        {% else %}<b>-</b>{% endif %}</p>
      {% if r.quote %}
      <p>实时价 <b>{{ r.quote.price }}</b>
         <span class="{{ 'up' if r.quote.pct_chg>=0 else 'down' }}">{{ '%+.2f'|format(r.quote.pct_chg*100) }}%</span>
         <span class="mut">· {{ r.quote.datetime }}</span></p>
      {% endif %}
      <p>空仓者: <b>{{ r.advice.entry_action }}</b> &nbsp; 持仓者: <b>{{ r.advice.hold_action }}</b></p>
    </div>
    <div class="card">
      <h3>关键价位</h3>
      <div class="levels">
        <div class="pill">建议买入区<br><b>{{ r.advice.levels.entry_low }} ~ {{ r.advice.levels.entry_high }}</b></div>
        <div class="pill">目标价<br><b style="color:#26a69a">{{ r.advice.levels.target }}</b></div>
        <div class="pill">止损价<br><b style="color:#ef5350">{{ r.advice.levels.stop_loss }}</b></div>
        <div class="pill">支撑位<br><b style="color:#ffca28">{{ r.advice.levels.support }}</b></div>
        <div class="pill">压力位<br><b style="color:#ffca28">{{ r.advice.levels.resistance }}</b></div>
      </div>
      <table>
        {% for k,label in [('ma20','MA20'),('ma60','MA60'),('rsi14','RSI14'),('atr14','ATR14'),('volume_ratio','量比'),('bb_position','布林位置')] %}
        <tr><th>{{ label }}</th><td>{{ r.advice.technical[k] if r.advice.technical[k] is not none else '-' }}</td></tr>
        {% endfor %}
      </table>
    </div>
  </div>

  <div class="grid">
    <div class="card"><h3>市场情绪</h3>
      {% set mk = r.advice.market %}
      <div class="levels">
        <div class="pill">恐贪指标<br><b>{{ '%0.0f'|format(mk.fear_greed) if mk.fear_greed is not none else '-' }}/100</b><br>
            <span class="mut">{{ mk.fear_greed_label or '-' }}</span></div>
        <div class="pill">期指基差<br><b>{{ '%+.2f'|format(mk.basis_avg*100) if mk.basis_avg is not none else '-' }}%</b><br>
            <span class="mut">{{ mk.basis_label or '-' }}</span></div>
        <div class="pill">涨跌家数<br><b>{{ '%0.0f'|format(mk.advance) if mk.advance is not none else '-' }}
            / {{ '%0.0f'|format(mk.decline) if mk.decline is not none else '-' }}</b><br>
            <span class="mut">涨停 {{ '%0.0f'|format(mk.limit_up) if mk.limit_up is not none else '-' }} 家</span></div>
        <div class="pill">建议仓位<br><b>{{ r.advice.position_hint or '-' }}</b></div>
      </div>
    </div>
    <div class="card"><h3>依据</h3>
      {% for s in r.advice.reasons %}<div class="reason">· {{ s }}</div>{% endfor %}
    </div>
  </div>

  <div class="grid">
    <div class="card"><h3>风险提示</h3>
      {% for s in r.advice.risks %}<div class="risk">! {{ s }}</div>{% else %}<div class="mut">无明显风险提示</div>{% endfor %}
    </div>
    <div class="card"><h3>模型信息</h3>
      <table>
        <tr><th>模型</th><td>{{ model.model_name }}</td><th>预测周期</th><td>{{ model.horizon }} 交易日</td></tr>
        <tr><th>验证准确率</th><td>{{ model.metrics.accuracy if model.metrics else '-' }}</td>
            <th>F1</th><td>{{ model.metrics.f1_weighted if model.metrics else '-' }}</td></tr>
        <tr><th>样本数</th><td>{{ model.n_samples }}</td><th>特征数</th><td>{{ model.n_features }}</td></tr>
        <tr><th>训练股票池</th><td colspan="3">{{ model.train_codes|length }} 只</td></tr>
      </table>
    </div>
  </div>

  <div class="card">
    <h3>走势图(收盘 + 均线 + 预测概率)</h3>
    {{ chart_html | safe }}
  </div>
  {% endif %}

  <div class="footer">
    说明:本系统基于 SilverQuant 框架改造,接入 Akshare 行情 + VectorBT 特征/回测 + LightGBM 机器学习预测。<br>
    预测结果与操作建议仅供研究参考,不构成任何投资建议。股市有风险,入市需谨慎。
  </div>
</div>
</body>
</html>
"""

PAGE_REVIEW = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日复盘 - 量化预测分析</title>
<style>
  :root { --bg:#0f1420; --card:#1a2130; --line:#2a3350; --txt:#e6e9f0; --mut:#8a94a8; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Microsoft YaHei",system-ui,sans-serif; background:var(--bg); color:var(--txt); }
  .wrap { max-width:1180px; margin:0 auto; padding:18px; }
  header { display:flex; align-items:center; gap:12px; padding:14px 0; flex-wrap:wrap; }
  header h1 { font-size:20px; margin:0; }
  .mut { color:var(--mut); font-size:13px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin:14px 0; }
  .card h3 { margin:0 0 10px; font-size:15px; color:#7aa2ff; }
  .line { font-size:14px; line-height:1.9; padding:3px 0; }
  .line.title { font-weight:700; color:#7aa2ff; margin:16px 0 6px; font-size:15px; }
  .line b { color:#ffca28; font-weight:700; }
  .up{color:#ef5350;} .down{color:#26a69a;} .flat{color:#ffca28;} .mut{color:var(--mut);}
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td,th { padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; }
  th { color:#b9c2d4; font-weight:600; background:#151c2b; }
  tr:hover td { background:#1d2638; }
  .tbl { margin:10px 0 14px; }
  .tbl-title { font-size:13px; color:#8a94a8; margin:10px 0 4px; font-weight:600; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .err { color:#ff6e6e; padding:12px; }
  nav a { display:inline-block; margin-left:8px; padding:7px 14px; border-radius:8px;
          border:1px solid var(--line); color:var(--txt); text-decoration:none; font-size:14px; }
  nav a:hover { border-color:#2f6fed; color:#7aa2ff; }
  .footer { color:var(--mut); font-size:12px; margin-top:16px; line-height:1.8; }
  @media(max-width:820px){ .grid{grid-template-columns:1fr;} }
</style>
</head>
<body>
{{ SIDE | safe }}
<div class="wrap">
  <header>
    <h1>📝 A股每日深度复盘</h1>
    <span class="mut">{{ date }} · 数据来源:东财/同花顺/新浪/央视联播</span>
  </header>

  {% if error %}<div class="card err">{{ error }}</div>{% endif %}

  {% if sections %}
  <div class="grid">
    <div class="card">
      <h3>📋 当日市场快照</h3>
      <table>
        <tr><th>上涨</th><td class="up">{{ tables.activity.advance }}</td><th>下跌</th><td class="down">{{ tables.activity.decline }}</td></tr>
        <tr><th>平盘</th><td>{{ tables.activity.flat }}</td><th>活跃度</th><td>{{ tables.activity.activity_pct }}%</td></tr>
        <tr><th>涨停</th><td class="up">{{ tables.activity.limit_up }} 家</td><th>跌停</th><td class="down">{{ tables.activity.limit_down }} 家</td></tr>
        {% if tables.north.north_available %}
        <tr><th>北向净买入</th><td colspan="3" class="{{ 'up' if tables.north.north_total_yi>=0 else 'down' }}">{{ '%+.2f'|format(tables.north.north_total_yi) }} 亿</td></tr>
        {% else %}
        <tr><th>北向</th><td colspan="3" class="mut">实时净买入已停止披露</td></tr>
        {% endif %}
        <tr><th>港股通(南向)</th><td colspan="3" class="{{ 'up' if tables.north.south_total_yi>=0 else 'down' }}">{{ '%+.2f'|format(tables.north.south_total_yi) }} 亿</td></tr>
      </table>
    </div>
    <div class="card">
      <h3>🔥 短线情绪速览</h3>
      <table>
        <tr><th>涨停池</th><td class="up">{{ tables.limit_up.total }} 家</td><th>最高连板</th><td>{{ tables.limit_up.max_lian }} 板</td></tr>
        <tr><th>炸板</th><td>{{ tables.limit_up.zhadan_total }} 次</td><th>封板资金</th><td>{{ tables.limit_up.total_money_yi }} 亿</td></tr>
        <tr><th>{{ tables.limit_up.ind_label or '涨停行业分布' }}</th><td colspan="3">{% for k,v in (tables.limit_up.industries or {}).items() %}{% if v is float %}{{ k }}{{ '%.1f'|format(v) }}亿{% else %}{{ k }}{{ v }}家{% endif %}{% if not loop.last %}、{% endif %}{% endfor %}</td></tr>
      </table>
    </div>
  </div>

  <div class="card">
    <h3>📅 市场量能 · 涨跌宽度 · 大盘资金(近10个交易日)</h3>
    <table>
      <tr><th>日期</th><th>上证收盘</th><th>涨跌幅</th><th>两市成交额(亿)</th>
          <th>主力净流入(亿)</th><th>上涨</th><th>下跌</th><th>涨停</th><th>跌停</th></tr>
      {% for m in tables.market_daily | reverse %}
      <tr>
        <td>{{ m.date }}</td>
        <td>{{ '%.2f'|format(m.close) if m.close is not none else '-' }}</td>
        <td class="{{ 'up' if (m.pct_chg or 0)>=0 else 'down' }}">{{ '%+.2f'|format(m.pct_chg) if m.pct_chg is not none else '-' }}%</td>
        <td>{{ '%.0f'|format(m.amount_yi) if m.amount_yi is not none else '-' }}</td>
        <td class="{{ 'up' if (m.main_yi or 0)>=0 else 'down' }}">{{ '%+.2f'|format(m.main_yi) if m.main_yi is not none else '-' }}</td>
        <td>{{ '%.0f'|format(m.advance) if m.advance is not none else '-' }}</td>
        <td>{{ '%.0f'|format(m.decline) if m.decline is not none else '-' }}</td>
        <td>{{ '%.0f'|format(m.limit_up) if m.limit_up is not none else '-' }}</td>
        <td>{{ '%.0f'|format(m.limit_down) if m.limit_down is not none else '-' }}</td>
      </tr>
      {% endfor %}
    </table>
    <p class="mut">注:两市成交额与主力净流入为东财沪/深指数口径;涨停家数为东财涨停池口径;上涨/下跌/跌停家数
      为乐咕当日口径,自每日复盘运行起自动累积补齐近10日历史。</p>
  </div>

  {% for key, sec in sections.items() %}
  <div class="card">
    <h3>{{ sec.title }}</h3>
    {% for it in sec['items'] %}
      {% if it.t is defined %}
        <div class="line">{{ it.t | boldify }}</div>
      {% elif it.head is defined %}
        <div class="line title">{{ it.head }}</div>
      {% elif it.table is defined %}
        <div class="tbl">
          {% if it.table.title %}<div class="tbl-title">{{ it.table.title }}</div>{% endif %}
          <table>
            <tr>{% for c in it.table.cols %}<th>{{ c }}</th>{% endfor %}</tr>
            {% for row in it.table.rows %}
            <tr>{% for cell in row %}
              {% if cell is mapping %}<td class="{{ cell.c }}">{{ cell.v | boldify }}</td>
              {% else %}<td>{{ cell | boldify }}</td>{% endif %}
            {% endfor %}</tr>
            {% endfor %}
          </table>
        </div>
      {% endif %}
    {% endfor %}
  </div>
  {% endfor %}
  {% endif %}

  <div class="footer">
    复盘内容由规则引擎基于当日行情/资金/事件数据自动生成,仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。
  </div>
</div>
</body>
</html>
"""


def _build_chart(r: dict) -> str:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import plotly.io as pio

    df = r["history"].tail(90)
    s = r["series"].tail(90)
    lv = r["advice"]["levels"]

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.6, 0.2, 0.2], vertical_spacing=0.02)
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="K线", increasing_line_color="#ef5350", decreasing_line_color="#26a69a"), row=1, col=1)
    for col, color, label in (("ma5", "#ffca28", "MA5"), ("ma20", "#42a5f5", "MA20"), ("ma60", "#ab47bc", "MA60")):
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df.index, y=df[col], name=label, line=dict(width=1, color=color)), row=1, col=1)
    for y, color, label in ((lv["target"], "#26a69a", f"目标{lv['target']}"),
                            (lv["stop_loss"], "#ef5350", f"止损{lv['stop_loss']}")):
        fig.add_hline(y=y, line_dash="dash", line_color=color, row=1, col=1,
                      annotation_text=label, annotation_font_size=10)

    fig.add_trace(go.Scatter(x=s.index, y=s["up"] * 100, name="P(上涨)%", line=dict(color="#ef5350")), row=2, col=1)
    fig.add_trace(go.Scatter(x=s.index, y=s["down"] * 100, name="P(下跌)%", line=dict(color="#26a69a")), row=2, col=1)

    fig.add_trace(go.Bar(x=df.index, y=df["volume"] / 1e6, name="成交量(百万手)",
                         marker_color="#5c6b80"), row=3, col=1)

    fig.update_layout(
        height=640, template="plotly_dark", margin=dict(l=10, r=10, t=30, b=10),
        xaxis_rangeslider_visible=False, showlegend=True, paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", yanchor="bottom", y=1.01))
    fig.update_yaxes(range=[0, 100], row=2, col=1)
    return pio.to_html(fig, full_html=False, include_plotlyjs="inline",
                       default_width="100%", default_height="100%")


@app.route("/")
def index():
    """默认首页:今日决策。带 ?code= 时仍可直达走势预测。"""
    if request.args.get("code"):
        return analyze_page()
    return redirect("/decision", code=302)


@app.route("/analyze")
def analyze_page():
    code = (request.args.get("code") or "").strip()
    if not code:
        return render_template_string(PAGE, code="", r=None, error=None, chart_html="", model={}, SIDE=_SIDE("home"))
    try:
        r = analyze(code)
        chart = _build_chart(r)
        model = r.get("model_info") or {}
        r_light = {
            "name": r["name"], "code": r["code"], "analyzed_at": r["analyzed_at"],
            "prediction": r["prediction"], "advice": r["advice"], "quote": r["quote"],
        }
        return render_template_string(PAGE, code=code, r=r_light, error=None, chart_html=chart, model=model, SIDE=_SIDE("home"))
    except Exception as e:  # noqa: BLE001
        return render_template_string(PAGE, code=code, r=None, error=f"分析失败: {e}", chart_html="", model={}, SIDE=_SIDE("home"))


@app.route("/api/analyze")
def api_analyze():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400
    try:
        r = analyze_light(code)
        return jsonify(r)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/model")
def api_model():
    from app.ml.predictor import load_meta
    return jsonify(load_meta() or {})


@app.route("/api/backtest")
def api_backtest():
    from app.backtest.vbt_validate import backtest_universe
    codes = (request.args.get("codes") or "").split(",")
    codes = [c.strip() for c in codes if c.strip()] or None
    res = backtest_universe(codes)
    cols = ["code", "name", "total_return", "sharpe", "max_drawdown", "win_rate", "trades", "profit_factor"]
    return jsonify(res[cols].where(res[cols].notna(), None).to_dict(orient="records"))


# ============================================================ 决策执行引擎(今日决策)
_DECISION_CACHE = {"t": 0.0, "data": None, "err": None}
_DECISION_TTL = 600


def _get_decision(refresh=False):
    if refresh or _DECISION_CACHE["data"] is None or _DECISION_CACHE["err"] or \
            _time.time() - _DECISION_CACHE["t"] > _DECISION_TTL:
        from app.decision.engine import decision_brief
        try:
            _DECISION_CACHE["data"] = decision_brief()
            _DECISION_CACHE["err"] = None
        except Exception as e:  # noqa: BLE001
            _DECISION_CACHE["data"] = None
            _DECISION_CACHE["err"] = str(e)
        _DECISION_CACHE["t"] = _time.time()
    return _DECISION_CACHE


def _grade_badge(g):
    return {"A": "b-core", "B": "b-branch", "C": "b-watch", "D": "b-wait"}.get(g, "b-wait")


# ---- 第五轮:全市场风格偏转信息(顶部结论卡 + 大盘开仓许可评级展示)
_STYLE_CLS = {"小盘风格": "b-core", "大盘风格": "b-branch", "均衡": "b-watch"}


def _market_style_of(d: dict) -> dict:
    """从决策包提取全市场风格信息(优先 raw 的 market_style,兜底取 stable 条目标签)。"""
    layers = (d or {}).get("layers") or {}
    st = ((layers.get("layer2_raw") or {}) or {}).get("market_style")
    if not st:
        for it in (layers.get("layer2") or {}).get("watch") or []:
            tag = it.get("market_style_tag")
            if tag:
                return {"bias": {"大盘风格": -1, "均衡": 0, "小盘风格": 1}.get(tag, 0),
                        "tag": tag, "from": "stable"}
        return {}
    return st


def _style_badge_html(style: dict) -> str:
    """全市场风格徽标(带相对动量提示)。"""
    tag = (style or {}).get("tag", "")
    if not tag:
        return ""
    mom = (style or {}).get("mom") or {}
    mom_txt = " / ".join(f"{k.replace('d', '日')} 小盘 {v * 100:+.2f}%" for k, v in mom.items()) if mom else ""
    title = f"大小盘相对动量: {mom_txt}" if mom_txt else "沪深300 vs 小盘指数相对动量"
    cls = _STYLE_CLS.get(tag, "b-watch")
    return f'<span class="badge {cls}" style="font-size:13px" title="{_h(title)}">{_h(tag)}</span>'


def _layer1_html(p1, style=None) -> str:
    rows = []
    for k, c in (p1.get("checks") or {}).items():
        state = "✅" if c.get("ok") else ("🟡" if c.get("ok_min") else "❌")
        rows.append(f"<tr><th>{_h(k)}</th><td>{_h(c.get('value'))}</td><td>{state} {_h(p1.get('grade_label'))}</td></tr>")
    style_line = f'<div class="line" style="font-size:13px">🧭 市场风格: {_style_badge_html(style)}</div>' if style else ""
    tips = "".join(f'<div class="line">· {_h(r)}</div>' for r in p1.get("reasons", []))
    return (f"<div class='card'><h3>评级:{_h(p1.get('grade_label'))} · 总仓位上限 <b>{p1['cap']:.0%}</b></h3>"
            f"{style_line}<div class='tbl'><table><tr><th>因子</th><th>现值</th><th>达标</th></tr>{''.join(rows)}</table></div>{tips}</div>")


def _ext_tag_html(it) -> str:
    """第五轮扩展因子标签(梯队/涨停股市值结构,仅人工复盘展示,不参与打分;全市场风格移入顶部结论卡)。"""
    tags = []
    if it.get("ladder_tag"):
        tags.append(f'<span class="badge b-watch" style="margin-left:4px;font-size:10px">{_h(it["ladder_tag"])}</span>')
    # 涨停股市值结构:板块内涨停股流通市值中位数(≥100亿大盘 / ≤50亿小盘),用于风格偏转排序
    if it.get("size_bias") == 1:
        tags.append('<span class="badge b-watch" style="margin-left:4px;font-size:10px" title="涨停股以中小市值为主(流通中位≤50亿)">涨停偏小盘</span>')
    elif it.get("size_bias") == -1:
        tags.append('<span class="badge b-branch" style="margin-left:4px;font-size:10px" title="涨停股以大中市值为主(流通中位≥100亿)">涨停偏大盘</span>')
    elif it.get("size_bias") == 0:
        tags.append('<span class="badge b-watch" style="margin-left:4px;font-size:10px" title="涨停股市值结构均衡(50~100亿)">涨停均衡</span>')
    return "".join(tags)


def _sector_chip(it) -> str:
    lab = {"core": "核心主攻", "defensive": "防御备选", "watch": "观察",
           "rejected": "淘汰", "candidate": "异动候选"}.get(it.get("level"), "")
    cls = {"core": "b-core", "defensive": "b-branch", "watch": "b-watch",
           "rejected": "b-wait", "candidate": "b-watch"}.get(it.get("level"), "b-wait")
    reason = "".join(f'<div class="mut" style="font-size:12px">· {_h(r)}</div>' for r in it.get("reasons", [])[:4])
    zt = f"{it.get('zt_count') or 0} 家"
    st = it.get("stats") or {}
    gain3 = f"{st['gain3'] * 100:+.1f}%" if st.get("gain3") is not None else "-"
    ret20 = f"{st['ret20'] * 100:+.1f}%" if st.get("ret20") is not None else "-"
    # 升级3:板块性价比维度(位置评级/盈亏比/操作优先级)
    pos = _h(it.get("pos_rating") or "-")
    rr = it.get("profit_ratio")
    rr_txt = f"{rr:.2f}" if rr is not None else "-"
    rr_lab = _h(it.get("rr_label") or "-")
    rr_cls = {"高性价比": "up", "中等性价比": "flat", "追高风险": "down", "无数据": "mut"}.get(it.get("rr_label"), "mut")
    pri = _h(it.get("priority") or "-")
    pri_cls = {"高": "up", "中": "flat", "低": "down"}.get(it.get("priority"), "mut")
    # 升级项1:资金面状态
    fst = _h(it.get("fund_status") or "-")
    rate = it.get("rate_1d")
    fund_txt = f"{rate * 100:.1f}%" if rate is not None else "-"
    rank = it.get("fund_rank_1d")
    rank_txt = f"{rank} 名" if rank else "-"
    # 第三轮:环比箭头(较上一交易日) + 点击下钻弹窗
    prev_row = (_SECTOR_PREV_MAP if _SECTOR_PREV_MAP else {}).get(it.get("name")) or {}
    delta_on = _web_ui_flag("delta_arrows")
    pct_delta = _delta_arrow_pct(it.get("pct_chg"), prev_row.get("pct_chg")) if delta_on else ""
    net_delta = _delta_arrow(it.get("net_yi"), prev_row.get("net_yi")) if delta_on else ""
    detail_on = _web_ui_flag("sector_detail")
    if detail_on:
        name_js = _h(it.get("name"))
        row_click = f" onclick='openSectorDetail(\"{name_js}\")' style='cursor:pointer'"
    else:
        row_click = ""
    return (f"<tr{row_click}>"
            f"<td><b>{_h(it.get('name'))}</b><span class='badge {cls}' style='margin-left:8px'>{lab}</span>"
            f"{_ext_tag_html(it)}</td>"
            f"<td>{it.get('score')}</td>"
            f"<td class='{'up' if (it.get('pct_chg') or 0) >= 0 else 'down'}'>{it.get('pct_chg', 0):+.2f}%{pct_delta}</td>"
            f"<td>{it.get('net_yi', 0):+.1f} 亿{net_delta}</td><td>{zt}</td>"
            f"<td class='mut'>{gain3} / {ret20}</td>"
            f"<td class='mut'>{pos}</td>"
            f"<td class='{rr_cls}'>{rr_txt} <span style='font-size:11px'>({rr_lab})</span></td>"
            f"<td class='{pri_cls}'>{pri}</td>"
            f"<td class='mut'>{fund_txt} / {rank_txt}<br><span style='font-size:11px'>{fst}</span></td>"
            f"<td class='mut'>{reason or '-'}</td></tr>")


def _target_item_html(it, trigger_on=True) -> str:
    if it.get("error"):
        return f'<tr><td colspan="9" class="mut">{_h(it["error"])}</td></tr>'
    lv = it.get("levels") or {}
    p_up = f"{it['p_up'] * 100:.0f}%" if it.get("p_up") is not None else "-"
    p_flat = f"{it['p_flat'] * 100:.0f}%" if it.get("p_flat") is not None else "-"
    p_down = f"{it['p_down'] * 100:.0f}%" if it.get("p_down") is not None else "-"
    chg = it.get("pct_chg")
    price = (f"{it.get('price')} <span class='{'up' if chg >= 0 else 'down'}'>{chg:+.2f}%</span>"
             if chg is not None else f"{it.get('price')}")
    if it.get("amount_wan"):
        price += f'<br><span class="mut">成交 {it["amount_wan"]:,.0f} 万</span>'
    elif it.get("amount_yi"):
        price += f'<br><span class="mut">成交 {it["amount_yi"]} 亿</span>'
    # 第三轮:情绪龙头高波动风险标签
    if _web_ui_flag("mood_risk_tag") and (it.get("role") == "情绪龙头"):
        risk_tag = f'<br>{_MOOD_RISK_TAG}'
    else:
        risk_tag = ""
    lv_txt = (f"支撑 {_h(lv.get('support'))}<br>压力 {_h(lv.get('resistance'))}"
              f"<br>止损 {_h(lv.get('stop_loss'))}" if lv else "-")
    trig = _h(it.get("trigger")) if trigger_on and it.get("trigger") else "-"
    act = it.get("action") or "-"
    act_cls = {"关注低吸": "up", "突破跟进": "up", "持有观察": "flat", "减仓兑现": "down", "观望": "flat"}.get(act, "mut")
    adj = "".join(f'<div class="mut" style="font-size:12px">· {_h(n)}</div>'
                  for n in (it.get("adj_notes") or [])[:2]) or "-"
    # 第六轮:正式/候选/降级 标识
    src = it.get("match_source") or ""
    stab = ""
    if it.get("is_stable") and (src and src != "normal"):
        stab = f'<br><span class="badge b-watch" style="font-size:10px">{_h(src)}</span>'
    elif not it.get("is_stable") and src:
        stab = f'<br><span class="badge b-wait" style="font-size:10px">{_h(src)}</span>'
    return (f"<tr><td><b>{_h(it.get('name'))}</b>{stab}{risk_tag}<br><span class='mut'>{_h(it.get('code'))}</span></td>"
            f"<td>{_h(it.get('role') or '-')}</td>"
            f"<td>{price}</td>"
            f"<td>{p_up} / {p_flat} / {p_down}</td>"
            f"<td class='{act_cls}'><b>{act}</b></td>"
            f"<td class='mut'>{lv_txt}</td>"
            f"<td class='mut'>{trig}</td>"
            f"<td class='mut'>{adj}</td></tr>")


def _layer3_html(targets: dict) -> str:
    parts = []
    for sector, t in (targets or {}).items():
        rows = []
        for role, seg in (("aggressive", t.get("aggressive")), ("steady", t.get("steady")), ("etf", t.get("etf"))):
            if not seg:
                continue
            rows.extend(_target_item_html(it, trigger_on=True) for it in seg.get("items", []))
        parts.append(f'<div class="card"><h3>🎯 {_h(sector)} · 标的匹配(三档)</h3>'
                     f'<div class="tbl"><table><tr><th>标的</th><th>档位</th><th>现价</th>'
                     f'<th>上涨/走平/下跌概率</th><th>建议</th><th>支撑/压力/止损</th><th>触发条件</th>'
                     f'<th>信号修正说明</th></tr>'
                     f"{''.join(rows) or '<tr><td colspan=8 class=mut>暂无匹配标的</td></tr>'}"
                     f"</table></div>"
                     f"{_candidate_fallback_html(t)}</div>")
    return "\n".join(parts) or '<div class="card"><h3>暂无达标主线</h3></div>'


def _candidate_fallback_html(t: dict) -> str:
    """第六轮:候选观察 / 降级兜底 分层展示(仅当外挂优化开启时非空)。"""
    cand = t.get("candidate_targets") or []
    fb = t.get("fallback_targets") or []
    if not cand and not fb:
        return ""
    html = ""
    if cand:
        rows = "".join(f'<tr><td>{_h(it.get("name"))}</td><td>{_h(it.get("code"))}</td>'
                       f'<td>{_h(it.get("role") or "-")}</td>'
                       f'<td>{it.get("pct_chg") if it.get("pct_chg") is not None else "-"}%</td>'
                       f'<td class="mut">{_h(it.get("match_source") or "candidate")}</td></tr>'
                       for it in cand if not it.get("error"))
        html += ("<details><summary style='font-size:12px;color:#8a94a8'>"
                 f"候选观察标的(驻留确认中/冷却中,仅展示不参与执行计划)· {len(cand)} 只</summary>"
                 "<div class='tbl'><table><tr><th>名称</th><th>代码</th><th>档位</th><th>涨幅</th>"
                 f"<th>来源</th></tr>{rows}</table></div></details>")
    if fb:
        rows = "".join(f'<tr><td>{_h(it.get("name"))}</td><td>{_h(it.get("code"))}</td>'
                       f'<td>{_h(it.get("role") or "-")}</td>'
                       f'<td class="mut">{_h(it.get("match_source") or "fallback")}</td></tr>'
                       for it in fb if not it.get("error"))
        html += ("<details><summary style='font-size:12px;color:#c29b62'>"
                 f"降级兜底匹配(原档位失败自动补选)· {len(fb)} 只</summary>"
                 "<div class='tbl'><table><tr><th>名称</th><th>代码</th><th>档位</th>"
                 f"<th>来源</th></tr>{rows}</table></div></details>")
    return html


def _plan_table_html(plans: dict, sector: str) -> str:
    seg = (plans or {}).get(sector, {})
    if not seg:
        return '<div class="mut">暂无执行参数</div>'
    rows = []
    for role, lab in (("steady", "稳健首选"), ("aggressive", "激进首选"), ("etf", "ETF")):
        p = seg.get(role) or {}
        if p.get("error") or (not p.get("ok") and p.get("reason")):
            rows.append(f"<tr><th>{lab}</th><td colspan='10' class='mut'>"
                        f"{_h(p.get('reason') or p.get('error'))}</td></tr>")
            continue
        if not p.get("ok"):
            continue
        b = p.get("batch", {})
        stock = (f"<b>{_h(p.get('name'))}</b><br><span class='mut'>{_h(p.get('code'))}</span>"
                 if p.get("code") else "-")
        trig = _h(p.get("trigger")) if p.get("trigger") else "-"
        mode_tag = f"<br><span class='mut' style='font-size:12px'>模式: {_h(p.get('mode_note') or '')}</span>"
        # 升级4:触发状态标签(未触发/触发中/已触发)
        ts = p.get("trigger_status") or {}
        ts_cls = {"trigger-on": "up", "trigger-off": "mut", "trigger-unknown": "mut"}.get(ts.get("label"), "mut")
        ts_txt = _h(ts.get("status") or "未触发")
        ts_note = _h(ts.get("note") or "")
        ts_html = (f"<b class='{ts_cls}'>{ts_txt}</b><br><span class='mut' style='font-size:11px'>{ts_note}</span>"
                   if ts else "-")
        rows.append(
            f"<tr><th>{lab}</th><td>{stock}{mode_tag}</td>"
            f"<td>{_h(p.get('price'))}</td>"
            f"<td>{_h(p.get('stop'))}</td>"
            f"<td>{_h(p.get('target1'))} / {_h(p.get('target2'))}</td>"
            f"<td>{p.get('position_pct', 0) * 100:.1f}%<br><span class='mut'>{p.get('shares')} 股 · {_fmt(p.get('position_value'))} 元</span></td>"
            f"<td>{b.get('first', {}).get('ratio', 0) * 100:.0f}% @ {_h(b.get('first', {}).get('price'))}<br>"
            f"{b.get('second', {}).get('ratio', 0) * 100:.0f}% @ {_h(b.get('second', {}).get('price'))}</td>"
            f"<td class='mut'>{trig}</td>"
            f"<td>{ts_html}</td>"
            f"<td class='mut'>{_h(p.get('note'))}</td></tr>")
    return ("<div class='tbl'><table><tr><th>档位</th><th>标的</th><th>现价</th><th>止损</th><th>目标1/2</th>"
            "<th>建议仓位</th><th>分批方案</th><th>触发条件</th><th>触发状态</th><th>说明</th></tr>"
            + "\n".join(rows) + "</table></div>")


# ============================================================ 第三轮:前端体验优化
from app import config as _cfg_mod

_TARGET_SNAP_DIR = os.path.join(config.DATA_DIR, "review")
_SECTOR_PREV_MAP = {}  # 每请求在 page_decision 里刷新 {name: {pct_chg, net_yi}}


def _web_ui_flag(key: str) -> bool:
    """读取 web_ui 开关(第三轮)。"""
    try:
        from app.support import settings as _st
        return bool((_st.load().get("web_ui") or {}).get(key, True))
    except Exception:  # noqa: BLE001
        return True


def _safe_date_str(d=None) -> str:
    return (d or _dt.date.today()).isoformat()


def _prev_trade_date(cur: str = None) -> str:
    """返回严格早于 cur(默认今日)的最近一次决策快照日期,供「昨日信号复盘 / 环比箭头」使用。
    只认已落盘的 targets_*.json,避免把本次请求刚写入的今日快照误当成昨日。"""
    cur = cur or str(_dt.date.today())
    import glob
    pat = os.path.join(_TARGET_SNAP_DIR, "targets_*.json")
    dates = [os.path.basename(p)[8:-5] for p in glob.glob(pat)]
    dates = [d for d in dates if d < cur]
    return max(dates) if dates else ""


def _save_target_snapshot(data: dict) -> None:
    """把今日决策的推荐标的 + 板块行情快照持久化,供次日「昨日信号复盘 / 环比箭头」使用。"""
    os.makedirs(_TARGET_SNAP_DIR, exist_ok=True)
    date = str(data.get("date") or _dt.date.today())
    plans = data.get("plans") or {}
    snap = {"date": date, "sectors": []}
    for sector, seg in plans.items():
        for role, p in seg.items():
            if not p.get("ok") or not p.get("code"):
                continue
            snap["sectors"].append({
                "sector": sector, "role": role,
                "code": p.get("code"), "name": p.get("name"),
                "asset_type": p.get("asset_type"), "position_pct": p.get("position_pct"),
            })
    if not snap["sectors"]:  # plans 空则从 targets 提取
        for sector, t in (data.get("targets") or {}).items():
            for role, seg in (("aggressive", t.get("aggressive")), ("steady", t.get("steady")), ("etf", t.get("etf"))):
                items = (seg or {}).get("items") or []
                for it in items[:1]:
                    if it.get("error") or not it.get("code"):
                        continue
                    snap["sectors"].append({"sector": sector, "role": role,
                                            "code": it["code"], "name": it.get("name"),
                                            "asset_type": role, "position_pct": None})
    path = os.path.join(_TARGET_SNAP_DIR, f"targets_{date}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    # 板块行情快照(环比箭头数据源)
    p2 = (data.get("layers") or {}).get("layer2") or {}
    sectors_daily = {}
    for it in ([p2.get("core")] if p2.get("core") else []) + \
               ([p2.get("defensive")] if p2.get("defensive") else []) + \
               (p2.get("watch") or []) + (p2.get("rejected") or []):
        if it:
            sectors_daily[it.get("name")] = {"pct_chg": it.get("pct_chg"), "net_yi": it.get("net_yi")}
    day_path = os.path.join(_TARGET_SNAP_DIR, f"layers_{date}.json")
    try:
        with open(day_path, "w", encoding="utf-8") as f:
            json.dump({"date": date, "sectors": sectors_daily}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ---- 优化项1:极简结论卡
def _conclusion_bar_html(con: dict, d: dict) -> str:
    core = con.get("core_sector") or "-"
    stock = con.get("core_stock") or "暂无可执行标的"
    act = "关注主线回踩/突破机会,分批布局"
    if con.get("grade") in ("C", "D"):
        act = "市场偏弱,以观望/持有兑现为主"
    risk = con.get("risk_tip") or "-"
    grade_cls = _grade_badge(con.get("grade"))
    style = _market_style_of(d)
    style_badge = _style_badge_html(style)
    return (f"<div class='card' id='conclusion-bar' style='border:0;background:linear-gradient(135deg,#1b2a63 0%,#17233f 100%);"
            f"padding:14px 18px;margin:10px 0'><div style='display:flex;gap:14px;align-items:center;flex-wrap:wrap'>"
            f"<div class='badge {grade_cls}' style='font-size:22px;padding:6px 16px'>{_h(con.get('grade_label') or '')}</div>"
            f"<div style='font-size:24px;font-weight:800;color:#fff'>总仓位上限 <span style='color:#ffca28'>{con.get('cap', 0):.0%}</span></div>"
            f"{style_badge}"
            f"<div style='flex:1;min-width:260px;font-size:15px;line-height:1.7'>"
            f"<b>首选方向:</b> {_h(core) or '—'} &nbsp; <b>首选标的:</b> {_h(stock)}<br>"
            f"<span style='color:#ffd54f;font-weight:600'>操作建议:</span> {_h(act)} &nbsp;"
            f"<span style='color:#ff8a80;font-weight:600'>核心风险:</span> {_h(risk)}</div>"
            f"</div><div class='mut' style='font-size:12px;margin-top:6px'>盯盘速览:{_h(con.get('line') or '')}</div></div>")


# ---- 优化项2:昨日信号复盘
def _load_prev_targets(cur: str = None) -> list:
    prev = _prev_trade_date(cur)
    if not prev:
        return []
    path = os.path.join(_TARGET_SNAP_DIR, f"targets_{prev}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("sectors") or []
    except (OSError, ValueError):
        return []


def _yesterday_review_html(cur: str = None) -> str:
    try:
        items = _enrich_prev_with_quotes(_load_prev_targets(cur))
        if not items:
            return ('<div class="card"><h3>📋 昨日信号复盘</h3>'
                    '<div class="mut">暂无昨日快照:今日生成决策后,次日将自动复盘昨日推荐标的今日表现。</div></div>')
        rows = []
        pcts = []
        up = 0
        for it in items:
            pct = it.get("pct_chg")
            label = ""
            if pct is None:
                label = '<span class="mut">-</span>'
            else:
                pcts.append(pct)
                if pct > 0:
                    up += 1
                cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
                label = f"<span class='{cls}'>{pct:+.2f}%</span>"
            rows.append(
                f"<tr><td>{_h(it.get('sector') or '')}</td><td>{_h(it.get('name') or '')}"
                f"<br><span class='mut'>{_h(it.get('code') or '')}</span></td>"
                f"<td>{_h(it.get('role') or '')}</td><td>{label}</td></tr>")
        n = len(items)
        avg = sum(pcts) / len(pcts) if pcts else None
        mx = max(pcts) if pcts else None
        mn = min(pcts) if pcts else None
        stat = (f"昨日推荐 <b>{n}</b> 只 · 上涨 <b class='up'>{up}</b> 只 · 胜率 "
                f"<b class='{'up' if n and up / n >= 0.5 else 'down'}'>{up / n * 100:.0f}%</b>"
                f" · 平均 <b>{avg:+.2f}%</b>" if avg is not None else f"昨日推荐 <b>{n}</b> 只")
        if mx is not None:
            stat += f" · 最大涨幅 <b class='up'>{mx:+.2f}%</b> · 最大跌幅 <b class='down'>{mn:+.2f}%</b>"
        return ('<details class="card"><summary><b>📋 昨日信号复盘</b> &nbsp;<span class="mut">'
                + _h(f"{_prev_trade_date(cur)} 推荐标的 → 今日表现") + '</span></summary>'
                + f"<div class='line'>{stat}</div>"
                + (f"<div class='tbl'><table><tr><th>板块</th><th>标的</th><th>档位</th><th>今日</th></tr>"
                   + "".join(rows) + "</table></div>" if rows else "")
                + "</details>")
    except Exception as e:  # noqa: BLE001
        return f'<div class="card"><h3>📋 昨日信号复盘</h3><div class="err">加载失败:{_h(e)}</div></div>'


def _prev_pick_set(prev_snaps: list) -> dict:
    """把昨日快照去重成 {code: item}。"""
    out = {}
    for s in prev_snaps:
        c = s.get("code")
        if c:
            out[c] = s
    return out


def _enrich_prev_with_quotes(snaps: list) -> list:
    """用今日实时行情填充昨日快照的 pct。"""
    if not snaps:
        return []
    codes = list(dict.fromkeys(s.get("code") for s in snaps if s.get("code")))
    if not codes:
        return snaps
    from app.data import fetcher as _fe
    try:
        quotes = _fe.get_spot_quotes(codes)
    except Exception:  # noqa: BLE001
        quotes = {}
    for s in snaps:
        q = quotes.get(s.get("code")) or {}
        s["quoted"] = q
        s["pct_chg"] = q.get("pct_chg")
        s["price"] = q.get("price")
    return snaps


# ---- 优化项3a:淘汰板块折叠显示
def _rejected_section_html(rejected: list, on: bool) -> str:
    if not rejected:
        return ""
    if not on:
        detail = "".join(_sector_chip(it) for it in rejected)
        return ('<details open><summary><b>已淘汰板块</b>(<span class="mut">'
                + str(len(rejected)) + ' 个</span>)</summary>' + detail + '</details>')
    head = f"已淘汰 {len(rejected)} 个"
    return (f"<details closed class='card'><summary><b>🗑 {_h(head)}</b>"
            f"<span class='mut' style='margin-left:8px'>点击展开查看详情</span></summary>"
            f"<div class='tbl'><table><tr><th>板块</th><th>评分</th><th>当日涨跌</th><th>主力净流入</th>"
            f"<th>涨停家数</th><th>3日/20日涨幅</th><th>位置评级</th><th>盈亏比</th><th>优先级</th>"
            f"<th>当日净流入率/排名</th><th>淘汰原因</th></tr>"
            + "".join(_sector_chip(it) for it in rejected)
            + "</table></div></details>")


def _candidate_section_html(candidate: list, on: bool) -> str:
    """稳定器异动候选列表(candidate):打分达标但驻留/冷却/同池替换确认中,未计入正式主线。"""
    if not candidate or not on:
        return ""
    rows = "".join(_sector_chip(it) for it in candidate)
    return ('<details closed><summary><b>🔥 异动候选</b>'
            f'(<span class="mut">{len(candidate)} 个 · 达标但防抖确认中,暂不计入正式主线</span>)</summary>'
            "<div class='tbl'><table><tr><th>板块</th><th>评分</th><th>当日涨跌</th><th>主力净流入</th>"
            "<th>涨停家数</th><th>3日/20日涨幅</th><th>位置评级</th><th>盈亏比</th><th>优先级</th>"
            "<th>当日净流入率/排名</th><th>状态说明</th></tr>"
            + rows
            + "</table></div></details>")


def _raw_debug_html(raw: dict, stats: dict, on: bool) -> str:
    """原始未防抖信号(raw)调试展示:仅供与 stable 对比,今日决策以 stable 为准。"""
    if not raw or not on:
        return ""
    raw_core = raw.get("core")
    raw_def = raw.get("defensive")
    core_txt = (f"{_h(raw_core['name'])} ({raw_core['score']} 分)" if raw_core else "无")
    def_txt = (f"{_h(raw_def['name'])} ({raw_def['score']} 分)" if raw_def else "无")
    switch_txt = (f"主线切换(raw {stats.get('raw_switches', 0)} 次 / stable "
                  f"{stats.get('stable_switches', 0)} 次)" if stats else "切换统计不可用")
    return ('<details closed><summary class="mut"><b>🛠 原始未防抖信号(raw · 调试)</b>'
            f'<span class="mut" style="margin-left:8px">raw核心:{core_txt} · raw防御:{def_txt} · {switch_txt}</span></summary>'
            '<div class="mut" style="font-size:12px">原始流水线直接输出,未经防抖稳定器;'
            '今日决策以 stable 稳定输出为准(避免盘中微小资金抖动造成主线反复横跳)。</div></details>')


def _prev_sector_map(cur: str = None) -> dict:
    """读取上一交易日板块行情快照 {name: {pct_chg, net_yi}}。"""
    prev = _prev_trade_date(cur)
    if not prev:
        return {}
    path = os.path.join(_TARGET_SNAP_DIR, f"layers_{prev}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("sectors") or {}
    except (OSError, ValueError):
        return {}


# ---- 优化项3b:标的匹配 Tab 切换
def _target_tabs_html(data: dict) -> str:
    targets = data.get("targets") or {}
    sectors = list(targets.keys())
    if not sectors:
        return '<div class="card"><h3>🎯 标的匹配(三档)</h3><div class="mut">暂无可匹配标的</div></div>'
    tabs = "".join(
        f'<button class="tabs-btn{" on" if i == 0 else ""}" data-tab="tab-{i}" type="button">{_h(s)}</button>'
        for i, s in enumerate(sectors))
    panes = []
    for i, s in enumerate(sectors):
        pane = _layer3_html({s: targets[s]})
        panes.append(f'<div class="tab-pane{" on" if i == 0 else ""}" id="tab-{i}">{pane}</div>')
    return ('<div class="card"><h3>🎯 标的匹配(三档 · 点击板块切换)</h3>'
            f'<div class="target-tabs">{tabs}</div>{"".join(panes)}</div>')


# ---- 优化项4:板块详情弹窗(结构 + 前端 JS/CSS)
def _sector_modal_html() -> str:
    if not _web_ui_flag("sector_detail"):
        return ""
    return """<div id="sector-modal" class="modal-mask" style="display:none" onclick="if(event.target===this)closeSectorModal()">
  <div class="modal-box">
    <div class="modal-head"><h3 id="sector-modal-title">板块详情</h3>
      <span class="modal-x" onclick="closeSectorModal()">✕</span></div>
    <div id="sector-modal-body"><div class="mut">加载中…</div></div>
  </div>
</div>
<script>
function openSectorDetail(name){
  var m=document.getElementById('sector-modal');
  document.getElementById('sector-modal-title').textContent=name+' · 板块详情';
  document.getElementById('sector-modal-body').innerHTML='<div class="mut">加载中…</div>';
  m.style.display='flex';
  fetch('/api/sector_detail?name='+encodeURIComponent(name)).then(function(r){return r.json()})
   .then(function(j){
     if(j.error){document.getElementById('sector-modal-body').innerHTML='<div class="err">'+j.error+'</div>';return;}
     var h='<div class="line" style="font-size:14px"><b>'+j.name+'</b></div>';
     h+='<div class="grid">'
        +'<div><div class="mut" style="margin:6px 0">打分明细</div><div class="tbl"><table>'+(j.breakdown_html||'')+'</table></div></div>'
        +'<div>'+(j.kline_html||'')+'</div></div>';
     h+='<div class="mut" style="margin:6px 0">入选理由 / 否决原因</div>'+ (j.reason_html||'');
     h+='<div class="grid"><div><div class="mut" style="margin:6px 0">成分股涨幅 Top10</div>'+(j.constituent_html||'')+'</div>'
        +'<div><div class="mut" style="margin:6px 0">今日相关新闻</div>'+(j.news_html||'')+'</div></div>';
     document.getElementById('sector-modal-body').innerHTML=h;
   })
   .catch(function(e){document.getElementById('sector-modal-body').innerHTML='<div class="err">请求失败:'+e+'</div>';});
}
function closeSectorModal(){document.getElementById('sector-modal').style.display='none';}
Array.prototype.forEach.call(document.querySelectorAll('.target-tabs .tabs-btn'),function(b){
  b.addEventListener('click',function(){
    document.querySelectorAll('.target-tabs .tabs-btn').forEach(function(x){x.classList.remove('on');});
    b.classList.add('on');
    document.querySelectorAll('.tab-pane').forEach(function(p){p.classList.remove('on');});
    document.getElementById(b.getAttribute('data-tab')).classList.add('on');
  });
});
</script>"""


# ---- 优化项3c:情绪龙头高波动风险标签
_MOOD_RISK_TAG = '<span class="mut" style="font-size:11px;color:#8a94a8">高波动·纯情绪博弈·建议极轻仓</span>'


# ---- 优化项3d:环比箭头
def _delta_arrow(cur, prev, good_up: bool = True) -> str:
    """cur/prev 为数值(可 None);返回 ↑↓ 变化值 HTML。"""
    if cur is None or prev is None:
        return ""
    try:
        cur = float(cur); prev = float(prev)
    except (TypeError, ValueError):
        return ""
    delta = cur - prev
    cls = "up" if (delta >= 0) else "down"
    if not good_up:
        cls = "down" if (delta >= 0) else "up"
    arrow = "↑" if (cur >= prev) else "↓"
    return f'<span class="delta {cls}" title="较前日" style="font-size:11px"> {arrow}{delta:+.2f}</span>'


def _delta_arrow_pct(cur, prev) -> str:
    if cur is None or prev is None:
        return ""
    try:
        c = float(cur); p = float(prev)
    except (TypeError, ValueError):
        return ""
    if p == 0:
        return ""
    d = c - p
    cls = "up" if d >= 0 else "down"
    arrow = "↑" if d >= 0 else "↓"
    return f'<span class="delta {cls}" style="font-size:11px">({arrow}{d:+.2f})</span>'


# ---- 优化项4:板块详情下钻
def _sector_detail_html(it: dict) -> str:
    """生成板块详情卡(函数内组织数据,弹窗由前端拼接)。"""
    bd = it.get("breakdown") or {}
    stats = it.get("stats") or {}
    rows = []
    def _row(k, v, fmt=None):
        txt = fmt(v) if fmt and v is not None else ("-" if v is None else str(v))
        rows.append(f"<tr><th>{_h(k)}</th><td>{_h(txt)}</td></tr>")
    _row("综合评分", it.get("score"))
    _row("资金分项(满分40)", bd.get("fund"))
    _row("· 5日资金分项", bd.get("fund_5d"))
    _row("· 单日资金分项", bd.get("fund_1d"))
    _row("趋势分项(满分30)", bd.get("trend"))
    _row("情绪分项(满分20)", bd.get("sentiment"))
    _row("消息分项(满分10)", bd.get("news"))
    _row("当日涨跌", it.get("pct_chg"), lambda v: f"{v:+.2f}%")
    _row("主力净流入", it.get("net_yi"), lambda v: f"{v:+.1f} 亿")
    _row("涨停家数", it.get("zt_count"))
    _row("净流入率(单日)", it.get("rate_1d"), lambda v: f"{v * 100 if v is not None else 0:.1f}%")
    _row("5日净流入率", it.get("rate_5d"), lambda v: f"{v * 100 if v is not None else 0:.1f}%")
    _row("位置评级", it.get("pos_rating"))
    _row("盈亏比", it.get("profit_ratio"))
    _row("操作优先级", it.get("priority"))
    _row("3日涨幅", stats.get("gain3"), lambda v: f"{v * 100:.1f}%")
    _row("20日涨幅", stats.get("ret20"), lambda v: f"{v * 100:.1f}%")
    _row("20日回撤", stats.get("dd20"), lambda v: f"{v * 100:.1f}%")
    reasons = it.get("reasons") or []
    reason = "".join(f"<div class='line' style='font-size:13px'>· {_h(r)}</div>" for r in reasons[:6])
    return {"name": it.get("name"), "score": it.get("score"),
            "breakdown_rows": rows, "reason": reason}


def _sector_detail_kline_html(name: str) -> str:
    """板块概念指数 K 线(Plotly 迷你图)。"""
    import plotly.graph_objects as go
    import plotly.io as pio
    try:
        from app.features.concept_features import _get_concept_close
        s = _get_concept_close(name)
        if s is None or len(s) < 2:
            return '<div class="mut">暂无指数数据</div>'
        close = s.astype(float)
        idx = [str(x)[:10] for x in close.index]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=idx, y=close.values, mode="lines",
                                 line=dict(color="#ef5350", width=1.5),
                                 fill="tozeroy", fillcolor="rgba(239,83,80,0.12)"))
        fig.update_layout(title=f"{_h(name)} 概念指数(近 {len(close)} 个交易日)",
                          template="plotly_dark", height=240, margin=dict(l=10, r=10, t=36, b=10),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          xaxis=dict(showgrid=False), yaxis=dict(showgrid=True, gridcolor="rgba(90,110,160,0.15)"))
        return pio.to_html(fig, full_html=False, include_plotlyjs="cdn",
                           default_width="100%", default_height="100%")
    except Exception as e:  # noqa: BLE001
        return f'<div class="mut">指数不可用</div>'


def _sector_constituent_top(name: str, n: int = 10) -> str:
    """板块成分股当日涨幅 Top N。"""
    try:
        from app.support import mainline as _ml
        spot = _ml._a_spot_map()
        stocks = _ml._match_stocks(name, spot)
        if not stocks:
            return '<div class="mut">暂无成分数据</div>'
        rows_o = sorted(stocks, key=lambda s: -(s.get("pct_chg") or -999))[:n]
        body = "".join(
            f"<tr><td>{i + 1}</td><td>{_h(s.get('name') or '-')}<br><span class='mut'>{_h(s.get('code') or '')}</span></td>"
            f"<td class='{'up' if (s.get('pct_chg') or 0) >= 0 else 'down'}'>{s.get('pct_chg', 0):+.2f}%</td></tr>"
            for i, s in enumerate(rows_o))
        return f"<div class='tbl'><table><tr><th>#</th><th>成分股</th><th>涨跌幅</th></tr>{body}</table></div>"
    except Exception as e:  # noqa: BLE001
        return f'<div class="mut">成分不可用</div>'


def _sector_news_html(name: str) -> str:
    """板块相关新闻摘要(复用 review events)。"""
    try:
        from app.review.data import collect_events
        ev = collect_events()
        news = ev.get("news") or []
        kw = _news_keywords(name)
        hits = []
        for n in news:
            text = f"{n.get('title', '')} {n.get('summary', '')}"
            if text and any(k and k in text for k in kw):
                hits.append(n)
                if len(hits) >= 6:
                    break
        if not hits:
            return '<div class="mut">今日无直接相关新闻</div>'
        rows = "".join(
            f'<div class="line" style="font-size:13px"><span class="mut">{_h(n.get("time") or "")}</span> '
            f'<b>{_h(n.get("title") or "")}</b><br><span class="mut">{_h(n.get("summary") or "")}</span></div>'
            for n in hits)
        return f'<div style="max-height:260px;overflow:auto">{rows}</div>'
    except Exception:  # noqa: BLE001
        return '<div class="mut">新闻数据不可用</div>'


def _news_keywords(name: str) -> list:
    """从板块名拆出关键词用于新闻命中。"""
    kws = [name]
    if len(name) >= 4:
        kws.append(name[:4])
    return [k for k in kws if k]


@app.route("/api/sector_detail")
def api_sector_detail():
    """板块详情下钻:打分明细 + K线 + 成分TOP + 新闻。"""
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"error": "缺少板块名称"}), 400
    try:
        from app.decision import engine as _en
        p2 = _en.mainline_select()
        it = None
        for grp in ("core", "defensive"):
            if p2.get(grp) and p2[grp]["name"] == name:
                it = p2[grp]
                break
        if it is None:
            for w in (p2.get("watch") or []):
                if w["name"] == name:
                    it = w
                    break
        if it is None:
            for rj in (p2.get("rejected") or []):
                if rj["name"] == name:
                    it = rj
                    break
        if it is None:
            # 稳定器输出(stable:core/defensive/watch/rejected/candidate)兜底,
            # 页面显示的是稳定结果,raw 与 stable 名单可能不一致
            from app.support import mainline_stabilizer as _stab
            st = _stab.get_output().get("stable") or {}
            for grp in ("core", "defensive"):
                if st.get(grp) and st[grp]["name"] == name:
                    it = st[grp]
                    break
            if it is None:
                for w in (st.get("watch") or []):
                    if w["name"] == name:
                        it = w
                        break
            if it is None:
                for rj in (st.get("rejected") or []):
                    if rj["name"] == name:
                        it = rj
                        break
            if it is None:
                for cd in (st.get("candidate") or []):
                    if cd["name"] == name:
                        it = cd
                        break
        if it is None:
            return jsonify({"error": f"未找到板块: {name}"}), 404
        detail = _sector_detail_html(it)
        return jsonify({
            "name": name,
            "breakdown_html": "".join(detail["breakdown_rows"]),
            "kline_html": _sector_detail_kline_html(name),
            "constituent_html": _sector_constituent_top(name),
            "news_html": _sector_news_html(name),
            "reason_html": detail["reason"],
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/decision")
def page_decision():
    refresh = request.args.get("refresh") == "1"
    c = _get_decision(refresh=refresh)
    if c["err"]:
        return _shell("decision", "今日决策",
                      '<header><h1>🎯 今日决策</h1><span class="mut">决策引擎</span></header>'
                      f'<div class="card err">决策生成失败: {_h(c["err"])}'
                      f' <a class="btn" href="/decision?refresh=1">重试</a></div>'
                      '<div class="footer">免责声明:仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>')
    d = c["data"]
    con = d["conclusion"]
    p1 = d["layers"]["layer1"]
    p2 = d["layers"]["layer2"]

    global _SECTOR_PREV_MAP
    _SECTOR_PREV_MAP = _prev_sector_map(d["date"])
    _save_target_snapshot(d)

    core = p2.get("core")
    defen = p2.get("defensive")
    plans = d.get("plans") or {}
    core_plan = _plan_table_html(plans, core["name"]) if core else '<div class="mut">无达标主线,暂无执行计划</div>'

    style = _market_style_of(d)
    layer1 = _layer1_html(p1, style)
    layer2_parts = []
    for it in ([core] if core else []) + ([defen] if defen else []) + (p2.get("watch") or []):
        layer2_parts.append(_sector_chip(it))
    layer2 = (f"<div class='card'><h3>准入线:{p2.get('pass_score')} 分 · 一票否决 + 分级"
              f"{' · 已启用防抖稳定器' if d['layers'].get('stabilizer_stats') else ''}</h3>"
              f"<div class='tbl'><table><tr><th>板块</th><th>评分</th><th>当日涨跌</th><th>主力净流入</th>"
              f"<th>涨停家数</th><th>3日/20日涨幅</th><th>位置评级</th><th>盈亏比</th><th>优先级</th>"
              f"<th>当日净流入率/排名</th><th>入选理由 / 否决原因</th></tr>"
              f"{''.join(layer2_parts) or '<tr><td colspan=11 class=mut>暂无板块数据</td></tr>'}</table></div>"
              + _candidate_section_html(p2.get("candidate") or [], _web_ui_flag("candidate_list"))
              + _rejected_section_html(p2.get("rejected") or [], _web_ui_flag("rejected_collapse"))
              + _raw_debug_html(d["layers"].get("layer2_raw"), d["layers"].get("stabilizer_stats"),
                                _web_ui_flag("raw_debug"))
              + "</div>")

    # 第三轮:极简结论卡(默认展开,叠加在原有结论卡上方)
    bar = _conclusion_bar_html(con, d) if _web_ui_flag("conclusion_bar") else ""
    # 第三轮:标的匹配 Tab 切换
    if _web_ui_flag("target_tabs"):
        layer3 = _target_tabs_html(d)
    else:
        layer3 = _layer3_html(d.get("targets"))
    # 第三轮:昨日信号复盘(页面底部折叠)
    yrev = _yesterday_review_html(d["date"]) if _web_ui_flag("yesterday_review") else ""

    content = [
        '<header><h1>🎯 今日决策</h1>'
        f'<span class="mut">{_h(d.get("date"))} · 总资金 {_fmt(d.get("total_asset"))} 元 · 风险偏好「{_h(d.get("taste"))}」</span>'
        f'<a class="btn" href="/decision?refresh=1">🔄 刷新</a> '
        '<a class="btn gray" href="/settings">⚙️ 调整参数</a></header>',
        '<div class="card" style="border:1px solid #5b4231;background:#2a2017;color:#e0b27a">'
        '⚠️ 本站全部内容仅供研究参考,不构成投资建议;所有「关注/观察/建议配置」表述均为中性研究语义,不构成任何买卖指令。股市有风险,入市需谨慎。</div>',
        bar,
        '<div class="card"><h3>🧭 决策过程拆解(四层漏斗)</h3>'
        '<details open><summary><b>① 大盘开仓许可评级</b></summary>' + layer1 + '</details>'
        '<details open><summary><b>② 主线概念遴选</b></summary>' + layer2 + '</details>'
        '<details open><summary><b>③ 标的精准匹配</b></summary>' + layer3 + '</details>'
        '<details open><summary><b>④ 执行参数</b></summary>' + core_plan + '</details></div>',
        yrev,
        _sector_modal_html(),
        '<div class="footer">决策引擎由四层漏斗自动收敛:市场许可 → 主线遴选 → 标的匹配 → 执行参数。仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>',
    ]
    return _shell("decision", "今日决策", "\n".join(content))


@app.route("/api/decision")
def api_decision():
    c = _get_decision(refresh=request.args.get("refresh") == "1")
    if c["err"]:
        return jsonify({"error": c["err"]}), 500
    return jsonify(c["data"])


# ---- 每日复盘 ----
_REVIEW_CACHE = {"t": 0.0, "data": None, "err": None}
_REVIEW_TTL = 600  # 秒

def _get_review(date: str = None) -> dict:
    from app.review import collect_review, generate_review
    if (_REVIEW_CACHE["data"] is None
            or _REVIEW_CACHE["err"] is not None
            or _time.time() - _REVIEW_CACHE["t"] > _REVIEW_TTL):
        try:
            d = collect_review(date=date, use_cache=True)
            r = generate_review(d)
            _REVIEW_CACHE["data"] = {"date": d.get("date"), **r}
            _REVIEW_CACHE["err"] = None
        except Exception as e:  # noqa: BLE001
            _REVIEW_CACHE["data"] = None
            _REVIEW_CACHE["err"] = str(e)
        _REVIEW_CACHE["t"] = _time.time()
    return _REVIEW_CACHE


def _jsonable_review(data: dict) -> dict:
    return {
        "date": data.get("date"),
        "sections": {k: {"title": v["title"], "lines": v["lines"]}
                     for k, v in (data.get("sections") or {}).items()},
        "tables": data.get("tables") or {},
    }


@app.route("/review")
def review():
    """每日复盘已合并进复盘报告,统一跳转到 /report。"""
    return redirect("/report", code=302)


@app.route("/api/review")
def api_review():
    date = (request.args.get("date") or "").strip() or None
    c = _get_review(date)
    if c["err"]:
        return jsonify({"error": c["err"]}), 500
    return jsonify(_jsonable_review(c["data"]))
# ============================================================ 模块2 持仓诊断
_PF_CACHE = {"t": 0.0, "data": None, "err": None}
_PF_TTL = 300


def _get_portfolio(refresh=False):
    if refresh or _PF_CACHE["data"] is None or _PF_CACHE["err"] or \
            _time.time() - _PF_CACHE["t"] > _PF_TTL:
        from app.support import portfolio as pf
        try:
            _PF_CACHE["data"] = pf.diagnose()
            _PF_CACHE["err"] = None
        except Exception as e:  # noqa: BLE001
            _PF_CACHE["data"] = None
            _PF_CACHE["err"] = str(e)
        _PF_CACHE["t"] = _time.time()
    return _PF_CACHE


def _pos_row_html(p) -> str:
    if not p.get("ok"):
        return (f'<div class="card"><h3>{_h(p["code"])} 诊断失败</h3>'
                f'<div class="err">{_h(p.get("error"))}</div></div>')
    cls = {"buy": "b-buy", "sell": "b-sell", "hold": "b-hold"}.get(p["advice_action"], "b-wait")
    pnl = p.get("pnl_pct")
    pnl_cls = "up" if (pnl or 0) >= 0 else "down"
    lv = p.get("levels") or {}
    reasons = "".join(f'<div class="mut">· {_h(r)}</div>' for r in p.get("reasons", [])[:3])
    return (
        f"<div class='card'><h3>{_h(p.get('name'))} ({_h(p.get('code'))}) "
        f"<span class='tag'>{_h(p.get('category'))}</span>"
        f"<span class='badge {cls}'>建议:{_h(p.get('advice_action_cn'))}</span></h3>"
        f"<div class='grid'><div><div class='line'>现价 <b>{_h(p.get('price'))}</b> · 成本 {_h(p.get('cost'))} · "
        f"浮盈 <b class='{pnl_cls}'>{pnl * 100:+.1f}%</b></div>"
        f"<div class='line'>市值 {_fmt(p.get('market_value'))} 元 · 占总资产 {p.get('weight', 0) * 100:.1f}% · 板块:{_h(p.get('sector') or '未知')}</div>"
        f"<div class='line'>预测:上涨 <b>{p['prediction']['p_up']:.0%}</b> / 震荡 {p['prediction']['p_flat']:.0%} / 下跌 {p['prediction']['p_down']:.0%} · {_h(p['prediction']['direction_cn'])}</div>"
        f"<div class='line'>支撑 {_h(lv.get('support'))} · 压力 {_h(lv.get('resistance'))} · 止损 {_h(lv.get('stop_loss'))} · 目标 {_h(lv.get('target'))}</div>"
        f"<div class='line'><b>操作方案:</b> {_h(p.get('plan'))}</div>{reasons}</div>"
        f"<div class='card'><h3>⚠️ 风险</h3>{''.join(f'<div class=\"flat\">! {_h(r)}</div>' for r in p.get('risks', [])) or '<div class=\"mut\">无</div>'}</div></div></div>")


@app.route("/portfolio")
def page_portfolio():
    from app.support.risk import load_portfolio
    refresh = request.args.get("refresh") == "1"
    c = _get_portfolio(refresh=refresh)
    positions = load_portfolio()
    rows = "".join(f"<tr><td>{_h(p['code'])}</td><td>{_h(p['category'])}</td><td>{_h(p['qty'])}</td>"
                   f"<td>{_h(p['cost'])}</td></tr>" for p in positions)
    pos_table = (f"<div class='tbl'><table><tr><th>代码</th><th>分类</th><th>数量</th><th>成本</th></tr>{rows}"
                 "</table></div>") if positions else '<div class="mut">尚未导入持仓,可在下方添加(支持 CSV 批量导入:code,qty,cost,category)。</div>'
    cards = "\n".join(_pos_row_html(p) for p in (c["data"] or {}).get("positions", []))
    summary = ""
    if c["data"]:
        s = c["data"]["summary"]
        risk_cls = "up" if s["risk_rating"] in ("中", "高") else "down"
        tips = "".join(f'<div class="flat">⚠ {_h(t)}</div>' for t in s.get("risk_tips", []))
        summary = (
            f"<div class='card'><h3>📊 组合概览</h3><div class='grid3'>"
            f"<div class='pill'>持仓数<b>{s.get('count', 0)}</b></div>"
            f"<div class='pill'>总市值<b>{_fmt(s.get('total_market_value'))}</b></div>"
            f"<div class='pill'>总仓位<b>{s.get('total_pct', 0) * 100:.1f}%</b></div>"
            f"<div class='pill'>风险评级<b class='{risk_cls}'>{_h(s.get('risk_rating'))}</b></div>"
            f"<div class='pill'>恐贪指数<b>{_h(s.get('fear_greed'))}</b></div>"
            f"<div class='pill'>总资产<b>{_fmt(s.get('total_asset'))}</b></div>"
            f"</div>{tips}</div>")
    err = f'<div class="card err">诊断失败: {_h(c["err"])}</div>' if c["err"] else ""
    content = [
        f"<header><h1>💼 个性化持仓诊断</h1>"
        f'<span class="mut">逐只诊断 → 三类操作方案(深套做差价 / 盈利止盈 / 观望触发) + 组合风控</span>'
        f'<a class="btn" href="/portfolio?refresh=1">🔄 重新诊断</a></header>',
        err, summary,
        '<div class="grid"><div class="card"><h3>📋 当前持仓</h3>' + pos_table + "</div>",
        '<div class="card"><h3>➕ 添加/导入持仓</h3>'
        '<form method="post" action="/api/portfolio/add" style="display:flex;gap:6px;flex-wrap:wrap" onsubmit="return pfSubmit(event,this,\'pfAddMsg\')">'
        '<input name="code" placeholder="代码 600519" required style="width:110px">'
        '<input name="qty" type="number" placeholder="数量" required style="width:80px">'
        '<input name="cost" type="number" step="0.001" placeholder="成本" required style="width:90px">'
        '<select name="category"><option>核心</option><option>波段</option><option>观察</option></select>'
        '<button class="btn">添加</button><span id="pfAddMsg" class="mut"></span></form>'
        '<form method="post" action="/api/portfolio/import" enctype="multipart/form-data" style="margin-top:10px" onsubmit="return pfSubmit(event,this,\'pfImpMsg\')">'
        '<input type="file" name="file" accept=".csv" required><button class="btn gray">导入 CSV</button>'
        '<span id="pfImpMsg" class="mut"></span></form>'
        '<form method="post" action="/api/portfolio/clear" style="margin-top:10px" onsubmit="return pfSubmit(event,this,\'pfClearMsg\')">'
        '<button class="btn red">清空持仓</button><span id="pfClearMsg" class="mut"></span></form></div>'
        '<div class="card"><h3>📝 今日操作记录(复盘合规校验用)</h3>'
        '<form method="post" action="/api/operations/add" style="display:flex;gap:6px;flex-wrap:wrap" onsubmit="return pfSubmit(event,this,\'opMsg\')">'
        '<input name="code" placeholder="代码 600519" required style="width:110px">'
        '<input name="qty" type="number" placeholder="数量" required style="width:80px">'
        '<input name="price" type="number" step="0.001" placeholder="成交价" required style="width:90px">'
        '<select name="action"><option value="buy">买入</option><option value="sell">卖出</option></select>'
        '<input name="reason" placeholder="备注(可选)" style="width:160px">'
        '<button class="btn">记录</button><span id="opMsg" class="mut"></span></form>' + _ops_table() + '</div></div>',
        '<script>function pfSubmit(ev,form,id){ev.preventDefault();var s=document.getElementById(id);s.innerText="⏳ 提交中…";'
        'var url=form.getAttribute("action")||form.action;'
        'fetch(url,{method:"POST",body:new FormData(form)}).then(function(r){return r.json()}).then(function(j){'
        'if(j.ok){s.innerText="✅ 成功";setTimeout(function(){location.reload()},600)}'
        'else{s.innerText="❌ "+(j.error||"失败")}}).catch(function(e){s.innerText="❌ "+e});return false}</script>',
        cards,
        '<div class="footer">持仓诊断基于历史量价 + 模型预测 + 板块归属,仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>',
    ]
    return _shell("portfolio", "持仓诊断", "\n".join(content))


@app.route("/api/portfolio")
def api_portfolio():
    c = _get_portfolio(refresh=request.args.get("refresh") == "1")
    if c["err"]:
        return jsonify({"error": c["err"]}), 500
    return jsonify(c["data"])


@app.route("/api/portfolio/add", methods=["POST"])
def api_portfolio_add():
    from app.support.risk import load_portfolio, save_portfolio
    try:
        code = str(request.form.get("code") or "").strip().zfill(6)
        qty = float(request.form.get("qty") or 0)
        cost = float(request.form.get("cost") or 0)
        cat = (request.form.get("category") or "观察").strip() or "观察"
        if not code.isdigit() or qty <= 0:
            return jsonify({"error": "代码/数量无效"}), 400
        pos = load_portfolio()
        for p in pos:
            if p["code"] == code:
                return jsonify({"error": f"{code} 已在持仓中,可先清空再导入"}), 400
        pos.append({"code": code, "qty": qty, "cost": cost, "category": cat})
        save_portfolio(pos)
        _PF_CACHE["data"] = None
        return jsonify({"ok": True, "code": code})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio/import", methods=["POST"])
def api_portfolio_import():
    from app.support.risk import load_portfolio, save_portfolio
    import csv as _csv
    import io as _io
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "缺少文件"}), 400
    try:
        text = f.read().decode("utf-8-sig")
        rows = list(_csv.DictReader(_io.StringIO(text)))
        pos = []
        for r in rows:
            code = str(r.get("code") or r.get("股票代码") or "").strip().zfill(6)
            if not code.isdigit():
                continue
            pos.append({"code": code,
                        "qty": float(r.get("qty") or r.get("持仓数量") or 0),
                        "cost": float(r.get("cost") or r.get("成本价") or 0),
                        "category": (r.get("category") or r.get("持仓分类") or "观察").strip() or "观察"})
        if not pos:
            return jsonify({"error": "CSV 无有效行(需含 code,qty,cost)"}), 400
        save_portfolio(pos)
        _PF_CACHE["data"] = None
        return jsonify({"ok": True, "count": len(pos)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"导入失败: {e}"}), 500


@app.route("/api/portfolio/clear", methods=["POST"])
def api_portfolio_clear():
    from app.support.risk import save_portfolio
    save_portfolio([])
    _PF_CACHE["data"] = None
    return jsonify({"ok": True})


def _ops_table() -> str:
    """今日操作记录表格(复盘合规校验的数据源)。"""
    import datetime as _dt
    from app.review.operations import load_operations
    ops = load_operations(_dt.date.today())
    if not ops:
        return '<div class="mut" style="margin-top:8px">今日暂无操作记录(合规校验按持仓状态审计)。</div>'
    rows = "".join(
        f"<tr><td>{_h(o.get('time') or o.get('date'))}</td>"
        f"<td>{_h(str(o.get('code', '')).zfill(6))}</td>"
        f"<td>{'买入' if o.get('action') == 'buy' else '卖出'}</td>"
        f"<td>{o.get('qty')}</td><td>{o.get('price')}</td>"
        f"<td class='mut'>{_h(o.get('reason') or '-')}</td></tr>" for o in ops)
    return f"<div class='tbl' style='margin-top:8px'><table><tr><th>时间</th><th>代码</th><th>方向</th><th>数量</th><th>价格</th><th>备注</th></tr>{rows}</table></div>"


@app.route("/api/operations", methods=["GET"])
def api_operations():
    import datetime as _dt
    from app.review.operations import load_operations
    date = request.args.get("date") or str(_dt.date.today())
    return jsonify({"date": date, "ops": load_operations(date)})


@app.route("/api/operations/add", methods=["POST"])
def api_operations_add():
    import datetime as _dt
    from app.review.operations import add_operation, apply_op_to_portfolio
    try:
        code = str(request.form.get("code") or "").strip().zfill(6)
        qty = float(request.form.get("qty") or 0)
        price = float(request.form.get("price") or 0)
        action = (request.form.get("action") or "buy").strip().lower()
        reason = (request.form.get("reason") or "").strip()
        if not code.isdigit() or qty <= 0 or price <= 0 or action not in ("buy", "sell"):
            return jsonify({"error": "参数无效"}), 400
        now = _dt.datetime.now()
        op = add_operation(now.strftime("%Y-%m-%d"), code, qty, price, action, reason)
        # 操作写回持仓(买加权加仓/新开,卖减仓记已实现盈亏)
        op["realized_pnl"] = apply_op_to_portfolio(op)
        op["time"] = now.strftime("%H:%M")
        from app.review.operations import load_operations, save_operations
        save_operations(load_operations(now.strftime("%Y-%m-%d")))
        _PF_CACHE["data"] = None
        return jsonify({"ok": True, "code": code, "action": action,
                        "realized_pnl": op.get("realized_pnl")})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/operations/clear", methods=["POST"])
def api_operations_clear():
    import datetime as _dt
    from app.review.operations import clear_operations
    date = request.form.get("date") or str(_dt.date.today())
    clear_operations(date)
    return jsonify({"ok": True})


# ============================================================ 模块3 复盘报告(内存展示 + 历史检索)
_REPORT_HTML = {"date": None, "markdown": None, "html": "", "summary": ""}
_REPORT_STAGE = {"state": "idle", "stage": "", "err": None}


def _set_report(res: dict) -> None:
    _REPORT_HTML["date"] = res["date"]
    _REPORT_HTML["markdown"] = res["markdown"]
    _REPORT_HTML["html"] = _md_to_html(res["markdown"])
    _REPORT_HTML["summary"] = res.get("summary", "")


def _report_history() -> list:
    """历史复盘日期列表(落盘 reports/*.md)。"""
    import os as _os
    d = _os.path.join(config.DATA_DIR, "reports")
    if not _os.path.isdir(d):
        return []
    files = sorted(f for f in _os.listdir(d) if f.startswith("review_"))
    return [f[7:15] for f in files]


def _load_report_file(date_str: str) -> str:
    """读取某日复盘 markdown 文件(date_str=YYYYMMDD)。"""
    import os as _os
    path = _os.path.join(config.DATA_DIR, "reports", f"review_{date_str}.md")
    if not _os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return ""


def _start_auto_report(interval_sec: int = 60) -> None:
    """唯一调度入口:交易日到点(auto_report_time,默认 16:00)自动生成复盘(内存展示,统一入口)。
    落盘由 settings 控制(need_save_report)。"""
    import datetime as _dt
    import threading as _th
    from app.support import settings as _st
    from app.support import daily_report as _rep

    def _run():
        while True:
            try:
                now = _dt.datetime.now()
                target = _st.load().get("auto_report_time", "16:00")
                try:
                    hh, mm = map(int, target.split(":"))
                except ValueError:
                    hh, mm = 16, 0
                today = now.strftime("%Y-%m-%d")
                if (now.weekday() < 5 and now.hour == hh and now.minute == mm
                        and _REPORT_HTML["date"] != today):
                    cfg = _st.load()
                    _set_report(_rep.generate(use_cache=True,
                                              save=bool(cfg.get("need_save_report", False))))
                    print(f"[report] 已生成复盘 {today} (页面内展示)")
            except Exception as e:  # noqa: BLE001
                print(f"[report] 调度失败: {e}")
            _time.sleep(interval_sec)

    _th.Thread(target=_run, daemon=True, name="daily-report").start()


def _report_page_content(err: str = None) -> str:
    """组装报告页内容(含历史检索下拉 / 复制摘要 / 刷新进度 JS)。"""
    if err:
        date = _REPORT_HTML["date"] or "暂无"
        return (f'<header><h1>📄 每日深度复盘报告</h1>'
                f'<span class="mut">AI 深度复盘文案 · 生成时间 {_h(date)}</span></header>'
                f'<div class="card"><h3 style="color:#e74c3c">❌ 生成失败</h3>'
                f'<div class="line">{_h(err)}</div></div>'
                '<div class="footer">报告内容仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>')
    html = _REPORT_HTML["html"]
    date = _REPORT_HTML["date"] or "暂无"
    err_card = ""
    if err and not html:
        err_card = (f'<div class="card"><h3 style="color:#e74c3c">❌ 自动生成失败</h3>'
                    f'<div class="line">{_h(err)}</div></div>')
    hist_ops = "".join(f'<option value="{h}">{h[:4]}-{h[4:6]}-{h[6:]}</option>'
                       for h in _report_history())
    toolbar = (
        f'<select id="rh" style="padding:8px 12px;border-radius:8px;border:1px solid #2a3350;'
        f'background:#0b1018;color:var(--txt);font-size:14px" onchange="rhGo()">'
        f'<option value="">历史复盘(按日期查看)</option>{hist_ops}</select>'
        f'<button class="btn" onclick="genReport()">🔄 生成/刷新</button>'
        f'<button class="btn gray" onclick="copySummary()">📋 复制摘要</button>'
        f'<span id="rst" class="mut"></span>')
    placeholder = ('<div class="card"><h3>尚未生成报告</h3><div class="line">点击「生成/刷新」'
                   '将把当日盘面核心数据交给大模型 API(未启用则展示结构化规则复盘),'
                   '生成深度复盘文案并直接显示在本页。</div></div>')
    script = (
        '<script>'
        'function rhGo(){var d=document.getElementById("rh").value;if(d)'
        'fetch("/api/report?date="+d).then(r=>r.json()).then(j=>{'
        'if(j.error){alert(j.error);return;}'
        'document.getElementById("rbody").innerHTML=j.html;'
        'document.getElementById("rdt").innerText="历史复盘 · "+j.date;})}'
        'function genReport(){var s=document.getElementById("rst");s.innerText="⏳ 开始生成…";'
        'fetch("/api/report/generate",{method:"POST"}).then(r=>r.json());'
        'var t=setInterval(function(){fetch("/api/report/status").then(r=>r.json()).then(j=>{'
        'if(j.state==="running"){s.innerText="⏳ "+j.stage+" …";}else{clearInterval(t);'
        's.innerText=j.err?("❌ "+j.err):"✅ 完成";if(!j.err)location.reload();}})},1200)}'
        'function copySummary(){fetch("/api/report/summary").then(r=>r.json()).then(j=>{'
        'if(j.summary){navigator.clipboard.writeText(j.summary).then(()=>alert("摘要已复制"));'
        'else alert("尚无摘要");}})}'
        '</script>')
    return (
        f'<header><h1>📄 每日深度复盘报告</h1>'
        f'<span class="mut" id="rdt">AI 深度复盘文案 · 生成时间 {_h(date)}</span>{toolbar}</header>'
        + err_card
        + f'<div id="rbody">{html or placeholder}</div>'
        + script
        + '<div class="footer">报告内容仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>')


@app.route("/report")
def page_report():
    err = request.args.get("err")
    # 首访:优先渲染当日已落盘的 md 文件;不存在则生成并落盘(save 按 need_save_report)后渲染。
    if not _REPORT_HTML["html"] and not err:
        try:
            from app.review.data import review_date
            from app.support import settings as _st
            rdate = review_date()
            fname = rdate.strftime("%Y%m%d") if hasattr(rdate, "strftime") \
                else str(rdate).replace("-", "")
            md_text = _load_report_file(fname)
            if md_text:
                _set_report({"date": str(rdate), "markdown": md_text, "summary": ""})
            else:
                from app.support import daily_report as _rep
                _set_report(_rep.generate(use_cache=True,
                                          save=bool(_st.load().get("need_save_report", False))))
        except Exception as e:  # noqa: BLE001
            err = str(e)
    return _shell("report", "复盘报告", _report_page_content(err=err))


@app.route("/api/report/generate", methods=["POST"])
def api_report_generate():
    """异步生成复盘:后台线程执行,页面轮询 /api/report/status 获取进度。落盘按 need_save_report。"""
    import threading as _th
    from app.support import daily_report as rep
    from app.support import settings as _st
    if _REPORT_STAGE["state"] == "running":
        return jsonify({"state": "running"})

    def _stage(msg):
        _REPORT_STAGE["stage"] = msg

    def _run():
        _REPORT_STAGE["state"] = "running"
        _REPORT_STAGE["err"] = None
        try:
            _set_report(rep.generate(use_cache=True,
                                     save=bool(_st.load().get("need_save_report", False)),
                                     on_stage=_stage))
        except Exception as e:  # noqa: BLE001
            _REPORT_STAGE["err"] = str(e)
        finally:
            _REPORT_STAGE["state"] = "done"

    _th.Thread(target=_run, daemon=True).start()
    return jsonify({"state": "running"})


@app.route("/api/report/status")
def api_report_status():
    return jsonify({"state": _REPORT_STAGE["state"], "stage": _REPORT_STAGE["stage"],
                    "err": _REPORT_STAGE["err"]})


@app.route("/api/report")
def api_report():
    dstr = request.args.get("date")
    if dstr:
        md = _load_report_file(dstr.replace("-", ""))
        if not md:
            return jsonify({"error": f"{dstr} 历史复盘不存在"}), 404
        return jsonify({"date": dstr, "markdown": md, "html": _md_to_html(md)})
    if not _REPORT_HTML["markdown"]:
        return jsonify({"error": "尚未生成报告"}), 404
    return jsonify({"date": _REPORT_HTML["date"], "markdown": _REPORT_HTML["markdown"]})


@app.route("/api/report/history")
def api_report_history():
    return jsonify({"dates": _report_history()})


@app.route("/api/report/summary")
def api_report_summary():
    if not _REPORT_HTML["summary"]:
        try:
            from app.support import daily_report as _rep
            from app.support import settings as _st
            _set_report(_rep.generate(use_cache=True,
                                      save=bool(_st.load().get("need_save_report", False))))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 500
    return jsonify({"date": _REPORT_HTML["date"], "summary": _REPORT_HTML["summary"]})


# ============================================================ 模块4 盘中预警
@app.route("/alerts")
def page_alerts():
    from app.support import monitor as mon
    alerts = mon.load_alerts()
    st = mon.status()
    badges = {"price": "价位", "sector": "板块", "mood": "情绪", "signal": "信号", "volume": "量能"}
    rows = []
    for a in alerts:
        lv = {"warning": "flat", "info": "mut"}.get(a.get("level"), "mut")
        rows.append(f"<tr><td>{_h(a.get('time'))}</td><td><span class='tag'>{_h(badges.get(a.get('rule'), a.get('rule')))}</span></td>"
                    f"<td class='{lv}'>{_h(a.get('msg'))}</td></tr>")
    table = ("<div class='tbl'><table><tr><th>时间</th><th>规则</th><th>内容</th></tr>" + "\n".join(rows) +
             "</table></div>") if rows else '<div class="mut">暂无预警记录。启动监控后按配置规则自动检查。</div>'
    state = "🟢 运行中" if st["running"] else "⚪ 已停止"
    content = [
        f"<header><h1>🚨 盘中监控与预警</h1>"
        f'<span class="mut">{state} · 上次检查 {_h(st.get("last_check") or "-")} · 最近一轮 {st.get("last_count", 0)} 条</span>'
        f'<a class="btn" href="/alerts?refresh=1">🔄 刷新</a></header>',
        '<div class="grid"><div class="card"><h3>监控控制</h3>'
        '<form method="post" action="/api/monitor/start" style="display:inline"><button class="btn">▶ 启动监控</button></form> '
        '<form method="post" action="/api/monitor/stop" style="display:inline"><button class="btn red">⏹ 停止</button></form> '
        '<div class="mut" style="margin-top:8px">监控规则:价位/板块异动/恐贪极值/模型信号/量能异常,刷新频率与阈值在「系统设置」中调整。</div></div>'
        '<div class="card"><h3>今日预警</h3><form method="post" action="/api/alerts/clear" style="display:inline"><button class="btn gray">🗑 清空</button></form>'
        '<div style="margin-top:8px">' + table + "</div></div></div>",
        '<div class="footer">预警仅为程序化提示,不构成投资建议。股市有风险,入市需谨慎。</div>',
    ]
    return _shell("alerts", "盘中预警", "\n".join(content))


@app.route("/api/alerts")
def api_alerts():
    from app.support import monitor as mon
    return jsonify({"date": dt_today(), "alerts": mon.load_alerts(), "status": mon.status()})


@app.route("/api/alerts/clear", methods=["POST"])
def api_alerts_clear():
    from app.support import monitor as mon
    mon.clear_alerts()
    return jsonify({"ok": True})


@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    from app.support import monitor as mon
    mon.start()
    return jsonify({"ok": True, "status": mon.status()})


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    from app.support import monitor as mon
    mon.stop()
    return jsonify({"ok": True, "status": mon.status()})


@app.route("/api/monitor/status")
def api_monitor_status():
    from app.support import monitor as mon
    return jsonify(mon.status())


def dt_today():
    import datetime
    return datetime.date.today().strftime("%Y-%m-%d")


# ============================================================ 模块5 风控(含系统设置)
_SETTING_FIELDS = [
    ("打分权重", [
        ("score_weights.capital", "资金强度权重", "number", 0, 100, "主线打分:资金净流入占比"),
        ("score_weights.trend", "趋势强度权重", "number", 0, 100, "板块涨幅+涨停家数占比"),
        ("score_weights.sentiment", "情绪共振权重", "number", 0, 100, "恐贪指数档位占比"),
        ("score_weights.news", "消息催化权重", "number", 0, 100, "财联社电报命中占比"),
    ]),
    ("主线识别", [
        ("mainline_top_n", "核心主线数量", "number", 1, 10, "评分前 N 名为核心主线"),
        ("mainline_branch_top_n", "补涨支线数量", "number", 3, 20, "评分前 M 名(含核心)标注支线"),
        ("mainline_dynamic_weight", "资金面动态权重", "checkbox", None, None, "开启:A级 5日20%+单日80%, C/D级 5日70%+单日30%;关闭固定 5日40%+单日60%"),
        ("leader_min_market_cap", "龙头最小流通市值(亿)", "number", 0, 1000, "低于该值剔除"),
        ("etf_min_amount", "ETF 最低成交额(万)", "number", 0, 100000, "低于该值不推荐"),
    ]),
    ("持仓诊断", [
        ("band_diff_pct", "做差价区间幅度", "number", 0.01, 0.2, "深套高抛/回补区间"),
        ("take_profit_floor", "止盈触发浮盈", "number", 0.05, 0.5, "盈利超此值启用止盈"),
        ("move_stop_trail", "移动止损回撤", "number", 0.02, 0.3, "现价回撤比例"),
    ]),
    ("风控仓位", [
        ("risk.single_pct", "单只仓位上限", "number", 0.01, 0.5, "占总资产比例"),
        ("risk.sector_pct", "板块仓位上限", "number", 0.1, 0.8, "单一板块占总资产比例"),
        ("risk.loss_reduce_threshold", "浮亏加仓限制阈值", "number", 0.05, 0.5, "浮亏超过此值禁止摊薄"),
        ("risk.add_increase_cap", "单次加仓上限比例", "number", 0.1, 2, "不超过现有仓位的比例"),
        ("risk.max_total_pct", "总仓位上限", "number", 0.1, 1, "极端行情的兜底上限"),
    ]),
    ("决策输入", [
        ("decision.total_asset", "总资金(元)", "number", 1000, 1000000000, "今日决策执行计划的仓位/股数计算基准"),
        ("decision.taste", "风险偏好", "text", None, None, "conservative / balanced / aggressive"),
    ]),
    ("监控预警", [
        ("monitor.enable", "启用监控总开关", "checkbox", None, None, "关闭后不会自动轮询"),
        ("monitor.refresh_sec", "轮询间隔(秒)", "number", 30, 3600, "监控检查频率"),
        ("monitor.sector_net_yi", "板块净流入阈值(亿)", "number", 0, 50, "超过则预警"),
        ("monitor.sector_pct", "板块涨幅阈值(%)", "number", 0, 10, "超过则预警"),
        ("monitor.volume_yesterday_ratio", "放量倍数(对昨日)", "number", 0.5, 10, "量能异常预警"),
        ("monitor.rules.price", "规则:价位预警", "checkbox", None, None, ""),
        ("monitor.rules.sector", "规则:板块异动", "checkbox", None, None, ""),
        ("monitor.rules.mood", "规则:情绪极值", "checkbox", None, None, ""),
        ("monitor.rules.signal", "规则:模型信号", "checkbox", None, None, ""),
        ("monitor.rules.volume", "规则:量能异常", "checkbox", None, None, ""),
    ]),
    ("自动报告", [
        ("auto_report_time", "自动生成时间", "text", None, None, "交易日 HH:MM,到点自动生成复盘报告"),
        ("portfolio_path", "持仓 CSV 路径", "text", None, None, "持仓文件保存位置"),
    ]),
    ("大模型文案", [
        ("llm.enable", "启用大模型文案", "checkbox", None, None, "关闭时报告 AI 部分用规则话术兜底"),
        ("llm.base_url", "接口地址 Base URL", "text", None, None, "OpenAI 兼容,如 https://api.openai.com/v1"),
        ("llm.api_key", "API 密钥", "text", None, None, "仅保存在本地 settings.json"),
        ("llm.model", "模型名", "text", None, None, "如 gpt-4o-mini / deepseek-chat"),
        ("llm.timeout", "请求超时(秒)", "number", 10, 300, "接口响应超时上限"),
        ("llm.max_tokens", "生成长度上限", "number", 200, 4000, "单次生成最大 token"),
    ]),
    ("前端体验", [
        ("web_ui.conclusion_bar", "极简结论卡", "checkbox", None, None, "顶部一行浓缩结论:市场评级/仓位/首选方向/首选标的/建议/风险"),
        ("web_ui.yesterday_review", "昨日信号复盘", "checkbox", None, None, "底部展示昨日推荐标的今日表现(胜率/涨跌幅统计)"),
        ("web_ui.rejected_collapse", "淘汰板块折叠", "checkbox", None, None, "仅显示已淘汰数量,点击展开详情"),
        ("web_ui.target_tabs", "标的匹配Tab切换", "checkbox", None, None, "点击板块才显示其标的列表,避免信息过载"),
        ("web_ui.mood_risk_tag", "情绪龙头风险标签", "checkbox", None, None, "情绪龙头小票附加「高波动·纯情绪博弈·建议极轻仓」"),
        ("web_ui.delta_arrows", "数值环比箭头", "checkbox", None, None, "关键数值对比前一交易日,标注 ↑↓ 与变化值"),
        ("web_ui.sector_detail", "板块详情弹窗", "checkbox", None, None, "点击板块行下钻:打分明细/K线/成分TOP/新闻摘要"),
        ("web_ui.candidate_list", "异动候选列表", "checkbox", None, None, "稳定器候选(candidate)展示于决策页"),
        ("web_ui.raw_debug", "原始信号调试", "checkbox", None, None, "原始未防抖(raw)主线信号调试展示(默认折叠)"),
    ]),
    ("防抖稳定器", [
        ("decision.mainline.enable_stabilizer", "启用防抖稳定器", "checkbox", None, None, "关闭时直接透传原始流水线结果,兼容历史回测"),
        ("decision.mainline.intraday_smooth_window", "单日资金平滑窗口(分钟)", "number", 0, 240, "0=关闭平滑;仅稳定器内生效,5日资金表不参与"),
        ("decision.mainline.rank_delta_thresh", "排名阻尼阈值", "number", 0, 0.01, "相邻板块净流入率差<此值视为同档位,不做阶梯扣分"),
        ("decision.mainline.STABILIZE_CYCLE", "驻留确认周期N", "number", 1, 20, "连续N个快照周期驻留/冷却/替换确认"),
        ("decision.mainline.COOL_DOWN_MINUTE", "冷却分钟数", "number", 0, 240, "被移出正式池后的冷却时长,冷却中只能进入candidate"),
        ("decision.mainline.PASS_HYSTERESIS_UP", "进入正式池分数", "number", 0, 100, "新板块进入正式池(passed)的门槛分数"),
        ("decision.mainline.PASS_HYSTERESIS_DOWN", "移出正式池分数", "number", 0, 100, "已在池内板块分数低于此值才允许移出(滞回)"),
        ("decision.mainline.weaken_news_on_no_5d_money", "消息脉冲削弱", "checkbox", None, None, "无5日资金净流入时,新闻催化满分降为低档"),
        ("decision.mainline.poll_interval_sec", "后台轮询间隔(秒)", "number", 0, 3600, "稳定器按此间隔推进一个周期;0=关闭后台轮询(仅访问时推进)"),
        ("decision.mainline.poll_trading_hours_only", "仅交易时段轮询", "checkbox", None, None, "工作日 9:30-11:30 / 13:00-15:00 轮询,节省接口调用"),
    ]),
    ("扩展因子", [
        ("decision.mainline.enable_extend_factor", "启用扩展因子", "checkbox", None, None, "连板梯队 + 大小盘风格偏转;默认关闭(回测保持原逻辑)"),
        ("decision.mainline.extend_factor.ladder.enabled", "梯队因子启用", "checkbox", None, None, "trend 内部重组为 涨跌归一0.6 + 涨停0.2 + 梯队0.2,trend 总权重30不变"),
        ("decision.mainline.extend_factor.ladder.pct_w", "梯队:涨跌权重", "number", 0, 1, "当日涨跌幅归一在 trend 内权重"),
        ("decision.mainline.extend_factor.ladder.zt_w", "梯队:涨停权重", "number", 0, 1, "涨停家数在 trend 内权重"),
        ("decision.mainline.extend_factor.ladder.ladder_w", "梯队:梯队权重", "number", 0, 1, "ladder_score 在 trend 内权重(三者合计≈1)"),
        ("decision.mainline.extend_factor.ladder.zhongjun_float_yi", "中军流通市值(亿)", "number", 10, 2000, "板块内涨停股流通市值>=此值视为中军涨停背书"),
        ("decision.mainline.extend_factor.ladder.gap_from_board", "断层判定起板位", "number", 2, 6, "最高连板>=此值才检查梯队断层"),
        ("decision.mainline.extend_factor.ladder.drop_confirm", "梯队变差确认周期N", "number", 1, 20, "盘中炸板导致梯队变差需连续N个快照周期确认(稳定器内生效)"),
        ("decision.mainline.extend_factor.ladder.drop_delta", "梯队变差判定阈值", "number", 0, 1, "ladder_score 降幅>=此值判定为梯队变差"),
        ("decision.mainline.extend_factor.style.sort_bias_thresh", "偏转排序分差阈值", "number", 0, 20, "同池板块分数差<=此值(分)才启用风格偏转排序(风格判定逻辑已固化)"),
    ]),
    ("标的匹配优化(第六轮)", [
        ("target_match.enable_target_stabilizer", "标的驻留防抖", "checkbox", None, None, "P0.1 连续N周期保持前2才晋升正式推荐,避免盘中标的频繁切换"),
        ("target_match.enable_tradable_filter", "可交易性过滤", "checkbox", None, None, "P0.2 剔除一字板/停牌/次新/流动性不足/ETF高溢价标的"),
        ("target_match.enable_advanced_rank", "分档选股升级", "checkbox", None, None, "P1 情绪龙头用情绪综合得分,中军用中军属性综合得分(替代单一指标)"),
        ("target_match.enable_excess_return_adjust", "超额收益修正", "checkbox", None, None, "P2.1 持续跑赢/跑输板块时调整动作优先级(不改GBM概率)"),
        ("target_match.enable_sector_boost_stable", "板块溢价联动防抖", "checkbox", None, None, "P2.2 仅正式core/defensive给板块溢价上修,候选/观察不给"),
        ("target_match.enable_fallback_match", "匹配失败降级兜底", "checkbox", None, None, "P2.3 档位内补选->跨档位->关联板块->error,不阻塞页面"),
        ("target_match.stabilizer.TARGET_STABILIZE_CYCLE", "驻留确认周期N", "number", 1, 20, "连续N个快照周期保持前2才晋升正式推荐"),
        ("target_match.stabilizer.TARGET_COOLDOWN_MINUTE", "剔除后冷却(分钟)", "number", 0, 240, "被剔除正式推荐后的冷却时间,冷却中仅作候选"),
        ("target_match.stabilizer.TARGET_KEEP_RANK", "保级排名范围", "number", 2, 20, "正式标的跌出前2但仍在前N内暂不剔除"),
        ("target_match.tradable_filter.min_list_days", "次新上市天数下限", "number", 1, 250, "上市天数<此值视为次新股剔除"),
        ("target_match.tradable_filter.aggressive_min_avg_amount", "情绪龙头20日均额(元)", "number", 0, 1000000000, "低于此值剔除(流动性过滤)"),
        ("target_match.tradable_filter.steady_min_avg_amount", "中军20日均额(元)", "number", 0, 1000000000, "低于此值剔除(流动性过滤)"),
        ("target_match.tradable_filter.etf_min_avg_amount", "ETF 20日均额(元)", "number", 0, 1000000000, "低于此值剔除"),
        ("target_match.tradable_filter.etf_max_premium", "ETF 溢价上限(小数)", "number", 0, 0.1, "场内溢价率高于此值剔除(如 0.005=0.5%)"),
        ("target_match.advanced_rank.aggressive_weights.ladder", "情绪权重:连板", "number", 0, 1, "连板高度/封单强度维度权重"),
        ("target_match.advanced_rank.aggressive_weights.pct_chg", "情绪权重:涨幅", "number", 0, 1, "10/20cm归一化涨幅权重"),
        ("target_match.advanced_rank.aggressive_weights.correlation", "情绪权重:相关性", "number", 0, 1, "个股与板块指数相关性权重(排除庄股)"),
        ("target_match.advanced_rank.aggressive_weights.amount", "情绪权重:成交额", "number", 0, 1, "当日成交额权重(保证流动性)"),
        ("target_match.advanced_rank.steady_weights.market_cap", "中军权重:市值", "number", 0, 1, "流通/总市值权重(行业地位)"),
        ("target_match.advanced_rank.steady_weights.avg_amount", "中军权重:20日均额", "number", 0, 1, "长期流动性权重"),
        ("target_match.advanced_rank.steady_weights.trend", "中军权重:趋势", "number", 0, 1, "MA20斜率趋势强度权重"),
        ("target_match.advanced_rank.steady_weights.amount", "中军权重:当日成交", "number", 0, 1, "当日成交额权重"),
    ]),
]


def _get_nested(cfg, path):
    cur = cfg
    for k in path.split("."):
        cur = cur.get(k) if isinstance(cur, dict) else None
    return cur


def _render_settings_form(cfg) -> str:
    parts = []
    for group, fields in _SETTING_FIELDS:
        rows = []
        for path, label, typ, *_ in fields:
            val = _get_nested(cfg, path)
            if typ == "checkbox":
                checked = "checked" if val else ""
                ctrl = f'<label><input type="checkbox" name="{path}" value="1" {checked}> 开启</label>'
            elif typ == "text":
                ctrl = f'<input type="text" name="{path}" value="{_h(val or "")}" style="width:260px">'
            else:
                ctrl = (f'<input type="number" name="{path}" value="{_h(val)}" '
                        f'min="{_h(fields[3]) if len(fields) > 3 else ""}" max="{_h(fields[4]) if len(fields) > 4 else ""}" '
                        f'step="any" style="width:110px">')
            rows.append(f"<tr><th>{_h(label)}</th><td>{ctrl}</td><td class='mut'>{_h(fields[-1] if len(fields) > 5 else '')}</td></tr>")
        parts.append(f"<div class='card'><h3>{_h(group)}</h3><div class='tbl'><table>{''.join(rows)}</table></div></div>")
    return "\n".join(parts)


@app.route("/settings")
def page_settings():
    from app.support import settings as st
    cfg = st.load()
    content = [
        "<header><h1>⚙️ 系统设置</h1>"
        '<span class="mut">阈值与规则实时生效(需保存),持久化至 data_cache/settings.json</span>'
        '<form id="settings-form" style="display:inline">'
        f'{_render_settings_form(cfg)}'
        '<div style="margin:8px 0"><button class="btn" type="submit">💾 保存全部设置</button></div></form>'
        '<button class="btn red" onclick="doReset()">恢复默认</button></header>'
        '<div id="toast"></div>'
        '<script>'
        'function showToast(msg, ok){var t=document.getElementById("toast");'
        't.textContent=msg;t.className="toast "+(ok?"ok":"err");t.style.display="block";'
        'clearTimeout(t._h);t._h=setTimeout(function(){t.style.display="none";},3200);}'
        'document.getElementById("settings-form").addEventListener("submit",function(e){'
        'e.preventDefault();var f=this,b=document.createElement("button");b.type="submit";'
        'b.style.display="none";f.appendChild(b);'
        'fetch("/api/settings/save",{method:"POST",body:new FormData(f)})'
        '.then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})'
        '.then(function(o){if(o.ok&&!o.j.error){showToast("✅ 设置已保存并生效",true);}'
        'else{showToast("❌ 保存失败: "+(o.j&&o.j.error||o.j&&o.j.message||"未知错误"),false);}}'
        ').catch(function(e){showToast("❌ 保存失败: "+e,false);});b.remove();});'
        'function doReset(){if(!confirm("确定恢复默认设置?")){return;}'
        'fetch("/api/settings/reset",{method:"POST"})'
        '.then(function(r){return r.json();})'
        '.then(function(j){if(!j.error){showToast("✅ 已恢复默认设置",true);setTimeout(function(){location.reload();},800);}'
        'else{showToast("❌ 恢复失败: "+j.error,false);}})'
        '.catch(function(e){showToast("❌ 恢复失败: "+e,false);});}'
        '</script>',
        '<div class="footer">⚠ 风控与监控阈值建议谨慎修改。免责声明:本系统全部内容仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>',
    ]
    return _shell("settings", "系统设置", "\n".join(content))


def _flatten_form(form) -> dict:
    out = {}
    for key in form:
        val = form.get(key)
        if key.startswith("monitor.rules.") or key == "llm.enable" or key.startswith("web_ui.") \
                or key in ("decision.mainline.enable_stabilizer",
                           "decision.mainline.weaken_news_on_no_5d_money",
                           "decision.mainline.poll_trading_hours_only",
                           "decision.mainline.enable_extend_factor",
                           "decision.mainline.extend_factor.ladder.enabled",
                           "decision.mainline.extend_factor.style.enabled",
                           "target_match.enable_target_stabilizer",
                           "target_match.enable_tradable_filter",
                           "target_match.enable_advanced_rank",
                           "target_match.enable_excess_return_adjust",
                           "target_match.enable_sector_boost_stable",
                           "target_match.enable_fallback_match"):
            val = True
        else:
            try:
                val = float(val)
                if val == int(val):
                    val = int(val)
            except (TypeError, ValueError):
                pass
        node = out
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val
    out.setdefault("llm", {}).setdefault("enable", False)
    out.setdefault("mainline_dynamic_weight", False)
    # web_ui 未勾选的开关回填 False(深合并会覆盖默认 True)
    for wk in ("conclusion_bar", "yesterday_review", "rejected_collapse", "target_tabs",
               "mood_risk_tag", "delta_arrows", "sector_detail",
               "candidate_list", "raw_debug"):
        out.setdefault("web_ui", {}).setdefault(wk, False)
    # 防抖稳定器/扩展因子未勾选的布尔开关回填 False
    for mk in ("enable_stabilizer", "weaken_news_on_no_5d_money",
               "poll_trading_hours_only", "enable_extend_factor"):
        out.setdefault("decision", {}).setdefault("mainline", {}).setdefault(mk, False)
    for ek in ("enabled",):
        out.setdefault("decision", {}).setdefault("mainline", {}) \
           .setdefault("extend_factor", {}).setdefault("ladder", {}).setdefault(ek, False)
        out.setdefault("decision", {}).setdefault("mainline", {}) \
           .setdefault("extend_factor", {}).setdefault("style", {}).setdefault(ek, False)
    # 标的匹配优化(第六轮)未勾选的布尔开关回填 False
    for tmk in ("enable_target_stabilizer", "enable_tradable_filter",
                "enable_advanced_rank", "enable_excess_return_adjust",
                "enable_sector_boost_stable", "enable_fallback_match"):
        out.setdefault("target_match", {}).setdefault(tmk, False)
    return out


@app.route("/api/settings")
def api_settings():
    from app.support import settings as st
    return jsonify(st.load())


@app.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    from app.support import settings as st
    try:
        st.save(_flatten_form(request.form))
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/reset", methods=["POST"])
def api_settings_reset():
    from app.support import settings as st
    st.reset()
    return jsonify({"ok": True})


def _warm_startup_cache():
    """服务启动后后台预热今日决策,避免用户首次访问触发慢速冷计算。"""
    import threading as _th
    import time as _t
    _t.sleep(2)
    try:
        _get_decision(refresh=True)
        print("[预热] 今日决策缓存已就绪")
    except Exception as e:  # noqa: BLE001
        print(f"[预热] 今日决策预热失败: {e}")


def main():
    if getattr(config, "RETRAIN_WEB_AUTO", True):
        from app.scheduler import start_daemon
        start_daemon(verbose=True)
        print("  月度重训 daemon 已启动(按固定月度频率自动适配市场风格)\n")
    try:
        _start_auto_report()
        print("  每日复盘报告调度已启动(到点自动生成并直接展示于页面)\n")
    except Exception as e:  # noqa: BLE001
        print(f"  报告调度启动失败: {e}")
    if getattr(config, "CONCEPT_REFRESH_DAILY", True):
        try:
            from app.features.concept_features import start_concept_refresh
            start_concept_refresh()
            print("  概念成分按日重抓调度已启动(收盘后自动同步同花顺成分变动)\n")
        except Exception as e:  # noqa: BLE001
            print(f"  概念成分重抓调度启动失败: {e}")
    if getattr(config, "WARM_STARTUP", True):
        _th = __import__("threading")
        _t = _th.Thread(target=_warm_startup_cache, daemon=True)
        _t.start()
        print("  启动预热线程已启动(后台生成今日决策与主线缓存)\n")
    try:
        from app.support.mainline_stabilizer import start_polling
        if start_polling() is not None:
            print("  主线防抖稳定器每5分钟轮询已启动(平滑与N周期确认独立于网页访问)\n")
    except Exception as e:  # noqa: BLE001
        print(f"  主线稳定器轮询启动失败(不影响主流程): {e}\n")
    host = os.environ.get("GUGA_HOST", "127.0.0.1")
    port = int(os.environ.get("GUGA_PORT", "8000"))
    print(f"\n  量化决策仪表盘(今日决策为默认首页): http://{host}:{port}/decision\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
