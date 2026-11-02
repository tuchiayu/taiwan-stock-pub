-- Legacy NULL coverage cannot distinguish absent codes from explicit no-quote.
ALTER TABLE daily_price_coverage ADD COLUMN status TEXT NOT NULL
    DEFAULT 'unverified' CHECK(status IN ('quoted', 'no_quote', 'unverified'));
UPDATE daily_price_coverage SET status = 'quoted'
WHERE close IS NOT NULL AND EXISTS (
    SELECT 1 FROM daily_prices p
    WHERE p.security_id = daily_price_coverage.security_id
      AND p.trade_date = daily_price_coverage.trade_date
      AND p.close = daily_price_coverage.close
);

-- Retained on Down, and reused on re-upgrade; never resurrect withdrawn prices.
CREATE TABLE IF NOT EXISTS daily_price_invalidations (
    id INTEGER PRIMARY KEY,
    security_id TEXT NOT NULL REFERENCES securities(id),
    market TEXT NOT NULL CHECK(market IN ('TWSE', 'TPEx')),
    trade_date TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    invalidated_at TEXT NOT NULL
);
