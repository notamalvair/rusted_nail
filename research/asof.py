"""As-of цены кривой из TOKEN_FEATURES_V1: цена между сделками не меняется,
поэтому состояние на момент create+W = последняя цена в окне, иначе предыдущее состояние."""
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import pqlite  # noqa: E402

FEATURES = ROOT / "datasets/TOKEN_FEATURES_V1/pump_create_features_v1.parquet"

WINDOWS = (5, 15, 30, 60, 120)
FEE = 0.0125 + 0.0025  # комиссия пула pump.fun + pumpapi, на сторону
SLIP = 0.03            # стресс-проскальзывание на сторону, как в билдере


def load(path=FEATURES):
    cols, _ = pqlite.read(path)
    f = pd.DataFrame(cols)
    prev = f["create_price_sol"].astype(float)
    for w in WINDOWS:
        obs = f[f"feature_window_observed_{w}s"] == True  # noqa: E712
        last = f[f"last_price_sol_{w}s"].astype(float)
        p = last.where(last > 0, prev)
        f[f"p{w}"] = p.where(obs)  # окно не наблюдено -> NaN
        prev = p
    f["p0"] = f["create_price_sol"].astype(float)
    return f


def net(ratio, fee=FEE, slip=0.0):
    return (1 - fee) ** 2 * ratio * (1 - slip) / (1 + slip) - 1


def summarize(r):
    r = r.dropna()
    if len(r) == 0:
        return dict(n=0)
    clip = r.clip(upper=r.quantile(0.99))
    return dict(n=len(r), mean=r.mean(), mean_clip99=clip.mean(), median=r.median(),
                win=(r > 0).mean(), p90=r.quantile(0.9))
