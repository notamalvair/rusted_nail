-- Exact decimal-text remains authoritative; DOUBLE is used only for approximate bands.
CASE
  WHEN value_double IS NULL OR NOT isfinite(value_double) OR value_double < 0 THEN NULL
  WHEN value_double <= p20 THEN 1
  WHEN value_double <= p40 THEN 2
  WHEN value_double <= p60 THEN 3
  WHEN value_double <= p80 THEN 4
  ELSE 5
END AS quintile

-- Fallback: DAY_ACTION_PROTOCOL -> ACTION_PROTOCOL -> ACTION.
-- Never cross action, quote_mint or layer. Parent events and event_legs do not mix.
