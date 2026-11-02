-- Additive official policy and identity metadata; personal overrides stay intact.
ALTER TABLE etf_profiles ADD COLUMN distribution_policy TEXT
    CHECK (distribution_policy IS NULL OR distribution_policy IN
        ('distributing', 'non_distributing', 'unknown'));
ALTER TABLE etf_profiles ADD COLUMN distribution_policy_source TEXT;
ALTER TABLE etf_profiles ADD COLUMN issuer_name TEXT;
ALTER TABLE etf_profiles ADD COLUMN basic_info_source TEXT;
ALTER TABLE catalog_sync_units ADD COLUMN field_results TEXT;
