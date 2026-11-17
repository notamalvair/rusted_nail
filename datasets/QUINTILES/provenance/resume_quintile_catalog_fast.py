import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import shutil
import time

import duckdb


NORMAL = pathlib.Path(__file__).with_name("build_quintile_catalog.py")
spec = importlib.util.spec_from_file_location("catalog_base", NORMAL)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

COMPLETED_METRICS = {
    ("parent_event", "aggregate_token_amount"),
    ("parent_event", "aggregate_quote_amount"),
    ("parent_event", "initial_buy_token_amount"),
    ("parent_event", "pool_price_quote"),
    ("parent_event", "pool_price_sol"),
}


def combined_query(layer, metric, column, actions, unit_kind, split_quote, paths_by_day):
    is_leg = layer == "event_leg"
    action_column = "leg_action" if is_leg else "action_raw"
    parent_expr = "parent_action" if is_leg else "CAST(NULL AS VARCHAR)"
    quote_expr = "quote_mint" if split_quote else "CAST(NULL AS VARCHAR)"
    action_sql = ",".join(base.quote(value) for value in actions)
    pieces = []
    for day, path in paths_by_day.items():
        pieces.append(f"""
SELECT {base.quote(day)} AS sample_day, {action_column} AS action,
       {parent_expr} AS parent_action, protocol, {quote_expr} AS quote_mint,
       {column} AS metric_text, TRY_CAST({column} AS DOUBLE) AS value
FROM read_parquet({base.quote(path.as_posix())})
WHERE protocol IN ('pump','pump-amm') AND {action_column} IN ({action_sql})
""")
    population = "finite_nonnegative;actions=" + ",".join(actions) + ";protocols=pump,pump-amm"
    if split_quote:
        population += ";split=quote_mint"
    return f"""
WITH typed AS (
    {' UNION ALL '.join(pieces)}
), aggregated AS (
    SELECT
        CASE WHEN grouping(sample_day)=0 THEN sample_day ELSE NULL END AS sample_day,
        action, parent_action,
        CASE WHEN grouping(protocol)=0 THEN protocol ELSE NULL END AS protocol,
        quote_mint,
        CASE WHEN grouping(sample_day)=0 THEN 'DAY_ACTION_PROTOCOL'
             WHEN grouping(protocol)=0 THEN 'ACTION_PROTOCOL'
             ELSE 'ACTION' END AS scope,
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
    GROUP BY GROUPING SETS (
        (sample_day,action,parent_action,protocol,quote_mint),
        (action,parent_action,protocol,quote_mint),
        (action,parent_action,quote_mint)
    )
)
INSERT INTO metric_distribution
SELECT
    {base.quote(base.RUN_ID)}, {base.quote(layer)}, {base.quote(metric)}, scope,
    sample_day, action, parent_action, protocol, quote_mint,
    {base.quote(unit_kind)}, {base.quote(population)},
    n_rows, n_text_null, n_cast_failed_or_nonfinite, n_negative, n_zero, n_valid,
    min_value, quantiles[1], quantiles[2], quantiles[3], quantiles[4], quantiles[5], quantiles[6], max_value,
    CASE WHEN {str(split_quote).upper()} AND quote_mint IS NULL THEN FALSE
         WHEN n_valid < {base.MIN_VALID} THEN FALSE
         WHEN quantiles IS NULL THEN FALSE
         WHEN NOT (quantiles[2] < quantiles[3] AND quantiles[3] < quantiles[4] AND quantiles[4] < quantiles[5]) THEN FALSE
         ELSE TRUE END,
    CASE WHEN {str(split_quote).upper()} AND quote_mint IS NULL THEN 'missing_quote_mint'
         WHEN n_valid < {base.MIN_VALID} THEN 'insufficient_valid_count'
         WHEN quantiles IS NULL THEN 'no_quantiles'
         WHEN NOT (quantiles[2] < quantiles[3] AND quantiles[3] < quantiles[4] AND quantiles[4] < quantiles[5]) THEN 'non_strict_boundaries'
         ELSE NULL END,
    'approx_quantile_double', 'written_unvalidated'
FROM aggregated
"""


def finish(connection, output, started):
    # DuckDB approx_quantile can emit DBL_MIN for tiny all-zero groups. A quantile
    # cannot lie outside observed extrema, so clamp the compact result before export.
    connection.execute("""
UPDATE metric_distribution SET
    p01 = greatest(min_value, least(p01, max_value)),
    p20 = greatest(min_value, least(p20, max_value)),
    p40 = greatest(min_value, least(p40, max_value)),
    p60 = greatest(min_value, least(p60, max_value)),
    p80 = greatest(min_value, least(p80, max_value)),
    p99 = greatest(min_value, least(p99, max_value))
WHERE min_value IS NOT NULL AND max_value IS NOT NULL
""")
    assign_sql = """-- Exact decimal-text remains authoritative; DOUBLE is used only for approximate bands.
CASE
  WHEN value_double IS NULL OR NOT isfinite(value_double) OR value_double < 0 THEN NULL
  WHEN value_double <= p20 THEN 1
  WHEN value_double <= p40 THEN 2
  WHEN value_double <= p60 THEN 3
  WHEN value_double <= p80 THEN 4
  ELSE 5
END AS quintile

-- Fallback: DAY_ACTION_PROTOCOL -> ACTION_PROTOCOL -> ACTION.
-- Never cross action, quote_mint or layer. Parent events and event_legs do not mix.
"""
    (output / "assign_quintile.sql").write_text(assign_sql, encoding="utf-8")
    exports = {
        "catalog_run.parquet": "catalog_run",
        "metric_definitions.parquet": "metric_definition",
        "quintile_thresholds.parquet": "metric_distribution",
    }
    for name, table in exports.items():
        path = output / name
        if path.exists():
            path.unlink()
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
                        "bytes": path.stat().st_size,
                        "passed": source_count == parquet_count and left == 0 and right == 0}
    invalid = connection.execute("""
SELECT
  count(*) FILTER (WHERE n_rows != n_text_null + n_cast_failed_or_nonfinite + n_negative + n_valid),
  count(*) FILTER (WHERE quintiles_eligible AND NOT (n_valid >= ? AND p20 < p40 AND p40 < p60 AND p60 < p80)),
  count(*) FILTER (WHERE quintiles_eligible AND unit_kind LIKE 'quote%' AND quote_mint IS NULL),
  count(*) FILTER (WHERE p01 IS NOT NULL AND NOT (min_value <= p01 AND p01 <= p20 AND p20 <= p40 AND p40 <= p60 AND p60 <= p80 AND p80 <= p99 AND p99 <= max_value))
FROM metric_distribution
""", [base.MIN_VALID]).fetchone()
    checks["semantic"] = {"bad_accounting": invalid[0], "bad_eligible": invalid[1],
                          "missing_quote_unit": invalid[2], "bad_monotonic": invalid[3],
                          "passed": all(value == 0 for value in invalid)}
    summary = connection.execute("""
SELECT count(*), count(*) FILTER (WHERE quintiles_eligible), count(DISTINCT metric_name),
       count(DISTINCT quote_mint) FILTER (WHERE quote_mint IS NOT NULL)
FROM metric_distribution
""").fetchone()
    report = {"status": "passed" if all(value["passed"] for value in checks.values()) else "failed",
              "run_id": base.RUN_ID, "policy_version": base.POLICY_VERSION,
              "source_status": "written_unvalidated", "full_source_validation_performed": False,
              "quantile_method": "DuckDB approx_quantile over TRY_CAST(decimal_text AS DOUBLE)",
              "threshold_rows": summary[0], "eligible_threshold_rows": summary[1],
              "metrics": summary[2], "distinct_quote_mints": summary[3],
              "checks": checks, "elapsed_seconds_resume": round(time.monotonic() - started, 2),
              "files": {name: {"bytes": (output / name).stat().st_size}
                        for name in ["metric_catalog.duckdb", *exports]}}
    (output / "BUILD_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if report["status"] != "passed":
        raise RuntimeError("Small-output validation failed")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("collection", type=pathlib.Path)
    args = parser.parse_args()
    output = args.collection / "QUINTILES"
    database = output / "metric_catalog.duckdb"
    if not database.is_file():
        raise FileNotFoundError("Partial metric catalog not found")
    status_path = output / "RUN_STATUS.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    dates = ("2026-11-02", "2026-11-09", "2026-11-16")
    event_paths = {day: args.collection / day / "events.parquet" for day in dates}
    leg_paths = {day: args.collection / day / "event_legs.parquet" for day in dates}
    temp = output / "resume_temp"
    temp.mkdir(exist_ok=True)
    connection = duckdb.connect(str(database))
    started = time.monotonic()
    try:
        connection.execute("SET threads=1")
        connection.execute("SET memory_limit='384MB'")
        connection.execute("SET temp_directory=?", [str(temp)])
        connection.execute("SET max_temp_directory_size='30GB'")
        connection.execute("SET preserve_insertion_order=false")
        connection.execute("SET enable_progress_bar=false")
        metrics = [("parent_event", item, event_paths) for item in base.PARENT_METRICS]
        metrics += [("event_leg", item, leg_paths) for item in base.LEG_METRICS]
        remaining = [(layer, item, paths) for layer, item, paths in metrics
                     if (layer, item[0]) not in COMPLETED_METRICS]
        status.update(status="fast_resume", completed_metrics=len(COMPLETED_METRICS),
                      total_metrics=len(metrics), remaining_metrics=len(remaining),
                      resume_started_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        for index, (layer, item, paths) in enumerate(remaining, 1):
            metric, column, source, actions, unit, split, description = item
            connection.execute("DELETE FROM metric_distribution WHERE layer=? AND metric_name=?", [layer, metric])
            connection.execute(combined_query(layer, metric, column, actions, unit, split, paths))
            status.update(status="fast_resume", current_layer=layer, current_metric=metric,
                          resumed_metrics_completed=index, remaining_metrics_total=len(remaining),
                          elapsed_seconds_resume=round(time.monotonic() - started, 1))
            status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
            print(json.dumps({"phase": "fast_metric_complete", "metric": metric,
                              "layer": layer, "completed": index, "total": len(remaining),
                              "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)
            time.sleep(0.1)
        report = finish(connection, output, started)
        status.update(status="passed", completed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                      threshold_rows=report["threshold_rows"],
                      eligible_threshold_rows=report["eligible_threshold_rows"],
                      elapsed_seconds_resume=report["elapsed_seconds_resume"])
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
                      "elapsed_seconds": report["elapsed_seconds_resume"],
                      "files": report["files"]}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
