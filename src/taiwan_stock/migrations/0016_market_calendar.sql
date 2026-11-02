CREATE TABLE market_calendar_events (
    source TEXT NOT NULL CHECK(source IN ('annual','notices')),
    year INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('closed','open','needs_review')),
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    published_at TEXT,
    fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    previous_snapshot TEXT,
    PRIMARY KEY(source,year,event_key,date)
);
CREATE INDEX market_calendar_date ON market_calendar_events(date);
CREATE TABLE market_calendar_coverage (
    source TEXT NOT NULL CHECK(source IN ('annual','notices')),
    year INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success','empty','partial','failed')),
    attempted_at TEXT NOT NULL,
    succeeded_at TEXT,
    next_attempt_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    snapshot TEXT,
    PRIMARY KEY(source,year)
);
