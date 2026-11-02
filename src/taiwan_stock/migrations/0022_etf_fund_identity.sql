-- TWSE fund identity, used to pair foreign-currency share classes (design 043).
ALTER TABLE etf_profiles ADD COLUMN fund_tax_id TEXT;
ALTER TABLE etf_profiles ADD COLUMN fund_type TEXT;
