# TOKEN_FEATURES_V1 — START HERE

Компактный исследовательский набор для поиска стратегии по новым Pump.fun-токенам, рассчитанный из полных PumpAPI Parquet. Одна строка feature-файла соответствует одному **create occurrence** Pump + WSOL.

## Claude prompt

> Прочитай START_HERE.md, FEATURE_DICTIONARY.md, OUTCOME_CONTRACT.md, PROVENANCE_LIMITATIONS.md и SCHEMA.json. Используй `pump_create_features_v1.parquet` только как признаки, доступные к указанному cutoff. Labels находятся строго отдельно в `pump_create_outcomes_v1.parquet`; соединяй по `(create_occurrence_id, cutoff_s, horizon_s)` и обучай только при `entry_price_found=true`, `outcome_window_observed=true`, а для endpoint return также `endpoint_price_found=true`. Не превращай NULL в false/0. Split фиксирован по датам: train=2026-11-02, validation=2026-11-09, test=2026-11-16. Parent aggregates и event-leg gross не складывай. Это pool-price proxy, не доказанный fill/VWAP и не готовая стратегия. Исходные большие Parquet имеют статус written_unvalidated.

## Файлы

- `pump_create_features_v1.parquet` — 6 060 строк, 260 признаковых/ID/quality колонок.
- `pump_create_outcomes_v1.parquet` — 114 634 строки labels/proxy outcomes.
- `pump_create_audit_v1.parquet` — 7 367 всех Pump create occurrences, включая 1 307 исключённых non-WSOL.
- `FEATURE_DICTIONARY.md` — семейства колонок и семантика окон.
- `OUTCOME_CONTRACT.md` — entry/endpoint/path labels и обязательные фильтры.
- `SCHEMA.json` — фактические Arrow-схемы.
- `MANIFEST.json` — SHA-256, размеры и роли.
- `BUILD_REPORT.json` — проверки маленьких файлов.
- `provenance/` — builder source для аудита.

## Когорта

```text
action_raw=create
protocol=pump
quote_mint=So11111111111111111111111111111111111111112 (WSOL)
base_mint/provider_received_ms/event_timestamp_ms не NULL
```

Не-WSOL create не удалены бесследно: они в audit с `included_in_cohort=false` и `exclusion_reason=non_wsol_quote`.

## Главное правило причинности

- `t0 = create_provider_received_ms`.
- Feature `*_Ws` использует события с provider receipt строго после create и не позже `t0+W`.
- Если полное W-секундное окно не помещается в датасет, `feature_window_observed_Ws=false`, а все window-поля NULL.
- Outcomes отделены физически, чтобы future columns нельзя было случайно подать в стратегию.

## Статус

Маленькие outputs полностью перечитаны и проверены по своим schemas/keys/invariants. Но они унаследовали `written_unvalidated`: большие source Parquet не были полностью сверены с исходными `.zst` по просьбе пользователя.
