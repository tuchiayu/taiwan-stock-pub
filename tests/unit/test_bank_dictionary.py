"""Unit tests for the bank alias dictionary (design 004 §4.5.3)."""

from taiwan_stock.catalog import bank_dictionary, sync_investment_trust


def test_canonical_name_normalizes_known_alias():
    assert (
        bank_dictionary.canonical_name("中國信託商業銀行股份有限公司")
        == "中國信託商業銀行"
    )
    assert bank_dictionary.canonical_name("中國信託") == "中國信託商業銀行"


def test_exchange_custodian_name_for_chb_resolves_to_a_catalogued_bank():

    assert bank_dictionary.canonical_name("彰化銀行股份有限公司") == "彰化銀行"
    assert sync_investment_trust._exchange_bank("彰化銀行股份有限公司") == "彰化銀行"


def test_yuanta_and_shinkong_exchange_names_resolve():
    # Design 047: names TWSE and 投信 pages used for these banks on 2026-10-30.

    for raw in ("元大銀行", "元大商業銀行", "元大商業銀行股份有限公司"):
        assert bank_dictionary.canonical_name(raw) == "元大商業銀行"
    assert sync_investment_trust._exchange_bank("元大銀行") == "元大商業銀行"
    assert (
        sync_investment_trust._exchange_bank("臺灣新光商業銀行股份有限公司")
        == "臺灣新光商業銀行"
    )


def test_issuer_page_spellings_share_one_bank():
    # Design 047: 投信 pages spelled these banks differently on 2026-10-30.
    assert bank_dictionary.canonical_name("上海商銀") == "上海商業儲蓄銀行"
    assert bank_dictionary.canonical_name("合庫銀行") == "合作金庫商業銀行"
    assert bank_dictionary.canonical_name("臺灣銀行(股)公司") == "臺灣銀行"
    assert bank_dictionary.canonical_name("臺灣銀行（股）公司") == "臺灣銀行"


def test_canonical_name_strips_whitespace():
    assert bank_dictionary.canonical_name("  玉山銀行  ") == "玉山銀行"


def test_canonical_name_passes_through_unknown_bank_unchanged():
    # Design 004 §4.5.3: a source can confirm a bank we haven't
    # catalogued — it just doesn't get name-variant normalization yet.
    assert bank_dictionary.canonical_name("測試銀行") == "測試銀行"


def test_canonical_name_none_for_blank_input():
    assert bank_dictionary.canonical_name(None) is None
    assert bank_dictionary.canonical_name("") is None


def test_display_names_returns_one_entry_per_bank_no_aliases():
    names = bank_dictionary.display_names()
    assert len(names) == len(set(names))
    assert "合作金庫商業銀行" in names
    assert "中國信託商業銀行" in names


def test_unicode_and_html_aliases_share_one_bank():
    assert (
        bank_dictionary.canonical_name("&nbsp;合作金庫商業銀行&nbsp;")
        == "合作金庫商業銀行"
    )
    assert bank_dictionary.canonical_name("  &nbsp; ") is None
