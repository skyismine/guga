# 第三轮:前端体验优化 — 修改说明

> 原则:只改页面展示与交互,不动任何核心计算逻辑。所有新增模块均有独立开关,可在「系统设置 → 前端体验」中开启/关闭,默认全部开启。
> 提交范围:`app/web/server.py`(主要)、`app/support/settings.py`(web_ui 开关)、`app/support/mainline.py` + `app/decision/engine.py`(仅透传已计算的分项字段,不动算分公式)。

## 优化项1:顶部极简结论卡

- 在风险提示卡下方、四层漏斗上方新增**一行浓缩结论条**(`#conclusion-bar`),默认展开。
- 固定展示:`【今日操作结论】市场X级 · 总仓位上限 XX% · 首选方向 XXX · 首选标的 XXX(稳健)/ XXX(ETF) · 操作建议:XXX · 核心风险:XXX`。
- 风格:深蓝渐变醒目色块 + 大字号(24px 仓位上限、22px 评级徽章),并附一行小字「盯盘速览」拼接引擎 `conclusion.line`。
- 开关:`web_ui.conclusion_bar`。

## 优化项2:昨日信号复盘

- 页面底部新增折叠模块,展示**上一交易日系统推荐标的的今日表现**。
- 数据来源:每次 `/decision` 渲染时把当日推荐(plans/targets 三档各首标)与板块行情快照持久化到 `data_cache/review/targets_<date>.json` 与 `layers_<date>.json`(Web 层数据积累,不落进引擎);次日读取上一交易日快照,用 `fetcher.get_spot_quotes` 拉今日实时涨跌对比。
- 统计字段:昨日推荐数量、上涨数量、**胜率**、**平均涨跌幅**、**最大涨幅**、**最大跌幅**。
- 首次运行(无历史快照)显示引导文案,不报错、不阻塞页面。
- 开关:`web_ui.yesterday_review`。

## 优化项3:信息降噪与交互优化

- **淘汰板块折叠**:`layer2 ② 主线概念遴选` 中的已淘汰板块从默认展开改为**默认折叠**,仅显示「🗑 已淘汰 X 个 / 点击展开查看详情」,点击 `<details closed>` 展开详情表。
- **标的匹配 Tab 切换**:`layer3 ③ 标的精准匹配` 默认多板块堆叠改为**Tab 按钮切换**(`.target-tabs`),点击对应板块才显示其标的列表;单板块时仍为单个卡片。
- **情绪龙头风险标签**:每只**情绪龙头**(aggressive 档)小票名称下新增灰色小字「高波动 · 纯情绪博弈 · 建议极轻仓」。
- **数值环比箭头**:板块表中「当日涨跌」「主力净流入」字段对比上一交易日快照,追加 `↑↓(+变化值)` 小字(涨红跌绿);无前日数据/无变化时不显示。
- 开关:`web_ui.rejected_collapse / target_tabs / mood_risk_tag / delta_arrows`。

## 优化项4:板块详情下钻弹窗

- 在 `layer2` 板块表每行添加 `onclick` 下钻(点击任意板块行):弹出**模态弹窗**(`.modal-mask/.modal-box`),无需跳页。
- 弹窗内容四块:
  1. **打分明细**:`/api/sector_detail` 返回该板块各分项得分(综合/资金5日+单日/趋势/情绪/消息)与行情、性价比字段;
  2. **板块指数 K 线**:Plotly 迷你折线图(`_sector_detail_kline_html`,复用 `_get_concept_close` 概念指数收盘,300 交易日窗口);
  3. **成分股涨幅 Top10**:`_match_stocks` 板块成分按当日 `pct_chg` 降序取前 10;
  4. **当日新闻摘要**:`collect_events` 按板块名关键词命中当日财联社/东财新闻前 6 条。
- API:`GET /api/sector_detail?name=XX` → `{name, breakdown_html, kline_html, constituent_html, news_html, reason_html}`。
- 样式:一体化暗色 modal(CSS 注入 `_shell`),支持遮罩点击关闭、✕ 关闭。
- 开关:`web_ui.sector_detail`。

## 引擎侧(仅输出透传,不改算分)

- `app/support/mainline.py`:`sector_scores` 在既有点位算分后,每行追加 `breakdown:{fund, fund_5d, fund_1d, trend, sentiment, news}`(存放已算出的中间量,公式未动)。
- `app/decision/engine.py`:`mainline_select` 的 `item`/`rejected` 条目透传该 `breakdown` 字典供前端弹窗展示。
- 校验:第一、二轮用例(`round1` 29/29、`round2` 36/36)全部回归,PASS 不变。

## 配置开关(settings.web_ui,默认全开)

| key | 说明 |
|---|---|
| `conclusion_bar` | 顶部极简结论卡 |
| `yesterday_review` | 底部昨日信号复盘 |
| `rejected_collapse` | 淘汰板块默认折叠 |
| `target_tabs` | 标的匹配 Tab 切换 |
| `mood_risk_tag` | 情绪龙头风险标签 |
| `delta_arrows` | 数值环比箭头 |
| `sector_detail` | 板块详情弹窗 |

设置页新增「前端体验」分组,checkbox 勾选持久化;未勾选回填 False(`_flatten_form` 处理 web_ui.*)。

## 校验(全部通过)

- `docs/round3_validate.py`(19 项 PASS, 0 FAIL):页面 200、全部新模块存在、板块详情 API 200 且六字段、设置页开关渲染、settings 默认开启、targets/layers 快照落盘。
- `docs/round3_validate_off.py`(5 项 PASS, 0 FAIL):开关全部关闭时各模块正常降级不出现在页面。
- `docs/round1_validate.py`(29/29)与 `round2_validate.py`(36/36)回归无退化。
- 前端交互(J)验证:tab 切换、弹窗开/关、chron(s) 用 fetch 到 `/api/sector_detail` 均由浏览器端触发;板行点击绑定 `openSectorDetail`。

## 回滚方式

- 全部改动集中在上述 4 文件(主要在于 `server.py` 新增函数 + `page_decision` 接线、`settings.py` 配置、`mainline.py/engine.py` 输出透传),可一次 revert。
- 计算逻辑未改;`web_ui` 全关即恢复为第三轮前页面外观;`breakdown` 字段为纯附加,不影响其它消费方。