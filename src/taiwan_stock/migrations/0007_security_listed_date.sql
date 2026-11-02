-- Design 005 §4.5 follow-up: a security with zero known distribution
-- events in the near-term query window looked identical whether it had
-- a full opportunity to pay and confirmed it didn't, or it simply
-- listed too recently to have had one yet (live case 2026-09-13:
-- 00400A listed 2026-04-09, ~150 days before its first eligible
-- payout window closes). `listed_date` lets that distinction be shown
-- instead of silently guessed at. Sourced from TWSE/TPEx's own basic
-- info (`上市日期`/`DateOfListing`) — TPEx's ETF list endpoint has no
-- such field, so TPEx-listed ETFs' `listed_date` stays NULL until a
-- source for it is found; NULL is treated the same as "unknown", not
-- "definitely not new".

ALTER TABLE securities ADD COLUMN listed_date TEXT;
