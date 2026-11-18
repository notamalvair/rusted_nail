"""Загрузка COHORT_TAPE_V1 и сведение к событиям (parent) и токенам.
Строка ленты = leg; цена/резерв/комиссия повторяются у всех legs одного parent."""
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import pqlite  # noqa: E402

DATA = ROOT / "datasets/COHORT_TAPE_V1/data"
# Максимум provider receipt по источникам (из feature-build владельца).
SOURCE_MAX_RECEIPT = {"2026-11-02": 1793617199994, "2026-11-09": 1794221999999, "2026-11-16": 1794826799988}
FEE = 0.0125 + 0.0025  # на сторону: пул pump.fun + pumpapi


def read(kind, day, cache=pathlib.Path("/tmp/nail_cache")):
    cache.mkdir(exist_ok=True)
    pkl = cache / f"{kind}_{day}.pkl"
    if pkl.exists():
        return pd.read_pickle(pkl)
    cols, _ = pqlite.read(DATA / f"{kind}_{day}.parquet")
    df = pd.DataFrame(cols)
    df.to_pickle(pkl)
    return df


def load_day(day):
    if day == "2026-11-16":
        raise RuntimeError("16.11 — test: не открывать до фиксации правил")
    tape = read("tape", day)
    tokens = read("tokens", day).set_index("token_idx")
    tape = tape.sort_values(["token_idx", "receipt_ms", "archive", "line", "leg_index"], kind="stable")
    # parent-событие: (token_idx, archive, line)
    ev = tape.drop_duplicates(["token_idx", "archive", "line"])[
        ["token_idx", "action", "protocol", "receipt_ms", "event_ms", "block", "archive", "line",
         "tx_signer", "price_sol", "reserve_quote", "network_fee_sol"]].reset_index(drop=True)
    ev["create_block"] = ev.token_idx.map(tokens.create_block)
    ev["create_receipt_ms"] = ev.token_idx.map(tokens.create_receipt_ms)
    ev["dblock"] = ev.block - ev.create_block
    ev["dt_ms"] = ev.receipt_ms - ev.create_receipt_ms
    legs = tape[tape.leg_action.isin(["buy", "sell"])].copy()
    legs["creator"] = legs.token_idx.map(tokens.creator)
    legs["create_block"] = legs.token_idx.map(tokens.create_block)
    legs["dblock"] = legs.block - legs.create_block
    legs["is_dev"] = legs.trader == legs.creator
    legs["in_create_tx"] = legs.action == "create"
    tokens["full_window_s"] = (np.minimum(tokens.end_receipt_ms, SOURCE_MAX_RECEIPT[day])
                               - tokens.create_receipt_ms) / 1000
    return ev, legs, tokens


def asof_block(ev, k):
    """Цена после последнего события в блоках <= create_block + k (по токенам)."""
    e = ev[ev.dblock <= k]
    return e.groupby("token_idx").price_sol.last()


def asof_time(ev, seconds):
    """Цена после последнего события с receipt <= create_receipt + seconds."""
    e = ev[ev.dt_ms <= seconds * 1000]
    return e.groupby("token_idx").price_sol.last()


def net(ratio, fee=FEE):
    return (1 - fee) ** 2 * ratio - 1


def summarize(r):
    r = r.dropna()
    if len(r) == 0:
        return dict(n=0)
    clip = r.clip(upper=r.quantile(0.99))
    return dict(n=len(r), mean=r.mean(), mean_clip99=clip.mean(), median=r.median(),
                win=(r > 0).mean(), p90=r.quantile(0.9))
