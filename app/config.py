"""全局配置:路径、预测参数、交易规则。

基于 SilverQuant 框架改造:本项目在其组件化思想上新增
Akshare 数据层 / VectorBT 特征层 / LightGBM 预测引擎。
"""

import os

# ---------------------------------------------------------------- 路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data_cache")
HIST_DIR = os.path.join(DATA_DIR, "hist")          # 日线缓存 (pickle)
MODEL_DIR = os.path.join(DATA_DIR, "models")       # 训练好的模型
REPORT_DIR = os.path.join(DATA_DIR, "reports")     # 报告/图表

for _d in (DATA_DIR, HIST_DIR, MODEL_DIR, REPORT_DIR):
    os.makedirs(_d, exist_ok=True)

# 缓存 TTL:日内行情刷新用 4 小时;历史数据 1 天
CACHE_TTL_SECONDS = 4 * 3600

# 概念成分按日重抓:每日收盘后自动重抓同花顺概念成分映射(概念增减个股自动同步)
CONCEPT_REFRESH_DAILY = True

# ---------------------------------------------------------------- 数据
HIST_DAYS = 600            # 训练/分析使用的历史交易日数量
MIN_HIST_DAYS = 120        # 少于该数量无法计算特征

# 数据源优先级:新浪(稳定) -> 东财(需补丁,偶发限流) -> 腾讯
DATA_SOURCE_ORDER = ("sina", "eastmoney", "tencent")

# ---------------------------------------------------------------- ETF 支持
ETF_PREFIXES = ("51", "52", "56", "58", "15", "16", "18")   # 沪:51/52/56/58, 深:15/16/18
ETF_DATA_SOURCE_ORDER = ("etf_sina", "etf_em")   # ETF 专用数据源(新浪基金为主,东财回退)
ETF_SAMPLE_CODES = [        # 流动性好的样本 ETF(跨宽基/行业/主题)
    "510300",   # 沪深300ETF
    "510500",   # 中证500ETF
    "588000",   # 科创50ETF
    "159915",   # 创业板ETF
    "510050",   # 上证50ETF
    "512880",   # 证券ETF
    "513100",   # 纳指ETF
    "518880",   # 黄金ETF
]

# ---------------------------------------------------------------- 预测
PREDICT_HORIZON = 5        # 预测未来几个交易日(5 日短中期)
PREDICT_THRESHOLD = 0.015  # 涨跌幅超过该值判定为 上涨/下跌 (1.5%);启用滚动分位数后仅作早期回退

# 标签阈值:滚动分位数(替代固定百分比,自动适配个股波动率与市场环境)
# 日期 t 的阈值 = 过去 LABEL_QUANTILE_WINDOW 日内"未来 horizon 日收益"的 30%/70% 分位数
# (窗口 shift(horizon) 保证只用 t 时点已实现数据,避免前视;三类样本占比天然约 30/40/30,长期稳定)
LABEL_QUANTILE_WINDOW = 250     # 滚动窗口(交易日)
LABEL_QUANTILE_LOW = 0.30       # 下跌阈值分位数(<= 该分位 -> 下跌)
LABEL_QUANTILE_HIGH = 0.70      # 上涨阈值分位数(>= 该分位 -> 上涨)
LABEL_QUANTILE_MIN_PERIODS = 60 # 窗口最小样本数,不足则剔除

# 标签阈值模式:quantile(滚动分位数,默认) | atr(固定收益阈值 + 个股 ATR 动态调整)
# atr 模式:对每只个股,阈值 = max(FIXED, k_atr * ATR14%/close),
#          用个股波动率个性化涨跌判定(高波动股阈值放宽,低波动股收紧)。
LABEL_MODE = "quantile"
LABEL_ATR_THRESHOLD = 0.015      # atr 模式固定阈值
LABEL_ATR_K = 1.0                # ATR 倍率:阈值下限 = k_atr * atr_pct

# 剔除不可交易样本(训练样本必须真实可成交):
# 当日涨停封板(一字/T 字,买入无法成交)或跌停(卖出无法成交)剔除该样本;
# 未来 horizon 内存在停牌(交易间隔 > MAX_GAP_DAYS)的样本剔除。
DROP_LIMIT_DAYS = True           # 剔除标签日处于涨/跌停(封板)的样本
DROP_HALT_DAYS = True            # 剔除未来 horizon 内含停牌段的样本
MAX_HALT_GAP_DAYS = 15           # 交易日间隔超过该值(自然日)视为停牌

# 用于训练的全市场样本股票(代码前缀), 建议覆盖主板/创业板/科创板
TRAIN_PREFIXES = ("60", "00", "30", "68")
_STATIC_TRAIN_CODES = [        # 静态基础池(扩充池缺失时的回退)
    "600519", "601318", "600036", "601899", "600030",
    "600900", "601012", "600887", "600309", "603259",
    "000001", "000858", "000333", "000651", "002594",
    "002415", "300750", "300059", "300124", "002230",
]


def _load_expanded_codes():
    """优先加载扩充训练池(data_cache/train_pool.json,>=100 只),否则回退静态池。"""
    import json
    import os
    path = os.path.join(DATA_DIR, "train_pool.json")
    try:
        if os.path.exists(path):
            d = json.load(open(path, encoding="utf-8"))
            codes = d.get("codes") or []
            if len(codes) >= 100:
                return codes
    except (OSError, ValueError):
        pass
    return None


TRAIN_STOCK_CODES = _load_expanded_codes() or _STATIC_TRAIN_CODES
TRAIN_YEARS_BACK = 3       # 取最近 N 年历史作为训练数据

# 月度重训调度:固定月度频率,持续适配市场风格变化
# 与 trainer.py 的 walk-forward 训练逻辑直接对接(train_all)。
RETRAIN_ENABLED = True              # 是否启用自动重训
RETRAIN_INTERVAL_DAYS = 30          # 固定月度:距上次重训满 N 天即触发(默认近似自然月)
RETRAIN_DAY_OF_MONTH = 0            # >0 时按"每月 X 日"触发,0 表示用间隔天数模式
RETRAIN_CHECK_SECONDS = 3600        # daemon 检查周期(秒)
RETRAIN_WEB_AUTO = True             # Web 启动时是否自动启动重训 daemon
RETRAIN_LOG = os.path.join(DATA_DIR, "retrain.log")

# 模型
MODEL_NAME = "gbm_3class"
MIN_TRAIN_SAMPLES = 500    # 训练样本下限,不足则无法训练
TEST_RATIO = 0.2           # 按时间切分的验证集比例(single_split 模式)

# 训练验证方式:walk_forward(时间序列滚动前视) | single_split(单次时间切分)
# walk-forward:把时间轴切为 WF_K_FOLDS 段,前 WF_INITIAL_FOLDS 段作初始训练,
# 其后每段作为一次测试折(训练集随测试段推进而扩展/滚动),全部无前视,
# 逐折报告指标并取最后一折(最新时段)模型部署,更贴合实盘连续预测。
TRAIN_MODE = "walk_forward"
WF_K_FOLDS = 4              # 时间切分数(>=3,测试折数 = K - INITIAL_FOLDS)
WF_INITIAL_FOLDS = 1        # 初始训练占用的最早段数(保证首折训练样本充足)
WF_FIXED_WINDOW_DAYS = 400  # 训练用固定滚动窗口(交易日);0=expand 使用全部历史
WF_MIN_TEST_DAYS = 20       # 每折测试段最少交易日数,不足则跳过该折

# 模型验证增强(默认值与任务设定一致)
# 概率校准:对训练集内部 CV 拟合 isotonic/sigmoid 校准器,校正模型输出概率,
#   使其接近真实频率(预测概率可直接解读为涨幅置信度)。默认开启。
CALIBRATE_ENABLED = True
CALIBRATE_METHOD = "isotonic"   # isotonic | sigmoid
CALIBRATE_CV = 3                # 校准用内部交叉验证折数
# 贝叶斯调参:optuna 驱动,用时间上更早的验证段评分;未安装 optuna 时自动降级
#   为轻量随机搜索。默认关闭(为控制训练时长,月度重训保持稳定超参)。
BAYESIAN_TUNE_ENABLED = False
BAYESIAN_TUNE_TRIALS = 20       # 调参试验次数
BAYESIAN_TUNE_VERIFY_DAYS = 120 # 调参验证段长度(交易日,取测试折之前的最近 N 日)
# 分池验证:按训练池分类(core_a500/emotional/risk/large_cap)分别评估样本外指标,
#   用于观察模型在不同类型股票上的泛化差异。默认关闭(不影响部署)。
SPLIT_VALIDATION_ENABLED = False

# 样本加权:类别平衡(缓解震荡类多数类偏向) × 主线标的 1.2(增强主线股权重)。
SAMPLE_WEIGHT_ENABLED = True

# 特征筛选(相关性去冗余 + 重要性 Top-N)
FEATURE_SELECT = True               # 训练时是否使用筛选后的特征子集
FEATURE_SELECT_TOP_N = 40           # 保留的特征数量
FEATURE_CORR_THRESHOLD = 0.8        # |相关系数| 超过该值视为冗余(按重要性贪心保留)
FEATURE_SELECTED_FILE = "selected_features.json"   # 选择结果(存于 MODEL_DIR)

# 新增高级特征(主线板块动量/量价衍生/市场风格),默认开启。
ADVANCED_FEATURES_ENABLED = True

# 特征标准化(滚动 z-score,仅用历史窗口,避免未来数据)
STANDARDIZE_ROLLING = True      # 是否启用滚动 z-score 标准化
STANDARDIZE_WINDOW = 250        # 均值/标准差滚动窗口(交易日)
STANDARDIZE_MIN_PERIODS = 60    # 窗口最小样本数,不足则产生 NaN(训练时剔除)

# 预测信号转操作建议的阈值
BUY_P_UP = 0.55            # P(上涨) >= 该值 -> 考虑买入
SELL_P_DOWN = 0.55         # P(下跌) >= 该值 -> 考虑卖出
STRONG = 0.68              # 强信号阈值

# ---------------------------------------------------------------- 市场情绪(恐贪/期指资金/涨跌家数)
MARKET_INDEX = "sh000300"  # 市场情绪代理指数(沪深300)
# 涨跌家数/宽度计算池已切换为 GBM 训练池(pool_builder 统一供数,
# 见 market_features._breadth_frame),不再单独维护样本篮子。
# 期指连续合约 <-> 对应现货指数(新浪代码): IF沪深300 / IH上证50 / IC中证500 / IM中证1000
FUTURES_MAP = {
    "if": ("IF0", "sh000300"),
    "ih": ("IH0", "sh000016"),
    "ic": ("IC0", "sh000905"),
    "im": ("IM0", "sh000852"),
}
FUTURES_VARIETY = {        # futures_zh_realtime 需要的品种名称
    "if": "沪深300指数期货", "ih": "上证50指数期货",
    "ic": "中证500指数期货", "im": "中证1000股指期货",
}

# 恐贪指标区间(0=极度恐慌, 100=极度贪婪)
FG_EXTREME_FEAR = 20       # <= 该值: 极度恐惧(情绪修复机会)
FG_FEAR = 30               # <= 该值: 偏恐慌
FG_GREED = 70              # >= 该值: 偏贪婪
FG_EXTREME_GREED = 80      # >= 该值: 极度贪婪(情绪反转风险)
# 全市场涨跌家数宽度阈值(上涨家数占比;优先用乐咕全市场数据,缺失时退回样本篮子)
MARKET_BREADTH_UP = 0.60   # 上涨占比 >= 60%: 普涨
MARKET_BREADTH_DOWN = 0.40 # 上涨占比 <= 40%: 普跌
# 期指基差阈值(相对指数收盘的升贴水)
BASIS_DEEP_DISCOUNT = -0.008   # 平均基差 <= -0.8%: 深贴水,机构偏空/对冲盘重
BASIS_PREMIUM = 0.003          # 平均基差 >= +0.3%: 升水,资金偏多
BASIS_IM_DISCOUNT = -0.015     # 中证1000基差 <= -1.5%: 小盘避险情绪
# 单股 ATR 波动区间(占股价比例)判定
ATR_HIGH_PCT = 0.040       # ATR/价 >= 4%: 高波动,建议降仓
ATR_LOW_PCT = 0.015        # ATR/价 <= 1.5%: 低波动

# ---------------------------------------------------------------- 交易规则 (A 股)
LOT_SIZE = 100             # 一手 = 100 股
COMMISSION = 0.00025       # 佣金(双边)
MIN_COMMISSION = 5.0       # 最低佣金
STAMP_TAX = 0.0005         # 印花税(仅卖出)
SLIPPAGE = 0.0005          # 滑点估算
T_PLUS_1 = True            # T+1
PRICE_LIMIT = 0.10         # 涨跌停 10%
