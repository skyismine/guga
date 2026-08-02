"""训练预测模型:  python run_train.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.ml.trainer import train_all

if __name__ == "__main__":
    train_all()
