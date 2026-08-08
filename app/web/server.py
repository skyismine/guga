"""Web 仪表盘:走势预测 + 实时操作建议 + 今日信号单。

启动: python run_web.py  (默认 http://127.0.0.1:8000)
"""
import io
import os
import re as _re
import sys
import time as _time

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
              ("/analyze", "home", "📈 走势预测"), ("/signals", "signals", "📋 今日信号单"),
              ("/review", "review", "📝 每日复盘"), ("/mainline", "mainline", "🔥 主线板块"),
              ("/portfolio", "portfolio", "💼 持仓诊断"), ("/report", "report", "📄 复盘报告"),
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
    return "\n".join(out)


def _bold(s):
    return _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _html.escape(s))


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
  @media(max-width:900px){ .grid,.grid3{grid-template-columns:1fr;} }
</style>
</head>
<body>""" + _SIDE(active) +
            '<div class="wrap">' + content + '</div></body></html>')


def _mainline_chart(items: list) -> str:
    import plotly.graph_objects as go
    import plotly.io as pio
    top = items[:12]
    fig = go.Figure(go.Bar(
        x=[it["score"] for it in top][::-1],
        y=[it["name"] for it in top][::-1], orientation="h",
        marker_color=["#ef5350" if it["level"] == "core" else "#ffca28" if it["level"] == "branch" else "#5c6b80"
                      for it in top][::-1]))
    fig.update_layout(title="主线板块综合评分(Top12)", template="plotly_dark",
                      height=520, margin=dict(l=10, r=10, t=40, b=10),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return pio.to_html(fig, full_html=False, include_plotlyjs="inline",
                       default_width="100%", default_height="100%")

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

PAGE_SIGNALS = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>今日信号单 - 量化预测分析</title>
<style>
  :root { --bg:#0f1420; --card:#1a2130; --line:#2a3350; --txt:#e6e9f0; --mut:#8a94a8; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Microsoft YaHei",system-ui,sans-serif; background:var(--bg); color:var(--txt); }
  .wrap { max-width:1180px; margin:0 auto; padding:18px; }
  header { display:flex; align-items:center; gap:12px; padding:14px 0; flex-wrap:wrap; }
  header h1 { font-size:20px; margin:0; }
  .mut { color:var(--mut); font-size:13px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:14px 0; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin:14px 0; }
  .card h3 { margin:0 0 10px; font-size:15px; }
  .up{color:#ef5350;} .down{color:#26a69a;} .flat{color:#ffca28;} .mut{color:var(--mut);}
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td,th { padding:6px 8px; border-bottom:1px solid var(--line); text-align:left; }
  th { color:var(--mut); font-weight:400; }
  .rank td:nth-child(1){color:var(--mut);} .rank tr:first-child td:nth-child(1){color:#ffca28;font-weight:600;}
  .badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:13px; font-weight:600; }
  .b-buy{background:#e5393522;color:#ef5350;border:1px solid #ef535055;}
  .b-sell{background:#00897b22;color:#26a69a;border:1px solid #26a69a55;}
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
    <h1>📋 今日信号单</h1>
    <span class="mut">股票池按预期收益排序 · Top-{{ top_n }} 买入候选 · 末位风险提示 · {{ date }}</span>
  </header>

  {% if error %}<div class="card err">{{ error }}</div>{% endif %}

  <div class="card">
    <h3>🎯 买入候选 Top-{{ top_n }}
      <span class="mut">(p_up ≥ {{ p_up_min }}%, 预期收益 ≥ 0)</span></h3>
    {{ top_html | safe }}
  </div>

  <div class="grid">
    <div class="card">
      <h3>⚠️ 风险提示(预期收益末位)</h3>
      {{ risk_html | safe }}
    </div>
    <div class="card">
      <h3>📊 全池排序</h3>
      {{ all_html | safe }}
    </div>
  </div>

  <div class="footer">
    信号仅基于量化模型排序,不构成投资建议。股市有风险,入市需谨慎。<br>
    排名依据:预期涨跌幅 = Σ P(类) × 该类平均未来收益;盈亏比 = 期望盈利/期望亏损。
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


# ---- 今日信号单 ----
_SIGNALS_CACHE = {"t": 0.0, "data": None, "err": None}
_SIGNALS_TTL = 300  # 秒,避免每次刷新都重算


def _signals_records(df) -> list:
    return df.where(pd.notna(df), None).to_dict(orient="records")


def _signals_table(records) -> str:
    if not records:
        return '<p class="mut">暂无数据</p>'
    rows = ["<table class='rank'><tr><th>#</th><th>代码</th><th>名称</th><th>现价</th>"
            "<th>上涨概率</th><th>下跌概率</th><th>方向</th><th>预期涨跌</th><th>盈亏比</th></tr>"]
    for i, r in enumerate(records, 1):
        rr = f"{r['reward_risk']:.2f}" if r.get("reward_risk") is not None else "-"
        d = r.get("direction")
        dir_cls = "up" if d == "上涨" else ("down" if d == "下跌" else "flat")
        exp = r.get("expected_return")
        exp_cls = "up" if (exp or 0) >= 0 else "down"
        rows.append(
            f"<tr><td>{i}</td><td>{r['code']}</td><td>{r['name']}</td><td>{r['close']}</td>"
            f"<td class='up'>{r['p_up'] * 100:.1f}%</td>"
            f"<td class='down'>{r['p_down'] * 100:.1f}%</td>"
            f"<td class='{dir_cls}'>{d}</td>"
            f"<td class='{exp_cls}'>{exp * 100:+.2f}%</td><td>{rr}</td></tr>")
    rows.append("</table>")
    return "\n".join(rows)


def _get_signals():
    if (_SIGNALS_CACHE["data"] is None
            or _SIGNALS_CACHE["err"] is not None
            or _time.time() - _SIGNALS_CACHE["t"] > _SIGNALS_TTL):
        from app.strategy.ranker import daily_signals
        try:
            _SIGNALS_CACHE["data"] = daily_signals()
            _SIGNALS_CACHE["err"] = None
        except Exception as e:  # noqa: BLE001
            _SIGNALS_CACHE["data"] = None
            _SIGNALS_CACHE["err"] = str(e)
        _SIGNALS_CACHE["t"] = _time.time()
    return _SIGNALS_CACHE


@app.route("/signals")
def signals():
    c = _get_signals()
    if c["err"]:
        return render_template_string(PAGE_SIGNALS, top_html="", risk_html="", all_html="",
                                      top_n=config.RANK_TOP_N, p_up_min=config.RANK_MIN_P_UP * 100,
                                      date="-", error=f"信号生成失败: {c['err']}", SIDE=_SIDE("signals"))
    res = c["data"]
    date = res["all"]["date"].iloc[0] if len(res["all"]) else "-"
    return render_template_string(PAGE_SIGNALS,
                                  top_html=_signals_table(_signals_records(res["top"])),
                                  risk_html=_signals_table(_signals_records(res["risk"])),
                                  all_html=_signals_table(_signals_records(res["all"])),
                                  top_n=config.RANK_TOP_N, p_up_min=config.RANK_MIN_P_UP * 100,
                                  date=date, error=None, SIDE=_SIDE("signals"))


@app.route("/api/signals")
def api_signals():
    c = _get_signals()
    if c["err"]:
        return jsonify({"error": c["err"]}), 500
    res = c["data"]
    return jsonify({
        "date": res["all"]["date"].iloc[0] if len(res["all"]) else None,
        "top_n": config.RANK_TOP_N,
        "buy": _signals_records(res["top"]),
        "risk": _signals_records(res["risk"]),
        "all": _signals_records(res["all"]),
    })


@app.route("/api/retrain", methods=["POST"])
def api_retrain():
    """手动触发月度重训(force=1 强制)。POST /api/retrain?force=1"""
    from app.scheduler import retrain_if_due
    try:
        res = retrain_if_due(force=request.args.get("force") == "1", verbose=False)
        return jsonify({"retrained": res["retrained"], "reason": res.get("reason"),
                        "summary": res.get("summary") or None})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


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


def _layer1_html(p1) -> str:
    rows = []
    for k, c in (p1.get("checks") or {}).items():
        state = "✅" if c.get("ok") else ("🟡" if c.get("ok_min") else "❌")
        rows.append(f"<tr><th>{_h(k)}</th><td>{_h(c.get('value'))}</td><td>{state} {_h(p1.get('grade_label'))}</td></tr>")
    tips = "".join(f'<div class="line">· {_h(r)}</div>' for r in p1.get("reasons", []))
    return (f"<div class='card'><h3>评级:{_h(p1.get('grade_label'))} · 总仓位上限 <b>{p1['cap']:.0%}</b></h3>"
            f"<div class='tbl'><table><tr><th>因子</th><th>现值</th><th>达标</th></tr>{''.join(rows)}</table></div>{tips}</div>")


def _sector_chip(it) -> str:
    lab = {"core": "核心主攻", "defensive": "防御备选", "watch": "观察", "rejected": "淘汰"}.get(it.get("level"), "")
    cls = {"core": "b-core", "defensive": "b-branch", "watch": "b-watch", "rejected": "b-wait"}.get(it.get("level"), "b-wait")
    reason = "".join(f'<div class="mut" style="font-size:12px">· {_h(r)}</div>' for r in it.get("reasons", [])[:4])
    extra = f" · 涨停 {it.get('zt_count')} 家" if it.get("zt_count") else ""
    return (f"<div class='line'>🔹 <b>{_h(it.get('name'))}</b> <span class='badge {cls}'>{lab}</span>"
            f" <span class='mut'>score {it.get('score')} · 涨 {it.get('pct_chg', 0):+.2f}% · 净流入 {it.get('net_yi', 0):+.1f} 亿{extra}</span>"
            f"<div style='margin-left:0'>{reason}</div></div>")


def _target_item_html(it, trigger_on=True) -> str:
    if it.get("error"):
        return f'<div class="line"><span class="mut">· {_h(it["error"])}</span></div>'
    lv = it.get("levels") or {}
    p_up = f"{it['p_up'] * 100:.0f}%" if it.get("p_up") is not None else "-"
    pos = f"现价 {it.get('price')}"
    if it.get("pct_chg") is not None:
        pos += f' <span class="{"up" if it["pct_chg"] >= 0 else "down"}">{it["pct_chg"]:+.2f}%</span>'
    if it.get("amount_yi"):
        pos += f' · 成交 {it["amount_yi"]} 亿'
    if it.get("amount_wan"):
        pos += f' · 成交 {it["amount_wan"]:,.0f} 万'
    lv_txt = (f'支撑 {_h(lv.get("support"))} / 压力 {_h(lv.get("resistance"))}'
              f' / 止损 {_h(lv.get("stop_loss"))}' if lv else "")
    trig = f'<div class="mut" style="font-size:12px">触发:{_h(it.get("trigger"))}</div>' if trigger_on and it.get("trigger") else ""
    return (f'<div class="line">★ {_h(it.get("role") or "")} 首选·{_h(it.get("name"))} ({_h(it.get("code"))}) · {pos}'
            f' <span class="mut">上涨概率 {p_up} · 建议:{_h(it.get("action"))}</span>'
            f'<div style="margin-left:0">{lv_txt}{trig}</div></div>')


def _layer3_html(targets: dict) -> str:
    parts = []
    for sector, t in (targets or {}).items():
        parts.append(f'<div class="card"><h3>🎯 {_h(sector)} · 标的匹配(三档)</h3>')
        for role, seg in (("aggressive", t.get("aggressive")), ("steady", t.get("steady")), ("etf", t.get("etf"))):
            if not seg:
                continue
            mood = "适用评级:" + "/".join(seg.get("mood", []))
            items = "".join(_target_item_html(it, trigger_on=(role != "etf")) for it in seg.get("items", []))
            parts.append(f'<div class="line"><b>{_h(seg.get("label"))}</b> <span class="tag">{mood}</span></div>{items}')
        parts.append("</div>")
    return "\n".join(parts) or '<div class="card"><h3>暂无达标主线</h3></div>'


def _plan_table_html(plans: dict, sector: str) -> str:
    seg = (plans or {}).get(sector, {})
    if not seg:
        return '<div class="mut">暂无执行参数</div>'
    rows = []
    for role, lab in (("steady", "稳健首选"), ("aggressive", "激进首选"), ("etf", "ETF")):
        p = seg.get(role) or {}
        if p.get("error"):
            rows.append(f"<tr><th>{lab}</th><td colspan='6' class='mut'>{_h(p.get('reason'))}</td></tr>")
            continue
        if not p.get("ok"):
            continue
        b = p.get("batch", {})
        rows.append(
            f"<tr><th>{lab}</th>"
            f"<td>{_h(p.get('price'))}</td>"
            f"<td>{_h(p.get('stop'))}</td>"
            f"<td>{_h(p.get('target1'))} / {_h(p.get('target2'))}</td>"
            f"<td>{p.get('position_pct', 0) * 100:.1f}%<br><span class='mut'>{p.get('shares')} 股 · {_fmt(p.get('position_value'))} 元</span></td>"
            f"<td>{b.get('first', {}).get('ratio', 0) * 100:.0f}% @ {_h(b.get('first', {}).get('price'))}<br>"
            f"{b.get('second', {}).get('ratio', 0) * 100:.0f}% @ {_h(b.get('second', {}).get('price'))}</td>"
            f"<td class='mut'>{_h(p.get('note'))}</td></tr>")
    return ("<div class='tbl'><table><tr><th>档位</th><th>现价</th><th>止损</th><th>目标1/2</th>"
            "<th>建议仓位</th><th>分批方案</th><th>说明</th></tr>" + "\n".join(rows) + "</table></div>")


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

    core = p2.get("core")
    defen = p2.get("defensive")
    core_t = (d.get("targets") or {}).get(core["name"]) if core else None
    def_t = (d.get("targets") or {}).get(defen["name"]) if defen else None
    plans = d.get("plans") or {}
    core_plan = _plan_table_html(plans, core["name"]) if core else '<div class="mut">无达标主线,暂无执行计划</div>'

    conclusion_html = (
        f"<div class='card' style='border-color:#2f6fed'><div style='display:flex;gap:18px;flex-wrap:wrap;align-items:center'>"
        f"<div><div class='badge {_grade_badge(con['grade'])}' style='font-size:20px'>{_h(con['grade_label'])}</div>"
        f"<div class='mut' style='margin-top:4px'>总仓位上限 <b style='font-size:20px'>{con['cap']:.0%}</b></div></div>"
        f"<div style='flex:1;min-width:280px'><div class='line' style='font-size:15px'>{_h(con['line'])}</div>"
        f"<div class='flat' style='margin-top:6px'>⚠️ 风险提示:{_h(con['risk_tip'])}</div></div>"
        f"</div></div>")

    layer1 = _layer1_html(p1)
    layer2_parts = []
    for it in ([core] if core else []) + ([defen] if defen else []) + (p2.get("watch") or []) + (p2.get("rejected") or []):
        layer2_parts.append(_sector_chip(it))
    layer2 = (f"<div class='card'><h3>准入线:{p2.get('pass_score')} 分 · 一票否决 + 分级</h3>"
              f"{''.join(layer2_parts) or '<div class=mut>暂无板块数据</div>'}</div>")
    layer3 = _layer3_html(d.get("targets"))

    def _layer4(sec_t, sec_name):
        return ("<div class='card'><h3>⚙️ 执行参数 · {}</h3>{}</div>"
                .format(_h(sec_name), _plan_table_html(d.get("plans") or {}, sec_name)))

    content = [
        '<header><h1>🎯 今日决策</h1>'
        f'<span class="mut">{_h(d.get("date"))} · 总资金 {_fmt(d.get("total_asset"))} 元 · 风险偏好「{_h(d.get("taste"))}」</span>'
        f'<a class="btn" href="/decision?refresh=1">🔄 刷新</a> '
        '<a class="btn gray" href="/settings">⚙️ 调整参数</a></header>',
        '<div class="card" style="border:1px solid #5b4231;background:#2a2017;color:#e0b27a">'
        '⚠️ 本站全部内容仅供研究参考,不构成投资建议;所有「关注/观察/建议配置」表述均为中性研究语义,不构成任何买卖指令。股市有风险,入市需谨慎。</div>',
        conclusion_html,
        '<div class="card"><h3>🧭 决策过程拆解(四层漏斗)</h3>'
        '<details open><summary><b>① 大盘开仓许可评级</b></summary>' + layer1 + '</details>'
        '<details open><summary><b>② 主线概念遴选</b></summary>' + layer2 + '</details>'
        '<details><summary><b>③ 标的精准匹配</b></summary>' + layer3 + '</details>'
        '<details open><summary><b>④ 执行参数</b></summary>' + core_plan + '</details></div>',
        '<div class="card"><h3>📋 执行计划表(首选标的)</h3>' + core_plan + "</div>",
        '<div class="card"><h3>🔗 全量数据入口</h3><div class="line">'
        '<a class="btn gray" href="/analyze">📈 走势预测</a> '
        '<a class="btn gray" href="/signals">📋 今日信号单</a> '
        '<a class="btn gray" href="/mainline">🔥 主线板块</a> '
        '<a class="btn gray" href="/portfolio">💼 持仓诊断</a> '
        '<a class="btn gray" href="/review">📝 每日复盘</a> '
        '<a class="btn gray" href="/alerts">🚨 盘中预警</a></div></div>',
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
    date = (request.args.get("date") or "").strip() or None
    c = _get_review(date)
    if c["err"]:
        return render_template_string(PAGE_REVIEW, date="-", sections=None,
                                      tables={}, error=f"复盘生成失败: {c['err']}", SIDE=_SIDE("review"))
    d = c["data"]
    return render_template_string(PAGE_REVIEW, date=d.get("date"),
                                  sections=d.get("sections"),
                                  tables=d.get("tables") or {}, error=None, SIDE=_SIDE("review"))


@app.route("/api/review")
def api_review():
    date = (request.args.get("date") or "").strip() or None
    c = _get_review(date)
    if c["err"]:
        return jsonify({"error": c["err"]}), 500
    return jsonify(_jsonable_review(c["data"]))


# ============================================================ 模块1 主线板块
_ML_CACHE = {"t": 0.0, "data": None, "err": None}
_ML_TTL = 600


def _get_mainline(refresh=False):
    if refresh or _ML_CACHE["data"] is None or _ML_CACHE["err"] or \
            _time.time() - _ML_CACHE["t"] > _ML_TTL:
        from app.support import mainline as ml
        try:
            _ML_CACHE["data"] = ml.mainline_summary()
            _ML_CACHE["err"] = None
        except Exception as e:  # noqa: BLE001
            _ML_CACHE["data"] = None
            _ML_CACHE["err"] = str(e)
        _ML_CACHE["t"] = _time.time()
    return _ML_CACHE


def _targets_html(t) -> str:
    if not t:
        return ""
    if t.get("error"):
        return f'<div class="mut">{_h(t["error"])}</div>'
    parts = []
    for s in t.get("stocks", []):
        role = _h(s.get("role", ""))
        name = _h(s.get("name", ""))
        code = _h(s.get("code", ""))
        extra = ""
        if s.get("pct_chg") is not None:
            extra += f' <span class="{"up" if s["pct_chg"] >= 0 else "down"}">{s["pct_chg"]:+.2f}%</span>'
        if s.get("amount_yi"):
            extra += f' <span class="mut">成交 {s["amount_yi"]} 亿</span>'
        if s.get("amount_wan"):
            extra += f' <span class="mut">成交 {s["amount_wan"]:.0f} 万</span>'
        sig = ""
        if s.get("p_up") is not None:
            cls = {"buy": "b-buy", "sell": "b-sell", "hold": "b-hold"}.get(s.get("action") == "" and "hold" or "buy")
            sig = (f' 上涨概率 {s["p_up"]:.0%} · 方向 {_h(s.get("direction", ""))} · '
                   f'建议 <b>{_h(s.get("action", ""))}</b>')
        lv = s.get("levels") or {}
        levels = f' 支撑 {_h(lv.get("support"))} / 压力 {_h(lv.get("resistance"))}' if lv else ""
        parts.append(f'<div class="line">• <span class="tag">{role}</span>{name} ({code}){extra}'
                     f'<div class="mut" style="margin-left:0">{sig}{levels}</div></div>')
    return "\n".join(parts)


def _oversold_html(pool) -> str:
    if not pool:
        return '<div class="mut">暂无符合条件的超跌反弹候选</div>'
    rows = []
    for p in pool:
        cls = {"buy": "b-buy", "sell": "b-sell", "hold": "b-hold"}.get(p.get("action") or "", "b-wait")
        rows.append(
            f"<tr><td>{_h(p.get('name'))}</td><td>{_h(p.get('code'))}</td>"
            f"<td class='down'>{_h(p.get('ret30')) and f'{p['ret30']*100:+.1f}%' or '-'}</td>"
            f"<td class='{'up' if (p.get('pct_chg') or 0) >= 0 else 'down'}'>{(p.get('pct_chg') or 0):+.2f}%</td>"
            f"<td>{_h(p.get('vol_ratio'))}</td><td>{_h(p.get('atr_pct')) and f'{p['atr_pct']*100:.1f}%' or '-'}</td>"
            f"<td>{(p.get('p_up') * 100 if p.get('p_up') is not None else None) and f'{p['p_up']*100:.0f}%' or '-'}</td>"
            f"<td><span class='badge {cls}'>{_h(p.get('action') or '')}</span></td></tr>")
    return ("<div class='tbl'><table><tr><th>名称</th><th>代码</th><th>30日跌幅</th><th>当日涨幅</th>"
            "<th>量比</th><th>ATR%</th><th>上涨概率</th><th>建议</th></tr>" + "\n".join(rows) + "</table></div>")


@app.route("/mainline")
def page_mainline():
    refresh = request.args.get("refresh") == "1"
    c = _get_mainline(refresh=refresh)
    if c["err"]:
        return _shell("mainline", "主线板块",
                      f'<header><h1>🔥 主线板块识别</h1><span class="mut">{c["err"]}</span></header>'
                      f'<div class="card err">主线分析失败: {_h(c["err"])}'
                      f' <a class="btn" href="/mainline?refresh=1">重试</a></div>')
    d = c["data"]
    fg = d.get("fear_greed")
    mood = (f'<div class="pill">恐贪指数<b>{fg}</b><br><span class="mut">{_h(d.get("fear_greed_label"))}</span></div>'
            if fg is not None else "")
    cards = []
    for it in d.get("items", []):
        lv = {"core": "b-core", "branch": "b-branch", "watch": "b-watch"}.get(it["level"], "b-watch")
        lab = {"core": "核心主线", "branch": "补涨支线", "watch": "观察"}.get(it["level"], "观察")
        zt = f"涨停 {it.get('zt_count', 0)} 家" if it.get("zt_count") else ""
        news = f'<span class="tag">📰 电报 {it["news_hits"]} 次</span>' if it.get("news_hits") else ""
        cards.append(
            f"<div class='card'><h3>#{it['rank']} {_h(it['name'])} "
            f"<span class='badge {lv}'>{lab}</span> <span class='mut'>score {it['score']}</span></h3>"
            f"<div class='line'>板块涨跌 <b class='{'up' if it['pct_chg'] >= 0 else 'down'}'>{it['pct_chg']:+.2f}%</b> · "
            f"主力净流入 {it['net_yi']:+.1f} 亿 · {zt}</div>"
            f"<div class='line'>领涨股:{_h(it.get('leader') or '-')} {news}</div>"
            f"{_targets_html(it.get('targets'))}</div>")
    content = [
        f"<header><h1>🔥 主线板块识别与龙头匹配</h1>"
        f'<span class="mut">{_h(d.get("date"))} · Top{d.get("top_n")} 为核心主线</span>'
        f'<a class="btn" href="/mainline?refresh=1">🔄 刷新</a></header>',
        f'<div class="grid">{mood}<div class="card"><h3>说明</h3><div class="line">打分 = 资金强度40 + 趋势30(涨幅+涨停家数) + 情绪共振20 + 消息催化10,权重可在系统设置调整。</div></div></div>',
        f'<div class="card">{_mainline_chart(d.get("items", []))}</div>',
        "\n".join(cards),
        '<div class="card"><h3>📉 超跌强承接池(近30日超跌 + 放量企稳)</h3>' + _oversold_html(d.get("oversold") or []) + "</div>",
        '<div class="footer">主线识别基于当日资金/涨停/情绪/新闻规则打分,仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>',
    ]
    return _shell("mainline", "主线板块", "\n".join(content))


@app.route("/api/mainline")
def api_mainline():
    c = _get_mainline(refresh=request.args.get("refresh") == "1")
    if c["err"]:
        return jsonify({"error": c["err"]}), 500
    return jsonify(c["data"])


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
        '<form method="post" action="/api/portfolio/add" style="display:flex;gap:6px;flex-wrap:wrap">'
        '<input name="code" placeholder="代码 600519" required style="width:110px">'
        '<input name="qty" type="number" placeholder="数量" required style="width:80px">'
        '<input name="cost" type="number" step="0.001" placeholder="成本" required style="width:90px">'
        '<select name="category"><option>核心</option><option>波段</option><option>观察</option></select>'
        '<button class="btn">添加</button></form>'
        '<form method="post" action="/api/portfolio/import" enctype="multipart/form-data" style="margin-top:10px">'
        '<input type="file" name="file" accept=".csv" required><button class="btn gray">导入 CSV</button></form>'
        '<form method="post" action="/api/portfolio/clear" style="margin-top:10px">'
        '<button class="btn red">清空持仓</button></form></div></div>',
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


# ============================================================ 模块3 复盘报告(内存展示,不落盘)
_REPORT_HTML = {"date": None, "markdown": None, "html": ""}


def _set_report(res: dict) -> None:
    _REPORT_HTML["date"] = res["date"]
    _REPORT_HTML["markdown"] = res["markdown"]
    _REPORT_HTML["html"] = _md_to_html(res["markdown"])


def _start_auto_report(interval_sec: int = 60) -> None:
    """后台线程:交易日到点自动生成复盘,直接写入内存供页面展示。"""
    import datetime as _dt
    import threading as _th
    from app.support import settings as _st
    from app.support import daily_report as _rep

    def _run():
        while True:
            try:
                now = _dt.datetime.now()
                target = _st.load().get("auto_report_time", "15:30")
                try:
                    hh, mm = map(int, target.split(":"))
                except ValueError:
                    hh, mm = 15, 30
                today = now.strftime("%Y-%m-%d")
                if (now.weekday() < 5 and now.hour == hh and now.minute == mm
                        and _REPORT_HTML["date"] != today):
                    _set_report(_rep.generate(use_cache=True, save=False))
                    print(f"[report] 已生成复盘 {today} (页面内展示)")
            except Exception as e:  # noqa: BLE001
                print(f"[report] 调度失败: {e}")
            _time.sleep(interval_sec)

    _th.Thread(target=_run, daemon=True, name="daily-report").start()


@app.route("/report")
def page_report():
    html = _REPORT_HTML["html"]
    date = _REPORT_HTML["date"] or "暂无"
    content = [
        f"<header><h1>📄 每日量化复盘报告</h1>"
        f'<span class="mut">五大模块自动聚合 · 生成时间 {_h(date)}</span>'
        f'<form method="post" action="/api/report/generate" style="display:inline"><button class="btn">🔄 生成/刷新</button></form></header>',
        html or '<div class="card"><h3>尚未生成报告</h3><div class="line">点击「生成/刷新」将聚合大盘复盘 / 主线板块 / 持仓诊断 / 风控校验,结果直接显示在本页(不保存文件)。</div></div>',
        '<div class="footer">报告内容仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>',
    ]
    return _shell("report", "复盘报告", "\n".join(content))


@app.route("/api/report/generate", methods=["POST"])
def api_report_generate():
    from app.support import daily_report as rep
    try:
        res = rep.generate(use_cache=True, save=False)
        _set_report(res)
        return jsonify({"ok": True, "date": res["date"]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/api/report")
def api_report():
    if not _REPORT_HTML["markdown"]:
        return jsonify({"error": "尚未生成报告"}), 404
    return jsonify({"date": _REPORT_HTML["date"], "markdown": _REPORT_HTML["markdown"]})


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
        ("leader_min_market_cap", "龙头最小流通市值(亿)", "number", 0, 1000, "低于该值剔除"),
        ("etf_min_amount", "ETF 最低成交额(万)", "number", 0, 100000, "低于该值不推荐"),
        ("oversold_pool_size", "超跌池数量", "number", 5, 50, "输出 Top N"),
    ]),
    ("超跌筛选", [
        ("oversold.drop_30d", "30日累计跌幅阈值", "number", 0, 1, "如 0.30 = 累计跌 30%"),
        ("oversold.vol_ratio", "放量倍数", "number", 0.5, 10, "当日量/5日均量"),
        ("oversold.max_atr_pct", "最大波动率 ATR%", "number", 0.01, 0.3, "过滤妖股"),
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
        '<form method="post" action="/api/settings/save" style="display:inline">'
        f'{_render_settings_form(cfg)}'
        '<div style="margin:8px 0"><button class="btn">💾 保存全部设置</button></div></form>'
        '<form method="post" action="/api/settings/reset" style="display:inline">'
        '<button class="btn red">恢复默认</button></form></header>',
        '<div class="footer">⚠ 风控与监控阈值建议谨慎修改。免责声明:本系统全部内容仅供研究参考,不构成投资建议。股市有风险,入市需谨慎。</div>',
    ]
    return _shell("settings", "系统设置", "\n".join(content))


def _flatten_form(form) -> dict:
    out = {}
    for key in form:
        val = form.get(key)
        if key.startswith("monitor.rules."):
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
    host = os.environ.get("GUGA_HOST", "127.0.0.1")
    port = int(os.environ.get("GUGA_PORT", "8000"))
    print(f"\n  量化决策仪表盘(今日决策为默认首页): http://{host}:{port}/decision\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
