"""DBの自動/手動バックアップ。

起動時に1日1回 backups/ へ日付付きコピーを作り、keep 世代を超える古いものを消す。
SQLite の backup API を使うので WAL 中でも安全にコピーできる。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from . import db
from .config import BACKUP_DIR, DB_PATH, Config


def _make_backup(dst: Path) -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    src = db.connect(DB_PATH)
    try:
        target = sqlite3.connect(dst)
        try:
            src.backup(target)
        finally:
            target.close()
    finally:
        src.close()


def _prune(keep: int) -> None:
    backups = sorted(BACKUP_DIR.glob("library-*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass


def daily_backup(conn, cfg: Config) -> str | None:
    """1日1回だけ実行。既に今日のバックアップがあれば何もしない。返り値=作成ファイル名。"""
    if not cfg.backup_enabled:
        return None
    today = date.today().isoformat()
    last = db.get_meta(conn, "last_backup_date")
    if last == today:
        return None
    dst = BACKUP_DIR / f"library-{today}.db"
    if not dst.exists():
        _make_backup(dst)
    _prune(cfg.backup_keep)
    db.set_meta(conn, "last_backup_date", today)
    conn.commit()
    return dst.name


def manual_backup(cfg: Config) -> str:
    """手動バックアップ。時刻付きファイル名で常に新規作成する。"""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dst = BACKUP_DIR / f"library-{stamp}.db"
    _make_backup(dst)
    _prune(cfg.backup_keep)
    return dst.name
