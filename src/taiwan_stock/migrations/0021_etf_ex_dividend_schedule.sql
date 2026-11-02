-- Official ex-dividend months are distinct from the legacy issuer evaluation months.
ALTER TABLE etf_profiles ADD COLUMN distribution_ex_dividend_schedule TEXT;
