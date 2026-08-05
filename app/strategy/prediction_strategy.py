"""预测策略:将 ML 预测信号接入 SilverQuant 风格的买卖流程。

- scan_buy:  对池内股票跑预测, 建议为 买入 且未持仓时下单
- scan_sell: 建议为 卖出/减仓 或 触发止损时清仓/减仓
与 SilverQuant 的 Buyer/Seller 职责对齐,可挂接到其框架。
"""
import logging
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.advice.advisor import ACTION_CN
from app.data.fetcher import get_daily_history, get_spot_quote
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features
from app.features.industry_features import prepare_features
from app.ml.predictor import Predictor
from app.strategy.paper_delegate import PaperDelegate

logger = logging.getLogger("prediction_strategy")


class PredictionPool:
    """股票池配置(对齐 SilverQuant PoolConf 风格)。"""
    def __init__(self, codes=None):
        self.codes = codes or ["600519", "601318", "600036", "000001", "300750"]
        self.slot_count = 10
        self.slot_capacity = 20000      # 每只股票预算
        self.daily_buy_max = 5
        self.min_price = 2.0
        self.price_adjust = "qfq"

    def get_code_list(self) -> list:
        return list(self.codes)


class PredictionStrategy:
    def __init__(self, pool: PredictionPool, delegate: PaperDelegate,
                 horizon=None, buy_threshold=None, sell_threshold=None):
        self.pool = pool
        self.delegate = delegate
        self.predictor = Predictor(horizon)
        self.buy_threshold = buy_threshold or 0.55
        self.sell_threshold = sell_threshold or 0.55

    # ------------------------------------------------------------ 选股
    def select_candidates(self, quotes: dict, curr_date: str) -> dict[str, dict]:
        """对池内每只股票跑预测,返回建议买入的标的。"""
        selections = {}
        positions = self.delegate.check_positions()
        held = {p.stock_code for p in positions}
        for code in self.pool.get_code_list():
            code = str(code).zfill(6)
            if code in held:
                continue
            quote = quotes.get(code)
            try:
                df = get_daily_history(code, days=240, adjust=self.pool.price_adjust)
                features = prepare_features(df, code)
                pred = self.predictor.predict_latest(features)
            except Exception as e:  # noqa: BLE001
                logger.warning("[预测失败] %s: %s", code, e)
                continue
            if pred["direction"] == "up" and pred["p_up"] >= self.buy_threshold:
                price = quote["price"] if quote else float(df["close"].iloc[-1])
                last_close = quote["prev_close"] if quote else float(df["close"].iloc[-2])
                if price < self.pool.min_price:
                    continue
                budget = self.pool.slot_capacity
                volume = math.floor(budget / price / 100) * 100
                selections[code] = {
                    "price": price,
                    "lastClose": last_close,
                    "volume": volume,
                    "p_up": pred["p_up"],
                }
        return selections

    # ------------------------------------------------------------ 买卖
    def scan_buy(self, quotes: dict, curr_date: str) -> int:
        positions = self.delegate.check_positions()
        count = self.delegate.get_holding_position_count(positions)
        if count >= self.pool.slot_count:
            return 0
        selections = self.select_candidates(quotes, curr_date)
        cash = self.delegate.check_asset().cash
        n = 0
        for code, sel in selections.items():
            if n >= self.pool.daily_buy_max:
                break
            if code in self.delegate.today_buys:
                continue
            amount = sel["price"] * sel["volume"]
            if amount > cash:
                continue
            ok = self.delegate.order_market_open(code, sel["price"], sel["volume"],
                                                 f"ML预测{p_up:.0%}" if (p_up := sel.get("p_up", 0)) else "ML预测")
            if ok:
                cash -= amount
                n += 1
                logger.info("[买入] %s 于 %.2f × %d", code, sel["price"], sel["volume"])
        return n

    def scan_sell(self, quotes: dict, curr_date: str, stop_loss_pct: float = 0.06) -> int:
        positions = self.delegate.check_positions()
        n = 0
        for p in positions:
            code = p.stock_code
            quote = quotes.get(code)
            try:
                df = get_daily_history(code, days=240, adjust=self.pool.price_adjust)
                features = prepare_features(df, code)
                pred = self.predictor.predict_latest(features)
            except Exception as e:  # noqa: BLE001
                logger.warning("[预测失败] %s: %s", code, e)
                continue
            price = quote["price"] if quote else float(df["close"].iloc[-1])

            sell_reason = None
            if pred["direction"] == "down" and pred["p_down"] >= self.sell_threshold:
                sell_reason = f"模型看跌 {pred['p_down']:.0%}"
            elif price < p.avg_price * (1 - stop_loss_pct):
                sell_reason = f"触及止损({stop_loss_pct:.0%})"
            elif p.can_use_volume <= 0:
                continue

            if sell_reason:
                vol = p.can_use_volume
                if self.delegate.order_market_close(code, price, vol, sell_reason):
                    logger.info("[卖出] %s 于 %.2f × %d (%s)", code, price, vol, sell_reason)
                    n += 1
        return n

    def settle(self, quotes: dict) -> float:
        return self.delegate.daily_settle(quotes)
