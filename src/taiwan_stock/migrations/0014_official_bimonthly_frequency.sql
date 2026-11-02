-- The original CHECK omitted bimonthly although official policies and the UI
-- support it. Rebuild only ETF reference profiles, preserving every value.
CREATE TABLE etf_profiles_next (
    security_id TEXT PRIMARY KEY REFERENCES securities (id),
    custodian_bank TEXT,
    distribution_frequency TEXT CHECK (
        distribution_frequency IS NULL OR distribution_frequency IN (
            'monthly', 'bimonthly', 'quarterly', 'semiannual',
            'annual', 'irregular', 'unknown'
        )
    ),
    fee_summary TEXT,
    benchmark TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    custodian_institution_raw TEXT,
    custodian_bank_source TEXT,
    custodian_bank_user_override TEXT,
    distribution_frequency_source TEXT,
    distribution_month_pattern TEXT,
    distribution_frequency_inferred TEXT,
    distribution_frequency_user_override TEXT,
    distribution_policy TEXT CHECK (
        distribution_policy IS NULL OR distribution_policy IN (
            'distributing', 'non_distributing', 'unknown'
        )
    ),
    distribution_policy_source TEXT,
    issuer_name TEXT,
    basic_info_source TEXT
);
INSERT INTO etf_profiles_next (
    security_id,
    custodian_bank,
    distribution_frequency,
    fee_summary,
    benchmark,
    updated_at,
    custodian_institution_raw,
    custodian_bank_source,
    custodian_bank_user_override,
    distribution_frequency_source,
    distribution_month_pattern,
    distribution_frequency_inferred,
    distribution_frequency_user_override,
    distribution_policy,
    distribution_policy_source,
    issuer_name,
    basic_info_source
) SELECT
    security_id,
    custodian_bank,
    distribution_frequency,
    fee_summary,
    benchmark,
    updated_at,
    custodian_institution_raw,
    custodian_bank_source,
    custodian_bank_user_override,
    distribution_frequency_source,
    distribution_month_pattern,
    distribution_frequency_inferred,
    distribution_frequency_user_override,
    distribution_policy,
    distribution_policy_source,
    issuer_name,
    basic_info_source
FROM etf_profiles;
DROP TABLE etf_profiles;
ALTER TABLE etf_profiles_next RENAME TO etf_profiles;
CREATE TRIGGER trg_etf_profiles_updated_at
AFTER UPDATE ON etf_profiles
FOR EACH ROW
BEGIN
    UPDATE etf_profiles SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE security_id = NEW.security_id;
END;
