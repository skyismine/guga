# guga — SilverQuant 改造: Akshare + VectorBT 量化预测分析系统

基于开源量化框架 [SilverQuant](https://github.com/silver6wings/SilverQuant)(`SilverQuant/` 子目录)改造的
**股票后续走势预测 + 当前实时操作建议**系统:

- **数据层**: 接入 **Akshare**,主用新浪数据源(稳定),东财/腾讯多源回退 + 本地缓存;
  支持 **A 股股票与 ETF/LOF**(ETF 走 `fund_etf_hist_sina` 专用数据源,含成交量)
- **特征层**: 用 **VectorBT** 快速计算个股技术指标特征(均线/MACD/RSI/布林/ATR/量能/形态)
  + **市场级特征**(涨跌家数宽度、恐贪合成指标、期指基差资金),共 63 个
- **预测层**: **LightGBM**(LGBMClassifier)三分类模型,预测未来 N 日 上涨/震荡/下跌 概率,
  按时间切分验证,避免前视
- **建议层**: 融合预测概率 + 技术面 + 市场情绪(ATR 波动/恐贪/期指资金/涨跌家数),
  输出 买入/加仓/持有/减仓/卖出/观望、建议仓位及买入区/目标价/止损价/支撑/压力位
- **验证层**: VectorBT 信号回测(全样本演示 + 样本外 OOS 回测)
- **接入层**: 按 SilverQuant 架构(池/买入/卖出/委托/订阅)实现预测策略,
  `PaperDelegate` 虚拟账户可无 QMT 运行,实盘可无缝切换 XtDelegate / GmDelegate
- **展示层**: CLI 命令行 + Web 仪表盘(Flask + Plotly 自包含图表)

## 目录结构

```
guga/
├── SilverQuant/            # 上游框架(原样保留, 作为改造底座)
├── app/
│   ├── config.py           # 全局配置(路径/预测参数/A股交易规则)
│   ├── analysis.py         # 统一分析管道(数据→特征→预测→建议)
│   ├── data/
│   │   ├── patch_requests.py  # requests 浏览器 UA 补丁(修复东财拦截)
│   │   ├── fetcher.py         # Akshare 数据层(新浪主源+回退+缓存)
│   │   └── market.py          # 市场情绪数据(期指基差/指数/涨跌家数乐咕)
│   ├── features/
│   │   ├── indicators.py      # VectorBT 技术指标特征
│   │   └── market_features.py # 市场级特征(宽度/恐贪/期指基差)+ 快照
│   ├── ml/                    # dataset / trainer / predictor
│   ├── advice/advisor.py      # 操作建议引擎
│   ├── backtest/vbt_validate.py  # VectorBT 信号回测(OOS 验证)
│   ├── strategy/               # PaperDelegate + 预测策略 + 运行器
│   ├── cli/analyze.py          # 命令行工具
│   └── web/server.py           # Flask 仪表盘
├── run_train.py            # 训练模型
├── run_analyze.py          # 命令行分析
├── run_web.py              # Web 仪表盘
├── run_predict.py          # SilverQuant 风格策略入口(回放/实盘模拟)
└── requirements.txt
```

## 环境准备

已在 `D:\miniconda3\envs\quant_simple` 验证(Python 3.14 + akshare + vectorbt 1.1 + lightgbm 4.7 + flask):

```bash
conda activate quant_simple
pip install -r requirements.txt
```

> 注意:不安装 SilverQuant 的完整 `requirements.txt`(其 pin 了 numpy==2.2/pandas==2.2,
> 会降级当前环境且需要 QMT 依赖)。

## 快速开始

### 1. 训练模型

```bash
python run_train.py
```

从 20 只样本股近 600 个交易日训练三分类模型,时间切分(前 80% 训练 / 后 20% 验证),
输出准确率、F1、涨/跌 AUC 排序指标与混淆矩阵,模型保存到 `data_cache/models/`。

### 2. 命令行分析

```bash
python run_analyze.py 600519            # 单只股票: 预测 + 操作建议 + 实时行情
python run_analyze.py 600519 300750     # 多只
python run_analyze.py 600519 --save     # 另存 K 线预测图
python run_analyze.py --train           # 训练
python run_analyze.py --backtest        # 全样本回测演示
```

### 3. Web 仪表盘

```bash
python run_web.py            # http://127.0.0.1:8000
```

- `/` 输入股票代码 → 走势预测 + 操作建议 + K 线/概率图
- `/signals` 今日信号单(股票池按预期收益排序:Top-N 买入候选 / 末位风险提示 / 全池排序)
- `/api/analyze?code=600519` JSON 接口
- `/api/signals` 今日信号单 JSON 接口
- `/api/backtest` 回测汇总接口
- `/api/model` 模型信息

### 4. SilverQuant 风格策略(虚拟账户)

```bash
python run_predict.py            # 历史回放验证(逐日驱动虚拟账户)
python run_predict.py live       # 交易时段实盘模拟(轮询新浪实时行情)
```

`run_predict.py` 按 SilverQuant `run_ai_gen.py` 的模式组装 `池 + 策略 + 委托`。
默认使用 `PaperDelegate` 虚拟账户(含 T+1、佣金、印花税)。
实盘/模拟盘切换:将 `app/strategy/runner.py` 中的 `PaperDelegate`
替换为 SilverQuant 的 `XtDelegate`(QMT 实盘)/ `GmDelegate`(掘金模拟盘)即可。

## ETF 支持

- 识别:代码前缀 `51/52/56/58`(沪)、`15/16/18`(深)判为 ETF/LOF(`fetcher.is_etf`)。
- 历史日线走 ETF 专用数据源 `fund_etf_hist_sina`(含成交量,东财 `fund_etf_hist_em` 复权回退),
  缓存与复权框架与股票一致;实时行情复用新浪 hq 接口。
- 特征/预测/建议/回测/市场情绪对 ETF 完全适用(仅依赖 OHLCV 与全市场数据)。
- 直接可用:`python run_analyze.py 510300`;回测/回放传入 ETF 代码即可,
  如 `python run_analyze.py --backtest` 后用 `ETF_SAMPLE_CODES` 或 `GUGA_POOL=510300,510500,588000`。
- 注意:ETF 新浪历史不含复权,特征以收益率/比值为主,对结果影响有限。

## 系统设计要点

### 数据源稳定性
东财接口存在 WAF 限流(TLS 指纹/频率),SilverQuant README 亦提示"Akshare 经常被 Ban IP"。
本项目:
1. `patch_requests` 给 requests 注入浏览器 UA(`HTTPAdapter.send` 层,框架 reader 同步受益);
2. **新浪作为主数据源**(`stock_zh_a_daily` 历史 + `hq.sinajs.cn` 实时),东财/腾讯回退;
3. 本地 pickle 缓存 + TTL,减少重复请求。

### 特征与标签(无前视)
- 特征仅用当期及历史数据(`compute_features`),时间切分训练,预测信号滞后 1 日执行;
- 标签 = 未来 `horizon` 日收益,**滚动分位数阈值**(默认):对每只个股,日期 t 的涨/跌阈值取
  过去 250 个交易日已实现未来收益的 30%/70% 分位数(窗口整体 shift(horizon) 保证无前视)。
  自动适配个股波动率与市场环境,三类样本占比长期稳定在约 30/40/30,根治固定百分比(±1.5%)
  的阈值漂移;`LABEL_QUANTILE_WINDOW<=0` 时回退固定阈值(`PREDICT_THRESHOLD`);
- 市场级特征(`market_*` 前缀,由 `market_features.py` 构建并按日期对齐):
  - **涨跌家数/宽度**: `market_adv_ratio`(样本篮子上涨占比)、`market_above_ma20`(站上 MA20 占比)、
    `market_hot_ratio`(强势股占比)、`market_dispersion`(截面离散度)等;
  - **恐贪指标**: `market_fear_greed`(0~100 合成:宽度 + 强度 + 指数动量/RSI + 期指基差 + 波动率);
  - **期指资金**: `market_basis_*`(IF/IH/IC/IM 四大期指相对现货指数的升贴水);
  - 实时端另有乐咕乐股全市场涨跌家数(涨停/跌停)与期指实时基差快照。
- ATR 已含于个股特征(`atr14` / `atr_pct`),同时用于建议层的仓位控制与止损。
- **行业/风格特征**(`industry_features.py`):个股所属申万一级行业指数涨跌 + 相对行业超额收益,
  帮助模型区分 **个股 alpha 与行业 beta**(提升跨风格/跨行业稳定性):
  - `ind_ret_1/5/20`(行业 beta)、`ind_ma20_gap`(行业趋势)、`ind_vol20`(行业波动);
  - `alpha_1/5/20`(个股-行业超额收益)、`alpha_trend`(相对行业动能)。
  行业指数取申万一级(sina `index_hist_sw`,稳定);个股→行业映射样本池静态表优先、任意代码
  动态解析兜底,均本地缓存;无行业标的(如 ETF)该组特征为 NaN,LightGBM 原生处理缺失。
  A/B 同窗口验证:含行业特征 跑赢全池 55.9% vs 不含 50.5%(Top3 未来3日均收益 +0.37% vs +0.31%)。
- **特征筛选**(`select_features.py`):相关性去冗余 + 重要性 Top-N。对特征两两计算相关系数,
  按 LightGBM gain 重要性降序贪心保留(|相关系数| > 0.8 视为冗余剔除同簇中重要性较低的),
  再取前 `FEATURE_SELECT_TOP_N`(默认 30)。结果存 `selected_features.json`,训练/回测统一使用。
  加入行业特征后共 72 个候选 → 筛至 30 个(含 `ind_*`/`alpha_*`)。
- **滚动 z-score 标准化**(`standardize.py`):每个特征按"自身原始序列"用历史窗口(250日)
  均值/标准差滚动标准化,仅用当日及以前数据(无前视)。个股特征按个股序列、市场特征按市场帧、
  行业特征按行业指数分别标准化,保证横截面可比(市场/行业特征同一天所有股票取值一致)。
  提升模型训练稳定性(同窗口 A/B:acc/F1 提升,排序信号中性)。

### 操作建议
`advisor.py` 综合 模型概率 + 均线排列 + RSI + MACD + 量比 + 布林位置,
叠加 **市场情绪信号** 后输出动作、区间价位、建议仓位与依据/风险提示:
- **恐贪指标**: 极度恐慌(<20)加分/降级卖出,极度贪婪(>80)减分/降级买入;
- **期指基差**: 深贴水(≤-0.8%)机构套保偏空 → 减分,升水(≥+0.3%) → 加分,IM 深贴水额外提示;
- **涨跌家数**: 普涨(占比≥70%)加分、普跌(≤30%)减分,涨停潮(≥80 家)短线情绪加分;
- **ATR 波动**: 高波动(≥4%)减分并提示降仓,同时给出基于 ATR 的建议仓位上限。

### 预测输出:预期涨跌幅 / 盈亏比
模型除三分类概率外,还输出两项交易参考(训练时统计各类别平均未来收益存入 meta):
- **预期涨跌幅** `expected_return` = Σ P(类) × 该类平均未来收益(未来 `horizon` 日的期望涨跌);
- **盈亏比** `reward_risk` = (P(上涨)×平均上涨收益) / (P(下跌)×|平均下跌收益|),
  即期望盈利/期望亏损,`≥1` 视为风险回报划算;`p_down≈0` 时不可计算返回 `None`。
CLI 与 Web 均展示这两项,`predict_series` 亦逐日输出,可用于回测与图表。

### 每日信号排序(Top-N,信号系统主入口)
`ranker.py` 提供面向实盘"只发信号、不做自动交易"的每日信号清单:
- `daily_signals()`:对股票池按 **预期收益** 排序,取前 `RANK_TOP_N` 为买入候选
  (要求 `p_up ≥ RANK_MIN_P_UP` 且预期收益 ≥ 0),末位为风险提示;用相对排序替代
  绝对概率阈值,天然适配个股波动率与市场环境差异;
- `rank_backtest()`:walk-forward 排序回测,统计样本外 top-N 的"未来 horizon 日实际
  收益"与全池均值/末位对比。实测(top3, 2026-03~07):Top3 均收益 **+0.42%/3日** >
  全池 +0.03% > 末位 -0.09%,跑赢全池占比 58.8%,排序信号呈单调性。
运行:`python -m app.strategy.ranker today`(今日信号) / `... bt`(排序回测)。

## 结果与声明

- 全样本回测(演示)与**样本外回测**(前 70% 训练/后 30% 验证)均可通过
  `python app/backtest/vbt_validate.py` 一键运行。
- 当前模型为 LightGBM(LGBMClassifier,63 特征):验证准确率 ≈44%,上涨/下跌排序 AUC ≈0.59/0.60;
  `run_predict.py` 7 股池回放收益 ≈+157%(等权基准 +38%),最大回撤 ≈-3%。
- 短期价格预测本质上极难,模型排序能力仅略高于随机;样本外信号稀疏。
  **本系统仅供量化研究与学习参考,不构成任何投资建议,据此操作风险自负。**
