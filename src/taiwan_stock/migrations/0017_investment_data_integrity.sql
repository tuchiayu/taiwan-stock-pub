-- Preserve insertion order when business dates and millisecond timestamps tie.
ALTER TABLE trades ADD COLUMN trade_order INTEGER;
UPDATE trades SET trade_order = rowid;
CREATE UNIQUE INDEX idx_trades_order ON trades(trade_order);
CREATE TRIGGER trg_trades_order
AFTER INSERT ON trades
WHEN NEW.trade_order IS NULL
BEGIN
    UPDATE trades SET trade_order = (
        SELECT COALESCE(MAX(trade_order), 0) + 1 FROM trades
    ) WHERE id = NEW.id;
END;

-- Only a successfully checked date report covers a catalog snapshot.
-- A NULL close denotes a checked code without a quote, never an unchecked code.
CREATE TABLE daily_price_reports (
    market TEXT NOT NULL CHECK(market IN ('TWSE', 'TPEx')),
    trade_date TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY(market, trade_date)
);
CREATE TABLE daily_price_coverage (
    market TEXT NOT NULL CHECK(market IN ('TWSE', 'TPEx')),
    trade_date TEXT NOT NULL,
    security_id TEXT NOT NULL REFERENCES securities(id),
    close TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY(market, trade_date, security_id)
);
