"""Official issuer/index enrichment, separate from policy/bank source results."""

import json
from collections import Counter

from taiwan_stock.catalog import host_pool, profile_values, sync_history
from taiwan_stock.catalog import official_profile_adapters as adapters
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared.dates import now_utc_iso, today_taipei


def coverage(conn):
    counts = Counter()
    for row in conn.execute(
        "SELECT p.* FROM etf_profiles p JOIN securities s ON s.id=p.security_id "
        "WHERE s.security_type='etf' AND s.status='active'"
    ):
        profile = dict(row)
        counts["total"] += 1
        counts["bank_known"] += bool(profile_values.custodian_bank(profile)["value"])
        value = profile_values.distribution_frequency(profile)
        if value["tier"] == "user_override":
            counts["frequency_user_override"] += 1
        elif value["value"] == "non_distributing":
            counts["non_distributing"] += 1
        else:
            # An inherited value is the fund's own official value (design 043).
            tier = "official" if value["tier"] == "share_class" else value["tier"]
            counts["frequency_" + tier] += 1
    return {
        key: counts[key]
        for key in (
            "total",
            "bank_known",
            "frequency_official",
            "frequency_inferred",
            "frequency_user_override",
            "frequency_unknown",
            "non_distributing",
        )
    }


def merge_basic(conn, security_id, data):
    row = conn.execute(
        "SELECT * FROM etf_profiles WHERE security_id=?", (security_id,)
    ).fetchone()
    if row is None:
        raise ValueError("ETF 缺少基本資料列")
    sources = profile_values.json_value(row["basic_info_source"], {}) or {}
    period = data.get("data_period", today_taipei()[:7].replace("-", ""))
    source = {
        "url": data["source_url"],
        "fetched_at": now_utc_iso(),
        "data_period": period,
    }
    if data.get("management_type"):
        sources["management_type"] = data["management_type"]

    def rank(item):
        url = item.get("url", "")
        priority = 1 if "sitca.org.tw" in url else 2 if "tpex.org.tw" in url else 3
        return item.get("data_period", ""), priority

    assignments, values, written = [], [], []
    for key in ("issuer_name", "benchmark"):
        value = data.get(key)
        old = sources.get(key, {})
        # A legacy value has no field-level date. A monthly association report
        # cannot establish that it is newer; preserve that value conservatively.
        if row[key] and not old and "sitca.org.tw" in source["url"]:
            continue
        if value and (not row[key] or rank(source) >= rank(old)):
            assignments.append(key + "=?")
            values.append(value)
            sources[key] = source
            written.append(key)
    if data.get("listed_date"):
        exists = conn.execute(
            "SELECT listed_date FROM securities WHERE id=?", (security_id,)
        ).fetchone()[0]
        if not exists:
            conn.execute(
                "UPDATE securities SET listed_date=? WHERE id=?",
                (data["listed_date"], security_id),
            )
            sources["listed_date"] = source
            written.append("listed_date")
    assignments.append("basic_info_source=?")
    values.append(json.dumps(sources, ensure_ascii=False))
    conn.execute(
        "UPDATE etf_profiles SET " + ",".join(assignments) + " WHERE security_id=?",
        (*values, security_id),
    )
    return written


def merge_sitca_policy(conn, security_id, data):
    """Fill missing ETF policy from the dated association report.

    An issuer's product page is more direct and often newer. Association
    rows may add a missing field, but cannot replace a conflicting direct
    value or an association value from a later report period.
    """
    from taiwan_stock.catalog.sync_investment_trust import (
        upsert_etf_bank_and_frequency,
    )

    policy = data.get("distribution_policy")
    frequency = data.get("frequency")
    if not policy:
        return [], []
    row = conn.execute(
        "SELECT * FROM etf_profiles WHERE security_id=?", (security_id,)
    ).fetchone()
    if row is None:
        raise ValueError("ETF 缺少基本資料列")
    sources = {
        field: profile_values.json_value(row[field + "_source"], {}) or {}
        for field in ("distribution_policy", "distribution_frequency")
    }
    period = data["data_period"]

    def protected(field):
        current = row[field]
        source = sources[field]
        return bool(current) and (
            not isinstance(source, dict)
            or source.get("origin") != "sitca"
            or source.get("data_period", "") > period
        )

    conflicts = []
    if (
        row["distribution_policy"]
        and row["distribution_policy"] != policy
        and protected("distribution_policy")
    ):
        conflicts.append("policy")
    if frequency and (
        row["distribution_frequency"]
        and row["distribution_frequency"] != frequency
        and protected("distribution_frequency")
    ):
        conflicts.append("frequency")
    if (
        policy == "non_distributing"
        and row["distribution_frequency"]
        and protected("distribution_frequency")
    ):
        conflicts.append("frequency")
    if (
        policy == "distributing"
        and row["distribution_policy"] == "non_distributing"
        and protected("distribution_policy")
    ):
        conflicts.append("policy")
    if conflicts:
        return [], sorted(set(conflicts))

    source = json.dumps(
        {
            "origin": "sitca",
            "url": data["source_url"],
            "fetched_at": now_utc_iso(),
            "data_period": period,
            "raw": data.get("distribution_frequency_raw"),
            "class": data.get("distribution_class_raw"),
        },
        ensure_ascii=False,
    )
    payload = {}
    written = []
    if not protected("distribution_policy") and row["distribution_policy"] != policy:
        payload.update(distribution_policy=policy, distribution_policy_source=source)
        written.append("distribution_policy")
    if (
        frequency
        and not protected("distribution_frequency")
        and row["distribution_frequency"] != frequency
    ):
        payload.update(
            distribution_frequency=frequency, distribution_frequency_source=source
        )
        written.append("distribution_frequency")
    if payload:
        upsert_etf_bank_and_frequency(conn, security_id, payload)
    return written, []


def sync_basic_info(conn, client, *, issuer=None, retry_run_id=None, sleep=None):
    return host_pool.run(
        basic_info_steps(
            conn, client, issuer=issuer, retry_run_id=retry_run_id, sleep=sleep
        )
    )


def basic_info_steps(conn, client, *, issuer=None, retry_run_id=None, sleep=None):
    """`host_pool` steps (design 053): the SITCA list and each TPEx product
    page are queued on their hosts, then written in the old order."""
    from taiwan_stock.catalog.investment_trust_adapters import guess_issuer

    rows = conn.execute(
        "SELECT s.id,s.code,s.name,s.market,p.issuer_name FROM securities s "
        "JOIN etf_profiles p ON p.security_id=s.id WHERE s.security_type='etf' "
        "AND s.status='active' ORDER BY s.market,s.code"
    ).fetchall()
    if issuer:
        rows = [
            r
            for r in rows
            if (guess_issuer(r["issuer_name"] or "") or guess_issuer(r["name"]))
            == issuer
        ]
    synced, failed, conflicts = 0, [], []
    with sync_history.run(conn, "etf_profile_basic", retry_run_id) as task:
        pool = host_pool.current.get()
        sitca = None
        if task.wanted("sitca"):
            task.plan("sitca", stage="list")
            sitca = pool.submit(
                "sitca", lambda: adapters.fetch_sitca_profiles(client), delay=0
            )
        pages = {}
        for row in rows:
            if row["market"] != "TPEx":
                continue
            key = "TPEx:" + row["code"]
            if not task.wanted(key):
                continue
            task.plan(key, security_id=row["id"], stage="basic")
            pages[key] = pool.submit(
                "TPEx",
                lambda code=row["code"]: adapters.fetch_tpex_profile(client, code),
                delay=1,
                pause=sleep,
                breaker=("etf-basic", 5, lambda exc: True),
            )
        yield
        # Catalog identity is independent of per-product policy; failures do not
        # prevent the TPEx source or the subsequent issuer step from running.
        if sitca is not None:
            try:
                data = host_pool.wait(sitca).result()
                count = 0
                policy_fields = set()
                with write_transaction(conn):
                    for row in rows:
                        if row["code"] in data:
                            merge_basic(conn, row["id"], data[row["code"]])
                            written, disagreements = merge_sitca_policy(
                                conn, row["id"], data[row["code"]]
                            )
                            policy_fields.update(written)
                            conflicts.extend(
                                f"{row['code']}:{field}" for field in disagreements
                            )
                            count += 1
                    task.record(
                        "sitca",
                        "success",
                        count=count,
                        fields=("issuer_name", "benchmark", *sorted(policy_fields)),
                        url=adapters.SITCA_LIST,
                    )
                synced += count
            except Exception as exc:
                task.record("sitca", "parse_failed", exc=exc)
                failed.append("sitca")
        for row in rows:
            key = "TPEx:" + row["code"]
            if key not in pages:
                continue
            try:
                data = host_pool.wait(pages[key]).result()
                with write_transaction(conn):
                    fields = merge_basic(conn, row["id"], data)
                    task.record(
                        key, "success", count=1, fields=fields, url=data["source_url"]
                    )
                synced += 1
            except host_pool.Skipped:
                continue  # five failures in a row: the rest stay pending
            except Exception as exc:
                task.record(key, "parse_failed", exc=exc)
                failed.append(key)
    return {
        "synced": synced,
        "failed_units": failed,
        "validation_errors": conflicts,
        "run_id": task.id,
    }
