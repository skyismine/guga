# 第二轮:决策实用性升级 — 修改说明

> 原则:纯增量修改、不破坏第一轮核心逻辑(LightGBM 预测 / VectorBT 特征 / fixing 规则)、所有新规则可配置、保留旧模式开关、输出中性合规。
> 提交范围:`app/decision/engine.py`、`app/support/settings.py`、`app/support/mainline.py`、`app/data/fetcher.py`、`app/web/server.py`。

## 升级1:资金面打分公平性(净流入率 + 5日准入 + 双周期 + 大盘联动)

**问题**:资金打分用绝对净流入,大盘板块(体量大、成交高)天然占优,中小题材永远垫底,不公平。

**改动**(`mainline.py` + `settings.py`):

1. `sector_scores` 新增**净流入率**(率 = 净流出额 ÷(流入+流出),同花顺无销额字段,分母=流入+流出),替换绝对净流入排序;
2. 新增 `decision.fund.use_net_rate` 开关:`True`=净流入率排名打分(新),`False`=绝对金额排名(旧模式);
3. 新增**5日准入**(`admission_enabled` / `admission_net_5d_min` / `admission_min_pct_5d`):
   - 5日净流出 > 阈值 剔除;
   - 5日累计涨幅 ≤ 阈值(量价背离:资金进、价格跌)剔除。
4. 新增**双周期权重**(`mainline_dynamic_weight`,默认关):
   - A级市场:5日 20% / 单日 80%;
   - B级市场:5日 40% / 单日 60%;
   - C/D级市场:5日 70% / 单日 30%。
   (风险偏好越高越偏短线单日,越保守越看中期 5 日趋势)
5. 资金状态(`fund_status`):持续流入 / 流入转弱 / 流出;输出 `fund_score_5d/1d/fund_rank_5d/fund_rank_1d/rate_5d/rate_1d`。

## 升级2:动态仓位矩阵(市场评级 × 标的类型 + 单板块总仓位上限)

**问题**:决策只有总仓位上限(市场)与单票上限(固定),缺少"不同市场环境 × 不同标的类型"的精细化仓位控制。

**改动**(`engine.py` + `settings.py`):

1. 新增矩阵配置 `decision.position_matrix`:

   ```
   enabled      总开关(默认开)
   cap.A~D      {mood 情绪龙头 | mid 中军龙头 | etf ETF | def_etf 防御备选ETF}
                A:8/8/10/8  B:5/8/10/8  C:0/3/5/3  D:0/3/3/3  (占总资金比例)
   sector_cap   A30% / B20% / C15% / D10%  单板块总仓位上限
   enforce     达到板块上限强制压缩 + 预警(默认开)
   ```

2. `_matrix_cap(grade, asset_type)` 读取矩阵上限;`matrix_cap ≤ 0` 时**禁止新开仓**,返回 `{"ok": False, "reason": "{grade} 级市场下「{asset_type}」类型禁止新开仓(仓位矩阵 0%)"}`;
   (修复:`row.get(asset_type) or row.get("mid")` 中 0 值被 or 覆盖为缺省值,bug 修复为「存在即取,回退仅当缺失」)
3. 实际仓位 = min(风险公式倒推仓位, 总仓位上限, 矩阵上限, 板块剩余);整百股截断;
4. 单板块累计:按 `决策顺序(steady→aggressive→etf)` 依次累计 `sector_used_pct`,后票受 `sector_cap - 已用` 约束并压缩,note 标注「板块已用 X%,上限 Y%,本票最多 Z%」;
5. `execution_plan` 新增参数 `grade / asset_type / sector_used_pct`;`single_cap`(旧模式)保留——当传入 `single_cap` 或未传 `asset_type` 时走旧逻辑(兼容第一轮用例)。

## 升级3:板块性价比维度(位置评级 / 盈亏比 / 优先级 / 定性结论)

**问题**:主线排名只看综合得分,高位追涨板块与低位启动板块无法区分风险和性价比。

**改动**(`engine.py` → `_sector_stats` + 新函数):

1. `_sector_stats_uncached` 增补:`price`(现价)、`sup20/res20`(近 20 日支撑/压力)、`dd20`(近 20 日最大回撤);
2. `_pos_rating(stats)` 位置评级:近 3 日涨幅 + 回撤两因子 → `低位启动 / 中位运行 / 短期高位`(阈值可配);
3. `_profit_ratio` 盈亏比 = `(res20 - price) / (price - sup20)`(潜在上行空间 / 下行风险);`_rr_label` → `高性价比(≥1.5)/ 中等性价比(≥1.0)/ 追高风险(<1.0)/ 无数据`;
4. `_priority` 操作优先级:核心+高盈比=高、防御=中、观察=低(与等级联动);
5. `_value_notes` 定性结论:如「资金技术双共振,低位启动持续性强——净流入率 8.2% 全市场第 1 名,8 家涨停形成板块效应」,所有计数/排名均来自真实数据;
6. `mainline_select` 对每只主线写入 `pos_rating / profit_ratio / rr_label / priority / value_note`,复用决策对象供 Web。

## 升级4:触发条件量化(交易时段动态判断)

**问题**:**触发条件**仅为静态文字(如「缩量企稳进行」),不区分盘中是否真实触发。

**改动**(`fetcher.py` + `engine.py`):

1. `fetcher.get_intraday_bars(code, period="5", limit=120)`:东财分钟K线(时间/开盘/最高/最低/收盘/成交额),失败返回空 DataFrame,不抛异常;
2. `_trigger_status(code, support, resistance, mode, tcfg)` 量化判断:
   - **回踩低吸(缩量企稳)**:现价在支撑 ±1% 内,连续 N 根 5 分钟K线量能 < 日内均值×0.8(含累计折减);
   - **突破跟进(有效突破)**:现价站稳压力位上方 ≥ M 分钟,且量能 ≥ 前 30 分钟均量×2;
   - 数据不可用(盘外/接口失败)→ `未知 / 盘中数据未就绪`,**不阻塞页面**;开关关闭 → 未触发。
   - 输出 `trigger_status: {status, label, note}`,`label ∈ trigger-on|trigger-off|trigger-unknown`;
3. 执行参数返回嵌入 `trigger_status`;Web `_plan_table_html` 纠错展示,状态标签样式:触发中/已触发=绿(up),未触发/未知=灰色(mut)。

## 校验用例(全部通过)

- `docs/round2_validate.py`(36 项 PASS, 0 FAIL):
  - 矩阵:A/B/C 级 × 情绪/中军/ETF/防御各档位上限、C 级禁开仓、单板块压缩、关闭矩阵回退旧模式;
  - 性价比:位置评级、盈亏比、优先级、定性结论数据支撑;
  - 触发:缩量企稳、有效突破、数据不可用降级、开关关闭;
  - 资金:5日准入、量价背离、净流入率分母、双周期权重、旧模式开关。
- 第一轮回归 `docs/round1_validate.py` 全跑到(保持 29/29 PASS),确保 `single_cap` 旧模式不破坏原自洽。

真实数据端到端(2026-08-09):

- 主线:CRO概念(B 级)核心主攻,性价比标注「短期高位·追高风险」,黄金 = 防御备选,观察池无倒挂;
- 执行参数(矩阵生效):温和首选 7.0%/B 级 8% 上限、激进 4.8%/B 级 5% 上限、ETF 8.2%/B 级 10% 上限;CRO 板块累计 20% 达 B 级 20% 上限,note 标注「板块已用 X%,上限 20%,本票最多 Y%」;
- 触发状态:非交易时段分钟K接口无数据 → 「未知 / 盘中数据未就绪」正确降级不阻塞;
- Web 页面:板块表层 11 列(板块/评分/涨跌/净流入/涨停/3日20日/位置/盈亏比/优先级/净流入率排名/理由)、执行参数表含「触发状态」列、矩阵与板块预警标注正常渲染。

## 回滚方式

全部改动集中在上述 5 文件,可整体撤销(revert)。未修改:预测器 / 特征 / heb. `position_matrix.enabled=False` 即还原旧固定仓位模式;`decision.fund.use_net_rate=False` 还原绝对资金旧打分;`mainline_dynamic_weight=False` 还原单周期打分。