"""Отбор запусков по тому, что видно к концу блока 0 (успеваем войти первыми в блоке 1).
Пороги квинтилей — только по train; validation — проверка. Выход: правила бота, выход по задержке."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from asof import load as load_features  # noqa: E402
from tape import asof_block, asof_time, load_day, net, summarize  # noqa: E402
from tape_exits import policy_returns  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

pd.set_option("display.width", 250)
pd.set_option("display.max_rows", 300)


def decision_features(day):
    ev, legs, tok = load_day(day)
    f = tok.copy()
    nd0 = legs[(~legs.is_dev) & (legs.leg_action == "buy") & (~legs.in_create_tx) & (legs.dblock == 0)]
    f["bundle_sol"] = nd0.groupby("token_idx").leg_quote.sum().reindex(f.index).fillna(0)
    f["bundle_buyers"] = nd0.groupby("token_idx").trader.nunique().reindex(f.index).fillna(0)
    devbuy = legs[legs.in_create_tx & (legs.leg_action == "buy")]
    f["dev_buy_sol"] = devbuy.groupby("token_idx").leg_quote.sum().reindex(f.index).fillna(0)
    cr = ev[ev.action == "create"].groupby("token_idx").network_fee_sol.first()
    f["create_fee_sol"] = cr.reindex(f.index)
    f = f.sort_values("create_receipt_ms")
    f["creator_prior_launches"] = f.groupby("creator").cumcount()  # только прошлые запуски
    feats = load_features()
    m = feats.set_index("create_occurrence_id").mayhem_mode
    f["mayhem"] = f.create_occurrence_id.map(m).astype(float)
    f = f.sort_index()
    f["y_rules"] = policy_returns(ev, legs, tok, 1, delayed=True)
    ok = f.index[f.full_window_s >= 120]
    f["y_hold120"] = net(asof_time(ev, 120).reindex(ok) / asof_block(ev, 0).reindex(ok))
    f["day"] = day
    return f


if __name__ == "__main__":
    d = pd.concat([decision_features(x) for x in ("2026-11-02", "2026-11-09")])
    cols = ["bundle_sol", "bundle_buyers", "dev_buy_sol", "create_fee_sol", "creator_prior_launches", "mayhem"]
    for y in ("y_rules", "y_hold120"):
        for c in cols:
            tr = d[d.day == "2026-11-02"]
            x = tr[c].dropna()
            qs = np.unique(np.r_[-np.inf, x.quantile([.2, .4, .6, .8]).values, np.inf])
            if len(qs) <= 2:
                qs = np.array([-np.inf, x.median(), np.inf])
            g = pd.cut(d[c], qs, duplicates="drop")
            rows = []
            for (day, b), yy in d[y].groupby([d.day, g], observed=True):
                rows.append(dict(day=day, bin=str(b), **summarize(yy)))
            t = pd.DataFrame(rows).pivot_table(index="bin", columns="day",
                                               values=["n", "mean", "mean_clip99", "median", "win"], sort=False)
            print(f"\n### {y} by {c}")
            print(t.round(3).to_string())
