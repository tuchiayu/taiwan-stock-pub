-- Design 004 §4.5.1: three-tier confidence model for `custodian_bank`
-- and `distribution_frequency` — official 投信-adapter value, system
-- inference (frequency only), and single-user manual override — kept
-- in separate columns so no tier's write can clobber another's, and
-- the display layer merges them by priority instead.

ALTER TABLE etf_profiles ADD COLUMN custodian_bank_source TEXT;
ALTER TABLE etf_profiles ADD COLUMN custodian_bank_user_override TEXT;

ALTER TABLE etf_profiles ADD COLUMN distribution_frequency_source TEXT;
ALTER TABLE etf_profiles ADD COLUMN distribution_month_pattern TEXT;
ALTER TABLE etf_profiles ADD COLUMN distribution_frequency_inferred TEXT;
ALTER TABLE etf_profiles ADD COLUMN distribution_frequency_user_override TEXT;
