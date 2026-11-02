"""Bank alias dictionary (design 004 §4.5.3).

Version-controlled by being a plain source file — no separate data
format or package-data wiring needed. Only maintains "which raw names
belong to the same bank"; it has no opinion on which bank custodies
which ETF (design 004 §3: that must come from an API/adapter or a
single-user manual override, never a hand-maintained ETF→bank table).

Seeded with every bank name actually observed across the 9 verified
投信 adapters (design 004 §4.3.1) — extend this table when a new
adapter surfaces a bank not yet listed here, rather than guessing at
a match.
"""

import unicodedata
from typing import NamedTuple

from taiwan_stock.catalog._parsing import clean_text


class Bank(NamedTuple):
    id: str
    display_name: str
    aliases: tuple[str, ...]


BANKS: tuple[Bank, ...] = (
    Bank(
        "tcb",
        "合作金庫商業銀行",
        (
            "合作金庫商業銀行",
            "合作金庫商業銀行股份有限公司",
            "合作金庫銀行",
            "合作金庫",
            # 凱基 page for 00749B, observed 2026-10-30.
            "合庫銀行",
        ),
    ),
    Bank(
        "sinopac", "永豐銀行", ("永豐銀行", "永豐商業銀行", "永豐商業銀行股份有限公司")
    ),
    Bank(
        "land",
        "臺灣土地銀行",
        ("臺灣土地銀行", "台灣土地銀行", "臺灣土地銀行股份有限公司", "土地銀行"),
    ),
    Bank(
        "taichung",
        "臺中商業銀行",
        ("臺中商銀", "台中商銀", "臺中商業銀行", "台中商業銀行"),
    ),
    Bank(
        "tbb",
        "臺灣中小企業銀行",
        ("台灣中小企業銀行", "臺灣中小企業銀行", "臺灣中小企業銀行股份有限公司"),
    ),
    Bank(
        "shinkong",
        "臺灣新光商業銀行",
        (
            "臺灣新光商業銀行",
            "台灣新光商業銀行",
            "新光銀行",
            # TWSE 保管機構 for 00730, observed 2026-10-30.
            "臺灣新光商業銀行股份有限公司",
        ),
    ),
    Bank(
        "ctbc",
        "中國信託商業銀行",
        (
            "中國信託商業銀行",
            "中國信託商業銀行股份有限公司",
            "中國信託",
            "中國信託銀行",
        ),
    ),
    Bank(
        "esun",
        "玉山銀行",
        ("玉山銀行", "玉山商業銀行", "玉山商業銀行股份有限公司"),
    ),
    Bank(
        "chb",
        "彰化銀行",
        (
            "彰化銀行",
            "彰化商業銀行",
            "彰化商業銀行股份有限公司",
            # TWSE 保管機構 for 00407A, observed 2026-10-15.
            "彰化銀行股份有限公司",
        ),
    ),
    Bank(
        "taishin",
        "台新國際商業銀行",
        ("台新國際商業銀行", "台新國際商業銀行股份有限公司", "台新銀行"),
    ),
    Bank(
        "hncb",
        "華南銀行",
        ("華南銀行", "華南商業銀行", "華南商業銀行股份有限公司"),
    ),
    Bank(
        "mega",
        "兆豐國際商業銀行",
        ("兆豐國際商業銀行", "兆豐國際商業銀行股份有限公司", "兆豐銀行"),
    ),
    Bank(
        "cathay",
        "國泰世華銀行",
        ("國泰世華銀行", "國泰世華商業銀行", "國泰世華商業銀行股份有限公司"),
    ),
    Bank(
        "first",
        "第一商業銀行",
        ("第一商業銀行", "第一銀行", "第一商業銀行股份有限公司"),
    ),
    Bank(
        "bot",
        "臺灣銀行",
        # "(股)公司": 兆豐 page for 00957B, observed 2026-10-30.
        ("臺灣銀行", "台灣銀行", "臺灣銀行股份有限公司", "臺灣銀行(股)公司"),
    ),
    Bank(
        "fubon",
        "台北富邦商業銀行",
        (
            "台北富邦商業銀行",
            "台北富邦銀行",
            "富邦銀行",
            "台北富邦商業銀行股份有限公司",
        ),
    ),
    # TWSE 保管機構 and 投信 pages, observed 2026-10-30 (design 047).
    Bank(
        "yuanta",
        "元大商業銀行",
        ("元大商業銀行", "元大銀行", "元大商業銀行股份有限公司"),
    ),
    # 元大 page for 00870B and 凱基 page for 00750B, observed 2026-10-30.
    Bank(
        "scsb",
        "上海商業儲蓄銀行",
        ("上海商業儲蓄銀行", "上海商銀", "上海商業儲蓄銀行股份有限公司"),
    ),
)

_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: bank.display_name for bank in BANKS for alias in bank.aliases
}


def canonical_name(raw: str | None) -> str | None:
    """Normalizes a raw bank name string to its catalogued display
    name; returns the input unchanged if it isn't a known alias
    (design 004 §4.5.3 — a source can still confirm a bank we haven't
    catalogued yet, it just won't get name-variant normalization until
    this table is extended)."""
    if not raw:
        return None
    cleaned = unicodedata.normalize("NFKC", clean_text(raw) or "").strip()
    return _ALIAS_TO_CANONICAL.get(cleaned, cleaned) or None


def display_names() -> list[str]:
    """The closed set of catalogued bank names — design 004 §4.5.1's
    `custodian_bank_user_override` dropdown is restricted to this list
    rather than free text, to avoid reintroducing the "same bank,
    multiple spellings" problem this design exists to fix (§2.1)."""
    return [bank.display_name for bank in BANKS]
