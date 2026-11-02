CREATE TABLE corporate_action_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES corporate_actions(id),
    broker_account_id TEXT NOT NULL REFERENCES broker_accounts(id),
    decision TEXT NOT NULL CHECK (decision IN ('confirmed', 'accepted_estimate', 'reopened')),
    reviewed_at TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    quantity TEXT NOT NULL,
    total_cost TEXT,
    basis_snapshot TEXT NOT NULL CHECK (json_valid(basis_snapshot)),
    basis_fingerprint TEXT NOT NULL,
    note TEXT NOT NULL CHECK (length(trim(note)) BETWEEN 1 AND 2000),
    CHECK (decision = 'reopened' OR total_cost IS NOT NULL)
);

CREATE INDEX corporate_action_reviews_pair
    ON corporate_action_reviews(action_id, broker_account_id, id DESC);
