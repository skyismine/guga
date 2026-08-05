"""统一分析管道:数据 -> 特征 -> 预测 -> 建议。CLI / Web / 策略共用。"""
import datetime as dt
from typing import Dict, Optional

from app.advice.advisor import generate_advice
from app.data.fetcher import (get_daily_history, get_spot_quote,
                              get_stock_name)
from app.features.indicators import compute_features
from app.features.market_features import attach_market_features, market_snapshot
from app.features.industry_features import prepare_features
from app.ml.predictor import Predictor


def _full_pipeline(code: str, with_quote: bool = True):
    """数据 -> 特征(含市场级+行业) -> 预测 -> 建议 -> 快照,供两种返回共用。

    features 为标准化特征(供模型);raw_features 为原始指标特征(供操作建议的阈值判断)。
    """
    df = get_daily_history(code, days=600, adjust="qfq")
    if len(df) < 120:
        raise ValueError(f"{code} 历史数据不足({len(df)}行)")
    raw_features = compute_features(df)
    features = prepare_features(df, code)
    predictor = Predictor()
    pred = predictor.predict_latest(features)
    quote = None
    if with_quote:
        try:
            quote = get_spot_quote(code)
        except Exception:
            quote = None
    mkt = market_snapshot()
    advice = generate_advice(df, raw_features, pred, quote, market=mkt)
    return df, features, predictor, pred, quote, mkt, advice


def analyze(code: str, with_quote: bool = True) -> Dict:
    """对单只股票执行完整分析,返回结构化结果。"""
    code = str(code).zfill(6)
    df, features, predictor, pred, quote, mkt, advice = _full_pipeline(code, with_quote)
    series = predictor.predict_series(features)

    return {
        "code": code,
        "name": get_stock_name(code),
        "analyzed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "date": pred["date"],
        "history_days": len(df),
        "history": df.tail(120),
        "features": features,
        "prediction": pred,
        "series": series,
        "advice": advice,
        "quote": quote,
        "market": mkt,
        "model_info": predictor.info(),
    }


def analyze_light(code: str) -> Dict:
    """不含历史序列的轻量分析(仅供 API 返回 JSON)。"""
    code = str(code).zfill(6)
    df, features, predictor, pred, quote, mkt, advice = _full_pipeline(code, with_quote=True)
    return {
        "code": code,
        "name": get_stock_name(code),
        "analyzed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "prediction": pred,
        "advice": advice,
        "quote": quote,
        "market": mkt,
        "model_info": predictor.info(),
    }
