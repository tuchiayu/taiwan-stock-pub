-- Design 002 §4.6.2: 11 core tables for the personal investment tracker.
-- `schema_migrations` itself is bootstrapped by the migration runner
-- before this file runs, so it is not created here.
--
-- Money, per-share amounts and quantities of shares are stored as
-- canonical decimal TEXT (never REAL) per §4.4; application code does
-- all arithmetic with Python Decimal.

CREATE TABLE securities (
    id TEXT PRIMARY KEY,
    market TEXT NOT NULL CHECK (market IN ('TWSE', 'TPEx')),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    security_type TEXT NOT NULL CHECK (security_type IN ('stock', 'etf')),
    industry TEXT,
    transfer_agent_name TEXT,
    transfer_agent_phone TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'delisted')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (market, code)
);

CREATE TRIGGER trg_securities_updated_at
AFTER UPDATE ON securities
FOR EACH ROW
BEGIN
    UPDATE securities SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE id = NEW.id;
END;

-- ETF-only extension row; §4.6.3 explains why this is split out instead
-- of nullable columns on `securities` (mirrors 12+ ETF-specific fields,
-- unlike the 2 stock-only transfer-agent columns kept inline).
CREATE TABLE etf_profiles (
    security_id TEXT PRIMARY KEY REFERENCES securities (id),
    custodian_bank TEXT,
    distribution_frequency TEXT
        CHECK (
            distribution_frequency IS NULL
            OR distribution_frequency IN (
                'monthly', 'quarterly', 'semiannual', 'annual', 'irregular', 'unknown'
            )
        ),
    fee_summary TEXT,
    benchmark TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TRIGGER trg_etf_profiles_updated_at
AFTER UPDATE ON etf_profiles
FOR EACH ROW
BEGIN
    UPDATE etf_profiles SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE security_id = NEW.security_id;
END;

CREATE TABLE daily_prices (
    security_id TEXT NOT NULL REFERENCES securities (id),
    trade_date TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER,
    PRIMARY KEY (security_id, trade_date)
);

-- §4.11: "前一有效交易日" is defined as the newest daily_prices row
-- before a given date, so this index keeps that lookup fast without a
-- separate market-calendar table.
CREATE INDEX idx_daily_prices_security_date
ON daily_prices (security_id, trade_date DESC);

CREATE TABLE distributions (
    id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL REFERENCES securities (id),
    ex_dividend_date TEXT,
    pay_date TEXT,
    amount_per_share TEXT,
    amount_status TEXT NOT NULL CHECK (amount_status IN ('estimated', 'final', 'unknown')),
    frequency_label TEXT
        CHECK (
            frequency_label IS NULL
            OR frequency_label IN (
                'monthly', 'quarterly', 'semiannual', 'annual', 'irregular', 'unknown'
            )
        ),
    UNIQUE (security_id, ex_dividend_date)
);

CREATE INDEX idx_distributions_security ON distributions (security_id);

CREATE TABLE corporate_actions (
    id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL REFERENCES securities (id),
    action_type TEXT NOT NULL
        CHECK (
            action_type IN ('split', 'stock_dividend', 'cash_capital_reduction', 'other')
        ),
    ratio_numerator INTEGER CHECK (ratio_numerator IS NULL OR ratio_numerator > 0),
    ratio_denominator INTEGER CHECK (ratio_denominator IS NULL OR ratio_denominator > 0),
    ex_date TEXT NOT NULL,
    note TEXT,
    UNIQUE (security_id, ex_date, action_type)
);

CREATE INDEX idx_corporate_actions_security ON corporate_actions (security_id);

CREATE TABLE broker_accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    dividend_bank_name TEXT,
    remittance_fee_note TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled'))
);

-- §4.8: the sole source of truth for holdings. Corrections are direct
-- UPDATEs (no revision chain); `created_at` is immutable and is the
-- stable tiebreaker for same-day FIFO / oversell replay ordering.
CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    broker_account_id TEXT NOT NULL REFERENCES broker_accounts (id),
    security_id TEXT NOT NULL REFERENCES securities (id),
    event_type TEXT NOT NULL CHECK (event_type IN ('buy', 'sell', 'opening')),
    trade_date TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price TEXT,
    executed_amount TEXT,
    commission TEXT,
    transaction_tax TEXT,
    other_fees TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_trades_account_security_date
ON trades (broker_account_id, security_id, trade_date, created_at);

CREATE TRIGGER trg_trades_updated_at
AFTER UPDATE ON trades
FOR EACH ROW
BEGIN
    UPDATE trades SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE id = NEW.id;
END;

CREATE TABLE dividend_receipts (
    id TEXT PRIMARY KEY,
    broker_account_id TEXT NOT NULL REFERENCES broker_accounts (id),
    security_id TEXT NOT NULL REFERENCES securities (id),
    distribution_id TEXT REFERENCES distributions (id),
    received_date TEXT NOT NULL,
    net_amount TEXT NOT NULL,
    note TEXT
);

CREATE INDEX idx_dividend_receipts_security ON dividend_receipts (security_id);

CREATE TABLE watchlist (
    security_id TEXT PRIMARY KEY REFERENCES securities (id),
    note TEXT,
    target_price TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TRIGGER trg_watchlist_updated_at
AFTER UPDATE ON watchlist
FOR EACH ROW
BEGIN
    UPDATE watchlist SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE security_id = NEW.security_id;
END;

-- §4.6.3: the one deliberate non-3NF table. Generic settings storage,
-- not relational business data.
CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
