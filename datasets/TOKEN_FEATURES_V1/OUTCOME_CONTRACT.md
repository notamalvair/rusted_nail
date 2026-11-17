# OUTCOME CONTRACT — pump-create-features-v1

Outcomes физически отделены от features. Никогда не присоединять labels до формирования временного split и выбранного cutoff.

## Ключ

`(create_occurrence_id, cutoff_s, horizon_s)` уникален. В таблице есть только `horizon_s > cutoff_s`.

Cutoffs: 5/15/30/60/120s. Horizons: 30/60/120/300/900s, если H>W.

## Entry proxy

- signal receipt = create receipt + cutoff;
- due receipt = signal + **400 ms**;
- entry = первый положительный pool_price_sol snapshot с provider receipt в `[due, due+2000ms]`;
- никаких backward-fill/interpolation;
- `entry_price_found` показывает наличие proxy.

Это pool-state snapshot, не fill и не VWAP.

## Window coverage

`outcome_window_observed=true`, только если entry существует и `entry_event_timestamp_ms + horizon <= date_max_event_timestamp_ms`.

Если entry отсутствует либо outcome window цензурировано, **все outcome-only значения и hit-flags NULL**, не false/0.

## Endpoint

Endpoint price = первый snapshot в event-time интервале `[entry_event+horizon, +5000ms]`.

`endpoint_price_found` может быть false даже при полном временном покрытии; return labels тогда NULL. Это не отрицательная доходность.

## Returns

- `gross_return = endpoint / entry - 1`.
- `net_roundtrip_proxy = (1-fee)^2 * endpoint/entry * (1-slippage)/(1+slippage) - 1`.
- defaults: fee 1.5% на сторону, slippage 3%, как пример Rust config.

Это proxy без размера позиции, liquidity impact, failed transaction, priority/Jito fees и фактического fill.

## Path outcomes

MFE/MAE и hit flags используют события от entry до **точного горизонта**, без endpoint tolerance после него.

- TP: price >= entry * 1.5
- SL: price <= entry * 0.8
- trailing: price <= running_peak * 0.75
- dev sell: sell leg с trader == create tx_signer
- `rust_max_hold_120_row` — строка горизонта 120s; не утверждает, что позиция реально закрылась.

Сохраняются первые event timestamps срабатывания. Одновременные TP/SL возможны только если разные snapshots попали в path; порядок сравнивать по timestamp, а при равенстве исходного instruction-order всё равно нет.

## Обязательные фильтры анализа

Для endpoint return:

```text
entry_price_found
AND outcome_window_observed
AND endpoint_price_found
```

Для MFE/MAE/barriers:

```text
entry_price_found
AND outcome_window_observed
```

Никогда не `coalesce(label, false/0)` для обучения.

## Leakage control

- Features используют provider receipt до cutoff.
- Outcomes используют source event-time после entry; proxy availability всё равно наследует PumpAPI semantics.
- Три даты разделены train/validation/test.
- Не выбирать гиперпараметры по test=16 ноября.
- Source full Parquet status written_unvalidated сохраняется.
