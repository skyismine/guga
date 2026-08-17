"""验证体系增强模块:概率校准 + 分池验证。

- calibrate_probabilities(model, X, y, method, cv):用内部交叉验证拟合校准器,
  返回包装后的校准模型(CalibratedClassifierCV)。calibrated 模型的 predict_proba
  更接近真实频率,供推理层直接解读为涨幅置信度。
- split_validation(model, X, y, categories):按训练池分类(core_a500/emotional/
  risk/large_cap)分别评估样本外指标,输出各子集 acc/AUC/F1,便于观察模型在不同
  类型股票上的泛化差异。
"""
from typing import Optional

import numpy as np
import pandas as pd

# 指标函数延迟导入,避免 sklearn 未安装时报错(与 trainer 一致)
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import (accuracy_score, f1_score, precision_score,
                             recall_score, roc_auc_score)


def calibrate_probabilities(model, X: np.ndarray, y: np.ndarray,
                            method: str = "isotonic", cv: int = 3):
    """对已训练模型做概率校准,返回包装后的模型。

    用训练集内部交叉验证拟合校准器(不额外消耗验证数据),校准后 predict_proba
    逼近真实频率。多分类 isotonic 校准在 sklearn>=1.1 支持。
    注意:返回的校准模型在 predict 时使用原模型预测,proba 为校准后概率。
    """
    cal = CalibratedClassifierCV(estimator=model, method=method, cv=cv)
    cal.fit(X, y)
    return cal


def _binary_auc(model, X: np.ndarray, y: np.ndarray, cls) -> Optional[float]:
    try:
        proba = model.predict_proba(X)
        idx = list(model.classes_).index(cls)
        return float(roc_auc_score((y == cls).astype(int), proba[:, idx]))
    except Exception:  # noqa: BLE001
        return None


def split_validation(model, X: np.ndarray, y: np.ndarray,
                     codes: np.ndarray, categories: dict) -> dict:
    """按训练池分类评估样本外指标。

    参数:
      model: 已训练模型
      X/y  : 测试集特征/标签
      codes: 与 X 行对齐的股票代码数组
      categories: {code: "core_a500"/"emotional"/"risk"/"large_cap"}
    返回 {scene: {acc, f1, up_auc, down_auc, n}}。
    """
    result = {}
    scenes = sorted({v for v in categories.values()})
    for scene in scenes:
        mask = np.array([categories.get(c, "other") == scene for c in codes])
        if mask.sum() < 30:
            continue
        xs, ys = X[mask], y[mask]
        pred = model.predict(xs)
        proba = model.predict_proba(xs)
        up_auc = _binary_auc(model, xs, ys, 2)
        dn_auc = _binary_auc(model, xs, ys, 0)
        result[scene] = {
            "n": int(mask.sum()),
            "accuracy": float(accuracy_score(ys, pred)),
            "f1_weighted": float(f1_score(ys, pred, average="weighted", zero_division=0)),
            "precision_weighted": float(precision_score(ys, pred, average="weighted", zero_division=0)),
            "recall_weighted": float(recall_score(ys, pred, average="weighted", zero_division=0)),
            "up_rank_metric": up_auc,
            "down_rank_metric": dn_auc,
        }
    return result


if __name__ == "__main__":
    pass