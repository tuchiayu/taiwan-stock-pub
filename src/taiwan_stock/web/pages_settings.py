"""Settings, database maintenance, and backup form pages."""

import sqlite3
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from taiwan_stock.infrastructure import backup
from taiwan_stock.infrastructure.db import read_transaction
from taiwan_stock.portfolio import settings
from taiwan_stock.portfolio.trading_rules import (
    exemption_summary,
    historical_tax_differences,
)
from taiwan_stock.shared.dates import today_taipei
from taiwan_stock.web.deps import get_data_dir, get_db
from taiwan_stock.web.templates import templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    created: str | None = None,
    restored: str | None = None,
    uploaded: str | None = None,
    settings_error: str | None = None,
    vacuumed: str | None = None,
    deleted: str | None = None,
    vacuum_error: str | None = None,
    data_dir: Path = Depends(get_data_dir),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            **settings_context(conn, data_dir),
            "created": created,
            "restored": restored,
            "restore_summary": restore_summary(conn) if restored else None,
            "uploaded": uploaded,
            "settings_error": settings_error,
            "vacuumed": vacuumed,
            "deleted": deleted,
            "vacuum_error": vacuum_error,
            "saved": request.query_params.get("saved"),
        },
    )


def settings_context(conn, data_dir):
    with read_transaction(conn):
        preferences = settings.read_settings(conn)
        displayed = {
            k: str(
                Decimal(v)
                * (
                    10
                    if k == "commission_discount"
                    else 1
                    if k == "min_commission"
                    else 100
                )
            )
            for k, v in preferences.items()
            if k != "quantity_display"
        }
        return {
            "tax_differences": historical_tax_differences(conn),
            "tax_exemptions": exemption_summary(today_taipei()),
            "data_dir": str(data_dir),
            "preferences": preferences,
            "display_preferences": displayed,
            "accounts": [
                dict(r)
                for r in conn.execute("SELECT * FROM broker_accounts ORDER BY name")
            ],
            "backups": backup.list_backups(data_dir),
            "db_size_bytes": backup.database_size_bytes(data_dir),
            "created": None,
            "restored": None,
            "restore_summary": None,
            "uploaded": None,
            "settings_error": None,
            "vacuumed": None,
            "deleted": None,
            "vacuum_error": None,
            "saved": None,
            "field_errors": {},
            "account_values": {},
            "editing_account": None,
            "preferences_failed": False,
        }


# Design 080 §4.2.3: what a restore brought back that CSV would not.
_SUMMARY_QUERIES = (
    ("交易", "筆", "SELECT COUNT(*) FROM trades WHERE voided_at IS NULL"),
    ("券商帳戶", "個", "SELECT COUNT(*) FROM broker_accounts"),
    ("自選", "檔", "SELECT COUNT(*) FROM watchlist"),
    ("個別持股領息銀行", "筆", "SELECT COUNT(*) FROM dividend_bank_preferences"),
    (
        "ETF 手動覆寫",
        "檔",
        "SELECT COUNT(*) FROM etf_profiles WHERE custodian_bank_user_override "
        "IS NOT NULL OR distribution_frequency_user_override IS NOT NULL",
    ),
    (
        "公司行動確認",
        "筆",
        "SELECT COUNT(*) FROM (SELECT DISTINCT action_id, broker_account_id "
        "FROM corporate_action_reviews)",
    ),
)


def restore_summary(conn) -> str:
    with read_transaction(conn):
        parts = [
            f"{label} {conn.execute(sql).fetchone()[0]:,} {unit}"
            for label, unit, sql in _SUMMARY_QUERIES
        ]
    return "目前資料：" + "、".join(parts) + "。"


@router.post("/settings/vacuum")
def vacuum_form(data_dir: Path = Depends(get_data_dir)) -> RedirectResponse:
    try:
        result = backup.vacuum(data_dir)
    except backup.BackupError as exc:
        return RedirectResponse(
            url=f"/settings?vacuum_error={quote(str(exc))}#maintenance",
            status_code=303,
        )
    reclaimed_kb = Decimal(result["reclaimed_bytes"]) / 1024
    return RedirectResponse(
        url=f"/settings?vacuumed={quote(f'{reclaimed_kb:.1f}')}#maintenance",
        status_code=303,
    )


@router.post("/settings/backups")
def create_backup_form(data_dir: Path = Depends(get_data_dir)) -> RedirectResponse:
    path = backup.create_backup(data_dir)
    return RedirectResponse(
        url=f"/settings?created={quote(path.name)}#backups", status_code=303
    )


@router.get("/settings/backups/download")
def download_backup(filename: str, data_dir: Path = Depends(get_data_dir)):
    try:
        path = backup.backup_path(data_dir, filename)
    except backup.BackupError as exc:
        return RedirectResponse(
            url=f"/settings?settings_error={quote(str(exc))}#backups", status_code=303
        )
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/settings/backups/upload")
def upload_backup_form(
    file: UploadFile, data_dir: Path = Depends(get_data_dir)
) -> RedirectResponse:
    # Sync def like the CSV import: the copy and SQLite checks are blocking.
    try:
        path = backup.import_backup(data_dir, file.file, file.filename)
    except backup.BackupError as exc:
        return RedirectResponse(
            url=f"/settings?settings_error={quote(f'上傳失敗：{exc}')}#backups",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/settings?uploaded={quote(path.name)}#backups", status_code=303
    )


@router.post("/settings/backups/restore")
def restore_backup_form(
    filename: str = Form(...), data_dir: Path = Depends(get_data_dir)
) -> RedirectResponse:
    try:
        backup.restore_backup(data_dir, filename)
    except backup.BackupError as exc:
        return RedirectResponse(
            url=f"/settings?settings_error={quote(str(exc))}#backups", status_code=303
        )
    return RedirectResponse(
        url=f"/settings?restored={quote(filename)}#backups", status_code=303
    )


@router.post("/settings/backups/delete")
def delete_backup_form(
    filename: str = Form(...), data_dir: Path = Depends(get_data_dir)
) -> RedirectResponse:
    try:
        backup.delete_backup(data_dir, filename)
    except backup.BackupError as exc:
        return RedirectResponse(
            url=f"/settings?settings_error={quote(str(exc))}#backups", status_code=303
        )
    return RedirectResponse(
        url=f"/settings?deleted={quote(filename)}#backups", status_code=303
    )
