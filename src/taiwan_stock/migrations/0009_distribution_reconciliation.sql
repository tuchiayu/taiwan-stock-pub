-- Design 009 §4.4: ETF distribution sources (TWSE `etfDiv`, TPEx
-- `etfExDiv`) have no stable event ID — verified live 2026-09-20 against
-- multiple codes/years, fields are limited to code/ex-dividend
-- date/record date/pay date/amount/remark text/announcement year. When a
-- previously-synced ex_dividend_date for a security stops appearing in a
-- resync of the same (security, year) unit, there is no reliable way to
-- tell whether it was corrected to a new date or genuinely removed, so
-- the stale row is flagged for manual review rather than auto-merged,
-- auto-cancelled, or deleted.

ALTER TABLE distributions ADD COLUMN reconciliation_status TEXT
    CHECK (reconciliation_status IS NULL OR reconciliation_status IN ('needs_review'));
ALTER TABLE distributions ADD COLUMN reconciliation_note TEXT;
ALTER TABLE distributions ADD COLUMN reconciliation_detected_at TEXT;

CREATE INDEX idx_distributions_reconciliation
    ON distributions (reconciliation_status)
    WHERE reconciliation_status IS NOT NULL;
