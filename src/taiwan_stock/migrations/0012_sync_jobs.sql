CREATE TABLE sync_jobs (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    origin TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    parameters TEXT NOT NULL,
    plan TEXT,
    retry_of TEXT REFERENCES sync_jobs(id),
    status TEXT NOT NULL CHECK(status IN ('queued','running','success','partial','failed','interrupted')),
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    phase TEXT NOT NULL DEFAULT '準備中',
    processed INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    failed_units INTEGER NOT NULL DEFAULT 0,
    owner_token TEXT NOT NULL,
    owner_pid INTEGER,
    heartbeat_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT
);
CREATE TABLE sync_job_resources (
    resource_key TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES sync_jobs(id)
);
CREATE TABLE sync_job_steps (
    job_id TEXT NOT NULL REFERENCES sync_jobs(id),
    step_key TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY(job_id, step_key)
);
ALTER TABLE catalog_sync_runs ADD COLUMN job_id TEXT REFERENCES sync_jobs(id);
CREATE INDEX sync_run_job ON catalog_sync_runs(job_id);
