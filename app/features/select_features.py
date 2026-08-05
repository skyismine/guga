"""特征筛选:相关性去冗余 + 重要性排序保留 Top-N。

流程:
  1. 用训练集(时间切分的训练段)训练 LightGBM,取 gain 重要性;
  2. 按重要性降序贪心遍历,若某特征与任一"已保留特征"的 |相关系数| > 阈值,视为
     冗余剔除(保留同簇中重要性最高的那个);
  3. 去冗余后按重要性取前 Top-N,持久化到 selected_features.json;
  4. 训练/回测时统一加载该子集,减少噪音输入、降低过拟合。
"""
import json
import os

import numpy as np
import pandas as pd

from app import config
from app.ml.dataset import build_dataset, time_split
from app.ml.trainer import _history_groups


def _importance(data: pd.DataFrame, feature_names: list) -> dict:
    """训练段 LightGBM 的 gain 重要性(与 trainer 同参,保证口径一致)。"""
    from lightgbm import LGBMClassifier
    train_df, _, _ = time_split(data)
    X, y = train_df[feature_names].values, train_df["label"].values
    model = LGBMClassifier(n_estimators=400, learning_rate=0.04,
                           max_depth=3, num_leaves=8, reg_lambda=1.0,
                           min_child_samples=50, subsample=0.8,
                           colsample_bytree=0.8, verbosity=-1, random_state=42)
    model.fit(X, y)
    return dict(zip(feature_names,
                    model.booster_.feature_importance(importance_type="gain")))


def select_features(n_keep: int = None, corr_thresh: float = None,
                    verbose: bool = True) -> dict:
    """执行特征筛选并保存结果。返回 {kept, top, dropped_corr, importance}。"""
    n_keep = n_keep or config.FEATURE_SELECT_TOP_N
    corr_thresh = corr_thresh or config.FEATURE_CORR_THRESHOLD

    groups = _history_groups(verbose)
    data, _, feature_names = build_dataset(list(groups.values()))
    imp = _importance(data, feature_names)

    # 特征只保留数值列
    feat_cols = [c for c in feature_names if c not in ("label", "code")]
    corr = data[feat_cols].corr()

    # 按重要性降序,贪心去冗余:与任一已保留特征强相关则剔除
    order = sorted(feat_cols, key=lambda f: imp.get(f, 0.0), reverse=True)
    kept, dropped = [], []
    for f in order:
        if all(abs(corr.loc[f, k]) <= corr_thresh for k in kept):
            kept.append(f)
        else:
            dropped.append(f)
    top = kept[:n_keep]

    # 保存
    path = os.path.join(config.MODEL_DIR, config.FEATURE_SELECTED_FILE)
    os.makedirs(config.MODEL_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(top, fp, ensure_ascii=False, indent=2)

    if verbose:
        print(f"[特征筛选] 共 {len(feat_cols)} 个特征,相关性去冗余剔除 {len(dropped)} 个,"
              f"保留 {len(kept)} 个,再按重要性取 Top-{len(top)}")
        n_drop = 0
        for f in dropped:
            partner = next((k for k in kept if abs(corr.loc[f, k]) > corr_thresh), "?")
            print(f"  - 剔除 {f}(与 {partner} 相关 {corr.loc[f, partner]:.2f})")
            n_drop += 1
            if n_drop >= 15:
                print(f"  ... 其余 {len(dropped) - n_drop} 个略")
                break
        print(f"[特征筛选] 保留 Top-{len(top)}: {top}")
        print(f"[特征筛选] 结果已保存: {path}")

    return {"kept": kept, "top": top, "dropped_corr": dropped,
            "importance": imp, "path": path}


def load_selected_features() -> list:
    """加载筛选结果特征列表(文件不存在或未启用则返回空列表)。"""
    if not getattr(config, "FEATURE_SELECT", True):
        return []
    path = os.path.join(config.MODEL_DIR, config.FEATURE_SELECTED_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError):
        return []


def apply_selection(data: pd.DataFrame, feature_names: list) -> list:
    """按筛选结果过滤 data 与特征名(仅保留 data 中实际存在的列)。返回过滤后的特征名。"""
    sel = load_selected_features()
    if not sel:
        return feature_names
    feat = [f for f in sel if f in data.columns]
    if not feat:
        return feature_names
    drop = [c for c in data.columns if c not in ("label", "code") and c not in feat]
    if drop:
        data.drop(columns=drop, inplace=True)
    return feat


if __name__ == "__main__":
    res = select_features()
    print(f"\n[特征筛选] Top-{len(res['top'])} 特征:")
    for i, f in enumerate(res["top"], 1):
        print(f"  {i:>2}. {f:<24} importance={res['importance'][f]:.1f}")
