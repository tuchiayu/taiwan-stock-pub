"""Integration tests for standard CSV trade import/export (design 002 §4.12.1)."""

from decimal import Decimal

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import csv_trades, trades

HEADER = (
    "broker_account_name,security_code,market,event_type,trade_date,"
    "quantity,unit_price,executed_amount,commission,transaction_tax,"
    "other_fees,note\n"
)


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    connection.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2330', 'TWSE', '2330', 'TSMC', 'stock', 'active')"
    )
    connection.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acct-1', '測試券商', 'active')"
    )
    yield connection
    connection.close()


def test_parse_csv_rejects_missing_required_column():
    with pytest.raises(csv_trades.ImportError_, match="缺少必要欄位"):
        csv_trades.parse_csv("security_code,market\n2330,TWSE\n")


def test_import_creates_trades_in_order(conn):
    content = (
        HEADER
        + "測試券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"
        + "測試券商,2330,TWSE,sell,2024-02-01,400,60,,,,,\n"
    )
    rows = csv_trades.parse_csv(content)

    created = csv_trades.import_trades(conn, rows)

    assert created == 2
    position = trades.compute_position(
        conn, "acct-1", "sec-2330", as_of_date="2024-06-01"
    )
    assert str(position.remaining_quantity) == "600"


def test_import_a_sell_depending_on_an_earlier_row_in_the_same_batch(conn):
    """The sell only becomes valid because of the buy two rows above it
    in the same file — this only works if rows are validated against
    the DB *as previous rows in this same import are applied*, not
    just against what existed before the import started.
    """
    content = (
        HEADER
        + "測試券商,2330,TWSE,buy,2024-01-01,500,50,,,,,\n"
        + "測試券商,2330,TWSE,sell,2024-02-01,500,60,,,,,\n"
    )
    rows = csv_trades.parse_csv(content)

    created = csv_trades.import_trades(conn, rows)

    assert created == 2


def test_import_is_all_or_nothing_on_unknown_broker(conn):
    content = (
        HEADER
        + "測試券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"
        + "不存在的券商,2330,TWSE,buy,2024-02-01,100,50,,,,,\n"
    )
    rows = csv_trades.parse_csv(content)

    with pytest.raises(csv_trades.ImportError_, match="找不到券商帳戶"):
        csv_trades.import_trades(conn, rows)

    count = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    assert count == 0


def test_import_is_all_or_nothing_on_unknown_security(conn):
    content = HEADER + "測試券商,9999,TWSE,buy,2024-01-01,1000,50,,,,,\n"
    rows = csv_trades.parse_csv(content)

    with pytest.raises(csv_trades.ImportError_, match="找不到商品"):
        csv_trades.import_trades(conn, rows)

    assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 0


def test_import_is_all_or_nothing_on_oversell(conn):
    content = (
        HEADER
        + "測試券商,2330,TWSE,buy,2024-01-01,100,50,,,,,\n"
        + "測試券商,2330,TWSE,sell,2024-02-01,200,60,,,,,\n"
    )
    rows = csv_trades.parse_csv(content)

    with pytest.raises(csv_trades.ImportError_) as excinfo:
        csv_trades.import_trades(conn, rows)
    assert excinfo.value.row_number == 3

    assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 0


def test_import_rejects_invalid_quantity(conn):
    content = HEADER + "測試券商,2330,TWSE,buy,2024-01-01,abc,50,,,,,\n"
    rows = csv_trades.parse_csv(content)

    with pytest.raises(csv_trades.ImportError_, match="不是整數"):
        csv_trades.import_trades(conn, rows)


def test_import_rejects_invalid_event_type(conn):
    content = HEADER + "測試券商,2330,TWSE,transfer,2024-01-01,100,50,,,,,\n"
    rows = csv_trades.parse_csv(content)

    with pytest.raises(csv_trades.ImportError_, match="買賣類型"):
        csv_trades.import_trades(conn, rows)


def test_export_then_reimport_round_trips(conn):
    trades.create_trade(
        conn,
        {
            "broker_account_id": "acct-1",
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
            "commission": "50",
        },
    )

    exported = csv_trades.export_trades_csv(conn)

    # Wipe and reimport from the export to prove the two column shapes
    # actually match, instead of standing up a second temp DB.
    conn.execute("DELETE FROM trades")
    rows = csv_trades.parse_csv(exported)
    created = csv_trades.import_trades(conn, rows)

    assert created == 1
    position = trades.compute_position(
        conn, "acct-1", "sec-2330", as_of_date="2024-06-01"
    )
    assert str(position.remaining_quantity) == "1000"
    assert position.remaining_cost == Decimal("50050")


def test_export_starts_with_bom_and_reimports_through_upload_unchanged(conn):
    import io

    trades.create_trade(
        conn,
        {
            "broker_account_id": "acct-1",
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    exported = csv_trades.export_trades_csv(conn)
    assert exported.startswith("﻿broker_account_name,")
    assert exported.encode("utf-8").startswith(b"\xef\xbb\xbf")

    decoded = csv_trades.read_upload_text(io.BytesIO(exported.encode("utf-8")))
    # With or without the BOM surviving decoding, the export is unchanged.
    for content in (decoded, exported):
        plan = csv_trades.preview_import(conn, content)
        assert plan.summary == {
            "created": 0,
            "updated": 0,
            "unchanged": 1,
            "duplicates": 0,
        }
        assert not plan.has_writes


def _add_security(conn, code):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', ?, ?, 'etf', 'active')",
        (f"sec-{code}", code, code),
    )


def test_import_restores_leading_zeros_excel_dropped(conn):
    _add_security(conn, "00752")
    _add_security(conn, "0050")
    content = (
        HEADER
        + "測試券商,752,TWSE,buy,2024-01-01,1000,20,,,,,\n"
        + "測試券商,50,TWSE,buy,2024-01-01,1000,100,,,,,\n"
        + "測試券商,2330,TWSE,buy,2024-01-01,1000,500,,,,,\n"
    )

    assert csv_trades.import_trades(conn, csv_trades.parse_csv(content)) == 3
    assert {
        r["security_id"] for r in conn.execute("SELECT security_id FROM trades")
    } == {"sec-00752", "sec-0050", "sec-2330"}


def test_import_rejects_ambiguous_zero_stripped_code(conn):
    _add_security(conn, "00878")
    _add_security(conn, "000878")
    content = HEADER + "測試券商,878,TWSE,buy,2024-01-01,1000,20,,,,,\n"

    with pytest.raises(csv_trades.ImportError_, match="000878、00878") as excinfo:
        csv_trades.import_trades(conn, csv_trades.parse_csv(content))
    assert excinfo.value.row_number == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 0


def test_import_prefers_exact_code_and_never_strips_a_present_zero(conn):
    _add_security(conn, "006208")
    _add_security(conn, "6208")
    _add_security(conn, "00752")
    exact = HEADER + "測試券商,6208,TWSE,buy,2024-01-01,1000,20,,,,,\n"
    assert csv_trades.import_trades(conn, csv_trades.parse_csv(exact)) == 1
    assert (
        conn.execute("SELECT security_id FROM trades").fetchone()["security_id"]
        == "sec-6208"
    )

    # `0752` kept a leading zero, so it is taken literally, not as `00752`.
    literal = HEADER + "測試券商,0752,TWSE,buy,2024-01-01,1000,20,,,,,\n"
    with pytest.raises(csv_trades.ImportError_, match="找不到商品「TWSE 0752」"):
        csv_trades.import_trades(conn, csv_trades.parse_csv(literal))


def test_import_accepts_excel_resaved_unpadded_dates(conn):
    content = (
        HEADER
        + "測試券商,2330,TWSE,buy,2024-1-2,1000,50,,,,,\n"
        + "測試券商,2330,TWSE,buy,2024/3/4,1000,50,,,,,\n"
        + "測試券商,2330,TWSE,buy,2024/11/25,1000,50,,,,,\n"
    )

    assert csv_trades.import_trades(conn, csv_trades.parse_csv(content)) == 3
    assert [
        r["trade_date"]
        for r in conn.execute("SELECT trade_date FROM trades ORDER BY trade_date")
    ] == ["2024-01-02", "2024-03-04", "2024-11-25"]


@pytest.mark.parametrize("value", ["2024/2/30", "3/4/2024", "2024.3.4", "20240304"])
def test_import_still_rejects_impossible_or_ambiguous_dates(conn, value):
    content = HEADER + f"測試券商,2330,TWSE,buy,{value},1000,50,,,,,\n"

    with pytest.raises(csv_trades.ImportError_, match="trade_date") as excinfo:
        csv_trades.import_trades(conn, csv_trades.parse_csv(content))
    assert excinfo.value.row_number == 2


def test_import_day_trade_column_discounts_sell_tax(conn):
    content = (
        "broker_account_name,security_code,market,event_type,trade_date,"
        "quantity,unit_price,executed_amount,commission,transaction_tax,"
        "other_fees,is_day_trade,note\n"
        "測試券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,,\n"
        "測試券商,2330,TWSE,sell,2024-01-01,1000,50,,,,,1,\n"
    )
    rows = csv_trades.parse_csv(content)

    created = csv_trades.import_trades(conn, rows)

    assert created == 2
    sell_row = conn.execute(
        "SELECT transaction_tax, is_day_trade FROM trades WHERE event_type = 'sell'"
    ).fetchone()
    # floor(50000*0.0015) = 75, half the normal 0.3% stock rate.
    assert sell_row["transaction_tax"] == "75"
    assert sell_row["is_day_trade"] == 1


def test_read_upload_text_supports_cp950_and_rejects_oversize_or_binary(conn):
    import io

    cp950_bytes = (
        HEADER + "測試券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,台積電備註\n"
    ).encode("cp950")
    decoded = csv_trades.read_upload_text(io.BytesIO(cp950_bytes))
    assert csv_trades.import_trades(conn, csv_trades.parse_csv(decoded)) == 1

    with pytest.raises(csv_trades.ImportError_, match="超過大小上限"):
        csv_trades.read_upload_text(
            io.BytesIO(b"a" * (csv_trades.MAX_UPLOAD_BYTES + 1))
        )

    # 0x80 followed by ASCII is invalid in both UTF-8 and CP950.
    with pytest.raises(csv_trades.ImportError_, match="無法以 UTF-8 或 Big5 解碼"):
        csv_trades.read_upload_text(io.BytesIO(b"\x80\x00\xff"))

    with pytest.raises(csv_trades.ImportError_, match="二進位字元"):
        csv_trades.parse_csv(
            HEADER + "測試券商\x00,2330,TWSE,buy,2024-01-01,100,50,,,,,\n"
        )


def _buy(conn, **changes):
    return trades.create_trade(
        conn,
        {
            "broker_account_id": "acct-1",
            "security_id": "sec-2330",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
            **changes,
        },
    )


def _edit_export(conn, edit):
    """Export, let `edit(rows)` change the dict rows, and re-serialize."""
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(csv_trades.export_trades_csv(conn))))
    rows = edit(rows) or rows
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def test_edited_export_updates_only_changed_trades_in_place(conn):
    first = _buy(conn)
    _buy(conn, trade_date="2024-02-01")
    before = conn.execute("SELECT * FROM trades WHERE id = ?", (first,)).fetchone()

    def edit(rows):
        rows[0]["unit_price"] = "51"
        rows[0]["executed_amount"] = ""
        rows[0]["note"] = "修正單價"
        # Excel's rewrite of an equal amount is not a change.
        rows[1]["executed_amount"] = "50000.00"

    content = _edit_export(conn, edit)
    plan = csv_trades.preview_import(conn, content)

    assert plan.summary == {"created": 0, "updated": 1, "unchanged": 1, "duplicates": 0}
    changed = plan.rows[0]
    assert changed.trade_id == first
    assert changed.changes == {
        "unit_price": ("50", "51"),
        "executed_amount": ("50000", "51000"),
        "note": (None, "修正單價"),
    }
    # Previewing writes nothing.
    assert (
        conn.execute("SELECT unit_price FROM trades WHERE id = ?", (first,)).fetchone()[
            0
        ]
        == "50"
    )

    result = csv_trades.confirm_import(conn, content, plan.digest, ())

    assert (result.created, result.updated) == (0, 1)
    after = conn.execute("SELECT * FROM trades WHERE id = ?", (first,)).fetchone()
    assert (after["unit_price"], after["executed_amount"], after["note"]) == (
        "51",
        "51000",
        "修正單價",
    )
    assert after["created_at"] == before["created_at"]
    assert after["trade_order"] == before["trade_order"]
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


def test_rows_identical_to_stored_trades_are_duplicates_matched_by_count(conn):
    _buy(conn, commission="71")
    content = HEADER + "測試券商,2330,TWSE,buy,2024-01-01,1000,50,,71,,,\n" * 2

    plan = csv_trades.preview_import(conn, content)

    assert [r.status for r in plan.rows] == ["duplicate", "create"]
    assert csv_trades.confirm_import(conn, content, plan.digest, ()).created == 1
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


def test_opted_in_duplicate_rows_are_created(conn):
    _buy(conn)
    content = HEADER + "測試券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"
    plan = csv_trades.preview_import(conn, content)

    with pytest.raises(csv_trades.ImportError_, match="只能勾選疑似重複的列"):
        csv_trades.confirm_import(conn, content, plan.digest, [3])
    assert csv_trades.confirm_import(conn, content, plan.digest, ["2"]).created == 1
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda rows: rows + [dict(rows[0])], "與第 2 列重複"),
        (lambda rows: rows[0].update(security_code="0050"), "不能透過 CSV 變更"),
    ],
)
def test_invalid_trade_id_rows_are_rejected(conn, edit, message):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-0050', 'TWSE', '0050', '0050', 'etf', 'active')"
    )
    _buy(conn)
    content = _edit_export(conn, edit)

    with pytest.raises(csv_trades.ImportError_, match=message):
        csv_trades.preview_import(conn, content)


def test_batch_validates_oversell_on_the_final_state_only(conn):
    _buy(conn)
    trades.create_trade(
        conn,
        {
            "broker_account_id": "acct-1",
            "security_id": "sec-2330",
            "event_type": "sell",
            "trade_date": "2024-02-01",
            "quantity": 1000,
            "unit_price": "60",
        },
    )

    def lower_both(rows):
        # Per-row replay would reject the lowered buy before the sell.
        rows[0]["quantity"] = rows[1]["quantity"] = "400"
        for row in rows:
            row["executed_amount"] = row["commission"] = row["transaction_tax"] = ""

    content = _edit_export(conn, lower_both)
    plan = csv_trades.preview_import(conn, content)
    assert csv_trades.confirm_import(conn, content, plan.digest, ()).updated == 2
    assert (
        str(
            trades.compute_position(
                conn, "acct-1", "sec-2330", "2024-06-01"
            ).remaining_quantity
        )
        == "0"
    )

    def lower_buy(rows):
        rows[0]["quantity"] = "100"
        rows[0]["executed_amount"] = rows[0]["commission"] = ""

    with pytest.raises(csv_trades.ImportError_, match="受本次匯入影響的列：2"):
        csv_trades.preview_import(conn, _edit_export(conn, lower_buy))


def test_confirm_rejects_a_preview_made_stale_by_later_writes(conn):
    content = HEADER + "測試券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,\n"
    plan = csv_trades.preview_import(conn, content)
    _buy(conn)

    with pytest.raises(csv_trades.StalePreviewError):
        csv_trades.confirm_import(conn, content, plan.digest, ())
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_blank_fee_on_an_update_falls_back_to_the_default(conn):
    trade_id = _buy(conn, commission="10")

    def clear_commission(rows):
        rows[0]["commission"] = ""

    plan = csv_trades.preview_import(conn, _edit_export(conn, clear_commission))

    assert plan.rows[0].trade_id == trade_id
    assert plan.rows[0].changes == {"commission": ("10", "71")}


def test_export_imports_into_another_database_without_editing(conn, tmp_path):
    """Another database knows neither the trade IDs nor the account ID;
    the account is matched by name and the trades keep their IDs, so a
    second import of the same export changes nothing."""
    first = _buy(conn, commission="71")
    _buy(conn, trade_date="2024-02-01", note="二月")
    exported = csv_trades.export_trades_csv(conn)

    other = get_connection(tmp_path / "other")
    migrate.apply_pending(other)
    other.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2330', 'TWSE', '2330', 'TSMC', 'stock', 'active')"
    )
    other.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acct-other', '測試券商', 'active')"
    )
    try:
        plan = csv_trades.preview_import(other, exported)
        assert plan.summary == {
            "created": 2,
            "updated": 0,
            "unchanged": 0,
            "duplicates": 0,
        }
        csv_trades.confirm_import(other, exported, plan.digest, ())

        stored = other.execute(
            "SELECT id, broker_account_id FROM trades ORDER BY trade_date"
        ).fetchall()
        assert stored[0]["id"] == first
        assert {r["broker_account_id"] for r in stored} == {"acct-other"}
        assert not csv_trades.preview_import(other, exported).has_writes
    finally:
        other.close()


def test_unknown_malformed_trade_id_gets_a_fresh_id(conn):
    content = (
        HEADER.rstrip("\n") + ",trade_id\n"
        "測試券商,2330,TWSE,buy,2024-01-01,1000,50,,,,,,1.23E+31\n"
    )

    plan = csv_trades.preview_import(conn, content)
    assert [r.status for r in plan.rows] == ["create"]
    csv_trades.confirm_import(conn, content, plan.digest, ())

    stored_id = conn.execute("SELECT id FROM trades").fetchone()[0]
    assert len(stored_id) == 32 and stored_id != "1.23E+31"
