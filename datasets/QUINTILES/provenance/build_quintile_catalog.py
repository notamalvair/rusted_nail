import argparse
import ctypes
import datetime as dt
import json
import os
import pathlib
import shutil
import sys
import time
import uuid

import duckdb


POLICY_VERSION = "quintile-catalog-v1"
RUN_ID = "pumpapi-3days-20261102-20261116"
CORE_ACTIONS = ("create", "buy", "sell", "migrate")
PROTOCOLS = ("pump", "pump-amm")
MIN_VALID = 1000

# name, parquet column, source field, actions, unit kind, split by quote mint, description
PARENT_METRICS = [
    ("aggregate_token_amount", "aggregate_token_amount_decimal_text", "tokenAmount", ("buy", "sell"), "human_base_token_units_cross_asset", False, "Parent aggregate/net base-token amount; not gross volume."),
    ("aggregate_quote_amount", "aggregate_quote_amount_decimal_text", "quoteAmount", CORE_ACTIONS, "quote_mint_units", True, "Parent aggregate/net quote amount; unit is quote_mint."),
    ("initial_buy_token_amount", "initial_buy_token_amount_decimal_text", "initialBuy", ("create", "migrate"), "human_base_token_units_cross_asset", False, "Initial buy amount for launch/migration events."),
    ("pool_price_quote", "pool_price_quote_decimal_text", "price", CORE_ACTIONS, "quote_mint_per_base_token", True, "Provider pool-state price in quote_mint, not execution VWAP."),
    ("pool_price_sol", "pool_price_sol_decimal_text", "priceSol", CORE_ACTIONS, "sol_per_base_token", False, "Provider pool-state SOL price."),
    ("pool_price_usd", "pool_price_usd_decimal_text", "priceUsd", CORE_ACTIONS, "usd_per_base_token", False, "Provider pool-state USD price."),
    ("market_cap_quote", "market_cap_quote_decimal_text", "marketCapQuote", CORE_ACTIONS, "quote_mint_units", True, "Provider market cap in quote_mint."),
    ("market_cap_sol", "market_cap_sol_decimal_text", "marketCapSol", CORE_ACTIONS, "sol", False, "Provider market cap in SOL."),
    ("market_cap_usd", "market_cap_usd_decimal_text", "marketCapUsd", CORE_ACTIONS, "usd", False, "Provider market cap in USD."),
    ("supply", "supply_decimal_text", "supply", CORE_ACTIONS, "human_base_token_units_cross_asset", False, "Observed human-unit token supply; cross-asset thresholds are descriptive."),
    ("reserve_base_real", "reserve_base_real_decimal_text", "tokensInPool", CORE_ACTIONS, "human_base_token_units_cross_asset", False, "Observed real base reserve."),
    ("reserve_quote_real", "reserve_quote_real_decimal_text", "quoteInPool", CORE_ACTIONS, "quote_mint_units", True, "Observed real quote reserve."),
    ("curve_virtual_base", "curve_virtual_base_decimal_text", "vTokensInBondingCurve", CORE_ACTIONS, "human_base_token_units_cross_asset", False, "Virtual bonding-curve base reserve."),
    ("curve_virtual_quote", "curve_virtual_quote_decimal_text", "vQuoteInBondingCurve", CORE_ACTIONS, "quote_mint_units", True, "Virtual bonding-curve quote reserve."),
    ("amm_virtual_base", "amm_virtual_base_decimal_text", "virtualTokensInPool", CORE_ACTIONS, "human_base_token_units_cross_asset", False, "Special AMM virtual base liquidity."),
    ("amm_virtual_quote", "amm_virtual_quote_decimal_text", "virtualQuoteInPool", CORE_ACTIONS, "quote_mint_units", True, "Special AMM virtual quote liquidity."),
    ("pool_fee_fraction", "pool_fee_fraction_decimal_text", "poolFeeRate", CORE_ACTIONS, "fraction", False, "Pool fee fraction, not percentage points."),
    ("sol_route_fee_fraction", "sol_route_fee_fraction_decimal_text", "solRouteFeeRate", CORE_ACTIONS, "fraction", False, "SOL route fee fraction."),
    ("holder_reward_rate_raw", "holder_reward_rate_raw_decimal_text", "holderRewardsRate", CORE_ACTIONS, "provider_rate_raw", False, "Protocol-specific holder reward rate; semantics retained as raw rate."),
    ("reported_network_fee_sol", "reported_network_fee_sol_decimal_text", "priorityFee", CORE_ACTIONS, "sol", False, "Provider network fee observation; may repeat within one signature."),
]
LEG_METRICS = [
    ("leg_token_amount", "token_amount_decimal_text", "breakdown[].tokenAmount", ("buy", "sell", "add", "remove"), "human_base_token_units_cross_asset", False, "Breakdown operation base-token amount; do not add to parent aggregate."),
    ("leg_quote_amount", "quote_amount_decimal_text", "breakdown[].quoteAmount", ("buy", "sell", "add", "remove"), "quote_mint_units", True, "Breakdown operation quote amount; unit is inherited quote_mint."),
]
SCOPES = ("DAY_ACTION_PROTOCOL", "ACTION_PROTOCOL", "ACTION")


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (name, ctypes.c_ulonglong) for name in (
            "total_physical", "available_physical", "total_pagefile", "available_pagefile",
            "total_virtual", "available_virtual", "available_extended")]


def wait_for_memory(free_mb=450, commit_mb=1024, timeout_seconds=3600):
    if sys.platform != "win32":
        return
    kernel = ctypes.windll.kernel32
    started = time.monotonic()
    last_notice = 0
    while True:
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise RuntimeError("Cannot check available memory")
        free = status.available_physical // 1024**2
        commit = status.available_pagefile // 1024**2
        if free >= free_mb and commit >= commit_mb:
            return
        if time.monotonic() - last_notice >= 30:
            print(json.dumps({"phase": "waiting_for_memory", "available_ram_mb": free,
                              "required_ram_mb": free_mb, "available_commit_mb": commit}), flush=True)
            last_notice = time.monotonic()
        if time.monotonic() - started >= timeout_seconds:
            raise TimeoutError("Not enough free memory within one hour")
        time.sleep(5)


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def file_list(paths):
    return "[" + ",".join(quote(path.as_posix()) for path in paths) + "]"


def action_filter(actions, column):
    return column + " IN (" + ",".join(quote(value) for value in actions) + ")"


def metric_query(layer, metric, column, actions, unit_kind, split_quote, scope, paths, day=None):
    is_leg = layer == "event_leg"
    action_column = "leg_action" if is_leg else "action_raw"
    parent_expr = "parent_action" if is_leg else "CAST(NULL AS VARCHAR)"
    source = "read_parquet(" + file_list(paths) + ")"
    filters = ["protocol IN ('pump','pump-amm')", action_filter(actions, action_column)]
    day_expr = "CAST(" + quote(day) + " AS VARCHAR)" if day else "CAST(NULL AS VARCHAR)"
    protocol_expr = "protocol" if scope != "ACTION" else "CAST(NULL AS VARCHAR)"
    quote_expr = "quote_mint" if split_quote else "CAST(NULL AS VARCHAR)"
    group_columns = ["sample_day", "action", "parent_action", "protocol", "quote_mint"]
    population = "finite_nonnegative;actions=" + ",".join(actions) + ";protocols=pump,pump-amm"
    if split_quote:
        population += ";split=quote_mint"
    return f"""
WITH typed AS (
    SELECT
        {day_expr} AS sample_day,
        {action_column} AS action,
        {parent_expr} AS parent_action,
        {protocol_expr} AS protocol,
        {quote_expr} AS quote_mint,
        {column} AS metric_text,
        TRY_CAST({column} AS DOUBLE) AS value
    FROM {source}
    WHERE {' AND '.join(filters)}
), aggregated AS (
    SELECT
        sample_day, action, parent_action, protocol, quote_mint,
        count(*)::UBIGINT AS n_rows,
        count(*) FILTER (WHERE metric_text IS NULL)::UBIGINT AS n_text_null,
        count(*) FILTER (WHERE metric_text IS NOT NULL AND (value IS NULL OR NOT isfinite(value)))::UBIGINT AS n_cast_failed_or_nonfinite,
        count(*) FILTER (WHERE value IS NOT NULL AND isfinite(value) AND value < 0)::UBIGINT AS n_negative,
        count(*) FILTER (WHERE value IS NOT NULL AND isfinite(value) AND value = 0)::UBIGINT AS n_zero,
        count(*) FILTER (WHERE value IS NOT NULL AND isfinite(value) AND value >= 0)::UBIGINT AS n_valid,
        min(value) FILTER (WHERE value IS NOT NULL AND isfinite(value) AND value >= 0) AS min_value,
        approx_quantile(value, [0.01,0.20,0.40,0.60,0.80,0.99])
            FILTER (WHERE value IS NOT NULL AND isfinite(value) AND value >= 0) AS quantiles,
        max(value) FILTER (WHERE value IS NOT NULL AND isfinite(value) AND value >= 0) AS max_value
    FROM typed
    GROUP BY {','.join(group_columns)}
)
INSERT INTO metric_distribution
SELECT
    {quote(RUN_ID)}, {quote(layer)}, {quote(metric)}, {quote(scope)}, sample_day,
    action, parent_action, protocol, quote_mint, {quote(unit_kind)}, {quote(population)},
    n_rows, n_text_null, n_cast_failed_or_nonfinite, n_negative, n_zero, n_valid,
    min_value,
    quantiles[1] AS p01, quantiles[2] AS p20, quantiles[3] AS p40,
    quantiles[4] AS p60, quantiles[5] AS p80, quantiles[6] AS p99,
    max_value,
    CASE WHEN {str(split_quote).upper()} AND quote_mint IS NULL THEN FALSE
         WHEN n_valid < {MIN_VALID} THEN FALSE
         WHEN quantiles IS NULL THEN FALSE
         WHEN NOT (quantiles[2] < quantiles[3] AND quantiles[3] < quantiles[4] AND quantiles[4] < quantiles[5]) THEN FALSE
         ELSE TRUE END AS quintiles_eligible,
    CASE WHEN {str(split_quote).upper()} AND quote_mint IS NULL THEN 'missing_quote_mint'
         WHEN n_valid < {MIN_VALID} THEN 'insufficient_valid_count'
         WHEN quantiles IS NULL THEN 'no_quantiles'
         WHEN NOT (quantiles[2] < quantiles[3] AND quantiles[3] < quantiles[4] AND quantiles[4] < quantiles[5]) THEN 'non_strict_boundaries'
         ELSE NULL END AS ineligible_reason,
    'approx_quantile_double' AS quantile_method,
    'written_unvalidated' AS inherited_source_status
FROM aggregated
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("collection", type=pathlib.Path)
    args = parser.parse_args()
    output = args.collection / "QUINTILES"
    if output.exists():
        raise FileExistsError("QUINTILES already exists; refusing to overwrite")
    output.mkdir()
    temp = output / "duckdb_temp"
    temp.mkdir()
    status_path = output / "RUN_STATUS.json"
    status = {"status": "waiting_for_memory", "run_id": RUN_ID, "policy_version": POLICY_VERSION,
              "source_status": "written_unvalidated", "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "completed_queries": 0, "total_queries": (len(PARENT_METRICS) + len(LEG_METRICS)) * 5}
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    wait_for_memory()
    if sys.platform == "win32":
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)
    dates = ("2026-11-02", "2026-11-09", "2026-11-16")
    event_paths = {day: args.collection / day / "events.parquet" for day in dates}
    leg_paths = {day: args.collection / day / "event_legs.parquet" for day in dates}
    for path in list(event_paths.values()) + list(leg_paths.values()):
        if not path.is_file():
            raise FileNotFoundError(path)
    index = json.loads((args.collection / "COLLECTION_INDEX.json").read_text(encoding="utf-8"))
    database = output / "metric_catalog.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute("SET threads=1")
        connection.execute("SET memory_limit='384MB'")
        connection.execute("SET temp_directory=?", [str(temp)])
        connection.execute("SET max_temp_directory_size='30GB'")
        connection.execute("SET preserve_insertion_order=false")
        connection.execute("SET enable_progress_bar=false")
        connection.execute("""
CREATE TABLE catalog_run (
    run_id VARCHAR PRIMARY KEY, policy_version VARCHAR, created_at_utc TIMESTAMPTZ,
    duckdb_version VARCHAR, source_status VARCHAR, source_events UBIGINT,
    source_legs UBIGINT, source_parquet_bytes UBIGINT, dates_json VARCHAR,
    full_source_validation_performed BOOLEAN, quantile_method VARCHAR,
    min_valid_count UBIGINT, note VARCHAR
);
CREATE TABLE metric_definition (
    layer VARCHAR, metric_name VARCHAR, column_name VARCHAR, source_field VARCHAR,
    applicable_actions_json VARCHAR, unit_kind VARCHAR, split_by_quote_mint BOOLEAN,
    description VARCHAR, PRIMARY KEY(layer, metric_name)
);
CREATE TABLE metric_distribution (
    run_id VARCHAR, layer VARCHAR, metric_name VARCHAR, scope VARCHAR,
    sample_day VARCHAR, action VARCHAR, parent_action VARCHAR, protocol VARCHAR,
    quote_mint VARCHAR, unit_kind VARCHAR, population_policy VARCHAR,
    n_rows UBIGINT, n_text_null UBIGINT, n_cast_failed_or_nonfinite UBIGINT,
    n_negative UBIGINT, n_zero UBIGINT, n_valid UBIGINT,
    min_value DOUBLE, p01 DOUBLE, p20 DOUBLE, p40 DOUBLE, p60 DOUBLE,
    p80 DOUBLE, p99 DOUBLE, max_value DOUBLE, quintiles_eligible BOOLEAN,
    ineligible_reason VARCHAR, quantile_method VARCHAR, inherited_source_status VARCHAR
)
""")
        connection.execute("INSERT INTO catalog_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            RUN_ID, POLICY_VERSION, dt.datetime.now(dt.timezone.utc), duckdb.__version__,
            "written_unvalidated", index["total_events"], index["total_legs"],
            index["total_parquet_bytes"], json.dumps(dates), False,
            "approx_quantile_double", MIN_VALID,
            "Threshold catalog only; parent events and breakdown legs are separate populations; no strategy outcomes."
        ])
        definitions = []
        for layer, metrics in (("parent_event", PARENT_METRICS), ("event_leg", LEG_METRICS)):
            for metric, column, source, actions, unit, split, description in metrics:
                definitions.append((layer, metric, column, source, json.dumps(actions), unit, split, description))
        connection.executemany("INSERT INTO metric_definition VALUES (?,?,?,?,?,?,?,?)", definitions)
        started = time.monotonic()
        for layer, metrics, paths_by_day in (("parent_event", PARENT_METRICS, event_paths), ("event_leg", LEG_METRICS, leg_paths)):
            for metric, column, source, actions, unit, split, description in metrics:
                for day in dates:
                    wait_for_memory(300, 768)
                    connection.execute(metric_query(layer, metric, column, actions, unit, split,
                                                    "DAY_ACTION_PROTOCOL", [paths_by_day[day]], day))
                    status["completed_queries"] += 1
                    status.update(status="running", current_layer=layer, current_metric=metric,
                                  current_scope="DAY_ACTION_PROTOCOL", current_day=day,
                                  elapsed_seconds=round(time.monotonic() - started, 1))
                    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
                for scope in ("ACTION_PROTOCOL", "ACTION"):
                    wait_for_memory(300, 768)
                    connection.execute(metric_query(layer, metric, column, actions, unit, split,
                                                    scope, list(paths_by_day.values())))
                    status["completed_queries"] += 1
                    status.update(status="running", current_layer=layer, current_metric=metric,
                                  current_scope=scope, current_day=None,
                                  elapsed_seconds=round(time.monotonic() - started, 1))
                    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
                print(json.dumps({"phase": "metric_complete", "layer": layer, "metric": metric,
                                  "queries": status["completed_queries"], "total": status["total_queries"],
                                  "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)
                time.sleep(0.2)
        assign_sql = """-- Join a metric value to one eligible threshold row selected by your fallback policy.\n-- Exact decimal-text remains the source; DOUBLE conversion is only for approximate bands.\nCASE\n  WHEN value_double IS NULL OR NOT isfinite(value_double) OR value_double < 0 THEN NULL\n  WHEN value_double <= p20 THEN 1\n  WHEN value_double <= p40 THEN 2\n  WHEN value_double <= p60 THEN 3\n  WHEN value_double <= p80 THEN 4\n  ELSE 5\nEND AS quintile\n\n-- Preferred fallback: DAY_ACTION_PROTOCOL -> ACTION_PROTOCOL -> ACTION.\n-- Never cross action or quote_mint to obtain an eligible row. Parent and event_leg never mix.\n"""
        (output / "assign_quintile.sql").write_text(assign_sql, encoding="utf-8")
        exports = {
            "catalog_run.parquet": "catalog_run",
            "metric_definitions.parquet": "metric_definition",
            "quintile_thresholds.parquet": "metric_distribution",
        }
        for name, table in exports.items():
            path = output / name
            connection.execute("COPY " + table + " TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(path)])
        checks = {}
        for name, table in exports.items():
            path = output / name
            source_count = connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            parquet_count = connection.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
            left = connection.execute("SELECT count(*) FROM ((SELECT * FROM " + table + ") EXCEPT ALL (SELECT * FROM read_parquet(?)))", [str(path)]).fetchone()[0]
            right = connection.execute("SELECT count(*) FROM ((SELECT * FROM read_parquet(?)) EXCEPT ALL (SELECT * FROM " + table + "))", [str(path)]).fetchone()[0]
            checks[name] = {"database_rows": source_count, "parquet_rows": parquet_count,
                            "db_minus_parquet": left, "parquet_minus_db": right,
                            "bytes": path.stat().st_size, "passed": source_count == parquet_count and left == 0 and right == 0}
        invalid_counts = connection.execute("""
SELECT
  count(*) FILTER (WHERE n_rows != n_text_null + n_cast_failed_or_nonfinite + n_negative + n_valid) AS bad_accounting,
  count(*) FILTER (WHERE quintiles_eligible AND NOT (n_valid >= ? AND p20 < p40 AND p40 < p60 AND p60 < p80)) AS bad_eligible,
  count(*) FILTER (WHERE quintiles_eligible AND unit_kind LIKE 'quote%' AND quote_mint IS NULL) AS missing_unit,
  count(*) FILTER (WHERE p01 IS NOT NULL AND NOT (min_value <= p01 AND p01 <= p20 AND p20 <= p40 AND p40 <= p60 AND p60 <= p80 AND p80 <= p99 AND p99 <= max_value)) AS bad_monotonic
FROM metric_distribution
""", [MIN_VALID]).fetchone()
        checks["semantic"] = {"bad_accounting": invalid_counts[0], "bad_eligible": invalid_counts[1],
                              "missing_quote_unit": invalid_counts[2], "bad_monotonic": invalid_counts[3],
                              "passed": all(value == 0 for value in invalid_counts)}
        summary = connection.execute("""
SELECT count(*) rows, count(*) FILTER (WHERE quintiles_eligible) eligible,
       count(DISTINCT metric_name) metrics, count(DISTINCT quote_mint) FILTER (WHERE quote_mint IS NOT NULL) quote_mints
FROM metric_distribution
""").fetchone()
        report = {"status": "passed" if all(value["passed"] for value in checks.values()) else "failed",
                  "run_id": RUN_ID, "policy_version": POLICY_VERSION,
                  "source_status": "written_unvalidated", "full_source_validation_performed": False,
                  "quantile_method": "DuckDB approx_quantile over TRY_CAST(decimal_text AS DOUBLE)",
                  "threshold_rows": summary[0], "eligible_threshold_rows": summary[1],
                  "metrics": summary[2], "distinct_quote_mints": summary[3],
                  "checks": checks, "elapsed_seconds": round(time.monotonic() - started, 2),
                  "files": {name: {"bytes": (output / name).stat().st_size} for name in ["metric_catalog.duckdb", *exports]}}
        (output / "BUILD_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if report["status"] != "passed":
            raise RuntimeError("Small-output validation failed")
        status.update(status="passed", completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                      elapsed_seconds=report["elapsed_seconds"], threshold_rows=summary[0],
                      eligible_threshold_rows=summary[1])
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except Exception as error:
        status.update(status="failed", error_type=type(error).__name__, error=str(error),
                      failed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        raise
    finally:
        connection.close()
        shutil.rmtree(temp, ignore_errors=True)
    print(json.dumps({"phase": "catalog_ready", "output": str(output),
                      "threshold_rows": report["threshold_rows"],
                      "eligible_threshold_rows": report["eligible_threshold_rows"],
                      "elapsed_seconds": report["elapsed_seconds"],
                      "files": report["files"]}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
