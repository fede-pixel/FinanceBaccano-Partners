"""Backup giornaliero del database (copia coerente tramite l'API di backup di SQLite)."""
import sqlite3
from datetime import date
from pathlib import Path

from app import config


def _db_path() -> Path:
    """Il database realmente in uso (di solito config.DB_PATH)."""
    from app.db import _engine

    if _engine is not None and _engine.url.database:
        return Path(_engine.url.database)
    return config.DB_PATH


def run_backup(force: bool = False) -> Path | None:
    db_path = _db_path()
    if not db_path.exists():
        return None
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = config.BACKUP_DIR / f"finance-{date.today().isoformat()}.db"
    if target.exists() and not force:
        return target
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    backups = sorted(config.BACKUP_DIR.glob("finance-*.db"))
    for old in backups[: -config.BACKUPS_TO_KEEP]:
        old.unlink()
    return target
