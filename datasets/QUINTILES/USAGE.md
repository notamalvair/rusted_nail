# USAGE — DuckDB, Python и Rust

## Алгоритм выбора threshold

Для наблюдения заранее известны:
- `layer` (`parent_event` либо `event_leg`);
- `metric_name`;
- `action`; для event_leg это leg action;
- `parent_action` для event_leg;
- `protocol` (`pump`/`pump-amm`);
- `sample_day` (`YYYY-MM-DD`);
- `quote_mint` для split-метрик;
- exact decimal-text value из полного Parquet.

Фильтр должен неизменно сохранять `layer + metric_name + action + parent_action + quote_mint`. Выбрать первую eligible-строку:

1. DAY_ACTION_PROTOCOL — совпадают day и protocol.
2. ACTION_PROTOCOL — `sample_day IS NULL`, совпадает protocol.
3. ACTION — `sample_day IS NULL AND protocol IS NULL`.

`ACTION` не GLOBAL: он всё ещё сохраняет action/layer/metric/parent_action/quote_mint, лишь объединяет три даты и оба разрешённых протокола.

Если eligible-кандидата нет — quintile = NULL. Нельзя сменить action, layer или quote_mint ради результата.

## DuckDB SQL

```sql
-- Только eligible threshold; параметры заменить значениями вашего наблюдения.
WITH candidates AS (
  SELECT *,
    CASE scope
      WHEN 'DAY_ACTION_PROTOCOL' THEN 1
      WHEN 'ACTION_PROTOCOL' THEN 2
      WHEN 'ACTION' THEN 3
    END AS fallback_rank
  FROM read_parquet('quintile_thresholds.parquet')
  WHERE quintiles_eligible
    AND layer = $layer
    AND metric_name = $metric_name
    AND action = $action
    AND parent_action IS NOT DISTINCT FROM $parent_action
    AND quote_mint IS NOT DISTINCT FROM $quote_mint
    AND (
      (scope='DAY_ACTION_PROTOCOL' AND sample_day=$sample_day AND protocol=$protocol)
      OR (scope='ACTION_PROTOCOL' AND sample_day IS NULL AND protocol=$protocol)
      OR (scope='ACTION' AND sample_day IS NULL AND protocol IS NULL)
    )
), threshold AS (
  SELECT * FROM candidates ORDER BY fallback_rank LIMIT 1
)
SELECT
  CASE
    WHEN $value_double IS NULL OR NOT isfinite($value_double) OR $value_double < 0 THEN NULL
    WHEN $value_double <= p20 THEN 1
    WHEN $value_double <= p40 THEN 2
    WHEN $value_double <= p60 THEN 3
    WHEN $value_double <= p80 THEN 4
    ELSE 5
  END AS quintile
FROM threshold;
```

DuckDB-база уже содержит таблицы:

```sql
ATTACH 'metric_catalog.duckdb' AS q;
SELECT * FROM q.metric_definition ORDER BY layer, metric_name;
SELECT * FROM q.metric_distribution WHERE quintiles_eligible LIMIT 20;
```

## Python + DuckDB

Требуется внешняя зависимость `duckdb`.

```python
from decimal import Decimal
import duckdb

con = duckdb.connect()
rows = con.execute("""
    SELECT scope, sample_day, action, parent_action, protocol, quote_mint,
           p20, p40, p60, p80
    FROM read_parquet(?)
    WHERE quintiles_eligible
      AND layer=? AND metric_name=? AND action=?
      AND parent_action IS NOT DISTINCT FROM ?
      AND quote_mint IS NOT DISTINCT FROM ?
""", ["quintile_thresholds.parquet", layer, metric_name,
      action, parent_action, quote_mint]).fetchall()

# Exact input remains Decimal. Conversion to float matches catalog method.
value_double = float(Decimal(value_decimal_text))
# Choose fallback using the rules above, then compare <= p20/.../p80.
```

## Python + PyArrow

Требуется `pyarrow`; подходит, если DuckDB не нужен:

```python
import pyarrow.compute as pc
import pyarrow.parquet as pq

table = pq.read_table(
    "quintile_thresholds.parquet",
    filters=[("quintiles_eligible", "=", True),
             ("layer", "=", layer),
             ("metric_name", "=", metric_name),
             ("action", "=", action)],
)
# Затем фильтровать nullable keys с явной семантикой NULL и применить fallback.
```

## Rust — концептуальная интеграция

Пакет не навязывает новый crate. Используйте уже выбранный в проекте Parquet/DuckDB reader и загрузите eligible thresholds в собственную структуру:

```rust
struct ThresholdKey {
    layer: String,
    metric_name: String,
    action: String,
    parent_action: Option<String>,
    quote_mint: Option<String>,
    scope: Scope,
    sample_day: Option<String>,
    protocol: Option<String>,
}

struct Bounds { p20: f64, p40: f64, p60: f64, p80: f64 }

fn quintile(value: f64, b: &Bounds) -> Option<u8> {
    if !value.is_finite() || value < 0.0 { return None; }
    Some(if value <= b.p20 { 1 }
         else if value <= b.p40 { 2 }
         else if value <= b.p60 { 3 }
         else if value <= b.p80 { 4 }
         else { 5 })
}
```

Для воспроизводимости входной decimal-text разбирайте точным decimal type, а в `f64` переводите только в момент сопоставления с approximate threshold. Не используйте квинтиль как готовый сигнал без отдельного бэктеста.

## Типичные ошибки

- Сложить parent amount и event_leg amount — двойной учёт.
- Сравнить quote values разных mint.
- Считать ACTION глобальным по всем действиям.
- Использовать строки `quintiles_eligible=false`.
- Присвоить Q1 значениям NULL, cast-fail, nonfinite или negative.
- Считать p20/p40/p60/p80 точными Decimal-квантилями — они approximate DOUBLE.
