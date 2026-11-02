-- Issue #37: Taiwan's day-trade transaction-tax discount (0.15% vs the
-- normal 0.3% stock rate) can't be derived from existing trade data —
-- eligibility (margin-tradable security, not a full-cash-delivery or
-- disposition stock) and same-day buy/sell matching aren't tracked
-- here, and ETFs never get the discount regardless. The user marks it
-- directly instead. NULL means "not applicable / not specified" (buys
-- and opening entries never set it); 0/1 only matters for sells.
ALTER TABLE trades ADD COLUMN is_day_trade INTEGER CHECK (is_day_trade IS NULL OR is_day_trade IN (0, 1));
