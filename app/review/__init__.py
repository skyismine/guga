"""A股每日复盘 · 数据采集(见 data.py)与文本生成(见 generator.py)。"""
from app.review.data import collect_review, review_date
from app.review.generator import generate_review

__all__ = ["collect_review", "review_date", "generate_review"]
