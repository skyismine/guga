"""超参调优:贝叶斯调参(optuna),未安装时降级为轻量随机搜索。

优化目标:在时间上更早的验证段(测试折之前最近 BAYESIAN_TUNE_VERIFY_DAYS 日)
上最大化 F1_weighted 与 up_auc 的复合指标,避免使用未来信息。
默认关闭(config.BAYESIAN_TUNE_ENABLED=False),由 trainer 按需调用。
"""
import time
from typing import Dict

import numpy as np

from app import config

_BASE = dict(n_estimators=400, learning_rate=0.04, max_depth=3, num_leaves=8,
             reg_lambda=1.0, min_child_samples=50, subsample=0.8,
             colsample_bytree=0.8, verbosity=-1, random_state=42)


def _score_params(params: dict, X_tr, y_tr, X_va, y_va,
                  sample_weight_tr=None) -> float:
    from lightgbm import LGBMClassifier
    from sklearn.metrics import f1_score, roc_auc_score
    try:
        model = LGBMClassifier(**params)
        model.fit(X_tr, y_tr, sample_weight=sample_weight_tr)
        pred = model.predict(X_va)
        f1 = f1_score(y_va, pred, average="weighted", zero_division=0)
        proba = model.predict_proba(X_va)
        up_auc = roc_auc_score((y_va == 2).astype(int),
                               proba[:, list(model.classes_).index(2)])
        return f1 + up_auc
    except Exception:  # noqa: BLE001
        return -1.0


def _param_space(trial) -> dict:
    """optuna trial -> 超参(围绕基线小幅扰动,保持可复现)。"""
    return dict(
        n_estimators=400,
        learning_rate=trial.suggest_float("learning_rate", 0.02, 0.08, log=True),
        max_depth=trial.suggest_int("max_depth", 2, 5),
        num_leaves=trial.suggest_int("num_leaves", 4, 24),
        reg_lambda=trial.suggest_float("reg_lambda", 0.0, 3.0),
        min_child_samples=trial.suggest_int("min_child_samples", 20, 120),
        subsample=trial.suggest_float("subsample", 0.6, 1.0),
        colsample_bytree=trial.suggest_float("colsample_bytree", 0.6, 1.0),
        verbosity=-1,
        random_state=42,
    )


def tune_hyperparams(X_tr: np.ndarray, y_tr: np.ndarray,
                     X_va: np.ndarray, y_va: np.ndarray,
                     sample_weight_tr=None, n_trials: int = None,
                     verbose: bool = True) -> Dict:
    """贝叶斯/随机搜索调参,返回最优超参 dict。"""
    n_trials = n_trials or int(getattr(config, "BAYESIAN_TUNE_TRIALS", 20))
    t0 = time.time()
    try:
        import optuna  # type: ignore
        have_optuna = True
    except ImportError:
        have_optuna = False

    if have_optuna:
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        def objective(trial):
            p = _param_space(trial)
            return _score_params(p, X_tr, y_tr, X_va, y_va, sample_weight_tr)

        study = optuna.create_study(direction="maximize",
                                    sampler=optuna.samplers.TPESampler(seed=42))
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        best = study.best_params
        best.update({k: v for k, v in _BASE.items() if k not in best})
        if verbose:
            print(f"[调参] optuna {n_trials} 试验, 耗时 {time.time()-t0:.0f}s")
        return best

    # 降级:随机搜索(不引入 sklearn.model_selection 的交叉验证开销)
    rng = np.random.default_rng(42)
    best_p, best_score = dict(_BASE), -1.0
    for _ in range(min(n_trials, 15)):
        p = dict(_BASE)
        p["learning_rate"] = float(rng.uniform(0.02, 0.08))
        p["max_depth"] = int(rng.integers(2, 6))
        p["num_leaves"] = int(rng.integers(4, 25))
        p["reg_lambda"] = float(rng.uniform(0.0, 3.0))
        p["min_child_samples"] = int(rng.integers(20, 121))
        p["subsample"] = float(rng.uniform(0.6, 1.0))
        p["colsample_bytree"] = float(rng.uniform(0.6, 1.0))
        s = _score_params(p, X_tr, y_tr, X_va, y_va, sample_weight_tr)
        if s > best_score:
            best_score, best_p = s, dict(p)
    if verbose:
        print(f"[调参] 随机搜索(optuna 未安装) 耗时 {time.time()-t0:.0f}s")
    return best_p


if __name__ == "__main__":
    pass