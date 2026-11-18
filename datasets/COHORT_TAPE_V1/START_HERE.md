# COHORT_TAPE_V1 — передача результатов Claude

## Выполнено

Экспортирован исходным `tools/export_cohort_tape_v1.py` ревизии `5e2953c4e38ae76a6db5e1834474d0caed46a8b9` набор трёх дат. Единственная правка exporter — добавление `block_number_raw` первым в BLOCK_CANDIDATES; точный diff и исходная версия сохранены.

| Дата | Токенов cohort/tape | Строк tape | Без блока | Без положительной цены | Размер tape+tokens | Работа / ожидание памяти |
|---|---:|---:|---:|---:|---:|---|
| 2026-11-02 | 1966 / 1966 | 359032 | 0 | 0 | 20130527 байт | 67.03 сек / 151.8 сек |
| 2026-11-09 | 2044 / 2044 | 305591 | 0 | 0 | 16473561 байт | 84.49 сек / 901.4 сек |
| 2026-11-16 | 2050 / 2050 | 261907 | 0 | 0 | 14093696 байт | 18.37 сек / 180.1 сек |

Всего: 6060 токенов, 926530 строк tape, 50697784 байта шести Parquet (~50.70 MB).

Проверены report-gates и Parquet footer-счётчики. Это НЕ полная валидация каждого преобразования. Source Parquet status остаётся `written_unvalidated`. Ноль пропусков и полное покрытие токенов не доказывают экономическую корректность, полноту всех операций или завершённый жизненный цикл.

## Состав пакета

- `data/tape_<date>.parquet`, `data/tokens_<date>.parquet` — три даты.
- `REPORT_ALL_DATES.json` — объединение дневных отчётов без утраты времени/статуса отдельных запусков.
- `reports/REPORT_<date>.json`, `reports/STATUS_<date>.json` — отдельные исходные отчёты и gates.
- `logs/` — полный stdout/stderr каждого экспорта, первоначальный и исправленный --describe.
- `exporter.patch` — точный diff.
- `provenance/export_cohort_tape_v1.py` и `.original.py` — применённый и исходный скрипты.
- `SCHEMA.json`, `MANIFEST.json` — реальные схемы/счётчики и SHA-256 файлов.
- `HOME_PC_TASKS.md` — оставшиеся задачи, без выполненных Rust-запусков.

Никаких .env, рабочей config с ключами, токенов Telegram, исходных гигабайтных events/legs, временных DuckDB либо cargo target в пакет не включено. Один raw час для replay передавать отдельно.

## Claude prompt

> Прочитай START_HERE.md, REPORT_ALL_DATES.json, SCHEMA.json, exporter.patch и HOME_PC_TASKS.md. Прежде всего проверь контракт tape: одна строка — leg либо событие без legs. Не складывай повторяющийся parent_quote_amount/network_fee_sol по всем legs. token_idx локален для даты; mint/creator находятся в tokens. Для портфеля пересортируй по времени; не используй token-major физический порядок как глобальный. 16 ноября — test: не подбирай правила по его сделкам до фиксации стратегии. Файлы прошли только report/footer-gates, исходники written_unvalidated. Не делай выводов об исполнении/прибыльности по одному price_sol. Rust здесь ещё не собирался, replay/paper не запускались.

## Ключи, порядок, время

- JOIN tape -> tokens: `(sample_day, token_idx)`, не token_idx один между датами.
- tokens `create_occurrence_id` — ID исходного create; base_mint и creator находятся именно в tokens.
- `creator` — tx_signer создания, proxy, не доказанный бенефициар.
- tape `action` — родительское action, `leg_action` — действие breakdown. У create внутри может быть buy-leg. Не считать parent create и его leg двумя независимыми покупками.
- `receipt_ms`, `event_ms` — INT64 epoch ms: при backtesting причинность по receipt, event-time отдельно.
- `block` — источник `events.block_number_raw`; не доказательство finalized slot/landing, номера могут повторяться.
- `archive`, `line`, `leg_index` — ссылка на родительскую source строку и индекс child. Tape не сохраняет event_row_id, tx_signature, pool_id; совпадение archive+line даёт локальный surrogate родителя, не заменяет tx signature.
- Порядок файла: token_idx, receipt_ms, archive, line, leg_index. Для глобального replay сортировать по receipt_ms/archive/line/leg_index с deterministic tie-break.
- `price_sol`, `reserve_quote`, `parent_quote_amount`, `network_fee_sol` повторяются у всех legs одного parent. Parent/fee учитывать один раз, gross legs считать отдельно.
- Amounts/цены преобразованы в DOUBLE: приблизительные аналитические значения, не exact decimal-text.
- Цена — состояние пула после события, не гарантированная fill price/VWAP.

## Coverage, цензура, ограничения

Когорта: create/pump/WSOL, дальнейшие события pump/pump-amm того же base_mint+WSOL. Окно nominal 15 минут после create, обрезается следующим create для mint и фактическим концом двухчасовых источников. Последние токены окна не имеют полных 15 минут будущей истории. tokens.end_receipt_ms само по себе не доказывает полноту фактически доступного окна.

Для временного покрытия известны provider receipt maxima из ранее сохранённого feature-build:
- 02.11: 1793617199994;
- 09.11: 1794221999999;
- 16.11: 1794826799988.

Проверять `create_receipt_ms + 900000 <= source_max_receipt_ms` и возможное обрезание `end_receipt_ms`; не назначать неполным токенам false/zero outcomes. Три независимых окна с недельными разрывами, состояние портфеля между ними не переносить.

Tape — версия автора. Дополнительных улучшений семантики/полей не вносилось; любые дальнейшие изменения согласовать и прислать diff. Она не содержит всех risk/metadata/pool fee полей для полного паритета с живым bot parser. Rust replay этой ревизии читает raw JSONL[.zst], не данный Parquet напрямую.
