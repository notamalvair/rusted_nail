"""Лента сделок новых токенов pump.fun (WSOL) за первые N минут после create.

Одна строка = одна операция breakdown (leg) либо событие без legs (create/migrate/...).
Цены и резервы — состояние пула ПОСЛЕ события. Нужна для бэктеста в rusted_nail:
вход в блоке 0/1/2, правила выхода, копитрейд, бандлы.

Запуск (в окружении, где уже стоит duckdb, как для build_token_features_v1.py):
    python -I export_cohort_tape_v1.py "ПУТЬ/PARQUETS_EXPORT" --describe
    python -I export_cohort_tape_v1.py "ПУТЬ/PARQUETS_EXPORT" --minutes 15

--describe только печатает колонки events.parquet и выбранную колонку блока, ничего не пишет.
Результат: PARQUETS_EXPORT/COHORT_TAPE_V1/tape_<date>.parquet + REPORT.json.
Скрипт НЕ запускался автором (нет данных и duckdb в его среде): начните с --describe.
"""

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sys
import time

import duckdb

WSOL = "So11111111111111111111111111111111111111112"
DATES = ("2026-11-02", "2026-11-09", "2026-11-16")
SPLITS = {"2026-11-02": "train", "2026-11-09": "validation", "2026-11-16": "test"}
BLOCK_CANDIDATES = ("block_number_raw", "block", "slot", "block_number", "block_slot", "source_block")
RAW_CANDIDATES = ("raw_json", "raw_payload_json", "raw_payload", "payload_json")


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def cast(column):
    return f"TRY_CAST({column} AS DOUBLE)"


def columns(connection, path):
    rows = connection.execute(f"DESCRIBE SELECT * FROM read_parquet({q(path.as_posix())})").fetchall()
    return [row[0] for row in rows]


def block_expr(cols, prefix=""):
    """Выражение номера блока: своя колонка, иначе поле block из исходного JSON."""
    for name in BLOCK_CANDIDATES:
        if name in cols:
            return f"TRY_CAST({prefix}{name} AS BIGINT)", name
    for name in RAW_CANDIDATES:
        if name in cols:
            return f"TRY_CAST(json_extract_string({prefix}{name}, '$.block') AS BIGINT)", f"{name}->$.block"
    return "CAST(NULL AS BIGINT)", "missing"


def export_day(connection, day, events_path, legs_path, out_path, minutes):
    cols = columns(connection, events_path)
    block_sql, block_source = block_expr(cols)
    block_sql_e, _ = block_expr(cols, "e.")
    window_ms = minutes * 60_000
    connection.execute(f"""
CREATE OR REPLACE TEMP TABLE cohort AS
SELECT row_number() OVER (ORDER BY provider_received_ms, source_archive_label, source_line, event_row_id) - 1 AS token_idx,
       event_row_id AS create_occurrence_id, base_mint, tx_signer AS creator,
       provider_received_ms AS create_receipt_ms, event_timestamp_ms AS create_event_ms,
       {block_sql} AS create_block
FROM read_parquet({q(events_path.as_posix())})
WHERE action_raw='create' AND protocol='pump' AND quote_mint={q(WSOL)}
  AND base_mint IS NOT NULL AND provider_received_ms IS NOT NULL AND event_timestamp_ms IS NOT NULL
""")
    # Повторный create того же mint обрезает окно предыдущего, как в build_token_features_v1.
    connection.execute(f"""
CREATE OR REPLACE TEMP TABLE cohort_iv AS
SELECT *, least(create_receipt_ms + {window_ms},
                coalesce(lead(create_receipt_ms) OVER (PARTITION BY base_mint ORDER BY create_receipt_ms, token_idx) - 1,
                         create_receipt_ms + {window_ms})) AS end_receipt_ms
FROM cohort
""")
    connection.execute(f"""
CREATE OR REPLACE TEMP TABLE ev AS
SELECT c.token_idx, e.event_row_id, e.action_raw AS action, e.protocol,
       e.provider_received_ms AS receipt_ms, e.event_timestamp_ms AS event_ms,
       {block_sql_e} AS block,
       e.source_archive_label AS archive, e.source_line AS line, e.tx_signer,
       {cast('e.pool_price_sol_decimal_text')} AS price_sol,
       {cast('e.reserve_quote_real_decimal_text')} AS reserve_quote,
       {cast('e.aggregate_quote_amount_decimal_text')} AS parent_quote_amount,
       {cast('e.reported_network_fee_sol_decimal_text')} AS network_fee_sol
FROM cohort_iv c JOIN read_parquet({q(events_path.as_posix())}) e
  ON e.base_mint = c.base_mint AND e.quote_mint = {q(WSOL)}
 AND e.provider_received_ms >= c.create_receipt_ms AND e.provider_received_ms <= c.end_receipt_ms
WHERE e.protocol IN ('pump', 'pump-amm')
  AND e.action_raw IN ('create', 'buy', 'sell', 'migrate', 'createPool', 'add', 'remove')
""")
    connection.execute(f"""
COPY (
 SELECT {q(day)} AS sample_day, {q(SPLITS[day])} AS dataset_split,
        ev.token_idx, ev.action, ev.protocol, ev.receipt_ms, ev.event_ms, ev.block,
        ev.archive, ev.line, ev.tx_signer, ev.price_sol, ev.reserve_quote,
        ev.parent_quote_amount, ev.network_fee_sol,
        l.leg_index, l.leg_action, l.trader,
        {cast('l.quote_amount_decimal_text')} AS leg_quote,
        {cast('l.token_amount_decimal_text')} AS leg_token
 FROM ev LEFT JOIN read_parquet({q(legs_path.as_posix())}) l ON l.event_row_id = ev.event_row_id
 ORDER BY ev.token_idx, ev.receipt_ms, ev.archive, ev.line, l.leg_index
) TO {q(out_path.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
""")
    tokens_path = out_path.with_name(out_path.stem.replace("tape_", "tokens_") + ".parquet")
    connection.execute(f"""
COPY (SELECT {q(day)} AS sample_day, {q(SPLITS[day])} AS dataset_split, * FROM cohort_iv ORDER BY token_idx)
TO {q(tokens_path.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)
""")
    stats = connection.execute(f"""
SELECT count(*), count(DISTINCT token_idx), count(*) FILTER (WHERE block IS NULL),
       count(*) FILTER (WHERE price_sol IS NULL OR price_sol <= 0)
FROM read_parquet({q(out_path.as_posix())})
""").fetchone()
    tokens = connection.execute(f"SELECT count(*) FROM read_parquet({q(tokens_path.as_posix())})").fetchone()[0]
    return {"date": day, "block_source": block_source, "tape_rows": stats[0], "tokens_in_tape": stats[1],
            "tokens_in_cohort": tokens, "rows_without_block": stats[2], "rows_without_price": stats[3],
            "tape_bytes": out_path.stat().st_size, "tokens_bytes": tokens_path.stat().st_size}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("collection", type=pathlib.Path)
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--dates", nargs="*", default=list(DATES))
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args()

    if args.describe:
        connection = duckdb.connect()
        for day in args.dates:
            cols = columns(connection, args.collection / day / "events.parquet")
            legs = columns(connection, args.collection / day / "event_legs.parquet")
            print(json.dumps({"date": day, "block_source": block_expr(cols)[1],
                              "events_columns": cols, "legs_columns": legs}, ensure_ascii=False, indent=1))
        return

    output = args.collection / "COHORT_TAPE_V1"
    output.mkdir(exist_ok=True)
    report = {"version": "cohort-tape-v1", "minutes": args.minutes,
              "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "dates": []}
    started = time.monotonic()
    for day in args.dates:
        out_path = output / f"tape_{day}.parquet"
        if out_path.exists():
            raise FileExistsError(f"{out_path} exists; refusing overwrite")
        temp = output / f"temp_{day}"
        temp.mkdir(exist_ok=True)
        connection = duckdb.connect()
        try:
            connection.execute("SET threads=1")
            connection.execute("SET memory_limit='384MB'")
            connection.execute("SET temp_directory=?", [str(temp)])
            connection.execute("SET max_temp_directory_size='30GB'")
            connection.execute("SET preserve_insertion_order=false")
            connection.execute("SET enable_progress_bar=false")
            result = export_day(connection, day, args.collection / day / "events.parquet",
                                args.collection / day / "event_legs.parquet", out_path, args.minutes)
        finally:
            connection.close()
            shutil.rmtree(temp, ignore_errors=True)
        result["elapsed_seconds"] = round(time.monotonic() - started, 1)
        report["dates"].append(result)
        (output / "REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    report["status"] = "written"
    (output / "REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
