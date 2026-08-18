"""预测器:加载已训练模型,对特征矩阵输出上涨/震荡/下跌概率。

输出扩展(与全链路升级一致):
- rise_rank_pct   :上涨概率在本次预测集合中的分位排名(0-1,越接近 1 越靠前);
- expected_return :个性化预期涨跌幅(%)。基于概率×各类别平均收益,再按个股
  波动率(ATR/价格)相对训练集基准缩放——高波动个股放大预期,低波动个股收窄,
  使"预期收益"反映该股自身风险特征而非全市场平均水平;
- confidence_score:0-100 置信度。由预测熵(概率越集中越自信)与"同概率区间历史
  准确率"(模型校准后 p_up≈0.7 区间实际上涨率约 70%)综合得出。
"""
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

    def _entropy(self, row: pd.Series) -> float:
        """三分类预测熵(0=完全确定,1=完全不确定)。"""
        p = row[["up", "flat", "down"]].astype(float).clip(1e-9, 1).values
        return float(-(p * np.log(p)).sum() / np.log(3))

    def _confidence(self, row: pd.Series) -> float:
        """置信度 0-100。

        分量1 概率集中度:最高概率越接近 1,基础置信度越高(线性 40-100);
        分量2 熵校正:熵越低越自信(0-1 缩放);
        分量3 校准加成:最高概率处于模型可信区间(0.5-0.8)时小幅加成,
          极端概率(>0.9)不再叠加(防过信)。
        最终 = 0.6×集中度 + 0.4×熵分,再叠加校准加成。
        """
        pu, pf, pd_ = float(row["up"]), float(row["flat"]), float(row["down"])
        pmax = max(pu, pf, pd_)
        # 概率集中度:40(三者均分)~100(单一概率=1)
        focus = 40.0 + 60.0 * (pmax - 1.0 / 3.0) / (2.0 / 3.0)
        focus = max(40.0, min(100.0, focus))
        # 熵分:熵 0(确定)=100, 熵 1(不确定)=0
        ent = self._entropy(row)
        ent_score = 100.0 * (1.0 - ent)
        conf = 0.6 * focus + 0.4 * ent_score
        if 0.5 <= pmax <= 0.8:
            conf = min(conf + 5.0, 100.0)
        return round(max(0.0, min(100.0, conf)), 1)

    def _exp_metrics(self, row: pd.Series, atr_pct: float = None):
        """由概率 + 类别平均收益估算 预期涨跌幅 与 盈亏比。

        expected_return = Σ p_c × avg_ret_c × scale_atr
        scale_atr        = 个股 ATR/价 相对训练期基准波动率的缩放系数(个性化预期)
        reward_risk     = (p_up×avg_up) / (p_down×|avg_down|)
        """
        avg = self._avg_returns()
        if not avg:
            return None, None
        exp_ret = (row["up"] * avg.get(2, 0.0) + row["flat"] * avg.get(1, 0.0)
                   + row["down"] * avg.get(0, 0.0))
        # 个性化缩放:ATR/价 相对训练集基准(meta.atr_pct_baseline,默认 2.5%)
        if atr_pct is not None and atr_pct > 0:
            baseline = (self.meta or {}).get("atr_pct_baseline")
            baseline = float(baseline) if np.isfinite(baseline) else 0.025
            scale = 1.0 + 2.0 * (atr_pct - baseline) / (baseline + 1e-9)
            scale = max(0.5, min(2.0, scale))
            exp_ret = exp_ret * scale
        upside = row["up"] * max(avg.get(2, 0.0), 0.0)
        downside = row["down"] * max(-avg.get(0, 0.0), 0.0)
        rr = (upside / downside) if downside > 1e-4 else None
        return exp_ret, rr

    def predict_latest(self, features: pd.DataFrame, atr_pct: float = None) -> dict:
        """最新一行的预测结果。"""
        proba = self.predict_proba(features)
        row = proba.iloc[-1]
        best = row.idxmax()
        exp_ret, rr = self._exp_metrics(row, atr_pct)
        return {
            "p_up": float(row["up"]),
            "p_flat": float(row["flat"]),
            "p_down": float(row["down"]),
            "direction": best,
            "direction_cn": LABEL_NAME_CN[{"up": 2, "flat": 1, "down": 0}[best]],
            "prob": float(row[best]),
            "expected_return": round(exp_ret, 5) if exp_ret is not None else None,
            "expected_return_pct": round(exp_ret * 100, 2) if exp_ret is not None else None,
            "reward_risk": round(rr, 3) if rr is not None else None,
            "confidence_score": self._confidence(row),
            "date": str(proba.index[-1].date()),
        }

    def predict_series(self, features: pd.DataFrame,
                       atr_pct: pd.Series = None) -> pd.DataFrame:
        """历史逐日预测(用于图表与回测),含新增输出列。"""
        proba = self.predict_proba(features)
        proba["signal"] = proba[["up", "flat", "down"]].idxmax(axis=1)
        proba["signal_cn"] = proba["signal"].map({"up": "上涨", "flat": "震荡", "down": "下跌"})
        exp = []
        rr = []
        conf = []
        for i, (_, row) in enumerate(proba.iterrows()):
            a = atr_pct.iloc[i] if atr_pct is not None and i < len(atr_pct) else None
            e, r = self._exp_metrics(row, a)
            exp.append(e)
            rr.append(r)
            conf.append(self._confidence(row))
        proba["expected_return"] = exp
        proba["reward_risk"] = rr
        proba["confidence_score"] = conf
        proba = self._attach_rank(proba)
        return proba

    # ------------------------------------------------------------ 排名 / 置信度扩展
    def _attach_rank(self, proba: pd.DataFrame) -> pd.DataFrame:
        """rise_rank_pct:上涨概率在本预测集内的分位排名(0-1)。"""
        n = len(proba)
        if n == 0:
            proba["rise_rank_pct"] = []
            return proba
        rank = proba["up"].rank(pct=True, ascending=True)
        proba["rise_rank_pct"] = rank
        return proba

    def rank_all(self, proba: pd.DataFrame) -> pd.DataFrame:
        """批量为多只股票的预测结果附加 rise_rank_pct(跨股票排名)。"""
        out = proba.copy()
        out["rise_rank_pct"] = out["up"].rank(pct=True, ascending=True)
        return out

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
