# START HERE — PumpAPI Quintile Catalog

Этот пакет самодостаточно описывает компактный каталог порогов квинтилей, рассчитанный поверх трёх наборов PumpAPI Parquet. Большие исходные Parquet в пакет не входят и передаются отдельно.

## Задание для Claude

Скопируй этот prompt в новую сессию:

> Изучи `START_HERE.md`, `DATA_DICTIONARY.md`, `USAGE.md`, `PROVENANCE_LIMITATIONS.md` и `SCHEMA.json`. Основные данные — `quintile_thresholds.parquet`; DuckDB-копия — `metric_catalog.duckdb`. Используй только строки `quintiles_eligible=true`. Выбирай порог по fallback `DAY_ACTION_PROTOCOL → ACTION_PROTOCOL → ACTION`, неизменно сохраняя `layer`, `metric_name`, `action`, для event_leg также `parent_action`, а для split-метрик `quote_mint`. Не объединяй parent events и event legs. Не считай каталог стратегией, PnL или полностью валидированным источником. Не ищи отсутствующие большие Parquet без моего прямого запроса. Перед выводами сверяй единицы в `metric_definitions.parquet`.

## Порядок чтения

1. `PROVENANCE_LIMITATIONS.md` — происхождение и ограничения.
2. `DATA_DICTIONARY.md` — смысл 22 метрик.
3. `SCHEMA.json` — машинная схема и правила ключей/NULL.
4. `USAGE.md` — SQL/Python/Rust и точный fallback.
5. `BUILD_REPORT.json` — пройденные проверки маленьких артефактов.
6. `MANIFEST.json` — размеры и SHA-256 всех файлов.

## Что использовать

- **Основной переносимый файл:** `quintile_thresholds.parquet`.
- **Словарь:** `metric_definitions.parquet`.
- **Provenance:** `catalog_run.parquet`.
- **Альтернатива:** `metric_catalog.duckdb`, содержащая те же три таблицы.
- **Пример классификации:** `assign_quintile.sql`.

Каталог содержит 38 369 диагностических групп, но только 1 127 пригодны для Q1–Q5. Остальные нужны для понимания малой выборки, нестрогих границ, null/cast/negative/zero.

## Ключевой принцип

Квинтиль — положение числового наблюдения внутри конкретного распределения, а не торговый сигнал. Порог нельзя переносить между action, layer, parent_action или quote_mint только ради получения Q1–Q5.
