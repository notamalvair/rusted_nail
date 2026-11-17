# BUILD REPORT — TOKEN_FEATURES_V1

Status: **passed** for the compact outputs. Source status remains `written_unvalidated`.

## Counts

| File | Rows | Columns |
|---|---:|---:|
| pump_create_features_v1.parquet | 6 060 | 260 |
| pump_create_outcomes_v1.parquet | 114 634 | 37 |
| pump_create_audit_v1.parquet | 7 367 | 26 |

Splits features: train 1 966, validation 2 044, test 2 050.
Audit: included WSOL 6 060; excluded non-WSOL 1 307.

## Passed checks

- feature/audit create IDs unique;
- outcome `(create,cutoff,horizon)` unique;
- all outcomes have horizon > cutoff;
- no outcome columns in feature file;
- censored or no-entry rows: all outcome-only values/hit flags NULL;
- path TP/SL/trailing/dev-sell timestamps never before entry or after exact horizon;
- endpoint timestamps within `[target,target+5000ms]`;
- no return without entry and endpoint prices;
- fully observed feature counts/gross are monotonic across larger windows;
- censored feature-window fields NULL;
- noncausal total duplicate-count fields absent from features;
- migrated_by 5/15/30/60/120 physical type BOOLEAN, nonnull true/false for observed windows, NULL for censored windows;
- no NaN/Inf in compact numeric outputs observed by review;
- all Arrow fields are nullable physically; semantic requirements are in DATASET_CONTRACT.json.

## Review-driven corrections

First compact build was preserved separately outside this package and must not be used. Final rebuild corrected:
1. NULL rather than false/0 for no-entry/censored outcomes;
2. exact-horizon path labels, with +5s tolerance only for endpoint;
3. removed future-aware total duplicate counts from model features;
4. migrated_by semantics and BOOLEAN types.

An independent read-only review confirmed the corrected blockers with zero violations. The five boolean columns were rewritten in the small feature file only; 6 060 rows and all other 255 columns matched in both directions.

## Known limitation

`outcome_window_observed` means temporal coverage after a found entry. It does not guarantee endpoint price; require `endpoint_price_found`. A large share of temporally covered rows lacks an endpoint snapshot in the narrow tolerance.

MFE/MAE output does not store timestamps of extrema; exact path-source audit would require returning to full events. Existing invariants passed, but source full validation was not performed.
