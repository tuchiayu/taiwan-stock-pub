-- Design 004 §4.5.1: `custodian_bank` was being filled directly from
-- TWSE `t187ap47_L`'s raw "保管機構" value, which is frequently the
-- central depository (集保) or the issuing 投信 itself rather than the
-- fund's actual custodian bank (see design 004 §4.3.1's cross-check).
-- The raw value moves to `custodian_institution_raw` for reference
-- only; `custodian_bank` is reserved for values confirmed via a 投信
-- adapter or user override (design 004 Phase E), which do not exist
-- yet, so it is cleared here rather than left holding unverified data.

ALTER TABLE etf_profiles ADD COLUMN custodian_institution_raw TEXT;

UPDATE etf_profiles
SET custodian_institution_raw = custodian_bank
WHERE custodian_bank IS NOT NULL;

UPDATE etf_profiles SET custodian_bank = NULL;
