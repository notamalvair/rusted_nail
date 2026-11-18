"""Гонка по блокам: вход в блоке create+k, выход через H секунд после создания.
Вход в блоке k: оптимистично — состояние на конец блока k-1 (мы первые в блоке k),
пессимистично — на конец блока k (последние). Только train/validation."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from tape import asof_block, asof_time, load_day, net, summarize  # noqa: E402

import pandas as pd  # noqa: E402

pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 200)
ENTRY_BLOCKS = (1, 2, 3, 5, 10, 25)
EXITS_S = (30, 120, 300, 900)


def race_table(day):
    ev, legs, tok = load_day(day)
    exits = {h: asof_time(ev, h) for h in EXITS_S}
    blocks = {k: asof_block(ev, k) for k in (0,) + ENTRY_BLOCKS + tuple(k - 1 for k in ENTRY_BLOCKS)}
    rows = []
    for k in ENTRY_BLOCKS:
        for h in EXITS_S:
            ok = tok.index[tok.full_window_s >= h]
            for side, entry in (("first_in_block", blocks[k - 1]), ("last_in_block", blocks[k])):
                r = net(exits[h].reindex(ok) / entry.reindex(ok))
                rows.append(dict(day=day, entry_block=k, exit_s=h, side=side, **summarize(r)))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    t = pd.concat([race_table(d) for d in ("2026-11-02", "2026-11-09")])
    for side in ("first_in_block", "last_in_block"):
        sub = t[t.side == side].pivot_table(index=["entry_block", "exit_s"], columns="day",
                                            values=["mean", "mean_clip99", "median", "win"], sort=False)
        print(f"\n### вход: {side}")
        print(sub.round(3).to_string())
