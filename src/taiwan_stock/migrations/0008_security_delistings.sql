-- Official evidence is retained independently of current catalog membership.
CREATE TABLE security_delistings (
    market TEXT NOT NULL CHECK (market IN ('TWSE', 'TPEx')),
    code TEXT NOT NULL,
    security_type TEXT NOT NULL CHECK (security_type IN ('stock', 'etf')),
    effective_date TEXT NOT NULL,
    last_trade_date TEXT,
    reason TEXT,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (market, code, source_url)
);
CREATE INDEX delisting_security ON security_delistings(market, code, effective_date);
