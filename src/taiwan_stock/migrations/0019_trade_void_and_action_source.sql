-- Issue #115 #2: which side last wrote a corporate action. Existing rows
-- cannot be told apart reliably, so they keep the official default.
ALTER TABLE corporate_actions ADD COLUMN source TEXT NOT NULL
    DEFAULT 'official' CHECK(source IN ('manual', 'official'));

-- Issue #115 #10: a voided trade is kept but excluded from every
-- calculation; all position reads go through live_trades.
ALTER TABLE trades ADD COLUMN voided_at TEXT;
CREATE VIEW live_trades AS SELECT * FROM trades WHERE voided_at IS NULL;
