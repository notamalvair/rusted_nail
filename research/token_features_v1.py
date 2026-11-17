"""Базовые результаты по TOKEN_FEATURES_V1 на as-of ценах. Запуск: python3 -I research/token_features_v1.py
Только train (02.11) и validation (09.11); test (16.11) не трогаем, пока правила не зафиксированы."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from asof import load, net, summarize  # noqa: E402

import pandas as pd  # noqa: E402

pd.set_option("display.width", 250)
f = load()
f = f[f.dataset_split != "test"]


def show(title, rows):
    print(f"\n### {title}")
    print(pd.DataFrame(rows).round(3).to_string(index=False))


rows = []
for split, d in f.groupby("dataset_split"):
    for c, e in [(0, 5), (0, 120), (5, 30), (5, 120), (15, 120), (30, 120), (60, 120)]:
        rows.append(dict(split=split, entry_s=c, exit_s=e, **summarize(net(d[f"p{e}"] / d[f"p{c}"]))))
show("Купить всё на секунде entry, продать на exit (вход 0 = цена создания, недостижимый идеал)", rows)

rows = []
for split, d in f.groupby("dataset_split"):
    rules = {
        "все": d.p5.notna(),
        "дев не продал к 5с": d.dev_sell_seen_5s == False,  # noqa: E712
        "дев продал к 5с": d.dev_sell_seen_5s == True,  # noqa: E712
        "без mayhem": d.mayhem_mode == False,  # noqa: E712
        "mayhem": d.mayhem_mode == True,  # noqa: E712
    }
    for name, m in rules.items():
        rows.append(dict(split=split, rule=name, **summarize(net(d.loc[m, "p120"] / d.loc[m, "p5"]))))
show("Вход на 5-й секунде, выход на 120-й, по фильтрам", rows)
