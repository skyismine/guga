"""新旧 LightGBM 模型对比验证脚本。

对比对象:
- 旧模型:data_cache/models/gbm_3class_h3.joblib(3日,滚动分位数标签,129只静态池)
- 新模型:data_cache/models/gbm_3class_h5.joblib(5日,全链路优化:中证A500训练池680只
  + 样本加权 + 高级特征 + 概率校准 + rolling验证)

对比维度(同一批样本股票/同一历史窗口):
  1. 三分类准确率 / F1 / 涨跌二值 AUC;
  2. 上涨信号排名质量(把 p_up 按分位分组,比较各组实际未来上涨率,检验区分度);
  3. 校准质量(预测概率 vs 实际频率,分箱检验);
  4. 输出字段(新模型 rise_rank_pct / expected_return_pct / confidence_score)。

用法:
    python docs/compare_models.py [--sample 20] [--days 200]
    --sample 抽样股票数(从训练池随机); --days 评估用历史交易日数
"""
import argparse
import os
import random
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.data.fetcher import get_daily_history  # noqa: E402
from app.features.concept_features import prepare_features  # noqa: E402
from app.ml.predictor import Predictor  # noqa: E402

NEW_HORIZON = 5


def _load_old_predictor() -> Predictor:
    """加载旧模型(h3),临时切换 horizon。"""
    return Predictor(horizon=3)


def _load_new_predictor() -> Predictor:
    return Predictor(horizon=NEW_HORIZON)


def _evaluate(pred: Predictor, df: pd.DataFrame, code: str) -> pd.DataFrame:
    """对单只股票历史特征逐日预测,返回预测表(含实际 horizon 收益)。"""
    features = prepare_features(df, code)
    series = pred.predict_series(features)
    close = df["close"]
    h = pred.horizon
    fwd = close.shift(-h) / close - 1
    series["actual_fwd_ret"] = fwd.reindex(series.index)
    series["code"] = code
    return series


def _metrics(pred: Predictor, results: pd.DataFrame) -> dict:
    """汇总指标:acc(信号方向命中)/ 三分类加权F1 / up&down AUC / 分箱校准。"""
    from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
    rows = results.dropna(subset=["actual_fwd_ret"]).copy()
    if rows.empty:
        return {}
    # 信号方向 vs 实际(上涨概率最高 -> 预期上涨)
    rows["pred_signal"] = rows[["up", "flat", "down"]].idxmax(axis=1)
    rows["pred_label"] = rows["pred_signal"].map({"up": 2, "flat": 1, "down": 0})
    actual = (rows["actual_fwd_ret"] > 0).astype(int).values
    up_signal = (rows["pred_signal"] == "up").astype(int).values
    # 上涨方向命中率:预测上涨信号的样本实际真上涨比例(召回视角,取正预测准确率)
    hit = float(rows.loc[up_signal == 1, "actual_fwd_ret"].gt(0).mean()) if up_signal.sum() else 0.0
    acc = accuracy_score(actual, up_signal)
    # 二分类 F1:信号=上涨(正) vs 实际>0
    f1 = f1_score(actual, up_signal, zero_division=0)
    up_auc = roc_auc_score(actual, rows["up"].values)
    down_auc = roc_auc_score(1 - actual, rows["down"].values)
    # 分箱校准:按 p_up 十分位分组,比较平均预测概率 vs 实际上涨率
    rows = rows.assign(bin=pd.qcut(rows["up"], 10, labels=False, duplicates="drop"))
    calib = rows.groupby("bin").agg(
        pred_up=("up", "mean"), actual_up=("actual_fwd_ret", lambda x: float((x > 0).mean())),
        n=("up", "size"))
    calib_mae = float(np.abs(calib["pred_up"] - calib["actual_up"]).mean())
    return {"n": len(rows), "acc_up_signal": acc, "f1_up_signal": f1,
            "up_hit_rate": hit, "up_auc": up_auc, "down_auc": down_auc,
            "calibration_mae": calib_mae}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--days", type=int, default=200)
    args = ap.parse_args()

    from app.ml.pool_builder import load_training_pool
    pool = load_training_pool()
    all_codes = pool.get("codes") or list(config.TRAIN_STOCK_CODES)
    random.seed(42)
    codes = random.sample(all_codes, min(args.sample, len(all_codes)))

    old_pred = _load_old_predictor()
    new_pred = _load_new_predictor()
    print(f"旧模型: h={old_pred.horizon} 新模型: h={new_pred.horizon}")
    print(f"抽样 {len(codes)} 只股票 × {args.days} 日评估\n")

    old_res, new_res = [], []
    for c in codes:
        try:
            df = get_daily_history(c, days=max(args.days, 320), adjust="qfq")
            df = df.tail(args.days)
            df.name = c
            old_res.append(_evaluate(old_pred, df, c))
            new_res.append(_evaluate(new_pred, df, c))
        except Exception as e:  # noqa: BLE001
            print(f"  {c} 跳过: {e}")
    if not old_res:
        print("无有效样本")
        return
    old_df = pd.concat(old_res)
    new_df = pd.concat(new_res)

    print("=" * 64)
    print(f"{'指标':<18}{'旧模型(h3)':<16}{'新模型(h5)':<16}")
    print("-" * 64)
    for k, label in [("acc_up_signal", "方向准确率"), ("f1_up_signal", "F1(上涨信号)"),
                     ("up_hit_rate", "上涨信号命中率"), ("up_auc", "上涨AUC"),
                     ("down_auc", "下跌AUC"), ("calibration_mae", "校准误差(MAE)")]:
        om = _metrics(old_pred, old_df)
        nm = _metrics(new_pred, new_df)
        print(f"{label:<18}{om.get(k, float('nan')):<16.4f}{nm.get(k, float('nan')):<16.4f}")
    print("-" * 64)

    print("\n新模型输出字段示例(最近3日,抽样首股):")
    sample_code = codes[0]
    df = get_daily_history(sample_code, days=args.days, adjust="qfq")
    df.name = sample_code
    feat = prepare_features(df, sample_code)
    series = new_pred.predict_series(feat)
    print(series[["up", "expected_return", "confidence_score", "rise_rank_pct"]].tail(3).to_string())
    latest = new_pred.predict_latest(feat)
    print(f"\n最新预测: direction={latest['direction_cn']} "
          f"expected_return_pct={latest['expected_return_pct']}% "
          f"confidence={latest['confidence_score']}")


if __name__ == "__main__":
    main()