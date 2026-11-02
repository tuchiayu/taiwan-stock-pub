"""`/api/v1/backups` — create/list/restore (design 002 §4.13)."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from taiwan_stock.infrastructure import backup
from taiwan_stock.web.deps import get_data_dir

router = APIRouter(prefix="/api/v1")


class RestoreIn(BaseModel):
    filename: str


@router.post("/backups", status_code=201)
def create_backup(data_dir: Path = Depends(get_data_dir)) -> dict:
    path = backup.create_backup(data_dir)
    return {"filename": path.name}


@router.get("/backups")
def list_backups(data_dir: Path = Depends(get_data_dir)) -> dict:
    return {"items": backup.list_backups(data_dir)}


@router.post("/backups/restore")
def restore_backup(body: RestoreIn, data_dir: Path = Depends(get_data_dir)) -> dict:
    try:
        backup.restore_backup(data_dir, body.filename)
    except backup.BackupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"restored": body.filename}


@router.get("/database-size")
def get_database_size(data_dir: Path = Depends(get_data_dir)) -> dict:
    return {"size_bytes": backup.database_size_bytes(data_dir)}


@router.post("/vacuum")
def run_vacuum(data_dir: Path = Depends(get_data_dir)) -> dict:
    try:
        return backup.vacuum(data_dir)
    except backup.BackupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
