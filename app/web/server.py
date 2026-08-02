"""Web 仪表盘:走势预测 + 实时操作建议。

启动: python run_web.py  (默认 http://127.0.0.1:8000)
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from flask import Flask, jsonify, render_template_string, request

from app.analysis import analyze, analyze_light
from app import config

app = Flask(__name__)

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
  @media(max-width:820px){ .grid{grid-template-columns:1fr;} }
</style>
</head>
<body>
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
    code = (request.args.get("code") or "").strip()
    if not code:
        return render_template_string(PAGE, code="", r=None, error=None, chart_html="", model={})
    try:
        r = analyze(code)
        chart = _build_chart(r)
        model = r.get("model_info") or {}
        r_light = {
            "name": r["name"], "code": r["code"], "analyzed_at": r["analyzed_at"],
            "prediction": r["prediction"], "advice": r["advice"], "quote": r["quote"],
        }
        return render_template_string(PAGE, code=code, r=r_light, error=None, chart_html=chart, model=model)
    except Exception as e:  # noqa: BLE001
        return render_template_string(PAGE, code=code, r=None, error=f"分析失败: {e}", chart_html="", model={})


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


def main():
    host = os.environ.get("GUGA_HOST", "127.0.0.1")
    port = int(os.environ.get("GUGA_PORT", "8000"))
    print(f"\n  量化预测仪表盘: http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
