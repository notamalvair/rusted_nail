# DATA DICTIONARY — quintile-catalog-v1

Все определения ниже извлечены из фактической таблицы `metric_definition` в `metric_catalog.duckdb`, а не восстановлены по памяти.

## Слои

- **`parent_event`** — верхнеуровневое событие PumpAPI: агрегат/netting и снимок состояния пула.
- **`event_leg`** — один элемент `breakdown[]`. `action` здесь означает действие leg; `parent_action` — действие родительского события. Не объединять строки с разными `parent_action`, если они отражают разный контекст.

Parent и legs — две альтернативные проекции одного экономического потока. Их нельзя суммировать вместе как независимый объём.

## Единицы

| `unit_kind` | Смысл |
|---|---|
| `quote_mint_units` | Количество конкретного `quote_mint`; разные mint не объединять |
| `quote_mint_per_base_token` | Цена base token в конкретном `quote_mint` |
| `human_base_token_units_cross_asset` | Human-unit base token; межтокеновое сравнение только описательное |
| `sol` / `usd` | SOL / USD |
| `sol_per_base_token` / `usd_per_base_token` | Цена base token в SOL / USD |
| `fraction` | Доля: 0.01 = 1%, не один процентный пункт |
| `provider_rate_raw` | Смысл провайдера не нормализован; сохранять как raw rate |

Для метрик `split_by_quote_mint=true` выбор threshold требует точного совпадения `quote_mint`. Для остальных `quote_mint=NULL` ожидаем и не означает пропуск.

## Event-leg метрики

| Метрика | Исходная колонка / поле | Actions | Единица | Split quote | Смысл |
|---|---|---|---|---:|---|
| `leg_quote_amount` | `quote_amount_decimal_text` / `breakdown[].quoteAmount` | buy,sell,add,remove | quote_mint_units | да | Quote amount отдельной операции breakdown |
| `leg_token_amount` | `token_amount_decimal_text` / `breakdown[].tokenAmount` | buy,sell,add,remove | human_base_token_units_cross_asset | нет | Base amount операции; не складывать с parent aggregate |

## Parent-event метрики

| Метрика | Колонка / source field | Actions | Единица | Split quote | Смысл |
|---|---|---|---|---:|---|
| `aggregate_quote_amount` | `aggregate_quote_amount_decimal_text` / `quoteAmount` | create,buy,sell,migrate | quote_mint_units | да | Parent aggregate/net quote amount |
| `aggregate_token_amount` | `aggregate_token_amount_decimal_text` / `tokenAmount` | buy,sell | human_base_token_units_cross_asset | нет | Parent aggregate/net base amount; не gross volume |
| `initial_buy_token_amount` | `initial_buy_token_amount_decimal_text` / `initialBuy` | create,migrate | human_base_token_units_cross_asset | нет | Initial buy при launch/migration |
| `pool_price_quote` | `pool_price_quote_decimal_text` / `price` | create,buy,sell,migrate | quote_mint_per_base_token | да | Состояние пула, не execution VWAP |
| `pool_price_sol` | `pool_price_sol_decimal_text` / `priceSol` | create,buy,sell,migrate | sol_per_base_token | нет | Цена пула в SOL |
| `pool_price_usd` | `pool_price_usd_decimal_text` / `priceUsd` | create,buy,sell,migrate | usd_per_base_token | нет | Цена пула в USD |
| `market_cap_quote` | `market_cap_quote_decimal_text` / `marketCapQuote` | create,buy,sell,migrate | quote_mint_units | да | Market cap в quote_mint |
| `market_cap_sol` | `market_cap_sol_decimal_text` / `marketCapSol` | create,buy,sell,migrate | sol | нет | Market cap в SOL |
| `market_cap_usd` | `market_cap_usd_decimal_text` / `marketCapUsd` | create,buy,sell,migrate | usd | нет | Market cap в USD |
| `supply` | `supply_decimal_text` / `supply` | create,buy,sell,migrate | human_base_token_units_cross_asset | нет | Observed supply; межтокеновые пороги описательные |
| `reserve_base_real` | `reserve_base_real_decimal_text` / `tokensInPool` | create,buy,sell,migrate | human_base_token_units_cross_asset | нет | Реальный base reserve |
| `reserve_quote_real` | `reserve_quote_real_decimal_text` / `quoteInPool` | create,buy,sell,migrate | quote_mint_units | да | Реальный quote reserve |
| `curve_virtual_base` | `curve_virtual_base_decimal_text` / `vTokensInBondingCurve` | create,buy,sell,migrate | human_base_token_units_cross_asset | нет | Virtual bonding-curve base reserve |
| `curve_virtual_quote` | `curve_virtual_quote_decimal_text` / `vQuoteInBondingCurve` | create,buy,sell,migrate | quote_mint_units | да | Virtual bonding-curve quote reserve |
| `amm_virtual_base` | `amm_virtual_base_decimal_text` / `virtualTokensInPool` | create,buy,sell,migrate | human_base_token_units_cross_asset | нет | Специальная AMM virtual base liquidity |
| `amm_virtual_quote` | `amm_virtual_quote_decimal_text` / `virtualQuoteInPool` | create,buy,sell,migrate | quote_mint_units | да | Специальная AMM virtual quote liquidity |
| `pool_fee_fraction` | `pool_fee_fraction_decimal_text` / `poolFeeRate` | create,buy,sell,migrate | fraction | нет | Pool fee fraction |
| `sol_route_fee_fraction` | `sol_route_fee_fraction_decimal_text` / `solRouteFeeRate` | create,buy,sell,migrate | fraction | нет | SOL route fee fraction |
| `holder_reward_rate_raw` | `holder_reward_rate_raw_decimal_text` / `holderRewardsRate` | create,buy,sell,migrate | provider_rate_raw | нет | Protocol-specific raw rate; не интерпретировать без docs |
| `reported_network_fee_sol` | `reported_network_fee_sol_decimal_text` / `priorityFee` | create,buy,sell,migrate | sol | нет | Provider fee observation; может повторяться внутри signature |

## Строка `metric_distribution`

Ключ выбора состоит из:

`layer + metric_name + scope + sample_day + action + parent_action + protocol + quote_mint`

- `parent_action` обычно NULL для parent_event, но важен для event_leg.
- `sample_day`: заполнен только для DAY_ACTION_PROTOCOL.
- `protocol`: NULL только для ACTION.
- `quote_mint`: заполнен только для split-метрик; для них отсутствие mint делает строку непригодной.

Диагностика:
- `n_rows` — популяция до проверки metric value;
- `n_text_null` — source metric NULL;
- `n_cast_failed_or_nonfinite` — не удалось DOUBLE либо NaN/Inf;
- `n_negative` — finite отрицательные, исключены из порогов;
- `n_zero` — нули среди valid;
- `n_valid` — finite и >=0;
- `min/p01/p20/p40/p60/p80/p99/max` — приблизительное распределение valid;
- `quintiles_eligible` и `ineligible_reason` — разрешение использовать Q1–Q5.

Баланс учёта: `n_rows = n_text_null + n_cast_failed_or_nonfinite + n_negative + n_valid`.
