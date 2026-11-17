# PROVENANCE AND LIMITATIONS — TOKEN_FEATURES_V1

## Source

Три независимых окна PumpAPI, 09:00–11:00 UTC:
- 2026-11-02 — train
- 2026-11-09 — validation
- 2026-11-16 — test

Parent/leg sources: 11 442 357 / 6 764 612 rows, mapper pumpapi-replay-v1.

Source status: **written_unvalidated**. Footer counts и схемы совпали с write reports, карантин пуст, но полная построчная сверка с `.zst` отменена пользователем. Feature dataset не повышает этот статус.

## Cohort

- All Pump create occurrences: 7 367.
- Included Pump+WSOL: 6 060.
- Excluded non-WSOL: 1 307; сохранены в audit.
- No implicit deduplication.
- Current data exact/logical duplicate counts оказались 1; total future-aware counts удалены из model features. Ranks и audit доступны.

## Output counts

- features: 6 060 unique create IDs
- audit: 7 367 unique create IDs
- outcomes: 114 634 unique `(create,cutoff,horizon)`

Splits features: train 1 966, validation 2 044, test 2 050.

## Time semantics

Features anchor: PumpAPI replay `provider_received_ms`, не время получения будущим ботом. Outcome horizons: provider/source `event_timestamp_ms`. Это модель провайдерских timestamps, не измерение вашей live latency.

При одинаковом timestamp точный on-chain instruction order неизвестен. Source archive label/line используются для deterministic tie-break там, где доступны.

## Pool linkage

Follow-up join: base_mint + WSOL, protocols pump/pump-amm. Это позволяет migration, но может включить параллельный pump-amm pool того же mint. `pool_count`, migration и create pool ID позволяют sensitivity filtering. Полный on-chain pool genealogy не доказан.

## Economic limitations

- Source prices — pool-state proxy, не execution VWAP.
- Нет position size/liquidity impact.
- Fee/slippage — фиксированные defaults, не реальные комиссии каждой сделки.
- Нет failed/expired transactions, priority/Jito fees, confirmation/finality.
- tx_signer как dev/creator — proxy.
- Missing breakdown делает leg features неполными; это отмечено.

## Censoring

У конца двухчасового окна feature/outcome flags контролируют coverage. Ненаблюдаемое — NULL, не ноль. Endpoint может отсутствовать при полном temporal coverage; нужен `endpoint_price_found`.

Левая цензура: cohort начинается с create, но 24h creator history из Rust sniper недоступна. Нельзя делать вид, что creator launch count за 24h полный.

## Исправления после review

Первая compact build сохранена отдельно как `TOKEN_FEATURES_V1_FLAWED_*` и не должна передаваться/использоваться. В исправленной версии:
- all outcome-only fields NULL при no-entry/censored window;
- path MFE/MAE/TP/SL/trailing/dev-sell обрезаны точным горизонтом;
- endpoint tolerance +5s применяется только к endpoint;
- future-aware total duplicate counts удалены из model features;
- observed `migrated_by_*` всегда true/false, не NULL.

## Что это не доказывает

Набор не является стратегией, рекомендацией, доказательством alpha или прибыльности. Малое число дат не отражает рыночные режимы. Любая модель требует walk-forward/out-of-sample и реалистичного execution simulation.
