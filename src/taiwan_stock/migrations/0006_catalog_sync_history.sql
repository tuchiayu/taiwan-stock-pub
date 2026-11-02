CREATE TABLE catalog_sync_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    retry_of TEXT REFERENCES catalog_sync_runs(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','success','partial','failed','interrupted'))
);
CREATE UNIQUE INDEX one_catalog_sync_per_source ON catalog_sync_runs(source) WHERE status = 'running';
CREATE TABLE catalog_sync_units (
    run_id TEXT NOT NULL REFERENCES catalog_sync_runs(id),
    unit_key TEXT NOT NULL,
    security_id TEXT REFERENCES securities(id),
    issuer TEXT,
    stage TEXT NOT NULL,
    period TEXT,
    attempted_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    fields TEXT NOT NULL DEFAULT '[]',
    source_url TEXT,
    error_type TEXT,
    reason TEXT,
    PRIMARY KEY (run_id, unit_key)
);
CREATE INDEX sync_unit_security ON catalog_sync_units(security_id, attempted_at);
