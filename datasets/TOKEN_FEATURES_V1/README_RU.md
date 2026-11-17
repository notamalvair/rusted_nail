# README — TOKEN_FEATURES_V1

Компактный feature/label/audit набор для исследования стратегий новых Pump.fun токенов с WSOL quote.

Начните с `START_HERE.md`. Поля признаков описаны в `FEATURE_DICTIONARY.md`, labels — в `OUTCOME_CONTRACT.md`, ограничения — в `PROVENANCE_LIMITATIONS.md`.

## Размеры

- features: 6 060 rows, 260 columns, ~6,07 MB
- outcomes: 114 634 rows, 37 columns, ~2,24 MB
- audit: 7 367 rows, 26 columns, ~1,06 MB

Три split по дате: train 02.11, validation 09.11, test 16.11. Это не случайный split.

## Статус

Маленькие outputs проверены по ключам, counts, leakage/censoring/horizon/monotonic invariants и типам. Большие source Parquet имеют статус `written_unvalidated`; это наследуется.

Не использовать flawed/incomplete папки, лежащие рядом с `TOKEN_FEATURES_V1`. Самодостаточный ZIP будет содержать только исправленную текущую папку без backup/working.
