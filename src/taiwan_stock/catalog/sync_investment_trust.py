"""投信 custodian banks and distribution frequencies (design 071).

Retry, circuit breaker and the shared upserts live in `sync.py`.
"""

import json

import httpx2

from taiwan_stock.catalog import (
    bank_dictionary,
    delistings,
    host_pool,
    share_classes,
    sync,
    sync_history,
)
from taiwan_stock.catalog import investment_trust_adapters as trust_adapters
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared import sync_progress
from taiwan_stock.shared.dates import now_utc_iso

# Pacing measured live; see the notes above `_RETRY_BACKOFF_SECONDS` in sync.py.
_REQUEST_DELAY_SECONDS = 1.0


# Design 004 §4.4.1: only a raw label from a source confirmed to carry
# an *official* frequency field maps to a classification value — a
# label this table doesn't recognize is left unmapped (`None`) rather
# than guessed at, and the raw text is still kept in
# `distribution_frequency_source` for a human to review.
_FREQUENCY_LABEL_MAP = {
    "月配": "monthly",
    "月配息": "monthly",
    "雙月配": "bimonthly",
    "雙月配息": "bimonthly",
    "季配": "quarterly",
    "季配息": "quarterly",
    "半年配": "semiannual",
    "半年配息": "semiannual",
    "年配": "annual",
    "年配息": "annual",
    "不定期": "irregular",
    "不定期配息": "irregular",
}


def _exchange_bank(institution_raw: str | None) -> str | None:
    """TWSE/TPEx's `custodian_institution_raw`, resolved to a catalogued
    bank name — usable directly as the custodian bank when it isn't the
    CSDC/投信-itself placeholder §4.3.1 warns about (frequently is).
    Live-verified 2026-09-13 (design 005 §4.4 follow-up): this official,
    daily-refreshed field agreed with a 投信 adapter's own scrape 60/61
    times where both existed, and was the *accurate* one in the one
    disagreement found (the issuer's own low-traffic marketing page was
    stale) — reliable enough to check first, going to the 投信 adapter
    only when this field can't resolve to a bank at all."""
    canonical = (
        bank_dictionary.canonical_name(institution_raw) if institution_raw else None
    )
    return canonical if canonical in bank_dictionary.display_names() else None


def _custodian_cross_check_status(
    adapter_bank: str, institution_raw: str | None
) -> str:
    """Design 004 §4.5.1's `custodian_bank_source.cross_check` — only
    meaningful when `institution_raw` itself resolves to a catalogued
    bank name (§4.3.1 — it's frequently a CSDC/投信 name instead, which
    cannot "conflict" or "confirm" anything). Advisory only: as of
    design 005 §4.4, `_exchange_bank` covers this same resolution as the
    *primary* source, not just a check against the 投信 adapter's value."""
    exchange = _exchange_bank(institution_raw)
    if exchange is None:
        return "single_source"
    if exchange == adapter_bank:
        return "confirmed"
    return "conflict"


def _base_settled(conn, run_id, base_id, latest):
    """Whether the TWD class's investment-trust outcome is settled (synced,
    empty, unsupported, ...): this run's when planned, else its latest one.
    A failure or no outcome yet leaves the share class to retry (design 063)."""
    row = conn.execute(
        "SELECT status FROM catalog_sync_units WHERE run_id=? AND security_id=?",
        (run_id, base_id),
    ).fetchone()
    unit = dict(row) if row else latest.get((base_id, "investment_trust", None))
    return bool(unit) and unit["status"] in sync_history.SETTLED


def _fetch_trust_data(
    client: httpx2.Client,
    issuer: str,
    code: str,
    *,
    capital_ids: dict[str, str],
    mega_ids: dict[str, str],
    sinopac_overview: dict[str, dict[str, str]],
    ezmoney_codes: dict[str, str],
    fuhwa_catalog=None,
    kgi_catalog=None,
    taishin_policies=None,
    ft_catalog=None,
    uob_catalog=None,
    jko_catalog=None,
    esun_catalog=None,
    blackrock_catalog=None,
    ab_catalog=None,
    jpm_catalog=None,
    allianz_catalog=None,
    ctbc_catalog=None,
    cathay_codes=None,
) -> dict:
    """Route to a verified issuer source; missing codes remain mapping failures."""
    from taiwan_stock.catalog import official_profile_adapters as official

    if issuer == "安聯":
        return official.fetch_allianz(client, code, allianz_catalog or {})
    if issuer == "聯博":
        return sync_history.listed(ab_catalog, code)
    if issuer == "摩根":
        return official.fetch_jpm(client, code, jpm_catalog or {})
    if issuer == "貝萊德":
        return official.fetch_blackrock(client, code, blackrock_catalog or {})
    if issuer == "玉山":
        return official.fetch_esun(client, code, esun_catalog or {})
    if issuer == "FT":
        return official.fetch_ft(client, code, ft_catalog or {})
    if issuer == "大華":
        return sync_history.listed(uob_catalog, code)
    if issuer == "街口":
        return sync_history.listed(jko_catalog, code)
    if issuer == "凱基":
        return sync_history.listed(kgi_catalog, code)
    if issuer == "野村":
        return official.fetch_nomura(client, code)
    if issuer == "復華":
        return official.fetch_fuhwa(client, code, fuhwa_catalog or {})
    if issuer == "群益":
        return trust_adapters.fetch_capital_custodian_and_frequency(
            client, sync_history.listed(capital_ids, code)
        )
    if issuer == "國泰":
        return trust_adapters.fetch_cathay_custodian_and_frequency(
            client, sync_history.listed(cathay_codes, code)
        )
    if issuer == "中信":
        return trust_adapters.fetch_ctbc_custodian_and_frequency(
            client, code, ctbc_catalog or {}
        )
    if issuer == "元大":
        return trust_adapters.fetch_yuanta_custodian(client, code)
    if issuer == "富邦":
        return trust_adapters.fetch_fubon_custodian(client, code)
    if issuer == "第一金":
        return trust_adapters.fetch_fsitc_custodian_and_frequency(client, code)
    if issuer == "兆豐":
        return trust_adapters.fetch_mega_custodian(
            client, sync_history.listed(mega_ids, code)
        )
    if issuer == "永豐":
        entry = sync_history.listed(sinopac_overview, code)
        try:
            result = trust_adapters.fetch_sinopac_custodian(
                client, entry["internal_id"]
            )
        except Exception as exc:
            if not entry.get("distribution_frequency_raw"):
                raise
            result = {
                "source_url": trust_adapters._SINOPAC_OVERVIEW_URL,
                "field_errors": {"bank": exc},
            }
        if "distribution_frequency_raw" in entry:
            result["distribution_frequency_raw"] = entry["distribution_frequency_raw"]
        return result
    if issuer == "台新":
        policies = (taishin_policies or {}).get(code, {})
        try:
            result = trust_adapters.fetch_taishin_custodian(client, code)
        except Exception as exc:
            if not policies.get("distribution_policy"):
                raise
            result = {
                "source_url": policies["policy_source_url"],
                "field_errors": {"bank": exc},
            }
        if result.get("distribution_policy") or result.get("field_errors"):
            return result
        return {**result, **policies}
    if issuer == "統一":
        return trust_adapters.fetch_ezmoney_custodian(
            client, sync_history.listed(ezmoney_codes, code)
        )
    raise trust_adapters.TrustAdapterError(f"未知投信：{issuer}")


def upsert_etf_bank_and_frequency(conn, security_id, fields):
    """Merge independent official fields without touching personal overrides."""
    previous = conn.execute(
        "SELECT distribution_frequency,distribution_policy FROM etf_profiles "
        "WHERE security_id=?",
        (security_id,),
    ).fetchone()
    columns = [
        "custodian_bank",
        "custodian_bank_source",
        "distribution_frequency",
        "distribution_frequency_source",
        "distribution_policy",
        "distribution_policy_source",
    ]
    assignments, values = [], []
    for column in columns:
        if fields.get(column) is not None:
            assignments.append(column + "=?")
            values.append(fields[column])
    if fields.get("distribution_policy") == "non_distributing":
        assignments += [
            "distribution_frequency=NULL",
            "distribution_frequency_source=NULL",
            "distribution_month_pattern=NULL",
            "distribution_ex_dividend_schedule=NULL",
        ]
    elif fields.get("distribution_frequency") is not None:
        # Clear stale legacy evaluation months when the official policy changes.
        assignments.append("distribution_month_pattern=?")
        values.append(fields.get("distribution_month_pattern"))
        if previous and previous["distribution_frequency"] not in (
            None,
            fields["distribution_frequency"],
        ):
            assignments.append("distribution_ex_dividend_schedule=NULL")
    if assignments:
        conn.execute(
            "UPDATE etf_profiles SET " + ",".join(assignments) + " WHERE security_id=?",
            (*values, security_id),
        )


def sync_investment_trust_data(conn, client, *, retry_run_id=None, issuer=None):
    """Fill official policies and banks independently; retain per-field outcomes."""
    return host_pool.run(
        investment_trust_steps(conn, client, retry_run_id=retry_run_id, issuer=issuer)
    )


def investment_trust_steps(conn, client, *, retry_run_id=None, issuer=None):
    """`host_pool` steps (design 053): each issuer's site is its own host, so
    the 21 issuers are fetched at once while each keeps its one-second
    pacing; the units are written in the old order afterwards."""
    from taiwan_stock.catalog import official_profile_adapters as official
    from taiwan_stock.catalog.profile_enrichment import merge_basic

    delistings.refresh_status(conn)
    etfs = conn.execute(
        "SELECT s.id,s.code,s.name,s.status,s.market,p.custodian_institution_raw,"
        "p.issuer_name FROM securities s JOIN etf_profiles p ON p.security_id=s.id "
        "WHERE s.security_type='etf' ORDER BY s.code"
    ).fetchall()
    supported = {
        "群益",
        "國泰",
        "中信",
        "元大",
        "富邦",
        "第一金",
        "兆豐",
        "永豐",
        "台新",
        "統一",
        "野村",
        "復華",
        "凱基",
        "FT",
        "大華",
        "街口",
        "玉山",
        "貝萊德",
        "聯博",
        "摩根",
        "安聯",
    }
    synced, skipped_no_issuer = 0, 0
    failed_units, list_lookup_failures = [], []
    with sync_history.run(conn, "investment_trust", retry_run_id) as task:
        # Share classes skip their issuer (which lists only the TWD class)
        # and take its values once every other unit is done (design 043).
        share_pairs = share_classes.pairs(conn)
        work, inheriting = [], []
        for etf in etfs:
            etf_issuer = trust_adapters.guess_issuer(
                etf["issuer_name"] or ""
            ) or trust_adapters.guess_issuer(etf["name"])
            if issuer is not None and etf_issuer != issuer:
                continue
            key = f"{etf_issuer or 'unsupported'}:{etf['code']}"
            if not task.wanted(key):
                continue
            task.plan(key, security_id=etf["id"], issuer=etf_issuer)
            if etf["status"] == "delisted":
                task.record(
                    key,
                    delistings.SKIPPED,
                    reason=delistings.skip_reason(etf["market"]),
                )
                continue
            if etf["id"] in share_pairs:
                inheriting.append((etf, key))
                continue
            work.append(
                (etf, etf_issuer, key, _exchange_bank(etf["custodian_institution_raw"]))
            )
        list_errors = {}
        lists = (
            ("capital_ids", "群益", trust_adapters.fetch_capital_product_ids),
            ("mega_ids", "兆豐", trust_adapters.fetch_mega_product_ids),
            ("sinopac_overview", "永豐", trust_adapters.fetch_sinopac_overview),
            ("ezmoney_codes", "統一", trust_adapters.fetch_ezmoney_fund_codes),
            ("fuhwa_catalog", "復華", official.fetch_fuhwa_catalog),
            ("kgi_catalog", "凱基", official.fetch_kgi_catalog),
            ("taishin_policies", "台新", official.fetch_taishin_policies),
            ("ctbc_catalog", "中信", trust_adapters.fetch_ctbc_catalog),
            ("cathay_codes", "國泰", trust_adapters.fetch_cathay_fund_codes),
            ("allianz_catalog", "安聯", official.fetch_allianz_catalog),
            ("ab_catalog", "聯博", official.fetch_ab_catalog),
            ("jpm_catalog", "摩根", official.fetch_jpm_catalog),
            ("blackrock_catalog", "貝萊德", official.fetch_blackrock_catalog),
            ("esun_catalog", "玉山", official.fetch_esun_catalog),
            ("ft_catalog", "FT", official.fetch_ft_catalog),
            ("uob_catalog", "大華", official.fetch_uob_catalog),
            ("jko_catalog", "街口", official.fetch_jko_catalog),
        )
        # Every key exists up front: workers only replace values, so another
        # issuer's `**catalogs` never sees the dict change size.
        catalogs = {name: {} for name, _, _ in lists}
        pool = host_pool.current.get()

        def load_list(name, label, loader):
            # Runs on the issuer's worker, ahead of that issuer's products.
            try:
                result = loader(client)
                if not result:
                    raise ValueError("投信名單未能解析")
            except Exception as exc:
                list_errors[label] = exc
                raise
            catalogs[name] = result
            return result

        list_responses = []
        for name, label, loader in lists:
            if not any(etf_issuer == label for _, etf_issuer, _, _ in work):
                continue
            task.plan("list:" + label, issuer=label, stage="list")
            list_responses.append(
                (
                    label,
                    pool.submit(
                        label,
                        lambda n=name, lb=label, ld=loader: load_list(n, lb, ld),
                        delay=0,
                    ),
                )
            )

        def fetch_product(etf_issuer, code):
            if etf_issuer in list_errors and etf_issuer != "台新":
                raise list_errors[etf_issuer]
            return sync._fetch_with_retry(
                _fetch_trust_data, client, etf_issuer, code, **catalogs
            )

        responses = {
            key: pool.submit(
                etf_issuer,
                lambda i=etf_issuer, c=etf["code"]: fetch_product(i, c),
                delay=_REQUEST_DELAY_SECONDS,
                breaker=(
                    "investment-trust",
                    sync._CIRCUIT_BREAKER_THRESHOLD,
                    sync._is_blocking_error,
                ),
            )
            for etf, etf_issuer, key, _ in work
            if etf_issuer in supported
        }
        yield
        for label, response in list_responses:
            key = "list:" + label
            try:
                task.record(
                    key, "success", count=len(host_pool.wait(response).result())
                )
            except Exception as exc:
                task.record(key, "parse_failed", exc=exc)
                list_lookup_failures.append(label)
        for etf, etf_issuer, key, exchange_bank in work:
            sync_progress.checkpoint()
            skipped = (
                host_pool.wait(responses[key]).exception()
                if etf_issuer in supported
                else None
            )
            if isinstance(skipped, host_pool.Skipped) and skipped.cause is None:
                continue  # the issuer's circuit breaker: left pending
            stamp = now_utc_iso()
            outcomes = {}
            exchange_bank_payload = None
            if exchange_bank:
                exchange_bank_payload = {
                    "custodian_bank": exchange_bank,
                    "custodian_bank_source": json.dumps(
                        {"origin": "exchange", "fetched_at": stamp},
                        ensure_ascii=False,
                    ),
                }
                outcomes["bank"] = {"status": "success", "origin": "exchange"}
            if etf_issuer not in supported:
                skipped_no_issuer += not bool(exchange_bank)
                outcomes["policy"] = {"status": "unsupported"}
                outcomes["frequency"] = {"status": "unsupported"}
                outcomes.setdefault("bank", {"status": "unsupported"})
                with write_transaction(conn):
                    if exchange_bank_payload:
                        upsert_etf_bank_and_frequency(
                            conn, etf["id"], exchange_bank_payload
                        )
                    task.record(
                        key,
                        "success" if exchange_bank else "unsupported",
                        count=int(bool(exchange_bank)),
                        fields=("bank",) if exchange_bank else (),
                        reason="尚無已驗證的投信政策來源",
                        field_results=outcomes,
                    )
                synced += bool(exchange_bank)
                continue
            try:
                data = host_pool.wait(responses[key]).result()
                if (
                    etf_issuer == "台新"
                    and etf_issuer in list_errors
                    and not data.get("distribution_policy")
                ):
                    data.setdefault("field_errors", {}).update(
                        frequency=list_errors[etf_issuer],
                        policy=list_errors[etf_issuer],
                    )
                raw = data.get("distribution_frequency_raw")
                policy = {
                    **official.policy(raw),
                    **{
                        k: data[k]
                        for k in ("distribution_policy", "frequency")
                        if data.get(k)
                    },
                }
                frequency = policy.get("frequency") or _FREQUENCY_LABEL_MAP.get(raw)
                distribution_policy = policy.get("distribution_policy") or (
                    "distributing" if frequency else None
                )
                scraped_bank = bank_dictionary.canonical_name(
                    data.get("custodian_bank")
                )
                bank = exchange_bank or scraped_bank
                url = data.get("source_url")
                source = {
                    "origin": "investment_trust",
                    "issuer": etf_issuer,
                    "fetched_at": stamp,
                    "url": url,
                    "raw": raw,
                    "policy_url": data.get("policy_source_url"),
                    "policy_period": data.get("policy_period"),
                }
                payload = {}
                if bank:
                    payload.update(
                        custodian_bank=bank,
                        custodian_bank_source=json.dumps(
                            {
                                **source,
                                "origin": "exchange"
                                if exchange_bank
                                else "investment_trust",
                                "cross_check": _custodian_cross_check_status(
                                    scraped_bank, etf["custodian_institution_raw"]
                                )
                                if scraped_bank
                                else "single_source",
                            },
                            ensure_ascii=False,
                        ),
                    )
                if frequency:
                    payload.update(
                        distribution_frequency=frequency,
                        distribution_frequency_source=json.dumps(
                            source, ensure_ascii=False
                        ),
                        distribution_month_pattern=json.dumps(
                            data["distribution_month_pattern"]
                        )
                        if data.get("distribution_month_pattern")
                        else None,
                    )
                if distribution_policy:
                    payload.update(
                        distribution_policy=distribution_policy,
                        distribution_policy_source=json.dumps(
                            {**source, "url": data.get("policy_source_url") or url},
                            ensure_ascii=False,
                        ),
                    )
                with write_transaction(conn):
                    upsert_etf_bank_and_frequency(conn, etf["id"], payload)
                    if data.get("benchmark") or data.get("listed_date"):
                        merge_basic(conn, etf["id"], data)
                    successful = []
                    for field, value in (
                        ("bank", bank),
                        ("frequency", frequency),
                        ("policy", distribution_policy),
                    ):
                        outcomes[field] = {
                            "status": "success" if value else "not_provided",
                            "url": data.get("policy_source_url") or url
                            if field != "bank"
                            else url,
                        }
                        if value:
                            successful.append(field)
                    field_errors = data.get("field_errors", {})
                    for name, exc in field_errors.items():
                        if name == "bank" and bank:
                            continue
                        status, _, reason = sync_history.error_info(exc)
                        outcomes[name] = {
                            "status": status,
                            "reason": reason,
                            "url": url,
                        }
                    errors = [
                        name
                        for name, outcome in outcomes.items()
                        if outcome["status"] not in ("success", "not_provided")
                    ]
                    # Design 048 §4.2.8: fields that only failed to connect
                    # leave the unit a transient failure, not a source change.
                    if not errors:
                        unit_status = "success"
                    elif all(
                        sync_history.is_transient_failure(outcomes[name]["status"])
                        for name in errors
                    ):
                        unit_status = "http_failed"
                    else:
                        unit_status = "parse_failed"
                    task.record(
                        key,
                        unit_status,
                        count=int(bool(successful)),
                        fields=successful,
                        url=url,
                        field_results=outcomes,
                        reason="部分欄位取得失敗" if errors else None,
                    )
                if errors:
                    failed_units.append(key)
                synced += 1
            except Exception as exc:
                # Design 055: after one maintenance page every later unit of
                # that issuer carries it too, so all read 來源維護中.
                if isinstance(exc, host_pool.Skipped):
                    exc = exc.cause
                status, _, reason = sync_history.error_info(exc)
                for field in ("bank", "frequency", "policy"):
                    outcomes.setdefault(field, {"status": status, "reason": reason})
                with write_transaction(conn):
                    if exchange_bank_payload:
                        upsert_etf_bank_and_frequency(
                            conn, etf["id"], exchange_bank_payload
                        )
                    task.record(
                        key,
                        "parse_failed",
                        fields=("bank",) if exchange_bank else (),
                        exc=exc,
                        field_results=outcomes,
                    )
                if sync_history.is_failure(status):
                    failed_units.append(key)
        # The public schedule is an independent source: its failure must not
        # roll back successful bank/policy updates, and a retry can target it.
        capital_etfs = [
            etf
            for etf in etfs
            if etf["status"] != "delisted"
            and etf["id"] not in share_pairs
            and (
                trust_adapters.guess_issuer(etf["issuer_name"] or "")
                or trust_adapters.guess_issuer(etf["name"])
            )
            == "群益"
        ]
        schedule_key = "schedule:群益"
        if capital_etfs and issuer in (None, "群益") and task.wanted(schedule_key):
            task.plan(schedule_key, issuer="群益", stage="schedule")
            try:
                schedule = sync._fetch_with_retry(
                    trust_adapters.fetch_capital_ex_dividend_schedule, client
                )
                products = schedule["products"]
                stamp = now_utc_iso()
                with write_transaction(conn):
                    for etf in capital_etfs:
                        entry = products.get(etf["code"])
                        value = (
                            json.dumps(
                                {
                                    **entry,
                                    "origin": "investment_trust",
                                    "issuer": "群益",
                                    "source_url": schedule["source_url"],
                                    "fetched_at": stamp,
                                },
                                ensure_ascii=False,
                            )
                            if entry
                            else None
                        )
                        conn.execute(
                            "UPDATE etf_profiles SET "
                            "distribution_ex_dividend_schedule=? "
                            "WHERE security_id=?",
                            (value, etf["id"]),
                        )
                    task.record(
                        schedule_key,
                        "success",
                        count=len(products),
                        fields=("ex_dividend_months",),
                        url=schedule["source_url"],
                    )
            except Exception as exc:
                task.record(schedule_key, "parse_failed", exc=exc)
                failed_units.append(schedule_key)
        # A TWD class that failed this run still holds its last valid values.
        latest = sync_history.latest_units(conn) if inheriting else {}
        for etf, key in inheriting:
            base = share_pairs[etf["id"]]
            with write_transaction(conn):
                filled = share_classes.inherit(conn, etf["id"], base)
                settled = _base_settled(conn, task.id, base["id"], latest)
                field_results = {
                    field: {
                        "status": "success" if name in filled else "not_provided",
                        "origin": "share_class",
                        "from_code": base["code"],
                    }
                    for field, name in (
                        ("bank", "custodian_bank"),
                        ("frequency", "distribution_frequency"),
                        ("policy", "distribution_policy"),
                    )
                }
                # Design 063: nothing to copy is settled only once the TWD
                # class really synced; otherwise a retry copies it later.
                if filled:
                    status, reason = "success", f"沿用主級別 {base['code']}"
                elif settled:
                    status = sync_history.NOT_LISTED
                    reason = f"主級別 {base['code']} 尚無資料"
                else:
                    status = sync_history.BASE_PENDING
                    reason = f"主級別 {base['code']} 尚未取得資料"
                task.record(
                    key,
                    status,
                    count=int(bool(filled)),
                    fields=[
                        field
                        for field, outcome in field_results.items()
                        if outcome["status"] == "success"
                    ],
                    reason=reason,
                    field_results=field_results,
                )
            synced += bool(filled)
    return {
        "synced": synced,
        "skipped_no_issuer": skipped_no_issuer,
        "failed_units": failed_units,
        "list_lookup_failures": list_lookup_failures,
        "run_id": task.id,
    }
