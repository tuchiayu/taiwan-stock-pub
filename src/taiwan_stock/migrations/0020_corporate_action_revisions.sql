-- Official ratio changes and their historical replay impact are append-only.
CREATE TABLE corporate_action_revisions (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    job_id TEXT,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);
-- No cascading FK: deleting/untracking source data must not erase audit history.
CREATE INDEX idx_action_revisions_action ON corporate_action_revisions(action_id, changed_at);
CREATE INDEX idx_action_revisions_security ON corporate_action_revisions(security_id, changed_at);
