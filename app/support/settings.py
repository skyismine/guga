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
    "leader_min_market_cap": 20.0,   # 情绪龙头剔除 <20 亿小票(亿)
    "leader_exclude": ["ST", "退"],  # 龙头剔除名称关键词
    "etf_min_amount": 5000.0,        # ETF 日均成交额下限(万元)
    "oversold_pool_size": 15,        # 超跌强承接池数量
    # ---- 模块1 超跌强承接筛选
    "oversold": {"drop_30d": 0.30, "vol_ratio": 1.5, "max_atr_pct": 0.07},
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
            "weights": {"mood": 40, "breadth": 25, "zt": 20, "amount": 15},  # 大盘打分权重(合计100)
        },
        # 第二层 主线一票否决
        "veto": {
            "max_gain_3d": 0.15,        # 近3日累计涨幅>=此值 淘汰
            "min_zt_in_sector": 2,      # 板块内涨停家数 <此值 淘汰
            "bad_news_kw": ["立案", "处罚", "退市", "预亏", "大幅减持",
                            "质押平仓", "风险警示", "违规"],  # 名称/领涨股利空关键词
        },
        # 第二层 主线准入与分级
        "mainline": {"pass_score": 60.0, "core_n": 1, "defensive_n": 1, "watch_n": 3},
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
        # 大盘打分成交额满分基准(亿元)
        "min_amount_yi": 10000.0,
    },
    # ---- 大模型文案(可选接入,OpenAI 兼容接口)
    "llm": {
        "enable": False,             # 总开关:关闭时报告用规则话术兜底
        "base_url": "https://api.openai.com/v1",
        "api_key": "",               # 密钥(仅本地保存,不出网)
        "model": "gpt-4o-mini",
        "timeout": 60,               # 请求超时(秒)
        "max_tokens": 1500,          # 生成长度上限
    },
    # ---- 合规话术(固定,不可编辑)
    "disclaimer": "以上内容为辅助决策参考,不构成投资建议。股市有风险,入市需谨慎。",
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
