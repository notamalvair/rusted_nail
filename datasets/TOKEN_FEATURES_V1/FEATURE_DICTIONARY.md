# FEATURE DICTIONARY — pump-create-features-v1

## Единица наблюдения

Одна строка = одно occurrence события `create/pump/WSOL`, ключ `create_occurrence_id = event_row_id`. Ничего не дедуплицировано молча. В текущих трёх окнах exact/logical повторов не обнаружено, но ranks сохранены.

## Идентификаторы и split

- `sample_day`: 2026-11-02 / 09 / 16.
- `dataset_split`: train / validation / test соответственно.
- provenance: archive SHA/label, source_line, raw payload SHA, tx signature.
- token: base_mint, quote_mint, create_pool_id, metadata и authority fields.
- `create_provider_received_ms`: причинный anchor доступности.
- `create_event_timestamp_ms`: source/blockchain event-time для QC и outcomes.
- `create_receipt_minus_event_ms`: разница двух часов провайдера, не latency будущего бота.

## Static create values

Сохранены пары text + DOUBLE proxy:
- initial buy;
- create price SOL;
- market cap SOL;
- реальные base/quote reserves;
- virtual curve base/quote reserves;
- pool/network fees.

`*_text` остаётся точным source numeric text. DOUBLE нужен для анализа; он приблизителен.

Create breakdown агрегирован отдельно в `initial_*`: buy/sell leg counts, quote/base gross, unique traders, cast failures. Эти операции не входят в post-create flow.

## Feature windows

W ∈ **5, 15, 30, 60, 120 секунд** receipt-time.

`feature_window_observed_Ws` обязателен. Если false — все window fields NULL, а не 0/false.

### Parent-event families

- `parent_buy/sell_events_Ws`
- `parent_buy/sell_quote_sum_Ws` — альтернативная parent/net проекция
- `missing_breakdown_trade_events_Ws`
- `price_snapshot_count_Ws`
- `last/min/max_price_sol_Ws`
- `last_market_cap_sol_Ws`
- latest real/virtual reserves
- `pool_count_Ws`, `migrated_by_Ws`, first migration delay
- price cast failures
- snapshot age
- `past_return`, `past_max_drawup`, `past_max_drawdown` — используют только цены до cutoff

### Event-leg families

- buy/sell leg counts и distinct parent-event counts
- unique buyers/sellers/traders
- gross base-token and quote amounts
- cast-failure counts
- first/last trade delays
- dev-sell flag (`trader == create tx_signer`, это proxy, не доказанный creator owner)
- `leg_net_quote_flow = buy_quote_gross - sell_quote_gross`
- `leg_detail_complete` — нет trade parent с missing/non-present breakdown в окне

Parent quote sums и leg gross — разные проекции одних событий; **не складывать**.

## Mint/pool linkage

После create события связываются по `base_mint + quote_mint=WSOL`, protocols pump/pump-amm. Это позволяет продолжить после migration. `pool_count` и migration flags отражают смену pool. Другие протоколы/quote не входят.

Если тот же mint создаётся повторно, интервалы occurrence не перекрываются: текущий create наблюдает до следующего create либо максимального track horizon.

## Missing и quality

- Полное наблюдаемое окно без операций корректно содержит нулевые counts/gross и false migration/dev-sell.
- Ненаблюдаемое окно — NULL.
- Missing breakdown не означает нулевой gross; `leg_detail_complete=false`.
- Negative/nonfinite values не используются как валидная цена/amount; cast issues посчитаны.

## Квинтили

Готовый общий каталог QUINTILES подходит только для исходных source metrics, не для window counts/flows/returns. Квинтили новых features следует обучать только на train-дате, иначе возникнет distribution leakage.
