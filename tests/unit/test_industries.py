import pytest

from taiwan_stock.catalog.industries import industry_filter_label, industry_name


@pytest.mark.parametrize(
    ("market", "code", "expected"),
    [
        ("TWSE", "03", "塑膠工業"),
        ("TWSE", "25", "電腦及週邊設備業"),
        ("TWSE", "17", "金融保險"),
        ("TPEx", "17", "金融業"),
        ("TPEx", "32", "文化創意業"),
        ("TPEx", "80", "管理股票"),
        ("TWSE", "99", "99"),
        ("TWSE", "32", "32"),
        ("unknown", "03", "03"),
        ("TWSE", None, "—"),
        ("TWSE", "歷史來源名稱", "歷史來源名稱"),
    ],
)
def test_industry_names_respect_market_and_preserve_unknown(market, code, expected):
    assert industry_name(code, market) == expected


def test_filter_names_preserve_code_and_market_differences():
    assert industry_filter_label("03") == "塑膠工業（03）"
    assert industry_filter_label("17") == "金融保險／金融業（17）"
    assert industry_filter_label("99") == "99"
