# guga — A股量化决策与交易复盘系统

基于 **Akshare 多源行情 + 同花顺金融数据 API(fuyao)+ LightGBM 三分类模型 + VectorBT 特征** 的
A 股量化决策系统,提供**阶段判定 → 主线适配 → 标的校验 → 仓位管控 → 复盘验证**的完整风控闭环:

- **四阶段体系**: 大盘评级+情绪+量能+连板映射全局 `market_phase`(退潮冰点/启动确认/主升发酵/高潮加速),
  作为全系统唯一阶段标识,向下贯穿主线准入与数量、标的盈亏比门槛、仓位矩阵、执行止损、复盘合规
- **决策引擎**: 四层架构(大盘开仓许可 → 主线三层分级遴选 → 板块标的三档梯队匹配 → 执行计划/仓位风控),
  主线评分 = 资金面40 + 趋势30 + 情绪20 + 消息催化10(+ 可选题材热度独立维度)
- **标的匹配**: 中军龙头(趋势核心) + 情绪龙头(领涨弹性) + 补涨优选(高低切换) 三档梯队,
  综合评分 + 1日滞回持续性过滤 + 差异化交易参数 + 分阶段盈亏比硬门槛(左侧低吸/右侧突破),三档强制去重
- **复盘报告**: 每日交易复盘(13 大模块),规则层生成全部表格/清单/方案/合规校验(含阶段合规),
  LLM 仅润色核心结论叙事段(数值校验防幻觉),自动落盘可历史检索
- **持仓与交易**: 操作流水实时写回持仓、账户模型(总资产=本金+已实现+浮盈)动态计算仓位与盈亏
- **数据层**: Akshare 多源回退(新浪主源 + 东财/腾讯 + ETF 双源)+ fuyao 同花顺官方 API 兜底/补充

## 目录结构

```
guga/
├── app/
│   ├── config.py                 # 全局配置(路径/预测参数/A股交易规则)
│   ├── data/
│   │   ├── patch_requests.py     # requests UA 补丁 + 默认超时兜底(防连接假死挂起)
│   │   ├── fuyao.py              # 同花顺金融数据 API 客户端(信封解析/缓存/节流)
│   │   ├── fetcher.py            # Akshare 日线多源回退 + fuyao 股票兜底 + 缓存
│   │   └── market.py             # 指数/期指基差/涨跌家数
│   ├── features/
│   │   ├── indicators.py         # VectorBT 技术指标特征
│   │   ├── market_features.py    # 市场级特征(宽度/恐贪/基差)+ mkt_all_ 全市场宽度(含回填)
│   │   ├── concept_features.py   # 同花顺概念成分/概念指数(fuyao 优先,THS 反爬兜底)
│   │   ├── industry_features.py  # 申万行业 beta + 个股 alpha
│   │   ├── select_features.py    # 特征筛选(相关性去冗余 + 重要性 Top30)
│   │   └── standardize.py        # 滚动 z-score 标准化
│   ├── ml/
│   │   ├── pool_builder.py       # GBM 训练池(680: core_a500 500 + emotional 100 + risk 50 + large_cap 30)
│   │   ├── dataset.py            # 样本构建(标签分位数/剔除涨停停牌)
│   │   ├── trainer.py            # LightGBM 三分类训练 + 月度调度
│   │   └── predictor.py          # 推理
│   ├── decision/
│   │   └── engine.py             # 决策引擎(四层: 大盘许可/主线遴选/标的匹配/执行计划)
│   ├── support/
│   │   ├── mainline.py           # 主线三层分级评分(sector_scores)
│   │   ├── mainline_stabilizer.py# 主线防抖稳定器(驻留/N周期确认/冷却)
│   │   ├── target_match.py       # 标的匹配 v2(三档梯队接入 + 可选增强层)
│   │   ├── tier_select.py        # 三档梯队选股(候选池分层/综合评分/1日滞回/去重/交易参数)
│   │   ├── daily_report.py       # 复盘报告生成(规则主体 + LLM 叙事段 + 落盘)
│   │   ├── operations.py         # 交易流水 + 合规审计 + 账户模型
│   │   ├── portfolio.py          # 持仓诊断(逐仓预测/盈亏/levels)
│   │   ├── risk.py               # 风控与仓位校验(持仓 CSV 读写 GBK 容错 + 备份)
│   │   ├── settings.py           # 全部配置(decision/score_weights/fuyao/account/discipline)
│   │   └── llm.py                # 大模型文案(OpenAI 兼容,核心结论叙事段)
│   ├── review/
│   │   ├── data.py               # 复盘数据采集(8 类,独立容错)
│   │   ├── generator.py          # 复盘 11 模块编排
│   │   ├── layers.py             # 主线三层分级研判(含情绪锚点)
│   │   ├── watch_pool.py         # 明日观察标的池(三档 + 超跌承接)
│   │   ├── positions.py          # 持仓明细/账户/逐仓方案/合规
│   │   ├── verify.py             # 当日决策效果验证 + 存档
│   │   ├── strategy_today.py     # 明日策略(前置条件/风险预案/盯盘Todo/纪律)
│   │   ├── special_data.py       # 同花顺特色数据(连板/热榜/龙虎榜/异动)
│   │   ├── snapshot.py           # 30秒速览 + 纯文本摘要
│   │   └── archive.py            # 历史归档/核心指标落库
│   └── web/server.py             # Flask 仪表盘(六页面 + 各类 API)
├── run_train.py                  # 训练模型
├── run_analyze.py                # 命令行分析
├── run_web.py                    # Web 仪表盘
└── requirements.txt
```

## 环境准备

已在 `D:\miniconda3\envs\quant_simple` 验证(Python 3.14.6 + akshare + vectorbt 1.1 + lightgbm + flask + pyarrow):

```bash
conda activate quant_simple
pip install -r requirements.txt
```

> 注意:不安装 SilverQuant 的完整 `requirements.txt`(其 pin 了旧版 numpy/pandas)。

### 可选:同花顺金融数据 API(fuyao)
用于日线兜底、涨停池/连板天梯、概念成分/概念指数、交易日历、复盘特色数据、mkt_all 历史回填。
在 `data_cache/settings.json` 的 `fuyao` 节配置(密钥仅本地保存,不提交):

```json
"fuyao": { "enabled": true, "api_key": "sk-fuyao-...", "base_url": "https://fuyao.aicubes.cn" }
```

## 快速开始

### 1. 构建训练池 + 训练模型

```bash
python -m app.ml.pool_builder    # 构建 680 只 GBM 训练池(core_a500/emotional/risk/large_cap)
python run_train.py              # 训练 LightGBM 三分类(约 84 候选特征 → 筛选 Top30)
```

### 2. 命令行分析

```bash
python run_analyze.py 600519            # 单只: 预测 + 操作建议 + 实时行情
python run_analyze.py 600519 300750     # 多只
```

### 3. Web 仪表盘

```bash
python run_web.py            # http://127.0.0.1:8800/decision(GUGA_HOST/GUGA_PORT 可覆盖)
```

| 页面 | 内容 |
|---|---|
| `/decision` | **今日决策**(默认首页): 大盘开仓许可 / 主线三层分级 / 板块标的三档梯队 / 执行计划与仓位 |
| `/analyze` | 个股走势预测(输入代码) |
| `/report` | **每日复盘报告**: 交易复盘(持仓/合规/逐仓方案/盯盘Todo/纪律),支持历史检索 |
| `/portfolio` | 持仓诊断 + 添加/导入持仓 + **今日操作记录录入** |
| `/alerts` | 盘中预警 |
| `/settings` | 系统设置(决策/权重/fuyao/账户/纪律等) |

### 4. 每日交易复盘

```bash
python -m app.support.daily_report   # 手动生成复盘(落盘 data_cache/reports/review_YYYYMMDD.md)
```

- 交易日 16:00 自动生成(收盘后,确保当日数据);`need_save_report=true` 时落盘 md 文件;
- `/report` 首访优先渲染当日已落盘 md(0 秒返回),不存在则生成后渲染;
- 复盘前置条件:在 `/portfolio` 录入当日买卖操作(合规校验数据源),并配置 `decision.total_asset` 本金。

## 数据源与稳定性

| 数据 | 主源 | 兜底链 |
|---|---|---|
| 个股日线 | 新浪 `stock_zh_a_daily` | 东财 → 腾讯 → **fuyao 官方历史K线** → 本地缓存(24h TTL) |
| ETF 日线 | `fund_etf_hist_sina` | 东财复权(当前 fuyao fund 端点不可用仍走 akshare) |
| 全A快照 | 东财 `stock_zh_a_spot_em` | 新浪分页(东财被拦时) |
| 涨停池 | 东财 | **fuyao 涨停池/连板天梯** |
| 概念成分/指数 | 同花顺(THS) | **fuyao 官方概念指数/成分**优先(摆脱反爬) |
| 概念资金流 | 同花顺 `stock_fund_flow_concept` | 快照回退 + 退避(无官方替代) |
| 交易日历 | 新浪 | **fuyao 交易日历** |
| 复盘特色数据 | — | fuyao(连板/热榜/龙虎榜/异动) |
| 全市场宽度历史 | — | fuyao 全市场 Parquet 回填(mkt_all_ 立即具备完整历史) |

稳定性设计:`patch_requests` 对未显式传 timeout 的请求兜底注入 15s(akshare 内部大量无超时请求,
防连接假死永久挂起);日线/市场帧缓存 TTL 延长至 24h(收盘后数据不再变动,避免全量重抓)。

## 决策引擎架构(四层 + 四阶段体系)

**全局阶段判定**(`engine.get_market_phase`,60s 缓存,全系统唯一 `market_phase`):
大盘评级 + 恐贪 + 量能 + 涨停家数 → 四阶段(退潮冰点/启动确认/主升发酵/高潮加速),各阶段参数固化在 `phase_cfg`:

| 阶段 | 总仓位上限 | 单票上限 | 单次新增 | 主线准入线 | 核心/发酵 | 盈亏比门槛(左/右) | 允许档位 | 操作基调 |
|---|---|---|---|---|---|---|---|---|
| 退潮磨底 | 30% | 1% | 2% | 65 | 2/0 | ≥2.0 / 禁右 | 中军+补涨 | 只减不加,极轻仓试错 |
| 启动确认 | 50% | 2% | 5% | 60 | 3/2 | ≥2.0 / ≥1.5 | 三档 | 回踩低吸+突破试加 |
| 主升发酵 | 70% | 5% | 10% | 58 | 4/3 | ≥1.5 / ≥1.2 | 三档 | 顺势加仓,持有为主 |
| 高潮加速 | 50% | 3% | 0 | 62 | 3/0 | 禁左 / 禁新开 | 仅中军 | 分批兑现,逐步降仓 |

四阶段贯穿全链路:
1. **第一层 大盘开仓许可**(`market_permit`): 恐贪/涨跌家数/量能/趋势 → A/B/C/D 评级 + **分阶段总仓位上限**(替代固定上限);
2. **第二层 主线三层分级遴选**(`mainline.sector_scores` → `mainline_stabilizer`):
   概念板块综合评分 = 资金面40 + 趋势30 + 情绪20 + 消息催化10,准入剔除(5日资金/量价背离),
   经防抖稳定器输出 核心主线 / 防御备选 / 观察;**准入线与核心/发酵数量随阶段动态调整,稳定器驻留周期联动**;
3. **第三层 板块标的三档梯队**(`tier_select`): 中军(趋势核心) + 情绪(领涨弹性) + 补涨(高低切换),
   候选池分层、三档强制去重、数量弹性、综合评分、1日滞回过滤单日脉冲、差异化交易参数
   (中军×1.5/-5%/+8% · 情绪×0.5/-8%/+15% · 补涨×0.6/-6%/+10%)、近3日涨幅超阈值自动下修;
   **新增 `trade_mode`(左侧低吸/右侧突破)与分阶段盈亏比硬门槛**(不达标剔除,档位禁用);
4. **第四层 执行计划/仓位风控**(`execution_plan`): ATR 止损/分批建仓/目标价/仓位矩阵,
   叠加单票/板块红线、**分阶段单次加仓上限(≤0 禁止新增)、止损阶段化(退潮×0.8/主升×1.2)、`operation_keynote` 操作基调**。

## 复盘报告(13 大模块)

`30秒速览 → 大盘综述(周期定位) → 板块轮动(结构分析) → 主线三层分级(含情绪锚点) →
明日观察标的池(三档+超跌承接) → 资金情绪交叉验证 → 同花顺特色数据(连板/热榜/龙虎榜/异动) →
当日决策效果验证 → 持仓与交易体系(账户/合规/逐仓方案/纪律) → 明日交易策略与开仓计划(阶段前置/前置条件/风险预案/盯盘Todo)`

- **规则主体**: 全部表格/清单/方案/合规校验/纪律由规则层渲染(数值准确);
- **LLM 叙事段**: 仅「核心结论」由大模型生成(输入含持仓/合规摘要),输出经数值一致性校验;
- **持仓与合规**: 交易流水(operations.jsonl)→ 合规审计(违规开仓/追高/超红线/破位未止损/**超阶段仓位上限/盈亏比不达标开仓**);
- **阶段联动**: 明日策略前置输出当前阶段/仓位上限/盈亏比门槛/操作基调,再给具体标的计划;
- **账户模型**: 总资产 = `decision.total_asset`(本金)+ 已实现盈亏 + 未实现浮盈,仓位/可用动态;
- **归档**: 含 `market_phase` 支持分阶段效果统计。

## 特征与标签

- **训练池**: 680 只(core_a500 500 + emotional 100 + risk 50 + large_cap 30),A500 成分按
  **历史调样期逐期裁剪**(消除幸存者偏差),训练/市场宽度/回测共用同一池;
- **市场级特征**: `market_adv_ratio/above_ma20/hot_ratio/dispersion`(680 池横截面宽度)+
  `market_fear_greed`(恐贪合成)+ `market_basis_*`(期指基差)+ `mkt_all_*`(全市场宽度,增量积累 + Parquet 回填);
- **行业/风格**: `ind_*`(申万行业 beta)+ `alpha_*`(个股-行业超额),提升跨风格稳定性;
- **标签**: 未来 horizon 日收益滚动分位数阈值(无前视),剔除涨停/停牌样本;
- **特征筛选**: 相关性去冗余 + 重要性 Top30;滚动 z-score 标准化(250 日窗口,市场/行业特征源端标准化)。
- **恐贪/开仓许可**: 宽度 + 强度 + 指数动量/RSI + 期指基差 + 波动率合成 0~100。

## 持仓与交易(数据文件)

| 文件 | 用途 |
|---|---|
| `data_cache/portfolio.csv` | 持仓(code/qty/cost/category),GBK 容错读取 + 保存前 .bak 备份 |
| `data_cache/operations.jsonl` | 交易流水(买卖/成交价/已实现盈亏),操作自动写回持仓 |
| `data_cache/settings.json` | 本地配置(api_key 等,gitignore 不提交) |
| `data_cache/reports/review_YYYYMMDD.md` | 复盘报告落盘 |
| `data_cache/review_archive.jsonl` | 决策快照/核心指标(演进追踪/效果验证/长周期统计) |

## 结果与声明

- 月度自动重训(scheduler 守护线程);模型特征随筛选自动更新(新特征累计足够后自动入模)。
- 短期价格预测本质上极难,模型排序能力仅略高于随机;本系统所有输出(含复盘文案)均为研究参考,
  **不构成任何投资建议,据此操作风险自负。**
