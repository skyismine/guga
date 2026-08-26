"""可配置设置:预警规则 / 风控阈值 / 打分权重 / 持仓路径。

所有参数内置默认值,Web「系统设置」页可改写并持久化到 data_cache/settings.json,
运行时以 json 覆盖默认值为准(缺失字段回退默认)。
"""
import json
import os

from app import config

_SETTINGS_PATH = os.path.join(config.DATA_DIR, "settings.json")

DEFAULTS = {
    # ---- 模块1 主线板块打分权重(合计 100)
    "score_weights": {"capital": 40, "trend": 30, "sentiment": 20, "news": 10},
    "mainline_top_n": 2,          # Top-N 为核心主线
    "mainline_branch_top_n": 5,   # 前 N 名(含主线)标注补涨支线
    "mainline_dynamic_weight": True,  # 资金面动态权重开关:A级 5日20%/单日80%, C/D级 5日70%/单日30%
    "mainline_fund_mode": "net_rate",  # 资金打分口径:net_rate(净流入率排名) | absolute(绝对金额排名,旧模式)
    "leader_min_market_cap": 20.0,   # 情绪龙头剔除 <20 亿小票(亿)
    "leader_exclude": ["ST", "退"],  # 龙头剔除名称关键词
    "etf_min_amount": 5000.0,        # ETF 日均成交额下限(万元)
    # ---- 模块2 持仓诊断
    "portfolio_path": os.path.join(config.DATA_DIR, "portfolio.csv"),
    "band_diff_pct": 0.06,       # 深度套牢做差价:高抛/回补区间幅度
    "take_profit_floor": 0.15,   # 盈利持仓止盈触发浮盈下限
    "move_stop_trail": 0.08,     # 移动止损回撤比例
    # ---- 模块5 风控仓位
    "risk": {
        "single_pct": 0.10,      # 单只标的仓位上限
        "sector_pct": 0.30,      # 单一板块/赛道仓位上限
        "loss_reduce_threshold": 0.20,   # 浮亏超 20% 触发加仓限制
        "add_increase_cap": 0.50,        # 单次加仓不超过原有仓位 50%
        "max_total_pct": 0.95,           # 总仓位上限(极端行情可下调)
        "position_by_mood": {            # 按恐贪指数档位动态调整总仓位上限
            "extreme_greed": 0.50, "greed": 0.70, "neutral": 0.85,
            "fear": 0.55, "extreme_fear": 0.30,
        },
    },
    # ---- 模块4 盘中预警
    "monitor": {
        "refresh_sec": 300,      # 刷新频率(秒)
        "enable": True,          # 总开关
        "rules": {               # 各规则开关
            "price": True, "sector": True, "mood": True,
            "signal": True, "volume": True,
        },
        "sector_net_yi": 5.0,    # 板块 10 分钟净流入阈值(亿)
        "sector_pct": 2.0,       # 板块涨幅阈值(%)
        "fg_extreme_low": 20,    # 恐贪极值(<=)
        "fg_extreme_high": 80,   # 恐贪极值(>=)
        "volume_yesterday_ratio": 1.0,  # 个股成交额 > 昨日全天倍数
    },
    # ---- 决策执行引擎(模块:今日决策)
    "decision": {
        # 决策输入参数(可在系统设置中修改)
        "total_asset": 1000000.0,   # 总资金(元),执行计划的仓位/股数计算基准
        "taste": "balanced",        # 风险偏好: conservative | balanced | aggressive
        # 第一层 大盘开仓许可评级(任一不满足即降级,取最低评级)
        "market": {
            "score_full": 70.0, "zt_full": 80, "adv_ratio_full": 1.5,   # A级条件
            "score_ok": 50.0, "zt_ok": 50, "adv_ratio_ok": 1.0,         # B级条件
            "score_hold": 30.0, "zt_hold": 30,                          # C级条件(或)
            "cap": {"A": 0.80, "B": 0.50, "C": 0.30, "D": 0.10},        # 各评级总仓位上限
        },
        # 第二层 主线一票否决
        "veto": {
            "max_gain_3d": 0.15,        # 近3日累计涨幅>=此值 淘汰
            "min_zt_in_sector": 2,      # 板块内涨停家数 <此值 淘汰
            "bad_news_kw": ["立案", "处罚", "退市", "预亏", "大幅减持",
                            "质押平仓", "风险警示", "违规"],  # 名称/领涨股利空关键词
        },
        # 第二层 主线准入与分级
        # 主线的定量分数阈值口径统一:80+非常强势(龙头),60-79强势,50-59蓄势,<50弱势
        "mainline": {
            "pass_score": 60.0, "core_n": 1, "defensive_n": 1, "watch_n": 3,
            # ---- 第四轮:盘中防抖稳定器(外层独立模块,不改动内部打分公式) ----
            "enable_stabilizer": True,      # 总开关; False=关闭防抖,直接透传原始流水线结果(兼容历史回测)
            "intraday_smooth_window": 25,   # 单日资金平滑窗口(分钟), 0=关闭平滑; 5分钟轮询下=约5个样本(仅稳定器内生效,5日表完全不改动)
            "rank_delta_thresh": 0.002,     # 排名打分阻尼阈值:相邻板块净流入率差<此值视为同档位,不做阶梯扣分
            "STABILIZE_CYCLE": 3,           # 连续N个快照周期驻留/冷却/替换确认
            "COOL_DOWN_MINUTE": 20,         # 被移出正式池后的冷却分钟数,冷却中只能进入 candidate
            "PASS_HYSTERESIS_UP": 62.0,     # 新板块进入正式池(passed)的分数门槛
            "PASS_HYSTERESIS_DOWN": 58.0,   # 已在正式池内的板块,分数低于此值才允许移出(滞回)
            "weaken_news_on_no_5d_money": True,  # 无5日资金净流入时,新闻催化满分降为低档(防消息脉冲)
            # ---- 第五轮:扩展因子(可配置开关,默认开启;回测可关闭保持原逻辑) ----
            "enable_extend_factor": True,   # 扩展因子总开关(连板梯队 + 大小盘风格偏转)
            "extend_factor": {
                "ladder": {                  # 板块连板梯队因子(trend 内部重组,trend 总权重30不变)
                    "enabled": True,
                    "pct_w": 0.6, "zt_w": 0.2, "ladder_w": 0.2,   # trend = 涨跌归一*0.6 + 涨停*0.2 + 梯队*0.2
                    "base_board": {0: 0.10, 1: 0.50, 2: 0.75, 3: 0.88, 4: 1.00},  # 最高连板->基础分
                    "tier_bonus": 0.06,      # 每个额外板位覆盖加分(梯队越完整越高)
                    "gap_penalty": 0.10,     # 梯队断层惩罚(有高位板缺相邻低板位)
                    "zhongjun_bonus": 0.12,  # 中军涨停加分(板块内大市值涨停股背书)
                    "zhongjun_float_yi": 100.0,  # 中军判定流通市值下限(亿)
                    "gap_from_board": 3,     # 最高连板>=此值才参与断层判定
                    "drop_confirm": 3,       # 梯队变差需连续N个快照周期确认(稳定器内生效)
                    "drop_delta": 0.25,      # ladder_score 降幅>=此值判定为梯队变差
                },
                "style": {                   # 全局大小盘风格偏转(判定逻辑已固化在 mainline.market_style_bias)
                    "sort_bias_thresh": 3.0, # 同池板块分数差<=此值才启用风格偏转排序
                },
            },
            # ---- 第四轮:后台定时轮询(独立于网页访问,推进平滑与N周期确认) ----
            "poll_interval_sec": 300,       # 稳定器后台轮询间隔(秒); 0=关闭后台轮询(退回"仅访问时推进")
            "poll_trading_hours_only": True,  # 仅交易时段(工作日 9:30-11:30 / 13:00-15:00)轮询,省接口调用
        },
        # 第二层 资金面打分(升级1:净流入率公平性修正)
        "fund": {
            "admission_enabled": True,  # 5日资金准入门槛(一票否决)
            "admission_blend": True,    # True=按(当日,5日)动态权重综合判定准入, 放行当日强回流反转板块; False=只看5日累计
            "admission_net_5d_min": 0.0,  # 综合资金净流入<=此值 剔除(原:仅5日累计)
            "admission_min_pct_5d": 0.0,  # 综合涨幅<=此值 视为量价背离剔除(原:仅5日累计涨幅)
            "use_net_rate": True,         # True=净流入率打分(公平性), False=绝对金额(旧模式)
            "rate_denom": "flow_sum",     # 净流入率分母:flow_sum(流入+流出) | amount(板块总成交额,数据缺失时回退flow_sum)
            "out_field": True,            # 输出5日资金状态/当日净流入率/资金排名
            "status_thresholds": {        # 5日资金状态判定
                "sustain": 0.0,           # 5日+单日均流入 -> 持续流入
            },
        },
        # 第二层 板块属性池(先分池,再池内分级,禁止跨池对比)
        "pool": {
            "aggressive_kw": [  # 进攻属性池关键词(命中其一即归进攻)
                "CRO", "创新药", "生物", "医疗", "半导体", "芯片", "集成电路",
                "AI", "人工智能", "算力", "计算机", "软件", "数据", "通信",
                "机器人", "高端装备", "新能源", "光伏", "储能", "锂电", "军工",
                "数字经济", "消费电子", "光模块", "PCB", "半导体设备",
            ],
            "defensive_kw": [  # 防御属性池关键词(命中其一即归防御)
                "黄金", "贵金属", "消费", "白酒", "食品", "煤炭", "电力",
                "公用事业", "银行", "保险", "医药商业", "中药", "家用电器",
                "农林牧渔", "化工", "钢铁", "有色金属", "快递物流",
            ],
            "default": "aggressive",  # 未命中任何关键词的板块归属(default: aggressive|defensive)
        },
        # 第二层 主线分级强制校验(防倒挂)
        "mainline_check": {
            "enforce": True,   # 观察池任一板块得分不得高于防御备选
            "fallback": "watch",  # 触发倒挂时的处理:调整标签并标注
        },
        # 第三层 板块-标的信号修正
        "signal": {
            "enabled": True,          # 总开关
            "levels": {               # 统一 5 档操作信号口径(全系统统一命名)
                "dip_buy": "关注低吸", "break": "突破跟进", "hold": "持有观察",
                "reduce": "减仓兑现", "wait": "观望",
            },
            "sector_boost": {          # 板块等级修正系数(核心主攻上修1档)
                "core": 1, "defensive": 0, "watch": 0, "rejected": 0,
            },
            "low_pos_ret3d": 0.05,     # 近3日涨幅<此值 视为低位启动(上修信号)
            "high_pos_ret3d": 0.15,    # 近3日涨幅>=此值 视为短期高位(下修信号)
            "note": True,              # 输出信号修正说明
        },
        # 第四层 不同风险偏好的单笔风险系数(占总资金)
        "risk": {"conservative": 0.01, "balanced": 0.015, "aggressive": 0.02},
        # 第四层 分批建仓比例
        "batch": {"first": 0.60, "second": 0.40},
        # 第四层 执行参数计算规则
        "plan": {
            "mode": "auto",            # 分批模式:auto|pullback(回踩低吸)|breakout(突破跟进)
            "pullback": {              # 回踩低吸模式:分批价位逐级降低
                "first_line": "ma5",   # 第一批仓位挂单线(5日线附近=支撑上沿)
                "second_line": "ma10", # 第二批仓位挂单线(10日线附近=支撑下沿)
            },
            "breakout": {              # 突破跟进模式:分批价位逐级抬高
                "first_line": "resistance",  # 第一批=压力位突破价
                "second_line": "confirm",    # 第二批=突破后回踩确认价
            },
            "target1_atr_mult": 0.5,   # 第一目标价 = 现价 x (1 + 0.5xATR)
            "target1_min_gain": 0.03,  # 第一目标价至少高于买入价 3%
            "pullback_span_max": 0.08, # 回踩区间(5日线~10日线)跨度上限 8%
            "position_check_tol": 0.05 # 仓位股数自洽校验偏差上限 5%
        },
        # 第四层 动态仓位矩阵(市场评级 x 标的类型),enabled=False 时回退固定 single_pct
        "position_matrix": {
            "enabled": True,           # 总开关:关闭则回退 risk.single_pct 固定单票仓位
            "cap": {                   # 市场评级 x 标的类型 -> 单标的总仓位上限(占总资金)
                "A": {"mood": 0.08, "mid": 0.12, "etf": 0.15, "def_etf": 0.10},
                "B": {"mood": 0.05, "mid": 0.08, "etf": 0.10, "def_etf": 0.08},
                "C": {"mood": 0.00, "mid": 0.03, "etf": 0.05, "def_etf": 0.10},
                "D": {"mood": 0.00, "mid": 0.00, "etf": 0.00, "def_etf": 0.00},
            },
            "sector_cap": {            # 单板块总仓位上限(B级基准),enforce 开启时自动压缩
                "A": 0.30, "B": 0.20, "C": 0.15, "D": 0.10,
            },
            "enforce": True,           # 超过单板块上限时自动压缩并给出预警
        },
        # 第三层 板块性价比维度(避免无脑追高)
        "value": {
            "enabled": True,           # 总开关
            "pos": {                   # 位置评级阈值
                "low_gain3": 0.05,     # 近3日涨幅<此值 视为低位
                "mid_gain3": 0.10,     # 近3日涨幅>=此值 视为短期高位
                "low_dd20": 0.10,      # 相对20日高点回撤>此值 视为低位(低位启动需回撤深)
                "mid_dd20": 0.05,      # 回撤>=此值 视为中位(高位需回撤浅)
            },
            "profit_ratio": {          # 短期盈亏比分档
                "high": 1.5,           # >1.5 高性价比
                "mid": 1.0,            # 1~1.5 中等;<1 追高风险
            },
            "note": True,              # 入选理由输出定性结论(结论+数据支撑)
        },
        # 第四层 触发条件量化(可执行)
        "trigger": {
            "enabled": True,           # 总开关
            "shrink": {                # 缩量企稳定义(5分钟K线)
                "band": 0.01,          # 价格落在支撑位±1%区间内
                "bars": 3,             # 连续3根5分钟K线
                "vol_ratio": 0.80,     # 成交额低于日内均值80%
            },
            "breakout": {              # 有效突破定义
                "above_minutes": 5,    # 站稳压力位上方>5分钟
                "vol_mult": 2.0,       # 对应5分钟成交额是前30分钟均量2倍以上
            },
            "minute_period": "5",      # 分钟K线周期(5分钟)
        },
        # 大盘打分成交额满分基准(亿元)
        "min_amount_yi": 10000.0,
    },
    # ---- 第六轮:标的精准匹配优化(外挂模块,全部默认关闭,关闭时输出与原始逻辑100%一致)
    "target_match": {
        "enable_target_stabilizer": True,  # P0.1 标的驻留防抖:连续N周期前2才晋升正式,避免盘中频繁切换
        "enable_tradable_filter": True,    # P0.2 可交易性基础过滤:一字板/停牌/次新/流动性/溢价剔除
        "enable_advanced_rank": True,      # P1 分档选股升级:情绪龙头用情绪综合得分,中军用中军属性得分
        "enable_excess_return_adjust": True,  # P2.1 个股超额收益修正:持续跑赢/跑输板块调整动作优先级
        "enable_sector_boost_stable": True,   # P2.2 板块溢价联动防抖:仅正式core/defensive给板块溢价,候选/观察不给
        "enable_fallback_match": True,        # P2.3 匹配失败降级兜底:档位内补选->跨档位->关联板块->error
        "stabilizer": {                        # P0.1 参数
            "TARGET_STABILIZE_CYCLE": 3,       # 连续N个快照周期保持前2才晋升正式推荐
            "TARGET_COOLDOWN_MINUTE": 15,      # 被剔除正式推荐后的冷却分钟数
            "TARGET_KEEP_RANK": 5,             # 正式标的跌出前2但仍在前5内,暂不剔除
        },
        "tradable_filter": {                   # P0.2 参数
            "min_list_days": 60,               # 上市天数<此值视为次新股剔除
            "aggressive_min_avg_amount": 30000000,  # 情绪龙头20日日均成交额下限(元)
            "steady_min_avg_amount": 100000000,     # 中军龙头20日日均成交额下限(元)
            "etf_min_avg_amount": 80000000,         # ETF 20日日均成交额下限(元)
            "etf_max_premium": 0.005,               # ETF 场内溢价率上限(5%)
        },
        "advanced_rank": {                   # P1 权重(维度缺失时自动归一化到可用维度)
            "aggressive_weights": {
                "ladder": 0.4, "pct_chg": 0.3, "correlation": 0.2, "amount": 0.1,
            },
            "steady_weights": {
                "market_cap": 0.4, "avg_amount": 0.3, "trend": 0.2, "amount": 0.1,
            },
        },
    },
    # ---- 大模型文案(可选接入,OpenAI 兼容接口)
    "llm": {
        "enable": False,             # 总开关:关闭时报告用规则话术兜底
        "base_url": "https://api.openai.com/v1",
        "api_key": "",               # 密钥(仅本地保存,不出网)
        "model": "gpt-4o-mini",
        "timeout": 60,               # 请求超时(秒)
        "max_tokens": 6000,          # 生成长度上限(推理类模型需预留推理预算,过低会导致只思考不出文)
    },
    # ---- 合规话术(固定,不可编辑)
    "disclaimer": "以上内容为辅助决策参考,不构成投资建议。股市有风险,入市需谨慎。",
    # ---- 复盘报告调度(模块3)
    "auto_report_time": "16:00",     # 交易日到点自动生成复盘(收盘后,确保最终收盘数据)
    "need_save_report": False,       # 自动调度是否落盘 Markdown 文件(页面内展示不受影响)
    # ---- 同花顺金融数据 API(fuyao.aicubes.cn,可选兜底/替换源)
    "fuyao": {
        "enabled": False,            # 是否启用(需已配置 api_key)
        "api_key": "",               # 密钥(仅本地保存,不出网)
        "base_url": "https://fuyao.aicubes.cn",
        "ttl": 3600,                 # 数据内存缓存(秒)
        "qps_gap": 0.3,              # 单次请求最小间隔(秒),规避 4001 频率超限
    },
    # ---- 第三轮:前端体验优化开关(Web 展示/交互,不影响任何计算逻辑)
    "web_ui": {
        "conclusion_bar": True,     # 优化项1:顶部极简结论卡
        "yesterday_review": True,   # 优化项2:底部昨日信号复盘
        "rejected_collapse": True,  # 优化项3a:淘汰板块默认折叠
        "target_tabs": True,        # 优化项3b:标的匹配 Tab 切换
        "mood_risk_tag": True,      # 优化项3c:情绪龙头高波动风险标签
        "delta_arrows": True,       # 优化项3d:数值字段环比箭头
        "sector_detail": True,      # 优化项4:板块详情下钻弹窗
        # ---- 第四轮:防抖稳定器前端展示 ----
        "candidate_list": True,     # 稳定器异动候选列表(candidate)
        "raw_debug": False,         # 原始未防抖信号(raw)调试展示(默认折叠)
    },
}


def load() -> dict:
    """读取生效配置(默认值 + json 覆盖)。"""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            over = json.load(f)
        _deep_update(cfg, over)
    except (OSError, ValueError):
        pass
    return cfg


def save(over: dict) -> None:
    """合并覆盖并持久化(仅写入存在键,其余用默认值补齐)。"""
    cfg = load()
    _deep_update(cfg, over)
    try:
        os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise RuntimeError(f"设置保存失败: {e}")


def reset() -> None:
    try:
        if os.path.exists(_SETTINGS_PATH):
            os.remove(_SETTINGS_PATH)
    except OSError:
        pass


def _deep_update(base: dict, patch: dict) -> None:
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
