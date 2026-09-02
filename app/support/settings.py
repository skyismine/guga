"""可配置设置:预警规则 / 风控阈值 / 打分权重 / 持仓路径。

所有参数内置默认值,Web「系统设置」页可改写并持久化到 data_cache/settings.json,
运行时以 json 覆盖默认值为准(缺失字段回退默认)。
"""
import datetime as dt
import json
import os
import shutil

from app import config

_SETTINGS_PATH = os.path.join(config.DATA_DIR, "settings.json")
_META_FILE = os.path.join(config.DATA_DIR, "settings.meta.json")
# 环境隔离: GUGA_ENV=dev|test|prod; 非 dev 时叠加 settings.{env}.json 覆盖
_ENV = os.environ.get("GUGA_ENV", "dev")
_ENV_OVERRIDE_FILE = os.path.join(config.DATA_DIR, f"settings.{_ENV}.json")
# 版本与最近有效配置(供非法配置回退)
_VERSION = 1
_LAST_VALID = None

# 6.3 配置校验规则: 路径 -> (类型, 范围)
_VALIDATE_RULES = {
    "decision.total_asset": ("number", 1000, 1e12),
    "decision.taste": ("choices", ["conservative", "balanced", "aggressive"]),
    "decision.market.score_full": ("number", 0, 100),
    "decision.market.score_ok": ("number", 0, 100),
    "decision.market.score_hold": ("number", 0, 100),
    "decision.mainline.STABILIZE_CYCLE": ("number", 1, 10),
    "decision.mainline.COOLDOWN_MINUTE": ("number", 0, 120),
    "decision.signal.max_delta": ("number", 0, 5),
    "decision.exec_param.concentration.single_sector": ("number", 0, 1),
    "decision.exec_param.risk_budget.total_pct": ("number", 0, 1),
    "decision.exec_param.risk_budget.single_pct": ("number", 0, 0.1),
    "target_match.candidate_pool_size": ("number", 1, 20),
    "target_match.model_monitor.accuracy_threshold": ("number", 0, 1),
    "etf_min_amount": ("number", 0, 1e9),
    "mainline_top_n": ("number", 1, 10),
    "monitor.refresh_sec": ("number", 10, 3600),
    "score_weights.capital": ("number", 0, 100),
    "score_weights.trend": ("number", 0, 100),
    "llm.enable": ("bool",),
    "fuyao.enabled": ("bool",),
    "web_ui.conclusion_bar": ("bool",),
}
# 敏感信息环境变量覆盖(不落盘)
_ENV_MAP = {
    "FUYAO_API_KEY": ("fuyao", "api_key"),
    "LLM_API_KEY": ("llm", "api_key"),
    "GUGA_ALERT_WEBHOOK": ("alert", "webhook"),
}


def _validate(cfg: dict) -> tuple:
    """类型 + 范围校验。返回 (ok, errors)。"""
    errors = []
    for path, spec in _VALIDATE_RULES.items():
        node = cfg
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is None:
            continue
        want = spec[0]
        if want == "bool":
            if not isinstance(node, bool):
                errors.append(f"{path}: 应为布尔,实际 {type(node).__name__}")
        elif want == "number":
            if not isinstance(node, (int, float)):
                errors.append(f"{path}: 应为数值,实际 {type(node).__name__}")
                continue
            if spec[1] is not None and node < spec[1]:
                errors.append(f"{path}: {node} < 下限 {spec[1]}")
            if spec[2] is not None and node > spec[2]:
                errors.append(f"{path}: {node} > 上限 {spec[2]}")
        elif want == "choices":
            if node not in spec[1]:
                errors.append(f"{path}: 非法取值 {node}")
    return (not errors), errors


def _env_overrides(cfg: dict) -> None:
    for env, (a, b) in _ENV_MAP.items():
        v = os.environ.get(env)
        if v:
            cfg.setdefault(a, {})[b] = v


def _read_meta() -> dict:
    try:
        with open(_META_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"version": 1, "updated_at": None, "env": _ENV}


def load() -> dict:
    """读取生效配置(默认 + json 覆盖 + 环境覆盖 + 敏感信息env)。校验失败回退上一版本。"""
    global _LAST_VALID
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            over = json.load(f)
        over.pop("_meta", None)
        _deep_update(cfg, over)
        if _ENV != "dev" and os.path.exists(_ENV_OVERRIDE_FILE):
            with open(_ENV_OVERRIDE_FILE, encoding="utf-8") as f:
                _deep_update(cfg, json.load(f))
        _env_overrides(cfg)
    except (OSError, ValueError):
        _env_overrides(cfg)
    ok, errors = _validate(cfg)
    if not ok:
        # 非法配置拒绝加载: 回退最近有效配置 + 告警
        if _LAST_VALID is not None:
            from app.support import fault as _flt
            _flt.warning("settings", "配置校验失败,回退上一版本", context={"errors": errors[:6]})
            return json.loads(json.dumps(_LAST_VALID))
    else:
        _LAST_VALID = json.loads(json.dumps(cfg))
    return cfg


def save(over: dict) -> None:
    """合并覆盖并持久化(校验通过才落盘, 保存前版本化备份)。"""
    global _LAST_VALID, _VERSION
    cfg = load()
    _deep_update(cfg, over)
    ok, errors = _validate(cfg)
    if not ok:
        raise RuntimeError(f"配置校验失败,拒绝保存: {'; '.join(errors[:6])}")
    os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
    meta = _read_meta()
    _VERSION = int(meta.get("version", 1)) + 1
    if os.path.exists(_SETTINGS_PATH):
        try:
            shutil.copy(_SETTINGS_PATH, os.path.join(
                config.DATA_DIR, f"settings.{_VERSION - 1}.bak.json"))
        except OSError:
            pass
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    with open(_META_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": _VERSION, "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "env": _ENV}, f, ensure_ascii=False, indent=2)
    _LAST_VALID = json.loads(json.dumps(cfg))


def rollback() -> dict:
    """回滚到上一版本配置(settings.{version-1}.bak.json)。"""
    meta = _read_meta()
    prev = max(0, int(meta.get("version", 1)) - 1)
    bak = os.path.join(config.DATA_DIR, f"settings.{prev}.bak.json")
    if not os.path.exists(bak):
        from app.support import fault as _flt
        _flt.warning("settings", "回滚失败: 无上一版本备份", context={"version": prev})
        return load()
    shutil.copy(bak, _SETTINGS_PATH)
    with open(_META_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": prev, "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                   "env": _ENV}, f, ensure_ascii=False, indent=2)
    return load()


def config_info() -> dict:
    """配置版本 / 环境 / 校验状态 / 备份清单。"""
    meta = _read_meta()
    baks = sorted(f for f in os.listdir(config.DATA_DIR)
                  if f.startswith("settings.") and f.endswith(".bak.json"))
    ok, errors = _validate(load())
    return {"version": meta.get("version", 1), "updated_at": meta.get("updated_at"),
            "env": _ENV, "env_override": os.path.exists(_ENV_OVERRIDE_FILE),
            "valid": ok, "validation_errors": errors[:8],
            "backups": [f for f in baks[-10:]]}


def reset() -> None:
    global _LAST_VALID
    _LAST_VALID = None
    try:
        if os.path.exists(_SETTINGS_PATH):
            os.remove(_SETTINGS_PATH)
    except OSError:
        pass


DEFAULTS = {
    # ---- 模块1 主线板块打分权重(基础合计 100; heat 为可选独立催化维度,不占基础分)
    "score_weights": {"capital": 40, "trend": 30, "sentiment": 20, "news": 10,
                      "heat_enabled": True, "heat": 3},
    "mainline_top_n": 2,          # Top-N 为核心主线
    "mainline_branch_top_n": 5,   # 前 N 名(含主线)标注补涨支线
    "mainline_dynamic_weight": True,  # 资金面动态权重开关:A级 5日20%/单日80%, C/D级 5日70%/单日30%
    "mainline_fund_mode": "net_rate",  # 资金打分口径:net_rate(净流入率排名) | absolute(绝对金额排名,旧模式)
    "leader_min_market_cap": 20.0,   # 情绪龙头剔除 <20 亿小票(亿)
    "leader_exclude": ["ST", "退"],  # 龙头剔除名称关键词
    "etf_min_amount": 5000.0,        # ETF 日均成交额下限(万元)
    # ---- 6.2 告警(webhook, 可用 GUGA_ALERT_WEBHOOK 环境变量覆盖, 不落盘密钥)
    "alert": {"webhook": ""},
    # ---- 6.1 熔断/日志参数(替代 fault.py 硬编码魔法数字)
    "system": {
        "cb_fail_threshold": 5,      # 模块连续失败次数触发熔断
        "cb_open_minutes": 10,       # 熔断时长(分钟)
        "global_error_rate": 0.30,   # 全局错误率阈值(触发全局熔断)
        "global_window": 30,         # 全局错误率滚动窗口
        "log_keep_days": 30,         # 日志保留天数
    },
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
        # ---- 2.4 评级滞回参数(可配置: 震荡市可加宽±3/趋势市收窄±1)
        "grade_hysteresis": {
            "up_bias": 5.0,          # 升级需更严(score+此值)
            "down_bias": 5.0,        # 降级需更松(score-此值)
            "confirm": 2,            # 盘中切换连续确认次数(5分钟窗口)
            "max_step": 1,           # 单日评级最多变化级数
            "crash_score_drop": 10.0,  # 熔断式降级: 评分较当前评级门槛骤降超此值→立即降级(跳过确认)
        },
        # 第二层 主线一票否决
        "veto": {
            "max_gain_3d": 0.15,        # 近3日累计涨幅>=此值 过热(现为扣分,阈值)
            "min_zt_in_sector": 2,      # 板块内涨停家数 <此值 扣分
            "bad_news_kw": ["立案", "处罚", "退市", "预亏", "大幅减持",
                            "质押平仓", "风险警示", "违规"],  # 名称/领涨股利空关键词(一票否决)
            "data_missing": True,       # 板块统计严重缺失 一票否决(3.1 保留项)
        },
        # ---- 3.1 一票否决改分级扣分(软性项不再直接否决) ----
        "veto_penalty": {
            "net_out": {"enabled": True, "max_pts": 10.0},   # 净流出按占成交额比例扣分(0-10)
            "zt_short": {"enabled": True, "per_missing": 5.0,  # 涨停不足min_zt: 每缺1家扣5分
                         "trend_exempt_pct": 3.0,   # 无涨停但有趋势豁免: 板块涨幅>此值(3%)
                         "trend_exempt_leader": 5.0, # 且领涨股涨幅>此值(5%)
                         "trend_exempt_pts": 3.0},   # 豁免时仅扣此分(替代缺家扣分, 防误杀大金融/消费趋势板块)
            "overheat": {"enabled": True, "threshold": 0.15, "max_pts": 10.0},  # 过热按超出幅度扣分
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
            "hyst_phase_span": 3.0,         # 滞回门槛随 stab_cycle_adj 阶段化: 每单位系数调整跨度(分)
                                            # 冷却时间 = base × stab_cycle_adj(退潮更久/主升更短)
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
            # ---- 3.2 准入线动态调整(板块历史分位 + 波动率) ----
            "admission_adj": {
                "enabled": True,
                "pct_up": 0.80, "pct_down": 0.20,   # 当前评分历史分位阈值(80%分位以上下调/20%以下上调)
                "pct_up_mid": 0.60, "pct_down_mid": 0.40,  # 中间分档阈值(60-80/20-40)
                "pct_pts": 5.0, "pct_mid_pts": 2.0,  # 分位调整幅度(极值/中间档)
                "vol_high": 10.0, "vol_low": 5.0,   # 评分波动率(60日标准差)阈值
                "vol_pts": 3.0,                     # 波动率调整幅度(分)
                "defensive_adj": 2.0,               # 防御属性板块准入线下调幅度(评分天然偏低)
                "max_adj": 10.0,                    # 准入线总浮动上限(分)
                "min_hist": 10,                     # 最少历史样本(不足不调整)
            },
            # ---- 3.3 新主线超越幅度(阶段化,配合 stab_cycle_adj 驻留周期) ----
            "lead_margin": {"retreat": 10.0, "startup": 6.0, "main": 5.0, "climax": 8.0},
            "confidence": {"base": 0.3, "cycle_w": 0.4, "margin_w": 0.3},  # 稳定器置信度权重
            # ---- 3.4 风格偏转分数微调(从"只调排序"升级为"分数微调") ----
            "style_adj": {
                "enabled": True, "min_pts": 1.0, "max_pts": 3.0,
                "max_total": 5.0, "low_conf_scale": 0.5,
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
            # ---- 4.4 多维信号修正(累计±3档上限) ----
            "phase_adjust": {          # 市场阶段修正
                "retreat": -1, "startup": 0, "main": 0, "climax": 0,
                "main_core_boost": 1,  # 主升期核心板块额外上修1档
            },
            "vol_price": {"enabled": True, "up_vol": 1, "up_shrink": -1, "down_vol": -2},
            "technical": {"enabled": True, "break_up": 1, "break_down": -2,
                          "near_pct": 0.02,        # 支撑/压力"临近"判定带宽
                          "near_support": 1,       # 回踩支撑位附近 → 低吸机会(上修1)
                          "near_resistance": -1},  # 逼近压力位 → 突破不确定(下修1)
            "tech_signal": {"enabled": True,       # 技术指标形态修正(RSI/MACD/KDJ/BB → 信号层)
                            "oversold_gold": 1,    # 超卖+金叉 → 关注低吸(上修1)
                            "overbought_dead": -1, # 超买+死叉 → 兑现(下修1)
                            "bb_over": -1,         # 突破布林上轨 → 超买回归(下修1)
                            "bb_under": 1},        # 跌破布林下轨 → 超卖反弹(上修1)
            "fund_flow": {"enabled": True, "up": 1, "down": -1},   # 近3日资金连续流入/流出(数据缺失跳过)
            "max_delta": 3,            # 单标的信号累计上修/下修不超过3档
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
        # ---- 第四层 执行参数优化(5.1-5.4) ----
        "exec_param": {
            "stop": {                  # 5.1 分标的类型止损(ATR 倍数 / 现价比例回退)
                "mood": {"atr": 2.0, "pct": 0.08},
                "mid": {"atr": 1.5, "pct": 0.05},
                "etf": {"atr": 1.0, "pct": 0.03},
                "def_etf": {"atr": 1.0, "pct": 0.03},
                "repair": {"atr": 1.2, "pct": 0.04},
            },
            "dynamic_stop": {          # 5.1 动态止损阶梯(浮盈阈值)
                "enabled": True,
                "breakeven_pct": 0.05,  # 浮盈5% → 保本止损
                "lock_pct": 0.10,       # 浮盈10% → 止损上移至浮盈5%
                "trail_pct": 0.20,      # 浮盈20% → 跟踪止损(最高价回撤8%)
                "trail_drawdown": 0.08,
            },
            "batch_type": {            # 5.2 分类型分批比例(可三批)
                "mood": [0.40, 0.30, 0.30],
                "mid": [0.50, 0.50],
                "etf": [0.70, 0.30],
                "def_etf": [0.70, 0.30],
                "repair": [0.30, 0.30, 0.40],
            },
            "batch_phase_first": {     # 5.2 分阶段首批比例系数(退潮降/主升提/高潮禁)
                "retreat": 0.5, "startup": 1.0, "main": 1.2, "climax": 0.0,
            },
            "target": {                # 5.3 分类型目标价(ATR1倍数 / t2 / t3 来源)
                "mood": {"atr1": 1.0, "t2": "prev_high", "t3": "hist_high"},
                "mid": {"atr1": 0.5, "t2": "res20", "t3": "hist_high"},
                "etf": {"atr1": 0.3, "t2": "prev_high", "t3": "year_high"},
                "def_etf": {"atr1": 0.3, "t2": "prev_high", "t3": "year_high"},
                "repair": {"atr1": 0.8, "t2": "prev_high", "t3": None},
            },
            "target_dynamic": {        # 5.3 动态目标:评级上/下调±5%, 达标后目标2上移×1.05
                "grade_up": 1.05, "grade_down": 0.95, "run_mult": 1.05,
            },
            "concentration": {         # 5.4 板块集中度
                "enabled": True,
                "single_sector": 0.30, "single_sector_main": 0.40,
                "top2_total": 0.50, "chain_total": 0.40, "chain_sectors": [],
            },
            "correlation": {           # 5.4 相关性限制
                "enabled": True, "corr_high": 0.8, "sum_mult": 1.5,
                "corr_new": 0.9, "new_halve": True, "window_days": 30,
            },
            "risk_budget": {           # 5.4 风险预算
                "enabled": True, "total_pct": 0.05, "single_pct": 0.015,
            },
            "pos_cutoff": {            # 高位回落位置修正阈值(近3日涨幅超此值下修)
                "steady": 0.18, "aggressive": 0.12, "repair": 0.10,
            },
            "vol_ratio": {             # 大盘量能比平滑参数(2.3): EWMA α + 异常值截断(Winsorize)
                "ewma_alpha": 0.5,     # EWMA 平滑系数(越大对新变化越敏感)
                "clip_lo": 0.3,        # 原始量能比截断下限
                "clip_hi": 3.0,        # 原始量能比截断上限(冷启动折算/数据毛刺防污染 EWMA)
            },
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
            # ---- 4.1 候选筛选增强 ----
            "min_avg_amount_base": 50000000,   # 全档位基础流动性下限(元,日均成交额<此值剔除)
            "exclude_kw": ["ST", "退市", "风险警示", "*"],   # 名称剔除关键词(ST/退市/风险警示)
            "bad_news_kw": ["立案", "处罚", "退市", "预亏", "大幅减持", "质押平仓"],  # 利空剔除
        },
        "advanced_rank": {                   # P1 权重(维度缺失时自动归一化到可用维度)
            "aggressive_weights": {
                "ladder": 0.4, "pct_chg": 0.3, "correlation": 0.2, "amount": 0.1,
            },
            "steady_weights": {
                "market_cap": 0.4, "avg_amount": 0.3, "trend": 0.2, "amount": 0.1,
            },
        },
        # ---- 4.1 候选池规模(每个角色初始候选数) ----
        "candidate_pool_size": 5,
        # ---- 4.2 分模式盈亏比双口径(短5日/中20日) ----
        "trade_rr_dual": {"enabled": True, "atr_mult": 1.0, "rr5_res_bias": 0.02},
        # ---- 4.3 模型集成投票 + 性能监控(开关默认开启,叠加注解不改变GBM主判) ----
        "enable_model_ensemble": True,       # 技术指标+市场环境 与 GBM 方向一致性投票(输出注解/轻度调权)
        "model_monitor": {
            "enabled": True, "accuracy_threshold": 0.55,  # 准确率<此值告警暂停参考
            "window_days": 60, "min_samples": 20,
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
    # ---- 账户与交易纪律(复盘「持仓/合规/逐仓方案」用)
    "account": {
        "initial_capital": None,     # 初始总资产/本金(元);缺省用今日决策 decision.total_asset 作本金
        "total_asset": None,         # 兼容旧配置:作为本金兜底(优先级低于 initial_capital)
        "available_cash": None,      # 可用资金(元);缺省 = 总资产 - 持仓市值(由账户模型计算)
    },
    "discipline": {
        "no_new_position": False,    # 默认不开新仓(今日买入非既有持仓=违规)
        "chase_pct": 5.0,            # 追高阈值:买入价较昨收涨幅超此值=追高违规(%)
        "single_cap": 0.02,          # 单票仓位红线(市值/总资产)
        "sector_cap": 0.05,          # 板块合计仓位红线(可选)
        "half_hour_stop": True,      # 破位半小时止损纪律(报告文案)
        "rules": [                   # 统一交易纪律章节(可增删)
            "主线优先级:非主线一律只减不加,不盲目追高加仓",
            "仓位红线:单票≤2%,板块合计≤5%,任何情况不超仓",
            "止损纪律:跌破支撑位半小时不收回,立刻止损/减仓,不硬扛不补仓",
            "盈亏比要求:新开仓盈亏比≥2:1,不符合宁可不做",
            "数据准确性:复盘输出前核实官方收盘数据",
        ],
    },
    # ---- 同花顺金融数据 API(fuyao.aicubes.cn,可选兜底/替换源)
    "fuyao": {
        "enabled": False,            # 是否启用(需已配置 api_key)
        "api_key": "",               # 密钥(仅本地保存,不出网)
        "base_url": "https://fuyao.aicubes.cn",
        "ttl": 3600,                 # 数据内存缓存(秒)
        "qps_gap": 0.3,              # 单次请求最小间隔(秒),规避 4001 频率超限
        "cooldown_sec": 60,          # 429/4001 限流后的冷却期(秒),期内快速失败不再发请求
        "news_heat_supplement": True,  # 财联社/东财新闻缺失时,用同花顺热榜/飙升榜/异动补充消息催化因子
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


def _deep_update(base: dict, patch: dict) -> None:
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
