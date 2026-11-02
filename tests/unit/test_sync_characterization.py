"""Design 071: branches of the catalog sync no other test reached, pinned
before the source flows moved out of `catalog/sync.py`."""

import httpx2
import pytest

from taiwan_stock.catalog import investment_trust_adapters as trust_adapters
from taiwan_stock.catalog import official_profile_adapters as official
from taiwan_stock.catalog import sync
from taiwan_stock.catalog import sync_investment_trust as trust_module

CATALOGS = {
    "capital_ids": {"00001": "cap-1"},
    "mega_ids": {"00001": "mega-1"},
    "sinopac_overview": {"00001": {"internal_id": "sp-1"}},
    "ezmoney_codes": {"00001": "ez-1"},
    "cathay_codes": {"00001": "cathay-1"},
    "ab_catalog": {"00001": {"from": "ab"}},
    "uob_catalog": {"00001": {"from": "uob"}},
    "jko_catalog": {"00001": {"from": "jko"}},
    "kgi_catalog": {"00001": {"from": "kgi"}},
}


def fetch(issuer, **overrides):
    return trust_module._fetch_trust_data(
        httpx2.Client(), issuer, "00001", **{**CATALOGS, **overrides}
    )


def recorder(monkeypatch, module, name):
    def adapter(*args):
        return {"adapter": name, "args": args[1:]}

    monkeypatch.setattr(module, name, adapter)


@pytest.mark.parametrize(
    "issuer,module,name,args",
    [
        ("安聯", official, "fetch_allianz", ("00001", {})),
        ("摩根", official, "fetch_jpm", ("00001", {})),
        ("貝萊德", official, "fetch_blackrock", ("00001", {})),
        ("玉山", official, "fetch_esun", ("00001", {})),
        ("FT", official, "fetch_ft", ("00001", {})),
        ("野村", official, "fetch_nomura", ("00001",)),
        ("復華", official, "fetch_fuhwa", ("00001", {})),
        (
            "群益",
            trust_adapters,
            "fetch_capital_custodian_and_frequency",
            ("cap-1",),
        ),
        (
            "國泰",
            trust_adapters,
            "fetch_cathay_custodian_and_frequency",
            ("cathay-1",),
        ),
        ("中信", trust_adapters, "fetch_ctbc_custodian_and_frequency", ("00001", {})),
        ("元大", trust_adapters, "fetch_yuanta_custodian", ("00001",)),
        ("富邦", trust_adapters, "fetch_fubon_custodian", ("00001",)),
        ("第一金", trust_adapters, "fetch_fsitc_custodian_and_frequency", ("00001",)),
        ("兆豐", trust_adapters, "fetch_mega_custodian", ("mega-1",)),
        ("統一", trust_adapters, "fetch_ezmoney_custodian", ("ez-1",)),
    ],
)
def test_each_issuer_goes_to_its_adapter(monkeypatch, issuer, module, name, args):
    recorder(monkeypatch, module, name)
    assert fetch(issuer) == {"adapter": name, "args": args}


@pytest.mark.parametrize(
    "issuer,expected",
    [
        ("聯博", {"from": "ab"}),
        ("大華", {"from": "uob"}),
        ("街口", {"from": "jko"}),
        ("凱基", {"from": "kgi"}),
    ],
)
def test_list_only_issuers_read_their_catalog(issuer, expected):
    assert fetch(issuer) == expected


def test_an_unknown_issuer_is_an_adapter_error():
    with pytest.raises(trust_adapters.TrustAdapterError, match="未知投信：其他"):
        fetch("其他")


def test_sinopac_keeps_the_listed_frequency_when_the_bank_page_fails(monkeypatch):
    def broken(*_args):
        raise ValueError("bank page changed")

    monkeypatch.setattr(trust_adapters, "fetch_sinopac_custodian", broken)
    entry = {"internal_id": "sp-1", "distribution_frequency_raw": "月配"}
    result = fetch("永豐", sinopac_overview={"00001": entry})
    assert result["source_url"] == trust_adapters._SINOPAC_OVERVIEW_URL
    assert str(result["field_errors"]["bank"]) == "bank page changed"
    assert result["distribution_frequency_raw"] == "月配"


def test_sinopac_without_a_listed_frequency_raises(monkeypatch):
    def broken(*_args):
        raise ValueError("bank page changed")

    monkeypatch.setattr(trust_adapters, "fetch_sinopac_custodian", broken)
    with pytest.raises(ValueError, match="bank page changed"):
        fetch("永豐")


def test_taishin_falls_back_to_the_listed_policy(monkeypatch):
    def broken(*_args):
        raise ValueError("bank page changed")

    monkeypatch.setattr(trust_adapters, "fetch_taishin_custodian", broken)
    policies = {
        "00001": {"distribution_policy": "distributing", "policy_source_url": "u"}
    }
    result = fetch("台新", taishin_policies=policies)
    assert result["source_url"] == "u"
    assert str(result["field_errors"]["bank"]) == "bank page changed"


def test_taishin_merges_the_listed_policy_into_a_bank_only_result(monkeypatch):
    monkeypatch.setattr(
        trust_adapters,
        "fetch_taishin_custodian",
        lambda *_args: {"custodian_bank": "台新銀行"},
    )
    policies = {"00001": {"distribution_policy": "distributing"}}
    result = fetch("台新", taishin_policies=policies)
    assert result == {
        "custodian_bank": "台新銀行",
        "distribution_policy": "distributing",
    }


@pytest.mark.parametrize(
    "exc,expected",
    [
        (
            httpx2.HTTPStatusError(
                "x",
                request=httpx2.Request("GET", "https://a.invalid"),
                response=httpx2.Response(503),
            ),
            "HTTP 503",
        ),
        (httpx2.ConnectError("refused"), "ConnectError"),
        (ValueError("first line\nsecond line"), "first line"),
        (ValueError(""), "ValueError"),
        (ValueError("x" * 200), "x" * 120),
    ],
)
def test_short_reason(exc, expected):
    assert sync._short_reason(exc) == expected
