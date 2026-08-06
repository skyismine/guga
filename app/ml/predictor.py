"""预测器:加载已训练模型,对特征矩阵输出上涨/震荡/下跌概率。"""
import json
import os
from typing import List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd

from app import config
from app.ml.dataset import LABEL_NAME, LABEL_NAME_CN


class Predictor:
    def __init__(self, horizon: int = None):
        self.horizon = horizon or config.PREDICT_HORIZON
        self.model = None
        self.meta = None
        self.feature_names: List[str] = []
        self.load()

    # ------------------------------------------------------------ 加载
    def model_path(self) -> str:
        return os.path.join(config.MODEL_DIR, f"{config.MODEL_NAME}_h{self.horizon}.joblib")

    def load(self) -> bool:
        path = self.model_path()
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"模型不存在: {path}\n请先运行训练脚本: python run_train.py")
        payload = joblib.load(path)
        self.model = payload["model"]
        self.meta = payload["meta"]
        self.feature_names = self.meta["feature_names"]
        return True

    # ------------------------------------------------------------ 预测
    _MISSING_WARNED = set()

    def _align(self, features: pd.DataFrame) -> np.ndarray:
        """对齐模型特征;缺失列(如无行业标的)补中性 0,防御性避免推理失败。"""
        missing = [c for c in self.feature_names if c not in features.columns]
        if missing:
            for c in missing:
                if c not in self._MISSING_WARNED:
                    print(f"[predictor] 特征缺失补 0: {c}")
                    self._MISSING_WARNED.add(c)
                features[c] = 0.0
        return features[self.feature_names].values

    def predict_proba(self, features: pd.DataFrame) -> pd.DataFrame:
        """对特征矩阵逐行预测,返回 上涨/震荡/下跌 三列概率。"""
        X = self._align(features)
        proba = self.model.predict_proba(X)
        classes = list(self.model.classes_)
        cols = {2: "up", 1: "flat", 0: "down"}
        out = pd.DataFrame(index=features.index)
        for c in classes:
            out[cols[c]] = proba[:, classes.index(c)]
        return out

    # ------------------------------------------------------------ 期望涨跌幅 / 盈亏比
    def _avg_returns(self) -> dict:
        """模型各类别平均未来收益(训练段统计,来自 meta)。"""
        avg = (self.meta or {}).get("class_avg_returns") or {}
        return {int(k): float(v) for k, v in avg.items()}

    def _exp_metrics(self, row: pd.Series):
        """由概率 + 类别平均收益估算 预期涨跌幅 与 盈亏比。

        expected_return = Σ p_c × avg_ret_c         (未来 horizon 日的期望涨跌幅)
        reward_risk     = (p_up×avg_up) / (p_down×|avg_down|)   (期望盈利/期望亏损)
        """
        avg = self._avg_returns()
        if not avg:
            return None, None
        exp_ret = (row["up"] * avg.get(2, 0.0) + row["flat"] * avg.get(1, 0.0)
                   + row["down"] * avg.get(0, 0.0))
        upside = row["up"] * max(avg.get(2, 0.0), 0.0)
        downside = row["down"] * max(-avg.get(0, 0.0), 0.0)
        rr = (upside / downside) if downside > 1e-4 else None
        return exp_ret, rr

    def predict_latest(self, features: pd.DataFrame) -> dict:
        """最新一行的预测结果。"""
        proba = self.predict_proba(features)
        row = proba.iloc[-1]
        best = row.idxmax()
        exp_ret, rr = self._exp_metrics(row)
        return {
            "p_up": float(row["up"]),
            "p_flat": float(row["flat"]),
            "p_down": float(row["down"]),
            "direction": best,
            "direction_cn": LABEL_NAME_CN[{"up": 2, "flat": 1, "down": 0}[best]],
            "prob": float(row[best]),
            "expected_return": round(exp_ret, 5) if exp_ret is not None else None,
            "reward_risk": round(rr, 3) if rr is not None else None,
            "date": str(proba.index[-1].date()),
        }

    def predict_series(self, features: pd.DataFrame) -> pd.DataFrame:
        """历史逐日预测(用于图表与回测)。"""
        proba = self.predict_proba(features)
        proba["signal"] = proba[["up", "flat", "down"]].idxmax(axis=1)
        proba["signal_cn"] = proba["signal"].map({"up": "上涨", "flat": "震荡", "down": "下跌"})
        exp = []
        rr = []
        for _, row in proba.iterrows():
            e, r = self._exp_metrics(row)
            exp.append(e)
            rr.append(r)
        proba["expected_return"] = exp
        proba["reward_risk"] = rr
        return proba

    # ------------------------------------------------------------ 模型信息
    def info(self) -> dict:
        if not self.meta:
            return {}
        m = dict(self.meta)
        m.pop("feature_names", None)
        return m


def load_meta(horizon: int = None) -> Optional[dict]:
    horizon = horizon or config.PREDICT_HORIZON
    path = os.path.join(config.MODEL_DIR, f"{config.MODEL_NAME}_h{horizon}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
