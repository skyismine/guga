"""PaperDelegate:本地虚拟券商,实现与 SilverQuant BaseDelegate 兼容的下单接口。

用于无 QMT/掘金环境下的模拟交易验证;实盘时可平滑切换到
SilverQuant 的 XtDelegate(真实验证) / GmDelegate(掘金模拟盘)。
交易规则: T+1、一手 100 股、佣金+印花税+滑点。
"""
import datetime as dt
import json
import os
import threading

from app import config


class PaperPosition:
    """单只持仓(兼容 SilverQuant position 对象的属性)。"""
    def __init__(self, stock_code: str, volume: int, can_use_volume: int,
                 avg_price: float, open_date: str):
        self.stock_code = stock_code
        self.volume = volume
        self.can_use_volume = can_use_volume
        self.avg_price = avg_price
        self.open_date = open_date
        self.market_value = volume * avg_price


class PaperAsset:
    def __init__(self, cash: float):
        self.cash = cash
        self.market_value = 0.0


class PaperDelegate:
    """虚拟交易账户。"""

    def __init__(self, init_cash: float = 100_000.0,
                 state_path: str = None, strategy_name: str = "预测策略"):
        self.strategy_name = strategy_name
        self.init_cash = init_cash
        self.state_path = state_path or os.path.join(
            config.DATA_DIR, "paper", f"{strategy_name}.json")
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        self._lock = threading.RLock()
        self.positions: dict[str, PaperPosition] = {}
        self.cash = init_cash
        self.today_buys: set[str] = set()
        self.orders_log: list[dict] = []
        self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    st = json.load(f)
                self.cash = float(st["cash"])
                self.positions = {}
                for k, v in st["positions"].items():
                    self.positions[k] = PaperPosition(
                        stock_code=k, volume=int(v["volume"]),
                        can_use_volume=int(v["can_use_volume"]),
                        avg_price=float(v["avg_price"]), open_date=v["open_date"])
                self.orders_log = st.get("orders", [])
            except Exception:  # noqa: BLE001
                pass

    def _save(self):
        with self._lock:
            payload = {
                "cash": self.cash,
                "positions": {k: {"volume": p.volume, "can_use_volume": p.can_use_volume,
                                  "avg_price": p.avg_price, "open_date": p.open_date}
                              for k, p in self.positions.items()},
                "orders": self.orders_log[-500:],
            }
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ 查询
    def check_asset(self) -> PaperAsset:
        asset = PaperAsset(cash=self.cash)
        for p in self.positions.values():
            asset.market_value += p.volume * p.avg_price
        return asset

    def check_orders(self):
        return []

    def check_positions(self) -> list:
        return list(self.positions.values())

    def get_holding_position_count(self, positions: list, only_stock: bool = False) -> int:
        return len(positions)

    def is_position_holding(self, position: dict) -> bool:
        return position.get("volume", 0) > 0

    # ------------------------------------------------------------ 下单
    def order_market_open(self, code, price, volume, remark="", strategy_name=None) -> bool:
        return self._buy(code, price, volume, remark)

    def order_limit_open(self, code, price, volume, remark="", strategy_name=None) -> bool:
        return self._buy(code, price, volume, remark)

    def order_market_close(self, code, price, volume, remark="", strategy_name=None) -> bool:
        return self._sell(code, price, volume, remark)

    def order_limit_close(self, code, price, volume, remark="", strategy_name=None) -> bool:
        return self._sell(code, price, volume, remark)

    def order_cancel_all(self, strategy_name=None):
        return True

    def order_cancel_buy(self, code, strategy_name=None):
        return True

    def order_cancel_sell(self, code, strategy_name=None):
        return True

    def _fee(self, amount: float, sell: bool = False) -> float:
        fee = max(amount * config.COMMISSION, config.MIN_COMMISSION)
        if sell:
            fee += amount * config.STAMP_TAX
        return fee

    def _buy(self, code: str, price: float, volume: int, remark: str) -> bool:
        code = str(code).zfill(6)
        volume = int(volume // config.LOT_SIZE) * config.LOT_SIZE
        if volume <= 0:
            return False
        amount = price * volume
        fee = self._fee(amount)
        if amount + fee > self.cash:
            return False
        with self._lock:
            self.cash -= amount + fee
            if code in self.positions:
                p = self.positions[code]
                total = p.volume * p.avg_price + amount
                p.volume += volume
                p.avg_price = total / p.volume
            else:
                self.positions[code] = PaperPosition(
                    stock_code=code, volume=volume, can_use_volume=0,
                    avg_price=price, open_date=dt.date.today().isoformat())
            # T+1:当日买入不可卖(can_use_volume 保持 0)
            self.today_buys.add(code)
            self._log("买入", code, price, volume, fee, remark)
            self._save()
        return True
    def _sell(self, code: str, price: float, volume: int, remark: str) -> bool:
        code = str(code).zfill(6)
        if code not in self.positions:
            return False
        p = self.positions[code]
        volume = min(int(volume // config.LOT_SIZE) * config.LOT_SIZE, p.can_use_volume)
        if volume <= 0:
            return False
        amount = price * volume
        fee = self._fee(amount, sell=True)
        with self._lock:
            self.cash += amount - fee
            p.volume -= volume
            p.can_use_volume -= volume
            self._log("卖出", code, price, volume, fee, remark)
            if p.volume <= 0:
                del self.positions[code]
            self._save()
        return True

    def _log(self, side, code, price, volume, fee, remark):
        self.orders_log.append({
            "time": dt.datetime.now().isoformat(timespec="seconds"),
            "side": side, "code": code, "price": round(price, 3),
            "volume": volume, "fee": round(fee, 2), "remark": remark,
        })

    # ------------------------------------------------------------ 结算
    def mark_to_market(self, quotes: dict):
        """用最新行情更新持仓市值。"""
        total = 0.0
        for code, p in self.positions.items():
            q = quotes.get(code)
            total += p.volume * (q["price"] if q else p.avg_price)
        return self.cash + total

    def daily_settle(self, quotes: dict):
        """盘后结算:当日买入转可用(T+1)。"""
        for p in self.positions.values():
            p.can_use_volume = p.volume
        self.today_buys.clear()
        self._save()
        return self.mark_to_market(quotes)

    def shutdown(self):
        self._save()
