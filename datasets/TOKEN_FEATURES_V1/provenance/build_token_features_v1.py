import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import sys
import time

import duckdb
import pyarrow.parquet as pq


VERSION = "pump-create-features-v1"
WSOL = "So11111111111111111111111111111111111111112"
WINDOWS = (5, 15, 30, 60, 120)
CUTOFFS = WINDOWS
HORIZONS = (30, 60, 120, 300, 900)
FEE = 0.015
SLIPPAGE = 0.03
ENTRY_DELAY_MS = 400
ENTRY_TOLERANCE_MS = 2000
ENDPOINT_TOLERANCE_MS = 5000
TRACK_MS = max(CUTOFFS) * 1000 + ENTRY_DELAY_MS + ENTRY_TOLERANCE_MS + max(HORIZONS) * 1000 + ENDPOINT_TOLERANCE_MS
DATES = ("2026-11-02", "2026-11-09", "2026-11-16")
SPLITS = {"2026-11-02": "train", "2026-11-09": "validation", "2026-11-16": "test"}


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (name, ctypes.c_ulonglong) for name in (
            "total_physical", "available_physical", "total_pagefile", "available_pagefile",
            "total_virtual", "available_virtual", "available_extended")]


def wait_for_memory(free_mb=280, commit_mb=1024, timeout=3600):
    if sys.platform != "win32":
        return
    kernel = ctypes.windll.kernel32
    started = time.monotonic()
    last = 0
    while True:
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise RuntimeError("Cannot query memory")
        free = status.available_physical // 1024**2
        commit = status.available_pagefile // 1024**2
        if free >= free_mb and commit >= commit_mb:
            return
        if time.monotonic() - last >= 30:
            print(json.dumps({"phase": "waiting_for_memory", "free_ram_mb": free,
                              "required_ram_mb": free_mb, "free_commit_mb": commit}), flush=True)
            last = time.monotonic()
        if time.monotonic() - started >= timeout:
            raise TimeoutError("Memory threshold not reached within one hour")
        time.sleep(5)


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cast(column):
    return f"TRY_CAST({column} AS DOUBLE)"


def feature_aggregate_sql(table, prefix, is_leg=False):
    expressions = []
    for window in WINDOWS:
        limit = window * 1000
        if is_leg:
            expressions.extend([
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='buy') AS {prefix}buy_count_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='sell') AS {prefix}sell_count_{window}s_raw",
                f"count(DISTINCT parent_event_row_id) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='buy') AS {prefix}buy_parent_events_{window}s_raw",
                f"count(DISTINCT parent_event_row_id) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='sell') AS {prefix}sell_parent_events_{window}s_raw",
                f"count(DISTINCT trader) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='buy' AND trader IS NOT NULL AND trader!='') AS unique_buyers_{window}s_raw",
                f"count(DISTINCT trader) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='sell' AND trader IS NOT NULL AND trader!='') AS unique_sellers_{window}s_raw",
                f"count(DISTINCT trader) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND trader IS NOT NULL AND trader!='') AS unique_traders_{window}s_raw",
                f"coalesce(sum(token_amount) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='buy' AND token_amount>=0),0) AS {prefix}buy_token_gross_{window}s_raw",
                f"coalesce(sum(token_amount) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='sell' AND token_amount>=0),0) AS {prefix}sell_token_gross_{window}s_raw",
                f"coalesce(sum(quote_amount) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='buy' AND quote_amount>=0),0) AS {prefix}buy_quote_gross_{window}s_raw",
                f"coalesce(sum(quote_amount) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='sell' AND quote_amount>=0),0) AS {prefix}sell_quote_gross_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND token_text IS NOT NULL AND token_amount IS NULL) AS leg_token_cast_failed_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND quote_text IS NOT NULL AND quote_amount IS NULL) AS leg_quote_cast_failed_{window}s_raw",
                f"min(dt_receipt_ms) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action IN ('buy','sell')) AS first_trade_delay_ms_{window}s_raw",
                f"max(dt_receipt_ms) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action IN ('buy','sell')) AS last_trade_delay_ms_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND leg_action='sell' AND trader=create_signer) > 0 AS dev_sell_seen_{window}s_raw",
            ])
        else:
            order = "struct_pack(provider_ms := provider_received_ms, source_label := source_archive_label, source_line := source_line, event_id := parent_event_row_id)"
            expressions.extend([
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND action_raw='buy') AS parent_buy_events_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND action_raw='sell') AS parent_sell_events_{window}s_raw",
                f"coalesce(sum(parent_quote_amount) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND action_raw='buy' AND parent_quote_amount>=0),0) AS parent_buy_quote_sum_{window}s_raw",
                f"coalesce(sum(parent_quote_amount) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND action_raw='sell' AND parent_quote_amount>=0),0) AS parent_sell_quote_sum_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND action_raw IN ('buy','sell') AND breakdown_status!='present') AS missing_breakdown_trade_events_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND price_sol>0) AS price_snapshot_count_{window}s_raw",
                f"arg_max(price_sol,{order}) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND price_sol>0) AS last_price_sol_{window}s_raw",
                f"min(price_sol) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND price_sol>0) AS min_price_sol_{window}s_raw",
                f"max(price_sol) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND price_sol>0) AS max_price_sol_{window}s_raw",
                f"max(dt_receipt_ms) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND price_sol>0) AS last_price_delay_ms_{window}s_raw",
                f"arg_max(market_cap_sol,{order}) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND market_cap_sol>=0) AS last_market_cap_sol_{window}s_raw",
                f"arg_max(reserve_base,{order}) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND reserve_base>=0) AS last_reserve_base_{window}s_raw",
                f"arg_max(reserve_quote,{order}) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND reserve_quote>=0) AS last_reserve_quote_{window}s_raw",
                f"arg_max(curve_virtual_base,{order}) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND curve_virtual_base>=0) AS last_curve_virtual_base_{window}s_raw",
                f"arg_max(curve_virtual_quote,{order}) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND curve_virtual_quote>=0) AS last_curve_virtual_quote_{window}s_raw",
                f"count(DISTINCT pool_id) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND pool_id IS NOT NULL) AS pool_count_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND action_raw='migrate') > 0 AS migrated_by_{window}s_raw",
                f"min(dt_receipt_ms) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND action_raw='migrate') AS first_migration_delay_ms_{window}s_raw",
                f"count(*) FILTER (WHERE dt_receipt_ms > 0 AND dt_receipt_ms <= {limit} AND price_text IS NOT NULL AND price_sol IS NULL) AS price_cast_failed_{window}s_raw",
            ])
    return f"CREATE TEMP TABLE {table}_features AS SELECT create_occurrence_id,\n" + ",\n".join(expressions) + f"\nFROM {table} GROUP BY create_occurrence_id"


def feature_final_select():
    fields = [
        "c.sample_day", "c.dataset_split", "c.create_occurrence_id", "c.event_row_id",
        "c.source_archive_sha256", "c.source_archive_label", "c.source_line", "c.raw_payload_sha256",
        "c.tx_signature", "c.tx_signer", "c.base_mint", "c.quote_mint", "c.pool_id AS create_pool_id",
        "c.event_timestamp_ms AS create_event_timestamp_ms", "c.provider_received_ms AS create_provider_received_ms",
        "c.provider_received_ms-c.event_timestamp_ms AS create_receipt_minus_event_ms",
        "c.token_name", "c.token_symbol", "c.token_program", "c.mint_authority", "c.freeze_authority",
        "c.creator_fee_address", "c.mayhem_mode", "c.cashback_enabled", "c.breakdown_status AS create_breakdown_status",
        "c.breakdown_count AS create_breakdown_count", "c.initial_buy_text", "c.initial_buy_value",
        "c.create_price_sol_text", "c.create_price_sol", "c.create_market_cap_sol_text", "c.create_market_cap_sol",
        "c.create_reserve_base_text", "c.create_reserve_base", "c.create_reserve_quote_text", "c.create_reserve_quote",
        "c.create_curve_virtual_base_text", "c.create_curve_virtual_base", "c.create_curve_virtual_quote_text", "c.create_curve_virtual_quote",
        "c.create_pool_fee_text", "c.create_pool_fee_fraction", "c.create_network_fee_text", "c.create_network_fee_sol",
        "c.exact_payload_occurrence_rank", "c.logical_create_rank",
        "c.logical_create_rank=1 AS is_primary_occurrence", "i.initial_buy_leg_count", "i.initial_sell_leg_count",
        "i.initial_buy_quote_gross", "i.initial_sell_quote_gross", "i.initial_buy_token_gross", "i.initial_sell_token_gross",
        "i.initial_unique_traders", "i.initial_leg_cast_failed_count",
    ]
    for window in WINDOWS:
        coverage = f"c.provider_received_ms+{window*1000}<=c.date_max_provider_ms"
        fields.append(f"{coverage} AS feature_window_observed_{window}s")
        for name in (
            "parent_buy_events", "parent_sell_events", "parent_buy_quote_sum", "parent_sell_quote_sum",
            "missing_breakdown_trade_events", "price_snapshot_count", "last_price_sol", "min_price_sol",
            "max_price_sol", "last_market_cap_sol", "last_reserve_base", "last_reserve_quote",
            "last_curve_virtual_base", "last_curve_virtual_quote", "pool_count", "migrated_by",
            "first_migration_delay_ms", "price_cast_failed",
        ):
            zero_names = {"parent_buy_events", "parent_sell_events", "parent_buy_quote_sum", "parent_sell_quote_sum",
                          "missing_breakdown_trade_events", "price_snapshot_count", "pool_count", "price_cast_failed"}
            if name == "migrated_by":
                expression = f"coalesce(p.{name}_{window}s_raw,false)"
            else:
                expression = f"coalesce(p.{name}_{window}s_raw,0)" if name in zero_names else f"p.{name}_{window}s_raw"
            fields.append(f"CASE WHEN {coverage} THEN {expression} ELSE NULL END AS {name}_{window}s")
        fields.append(f"CASE WHEN {coverage} AND p.last_price_delay_ms_{window}s_raw IS NOT NULL THEN {window*1000}-p.last_price_delay_ms_{window}s_raw ELSE NULL END AS snapshot_age_ms_{window}s")
        fields.append(f"CASE WHEN {coverage} AND c.create_price_sol>0 AND p.last_price_sol_{window}s_raw>0 THEN p.last_price_sol_{window}s_raw/c.create_price_sol-1 ELSE NULL END AS past_return_{window}s")
        fields.append(f"CASE WHEN {coverage} AND c.create_price_sol>0 AND p.max_price_sol_{window}s_raw>0 THEN p.max_price_sol_{window}s_raw/c.create_price_sol-1 ELSE NULL END AS past_max_drawup_{window}s")
        fields.append(f"CASE WHEN {coverage} AND c.create_price_sol>0 AND p.min_price_sol_{window}s_raw>0 THEN p.min_price_sol_{window}s_raw/c.create_price_sol-1 ELSE NULL END AS past_max_drawdown_{window}s")
        for name in (
            "leg_buy_count", "leg_sell_count", "leg_buy_parent_events", "leg_sell_parent_events",
            "unique_buyers", "unique_sellers", "unique_traders", "leg_buy_token_gross", "leg_sell_token_gross",
            "leg_buy_quote_gross", "leg_sell_quote_gross", "leg_token_cast_failed", "leg_quote_cast_failed",
            "first_trade_delay_ms", "last_trade_delay_ms", "dev_sell_seen",
        ):
            zero_names = {"leg_buy_count", "leg_sell_count", "leg_buy_parent_events", "leg_sell_parent_events",
                          "unique_buyers", "unique_sellers", "unique_traders", "leg_buy_token_gross", "leg_sell_token_gross",
                          "leg_buy_quote_gross", "leg_sell_quote_gross", "leg_token_cast_failed", "leg_quote_cast_failed"}
            expression = f"coalesce(l.{name}_{window}s_raw,0)" if name in zero_names else f"l.{name}_{window}s_raw"
            fields.append(f"CASE WHEN {coverage} THEN {expression} ELSE NULL END AS {name}_{window}s")
        fields.append(f"CASE WHEN {coverage} THEN coalesce(l.leg_buy_quote_gross_{window}s_raw,0)-coalesce(l.leg_sell_quote_gross_{window}s_raw,0) ELSE NULL END AS leg_net_quote_flow_{window}s")
        fields.append(f"CASE WHEN {coverage} THEN coalesce(p.missing_breakdown_trade_events_{window}s_raw,0)=0 ELSE NULL END AS leg_detail_complete_{window}s")
    return ",\n".join(fields)


def build_daily(connection, day, events_path, legs_path, output_dir):
    max_provider, max_event = connection.execute(
        "SELECT max(provider_received_ms),max(event_timestamp_ms) FROM read_parquet(?)", [str(events_path)]).fetchone()
    if max_provider is None or max_event is None:
        raise RuntimeError("Missing date bounds")
    connection.execute(f"""
CREATE TEMP TABLE create_all AS
WITH base AS (
 SELECT {q(day)} AS sample_day, {q(SPLITS[day])} AS dataset_split,
   event_row_id, source_archive_sha256, source_archive_label, source_line, raw_payload_sha256,
   tx_signature, tx_signer, base_mint, quote_mint, pool_id, event_timestamp_ms, provider_received_ms,
   token_name, token_symbol, token_program, mint_authority, freeze_authority, creator_fee_address,
   mayhem_mode, cashback_enabled, breakdown_status, breakdown_count,
   initial_buy_token_amount_decimal_text AS initial_buy_text,
   {cast('initial_buy_token_amount_decimal_text')} AS initial_buy_value,
   pool_price_sol_decimal_text AS create_price_sol_text,
   {cast('pool_price_sol_decimal_text')} AS create_price_sol,
   market_cap_sol_decimal_text AS create_market_cap_sol_text,
   {cast('market_cap_sol_decimal_text')} AS create_market_cap_sol,
   reserve_base_real_decimal_text AS create_reserve_base_text,
   {cast('reserve_base_real_decimal_text')} AS create_reserve_base,
   reserve_quote_real_decimal_text AS create_reserve_quote_text,
   {cast('reserve_quote_real_decimal_text')} AS create_reserve_quote,
   curve_virtual_base_decimal_text AS create_curve_virtual_base_text,
   {cast('curve_virtual_base_decimal_text')} AS create_curve_virtual_base,
   curve_virtual_quote_decimal_text AS create_curve_virtual_quote_text,
   {cast('curve_virtual_quote_decimal_text')} AS create_curve_virtual_quote,
   pool_fee_fraction_decimal_text AS create_pool_fee_text,
   {cast('pool_fee_fraction_decimal_text')} AS create_pool_fee_fraction,
   reported_network_fee_sol_decimal_text AS create_network_fee_text,
   {cast('reported_network_fee_sol_decimal_text')} AS create_network_fee_sol,
   row_number() OVER (PARTITION BY raw_payload_sha256 ORDER BY provider_received_ms,source_archive_label,source_line,event_row_id) AS exact_payload_occurrence_rank,
   count(*) OVER (PARTITION BY raw_payload_sha256) AS exact_payload_occurrence_count,
   row_number() OVER (PARTITION BY base_mint,quote_mint,pool_id ORDER BY provider_received_ms,source_archive_label,source_line,event_row_id) AS logical_create_rank,
   count(*) OVER (PARTITION BY base_mint,quote_mint,pool_id) AS logical_create_count
 FROM read_parquet({q(events_path.as_posix())})
 WHERE action_raw='create' AND protocol='pump'
)
SELECT *,
 quote_mint={q(WSOL)} AND base_mint IS NOT NULL AND provider_received_ms IS NOT NULL AND event_timestamp_ms IS NOT NULL AS included_in_cohort,
 CASE WHEN quote_mint IS NULL THEN 'missing_quote_mint'
      WHEN quote_mint!={q(WSOL)} THEN 'non_wsol_quote'
      WHEN base_mint IS NULL THEN 'missing_base_mint'
      WHEN provider_received_ms IS NULL THEN 'missing_provider_received_ms'
      WHEN event_timestamp_ms IS NULL THEN 'missing_event_timestamp_ms'
      ELSE NULL END AS exclusion_reason
FROM base
""")
    connection.execute(f"""
CREATE TEMP TABLE cohort AS
WITH ordered AS (
 SELECT *, lead(provider_received_ms) OVER (
   PARTITION BY base_mint,quote_mint ORDER BY provider_received_ms,source_archive_label,source_line,event_row_id
 ) AS next_create_receipt_ms
 FROM create_all WHERE included_in_cohort
)
SELECT event_row_id AS create_occurrence_id, *, {max_provider}::BIGINT AS date_max_provider_ms,
       {max_event}::BIGINT AS date_max_event_ms,
       CASE WHEN next_create_receipt_ms IS NULL THEN provider_received_ms+{TRACK_MS}
            WHEN next_create_receipt_ms>provider_received_ms THEN least(provider_received_ms+{TRACK_MS},next_create_receipt_ms-1)
            ELSE provider_received_ms END AS interval_end_receipt_ms
FROM ordered
""")
    connection.execute(f"""
CREATE TEMP TABLE events_slim AS
SELECT event_row_id, source_archive_label, source_line, action_raw, protocol, pool_id, base_mint, quote_mint,
       event_timestamp_ms, provider_received_ms, breakdown_status,
       aggregate_quote_amount_decimal_text AS parent_quote_text,
       {cast('aggregate_quote_amount_decimal_text')} AS parent_quote_amount,
       pool_price_sol_decimal_text AS price_text, {cast('pool_price_sol_decimal_text')} AS price_sol,
       {cast('market_cap_sol_decimal_text')} AS market_cap_sol,
       {cast('reserve_base_real_decimal_text')} AS reserve_base,
       {cast('reserve_quote_real_decimal_text')} AS reserve_quote,
       {cast('curve_virtual_base_decimal_text')} AS curve_virtual_base,
       {cast('curve_virtual_quote_decimal_text')} AS curve_virtual_quote
FROM read_parquet({q(events_path.as_posix())})
WHERE quote_mint={q(WSOL)} AND base_mint IS NOT NULL
  AND protocol IN ('pump','pump-amm')
  AND action_raw IN ('create','buy','sell','migrate','createPool','add','remove')
  AND provider_received_ms IS NOT NULL AND event_timestamp_ms IS NOT NULL
""")
    connection.execute("""
CREATE TEMP TABLE event_map AS
SELECT c.create_occurrence_id, c.tx_signer AS create_signer,
       e.event_row_id AS parent_event_row_id, e.source_archive_label, e.source_line,
       e.action_raw, e.protocol, e.pool_id, e.base_mint, e.quote_mint,
       e.event_timestamp_ms, e.provider_received_ms,
       e.provider_received_ms-c.provider_received_ms AS dt_receipt_ms,
       e.event_timestamp_ms-c.event_timestamp_ms AS dt_event_ms,
       e.breakdown_status, e.parent_quote_text, e.parent_quote_amount,
       e.price_text, e.price_sol, e.market_cap_sol, e.reserve_base, e.reserve_quote,
       e.curve_virtual_base, e.curve_virtual_quote
FROM cohort c JOIN events_slim e
  ON e.base_mint=c.base_mint AND e.quote_mint=c.quote_mint
 AND e.provider_received_ms>c.provider_received_ms
 AND e.provider_received_ms<=c.interval_end_receipt_ms
 AND e.event_row_id!=c.create_occurrence_id
""")
    connection.execute(f"""
CREATE TEMP TABLE initial_leg_features AS
SELECT c.create_occurrence_id,
 count(*) FILTER (WHERE l.leg_action='buy') AS initial_buy_leg_count,
 count(*) FILTER (WHERE l.leg_action='sell') AS initial_sell_leg_count,
 coalesce(sum({cast('l.quote_amount_decimal_text')}) FILTER (WHERE l.leg_action='buy' AND {cast('l.quote_amount_decimal_text')}>=0),0) AS initial_buy_quote_gross,
 coalesce(sum({cast('l.quote_amount_decimal_text')}) FILTER (WHERE l.leg_action='sell' AND {cast('l.quote_amount_decimal_text')}>=0),0) AS initial_sell_quote_gross,
 coalesce(sum({cast('l.token_amount_decimal_text')}) FILTER (WHERE l.leg_action='buy' AND {cast('l.token_amount_decimal_text')}>=0),0) AS initial_buy_token_gross,
 coalesce(sum({cast('l.token_amount_decimal_text')}) FILTER (WHERE l.leg_action='sell' AND {cast('l.token_amount_decimal_text')}>=0),0) AS initial_sell_token_gross,
 count(DISTINCT l.trader) FILTER (WHERE l.trader IS NOT NULL AND l.trader!='') AS initial_unique_traders,
 count(*) FILTER (WHERE (l.token_amount_decimal_text IS NOT NULL AND {cast('l.token_amount_decimal_text')} IS NULL)
                       OR (l.quote_amount_decimal_text IS NOT NULL AND {cast('l.quote_amount_decimal_text')} IS NULL)) AS initial_leg_cast_failed_count
FROM cohort c LEFT JOIN read_parquet({q(legs_path.as_posix())}) l ON l.event_row_id=c.create_occurrence_id
GROUP BY c.create_occurrence_id
""")
    connection.execute(f"""
CREATE TEMP TABLE mapped_legs AS
SELECT em.create_occurrence_id, em.create_signer, em.parent_event_row_id,
       em.dt_receipt_ms, em.dt_event_ms, em.event_timestamp_ms, em.provider_received_ms,
       l.leg_index, l.leg_action, l.trader,
       l.token_amount_decimal_text AS token_text, {cast('l.token_amount_decimal_text')} AS token_amount,
       l.quote_amount_decimal_text AS quote_text, {cast('l.quote_amount_decimal_text')} AS quote_amount
FROM event_map em JOIN read_parquet({q(legs_path.as_posix())}) l ON l.event_row_id=em.parent_event_row_id
WHERE l.leg_action IN ('buy','sell','add','remove')
""")
    connection.execute(feature_aggregate_sql("event_map", "parent_", False))
    connection.execute(feature_aggregate_sql("mapped_legs", "leg_", True))
    feature_path = output_dir / f"features_{day}.parquet"
    connection.execute(f"""
COPY (
 SELECT {feature_final_select()}
 FROM cohort c
 LEFT JOIN initial_leg_features i USING(create_occurrence_id)
 LEFT JOIN event_map_features p USING(create_occurrence_id)
 LEFT JOIN mapped_legs_features l USING(create_occurrence_id)
 ORDER BY c.provider_received_ms,c.source_archive_label,c.source_line,c.event_row_id
) TO {q(feature_path.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)
""")
    audit_path = output_dir / f"audit_{day}.parquet"
    connection.execute(f"""
COPY (
 SELECT sample_day,dataset_split,event_row_id AS create_occurrence_id,source_archive_sha256,
        source_archive_label,source_line,raw_payload_sha256,base_mint,quote_mint,pool_id,
        event_timestamp_ms,provider_received_ms,included_in_cohort,exclusion_reason,
        exact_payload_occurrence_rank,exact_payload_occurrence_count,
        logical_create_rank,logical_create_count,logical_create_rank=1 AS is_primary_occurrence,
        breakdown_status,breakdown_count,
        provider_received_ms+5000<={max_provider} AS coverage_5s,
        provider_received_ms+15000<={max_provider} AS coverage_15s,
        provider_received_ms+30000<={max_provider} AS coverage_30s,
        provider_received_ms+60000<={max_provider} AS coverage_60s,
        provider_received_ms+120000<={max_provider} AS coverage_120s
 FROM create_all ORDER BY provider_received_ms,source_archive_label,source_line,event_row_id
) TO {q(audit_path.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)
""")
    cutoffs = ",".join(f"({value})" for value in CUTOFFS)
    horizons = ",".join(f"({value})" for value in HORIZONS)
    connection.execute(f"""
CREATE TEMP TABLE signals AS
SELECT c.*, x.cutoff_s, c.provider_received_ms+x.cutoff_s*1000+{ENTRY_DELAY_MS} AS entry_due_receipt_ms
FROM cohort c CROSS JOIN (VALUES {cutoffs}) x(cutoff_s)
WHERE c.provider_received_ms+x.cutoff_s*1000<=c.date_max_provider_ms
""")
    connection.execute(f"""
CREATE TEMP TABLE entries AS
SELECT s.create_occurrence_id,s.sample_day,s.dataset_split,s.base_mint,s.quote_mint,
       s.tx_signer AS create_signer,s.cutoff_s,s.date_max_event_ms,
       e.parent_event_row_id AS entry_event_row_id,e.event_timestamp_ms AS entry_event_timestamp_ms,
       e.provider_received_ms AS entry_provider_received_ms,e.price_sol AS entry_price_sol,
       e.provider_received_ms-s.entry_due_receipt_ms AS entry_delay_after_due_ms
FROM signals s LEFT JOIN LATERAL (
  SELECT * FROM event_map e
  WHERE e.create_occurrence_id=s.create_occurrence_id AND e.price_sol>0
    AND e.provider_received_ms>=s.entry_due_receipt_ms
    AND e.provider_received_ms<=s.entry_due_receipt_ms+{ENTRY_TOLERANCE_MS}
  ORDER BY e.provider_received_ms,e.source_archive_label,e.source_line,e.parent_event_row_id LIMIT 1
) e ON TRUE
""")
    connection.execute(f"""
CREATE TEMP TABLE outcome_grid AS
SELECT e.*, h.horizon_s,
       e.entry_event_timestamp_ms+h.horizon_s*1000 AS target_event_timestamp_ms,
       e.create_occurrence_id||':'||e.cutoff_s::VARCHAR||':'||h.horizon_s::VARCHAR AS outcome_id
FROM entries e CROSS JOIN (VALUES {horizons}) h(horizon_s)
WHERE h.horizon_s>e.cutoff_s
""")
    connection.execute(f"""
CREATE TEMP TABLE outcome_ordered AS
SELECT g.outcome_id,g.create_occurrence_id,g.cutoff_s,g.horizon_s,
       em.parent_event_row_id,em.event_timestamp_ms,em.provider_received_ms,em.price_sol,
       max(em.price_sol) OVER (PARTITION BY g.outcome_id ORDER BY em.event_timestamp_ms,em.provider_received_ms,em.source_line,em.parent_event_row_id ROWS UNBOUNDED PRECEDING) AS running_peak
FROM outcome_grid g JOIN event_map em ON em.create_occurrence_id=g.create_occurrence_id
WHERE g.entry_price_sol>0 AND em.price_sol>0
  AND em.event_timestamp_ms>=g.entry_event_timestamp_ms
  AND em.event_timestamp_ms<=g.target_event_timestamp_ms
""")
    connection.execute("""
CREATE TEMP TABLE outcome_stats AS
SELECT outcome_id,
 arg_min(price_sol,struct_pack(event_ms := event_timestamp_ms, provider_ms := provider_received_ms, event_id := parent_event_row_id))
   FILTER (WHERE event_timestamp_ms>=g.target_event_timestamp_ms AND event_timestamp_ms<=g.target_event_timestamp_ms+5000) AS endpoint_price_sol,
 min(event_timestamp_ms) FILTER (WHERE event_timestamp_ms>=g.target_event_timestamp_ms AND event_timestamp_ms<=g.target_event_timestamp_ms+5000) AS endpoint_event_timestamp_ms,
 max(price_sol) AS max_price_sol, min(price_sol) AS min_price_sol,
 min(event_timestamp_ms) FILTER (WHERE price_sol>=g.entry_price_sol*1.5) AS first_take_profit_event_ms,
 min(event_timestamp_ms) FILTER (WHERE price_sol<=g.entry_price_sol*0.8) AS first_stop_loss_event_ms,
 min(event_timestamp_ms) FILTER (WHERE price_sol<=running_peak*0.75) AS first_trailing_25_event_ms
FROM outcome_ordered o JOIN outcome_grid g USING(outcome_id)
GROUP BY outcome_id
""")
    connection.execute("""
CREATE TEMP TABLE outcome_dev_sell AS
SELECT g.outcome_id,min(l.event_timestamp_ms) AS first_dev_sell_event_ms
FROM outcome_grid g JOIN mapped_legs l ON l.create_occurrence_id=g.create_occurrence_id
WHERE g.entry_price_sol>0 AND l.leg_action='sell' AND l.trader=g.create_signer
  AND l.event_timestamp_ms>=g.entry_event_timestamp_ms
  AND l.event_timestamp_ms<=g.target_event_timestamp_ms
GROUP BY g.outcome_id
""")
    outcome_path = output_dir / f"outcomes_{day}.parquet"
    net_factor = ((1 - FEE) ** 2) * ((1 - SLIPPAGE) / (1 + SLIPPAGE))
    connection.execute(f"""
COPY (
 SELECT g.sample_day,g.dataset_split,g.create_occurrence_id,g.base_mint,g.quote_mint,
        g.cutoff_s,g.horizon_s,g.entry_event_row_id,g.entry_event_timestamp_ms,
        g.entry_provider_received_ms,g.entry_price_sol,g.entry_delay_after_due_ms,
        g.entry_price_sol IS NOT NULL AS entry_price_found,
        g.entry_event_timestamp_ms IS NOT NULL AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms AS outcome_window_observed,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.endpoint_event_timestamp_ms END AS endpoint_event_timestamp_ms,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.endpoint_price_sol END AS endpoint_price_sol,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.endpoint_price_sol IS NOT NULL END AS endpoint_price_found,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms AND s.endpoint_price_sol>0 THEN s.endpoint_price_sol/g.entry_price_sol-1 END AS gross_return,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms AND s.endpoint_price_sol>0 THEN {net_factor}*(s.endpoint_price_sol/g.entry_price_sol)-1 END AS net_roundtrip_proxy,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms AND s.max_price_sol>0 THEN s.max_price_sol/g.entry_price_sol-1 END AS mfe_return,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms AND s.min_price_sol>0 THEN s.min_price_sol/g.entry_price_sol-1 END AS mae_return,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.first_take_profit_event_ms END AS first_take_profit_event_ms,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.first_stop_loss_event_ms END AS first_stop_loss_event_ms,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.first_trailing_25_event_ms END AS first_trailing_25_event_ms,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN d.first_dev_sell_event_ms END AS first_dev_sell_event_ms,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.first_take_profit_event_ms IS NOT NULL END AS take_profit_50_hit,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.first_stop_loss_event_ms IS NOT NULL END AS stop_loss_20_hit,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN s.first_trailing_25_event_ms IS NOT NULL END AS trailing_25_hit,
        CASE WHEN g.entry_price_sol>0 AND g.entry_event_timestamp_ms+g.horizon_s*1000<=g.date_max_event_ms THEN d.first_dev_sell_event_ms IS NOT NULL END AS dev_sell_after_entry,
        g.horizon_s=120 AS rust_max_hold_120_row,
        {FEE}::DOUBLE AS fee_fraction_each_side,{SLIPPAGE}::DOUBLE AS slippage_fraction,
        {ENTRY_DELAY_MS}::INTEGER AS modeled_entry_delay_ms,{ENTRY_TOLERANCE_MS}::INTEGER AS entry_tolerance_ms,
        {ENDPOINT_TOLERANCE_MS}::INTEGER AS endpoint_tolerance_ms,
        'pool_state_price_proxy_not_fill_vwap' AS outcome_method,
        'written_unvalidated' AS inherited_source_status
 FROM outcome_grid g LEFT JOIN outcome_stats s USING(outcome_id)
 LEFT JOIN outcome_dev_sell d USING(outcome_id)
 ORDER BY g.create_occurrence_id,g.cutoff_s,g.horizon_s
) TO {q(outcome_path.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)
""")
    counts = {
        "date": day,
        "all_pump_creates": connection.execute("SELECT count(*) FROM create_all").fetchone()[0],
        "included_wsol_creates": connection.execute("SELECT count(*) FROM cohort").fetchone()[0],
        "features": connection.execute("SELECT count(*) FROM read_parquet(?)", [str(feature_path)]).fetchone()[0],
        "audit": connection.execute("SELECT count(*) FROM read_parquet(?)", [str(audit_path)]).fetchone()[0],
        "outcomes": connection.execute("SELECT count(*) FROM read_parquet(?)", [str(outcome_path)]).fetchone()[0],
        "excluded_reasons": connection.execute("SELECT coalesce(exclusion_reason,'included'),count(*) FROM create_all GROUP BY 1 ORDER BY 2 DESC").fetchall(),
        "date_max_provider_ms": max_provider,
        "date_max_event_ms": max_event,
    }
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("collection", type=pathlib.Path)
    args = parser.parse_args()
    output = args.collection / "TOKEN_FEATURES_V1"
    if output.exists():
        raise FileExistsError("TOKEN_FEATURES_V1 exists; refusing overwrite")
    output.mkdir()
    working = output / "working"
    working.mkdir()
    status_path = output / "RUN_STATUS.json"
    status = {"status": "waiting_for_memory", "version": VERSION,
              "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "completed_dates": []}
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    if sys.platform == "win32":
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)
    results = []
    started = time.monotonic()
    try:
        for day in DATES:
            wait_for_memory()
            temp = working / ("temp_" + day)
            temp.mkdir()
            database = working / ("build_" + day + ".duckdb")
            connection = duckdb.connect(str(database))
            try:
                connection.execute("SET threads=1")
                connection.execute("SET memory_limit='384MB'")
                connection.execute("SET temp_directory=?", [str(temp)])
                connection.execute("SET max_temp_directory_size='30GB'")
                connection.execute("SET preserve_insertion_order=false")
                connection.execute("SET enable_progress_bar=false")
                result = build_daily(connection, day, args.collection / day / "events.parquet",
                                     args.collection / day / "event_legs.parquet", working)
                results.append(result)
            finally:
                connection.close()
                shutil.rmtree(temp, ignore_errors=True)
                if database.exists():
                    database.unlink()
            status["completed_dates"].append(day)
            status.update(status="running", current_date=day, elapsed_seconds=round(time.monotonic() - started, 1), results=results)
            status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
            print(json.dumps({"phase": "date_complete", **result, "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)
        connection = duckdb.connect()
        try:
            connection.execute("SET threads=1")
            for prefix, final in (("features", "pump_create_features_v1.parquet"),
                                  ("outcomes", "pump_create_outcomes_v1.parquet"),
                                  ("audit", "pump_create_audit_v1.parquet")):
                files = [working / f"{prefix}_{day}.parquet" for day in DATES]
                list_sql = "[" + ",".join(q(path.as_posix()) for path in files) + "]"
                connection.execute(f"COPY (SELECT * FROM read_parquet({list_sql}, union_by_name=true)) TO {q((output/final).as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)")
        finally:
            connection.close()
        status.update(status="written", elapsed_seconds=round(time.monotonic() - started, 1), results=results)
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except Exception as error:
        status.update(status="failed", error_type=type(error).__name__, error=str(error),
                      failed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(), elapsed_seconds=round(time.monotonic()-started,1))
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        raise
    print(json.dumps({"phase": "written", "output": str(output), "results": results,
                      "elapsed_seconds": round(time.monotonic() - started, 1)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
