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
PREDICT_HORIZON = 3        # 预测未来几个交易日(3 日短中期)
PREDICT_THRESHOLD = 0.015  # 涨跌幅超过该值判定为 上涨/下跌 (1.5%);启用滚动分位数后仅作早期回退

# 标签阈值:滚动分位数(替代固定百分比,自动适配个股波动率与市场环境)
# 日期 t 的阈值 = 过去 LABEL_QUANTILE_WINDOW 日内"未来 horizon 日收益"的 30%/70% 分位数
# (窗口 shift(horizon) 保证只用 t 时点已实现数据,避免前视;三类样本占比天然约 30/40/30,长期稳定)
LABEL_QUANTILE_WINDOW = 250     # 滚动窗口(交易日)
LABEL_QUANTILE_LOW = 0.30       # 下跌阈值分位数(<= 该分位 -> 下跌)
LABEL_QUANTILE_HIGH = 0.70      # 上涨阈值分位数(>= 该分位 -> 上涨)
LABEL_QUANTILE_MIN_PERIODS = 60 # 窗口最小样本数,不足则剔除

# ---------------------------------------------------------------- 每日信号排序(Top-N)
# 信号系统不做自动交易,仅每日收盘后输出"买什么/卖什么":对股票池按预期收益排序,
# 取前 RANK_TOP_N 为买入候选,后 RANK_TOP_N 为风险提示(替代绝对概率阈值)。
RANK_TOP_N = 3            # 每日推荐买入股票数量
RANK_MIN_P_UP = 0.40      # 候选股 p_up 下限(过滤低置信,防纯排序选入烂票)
RANK_MIN_EXP_RET = 0.0    # 候选股预期收益下限(<=0 视为无上涨空间)

# 用于训练的全市场样本股票(代码前缀), 建议覆盖主板/创业板
TRAIN_PREFIXES = ("60", "00", "30")
TRAIN_STOCK_CODES = [      # 一组流动性好、覆盖各行业的样本股
    "600519", "601318", "600036", "601899", "600030",
    "600900", "601012", "600887", "600309", "603259",
    "000001", "000858", "000333", "000651", "002594",
    "002415", "300750", "300059", "300124", "002230",
]
TRAIN_YEARS_BACK = 3       # 取最近 N 年历史作为训练数据

# 模型
MODEL_NAME = "gbm_3class"
MIN_TRAIN_SAMPLES = 500    # 训练样本下限,不足则无法训练
TEST_RATIO = 0.2           # 按时间切分的验证集比例

# 特征筛选(相关性去冗余 + 重要性 Top-N)
FEATURE_SELECT = True               # 训练时是否使用筛选后的特征子集
FEATURE_SELECT_TOP_N = 30           # 保留的特征数量
FEATURE_CORR_THRESHOLD = 0.8        # |相关系数| 超过该值视为冗余(按重要性贪心保留)
FEATURE_SELECTED_FILE = "selected_features.json"   # 选择结果(存于 MODEL_DIR)

# 预测信号转操作建议的阈值
BUY_P_UP = 0.55            # P(上涨) >= 该值 -> 考虑买入
SELL_P_DOWN = 0.55         # P(下跌) >= 该值 -> 考虑卖出
STRONG = 0.68              # 强信号阈值

# ---------------------------------------------------------------- 市场情绪(恐贪/期指资金/涨跌家数)
MARKET_INDEX = "sh000300"  # 市场情绪代理指数(沪深300)
MARKET_BASKET = TRAIN_STOCK_CODES   # 涨跌家数/宽度用样本篮子(与训练股票一致,缓存复用)
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
