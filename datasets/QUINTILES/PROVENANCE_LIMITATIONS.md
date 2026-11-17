# PROVENANCE AND LIMITATIONS

## Источник

Каталог построен по шести нормализованным Parquet:

- 2026-11-02, архивные окна 09 и 10 UTC;
- 2026-11-09, архивные окна 09 и 10 UTC;
- 2026-11-16, архивные окна 09 и 10 UTC.

Это три независимых двухчасовых окна с недельными разрывами, не непрерывная история. Parent events: 11 442 357 строк; event legs: 6 764 612 строк; полные Parquet: 7 686 589 085 байт.

## Наследованный статус

`source_status = written_unvalidated`.

Нормализация завершилась без карантина, footer counts совпали с write reports, схемы трёх дат совпали. Но полная построчная сверка больших Parquet с исходными `.zst` отменена пользователем; output SHA полных Parquet не вычислялись. Каталог квинтилей не повышает этот статус.

Маленькие выходы каталога проверены полностью:
- DuckDB row counts == exported Parquet row counts;
- EXCEPT ALL в обе стороны дал 0;
- accounting ошибок 0;
- eligible-policy ошибок 0;
- eligible quote rows без quote_mint: 0;
- нарушений монотонности после repair: 0.

Подробности: `BUILD_REPORT.json`.

## Метод

- Source exact values хранятся как decimal-text в больших Parquet.
- Для каталога: `TRY_CAST(decimal_text AS DOUBLE)`.
- В расчёт quantile входят только finite `value >= 0`.
- NULL, cast-fail/nonfinite и negative считаются отдельно.
- DuckDB `approx_quantile(value, [0.01,0.20,0.40,0.60,0.80,0.99])`.
- `quintiles_eligible=true` только если `n_valid >= 1000` и `p20 < p40 < p60 < p80`.
- Использованы только protocols `pump`, `pump-amm` и применимые actions из словаря.

В 613 tiny all-zero непригодных группах `approx_quantile` вернул минимальное положительное DOUBLE `2.2250738585072014e-308` при observed max=0. Все approximate quantiles затем математически ограничены `[min_value,max_value]`. Эти группы остаются ineligible; исправление не создаёт сигналов и устраняет невозможный quantile вне observed range.

## Размер и статистика

- 22 метрики.
- 38 369 строк распределений.
- 1 127 eligible threshold rows.
- 37 134 ineligible: `insufficient_valid_count`.
- 108 ineligible: `non_strict_boundaries`.
- 792 различных ненулевых quote_mint в диагностических группах.

Большое число диагностических строк объясняется разбиением quote-метрик по mint. Большинство tiny-групп не использовать для классификации.

## Чего здесь нет

- Нет исходных событий или raw_json.
- Нет token-level 5/15/30/60s features.
- Нет PnL, future returns, labels, entry/exit rules.
- Нет моделирования slippage, latency, liquidity impact или fills.
- Нет полного holder graph/bundling proof.
- Нет стратегии и доказанной прибыльности.

## Временные границы и цензура

Некоторые source timestamp слегка пересекают номинальные границы архивов (например, 2 ноября минимум на 7 мс раньше 09:00); строки не фильтровались молча. Токены могли быть созданы до окна или продолжить жизнь после него. Нельзя трактовать отсутствие create/outcome внутри окна как отсутствие события в реальности.

## Воспроизводимость

`provenance/` содержит копии builder/resume/finalizer scripts как аудит алгоритма. Они не являются standalone командой без больших Parquet и Python dependencies. Версии:
- policy: `quintile-catalog-v1`;
- DuckDB: 1.5.6;
- mapper источника: `pumpapi-replay-v1`.

`RUN_STATUS.json` сохраняет историю медленного запуска и возобновления. Итоговый status — passed; промежуточный failure был вызван semantic check невозможных tiny all-zero approximate values и исправлен clamping, не повторным изменением источников.
