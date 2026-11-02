"""Design 063: foreign-currency listings and the share-class retry status."""

import pytest

from taiwan_stock.catalog import share_classes, sync_history

FOREIGN = "國外成份/加掛外幣證券指數股票型基金"


@pytest.mark.parametrize(
    "fund_type,name",
    [
        (FOREIGN, "富邦上証+R"),
        (FOREIGN, "國泰中國A50+U"),
        (FOREIGN, "群益深証中小+R"),
        (FOREIGN, "國泰日經225+U"),
        (FOREIGN, "國泰美國道瓊+U"),
        (FOREIGN, "00774C"),
        (None, "國泰20年美債+櫃U"),
    ],
)
def test_foreign_currency_listings(fund_type, name):
    assert share_classes.is_foreign_currency(fund_type, name)


@pytest.mark.parametrize(
    "fund_type,name",
    [
        ("槓桿/反向指數股票型基金", "元大台灣50正2"),
        ("槓桿/反向指數股票型基金", "元大台灣50反1"),
        ("指數股票型期貨信託基金", "期元大S&P黃金"),
        (None, "平衡凱基美國TOP"),
        ("國內成分證券指數股票型基金", "元大台灣50"),
        (None, "元大美債20年"),
        (None, None),
    ],
)
def test_twd_listings(fund_type, name):
    assert not share_classes.is_foreign_currency(fund_type, name)


def test_base_pending_is_transient_and_retried():
    assert sync_history.is_transient_failure(sync_history.BASE_PENDING)
    assert sync_history.BASE_PENDING not in sync_history.SETTLED
