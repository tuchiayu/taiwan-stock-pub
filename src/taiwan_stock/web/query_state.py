"""Canonical, encoded GET links for tables and post/redirect/get actions."""

from urllib.parse import urlencode

PARAMS = {
    "security_type",
    "view",
    "q",
    "market",
    "industry",
    "pay_month",
    "custodian_bank",
    "distribution_frequency",
    "sort",
    "direction",
    "page",
    "include_delisted",
}


def url(state, **changes):
    values = {**state, **changes}
    return "/securities?" + urlencode(
        {k: v for k, v in values.items() if k in PARAMS and v is not None and v != ""}
    )


def sort_links(state, keys):
    return {
        key: url(
            state,
            sort=key,
            page=1,
            direction=("desc" if state["direction"] == "asc" else "asc")
            if state["sort"] == key
            else (
                "desc"
                if key
                in ("yield", "latest_close", "change_pct", "net_assets", "holders")
                else "asc"
            ),
        )
        for key in keys
    }
