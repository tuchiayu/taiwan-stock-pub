-- Design 004 §4.5.2: `distributions` gains `record_date` (收益分配基準日,
-- provided by the TWSE ETF distribution list but not modeled before)
-- and per-event `source`/`fetched_at` so rows from different adapters
-- (e.g. the ETF distribution list vs. the stock ex-dividend notice
-- table) can be told apart and merged field-by-field rather than
-- blindly overwriting each other.

ALTER TABLE distributions ADD COLUMN record_date TEXT;
ALTER TABLE distributions ADD COLUMN source TEXT;
ALTER TABLE distributions ADD COLUMN fetched_at TEXT;
