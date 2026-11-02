-- Design 010: the dividend-receiving bank is a per (broker account,
-- security) user preference, independent of the ETF's own custodian
-- bank (`etf_profiles.custodian_bank`, purely informational — where the
-- fund's distribution is paid FROM). This lets the same broker account
-- override different holdings to different receiving banks (e.g. to
-- avoid a cross-bank transfer fee when a holding's custodian happens to
-- match one of the investor's own bank accounts), falling back to
-- `broker_accounts.dividend_bank_name` when unset.
CREATE TABLE dividend_bank_preferences (
    broker_account_id TEXT NOT NULL REFERENCES broker_accounts (id),
    security_id TEXT NOT NULL REFERENCES securities (id),
    bank_name TEXT NOT NULL,
    PRIMARY KEY (broker_account_id, security_id)
);
