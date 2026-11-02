-- ETF size (SITCA monthly) and holder counts (TDCC weekly), kept per period (design 044).
CREATE TABLE etf_size_snapshots (
    security_id TEXT NOT NULL REFERENCES securities(id),
    data_period TEXT NOT NULL CHECK (data_period GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    net_assets_twd INTEGER NOT NULL CHECK (net_assets_twd >= 0),
    nav_twd TEXT,
    units INTEGER CHECK (units >= 0),
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (security_id, data_period)
);
CREATE INDEX etf_size_snapshots_period ON etf_size_snapshots(data_period);

CREATE TABLE etf_holder_snapshots (
    security_id TEXT NOT NULL REFERENCES securities(id),
    data_date TEXT NOT NULL CHECK (data_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    holders INTEGER NOT NULL CHECK (holders >= 0),
    units INTEGER NOT NULL CHECK (units >= 0),
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (security_id, data_date)
);
CREATE INDEX etf_holder_snapshots_date ON etf_holder_snapshots(data_date);

CREATE TABLE etf_scale_coverage (
    source TEXT PRIMARY KEY CHECK (source IN ('size', 'holders')),
    latest_period TEXT,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    attempted_at TEXT NOT NULL,
    succeeded_at TEXT,
    next_attempt_at TEXT NOT NULL,
    message TEXT
);
