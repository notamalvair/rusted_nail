"""Вход первым в блоке k (k=1,2), выходы:
- быстрый флип: продать на конец блока create+m (m=5..50, ~1-10 с);
- правила бота: TP +50%, SL -20%, trailing 25%, продажа дева, max hold 120 с.
Исполнение выхода: по цене события-триггера (оптимистично) или следующего события (с задержкой)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from tape import asof_block, load_day, net, summarize  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

pd.set_option("display.width", 250)


def policy_returns(ev, legs, tok, k, tp=0.5, sl=0.2, trail=0.25, max_hold_s=120, delayed=False):
    entry = asof_block(ev, k - 1)
    dev_sell_ms = (legs[legs.is_dev & (legs.leg_action == "sell")]
                   .groupby("token_idx").receipt_ms.min())
    out = {}
    for tid, g in ev[ev.dblock >= k].groupby("token_idx", sort=False):
        if tid not in entry.index or tok.full_window_s.get(tid, 0) < max_hold_s + 30:
            continue
        p0 = entry[tid]
        t0 = g.receipt_ms.iloc[0]
        prices = g.price_sol.to_numpy()
        times = g.receipt_ms.to_numpy()
        peak = p0
        exit_i = None
        dsell = dev_sell_ms.get(tid, np.inf)
        for i in range(len(prices)):
            p = prices[i]
            peak = max(peak, p)
            r = p / p0 - 1
            if (r >= tp or r <= -sl or (peak > p0 and p <= peak * (1 - trail))
                    or times[i] >= dsell or times[i] - t0 >= max_hold_s * 1000):
                exit_i = i
                break
        if exit_i is None:
            px = prices[-1]  # окно кончилось раньше max hold: последнее состояние
        else:
            j = min(exit_i + 1, len(prices) - 1) if delayed else exit_i
            px = prices[j]
        out[tid] = net(px / p0)
    return pd.Series(out)


if __name__ == "__main__":
    rows = []
    for day in ("2026-11-02", "2026-11-09"):
        ev, legs, tok = load_day(day)
        for k in (1, 2):
            entry = asof_block(ev, k - 1)
            for m in (5, 10, 25, 50):
                ok = tok.index[tok.full_window_s >= 30]
                r = net(asof_block(ev, m).reindex(ok) / entry.reindex(ok))
                rows.append(dict(day=day, entry_block=k, exit=f"флип: конец блока {m}", **summarize(r)))
            for delayed in (False, True):
                r = policy_returns(ev, legs, tok, k, delayed=delayed)
                rows.append(dict(day=day, entry_block=k,
                                 exit="правила бота" + (" + задержка 1 событие" if delayed else ""), **summarize(r)))
    t = pd.DataFrame(rows).pivot_table(index=["entry_block", "exit"], columns="day",
                                       values=["n", "mean", "mean_clip99", "median", "win"], sort=False)
    print(t.round(3).to_string())
