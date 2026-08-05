"""SQLite スキーマ・接続・マイグレーション。

マイグレーション方針(README参照):
  meta テーブルの schema_version を見て、必要な ALTER/CREATE を順に適用する。
  各バージョンのステップは MIGRATIONS に追記していく。破壊的変更の前には
  起動時バックアップ(backups/)が効く。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA_VERSION = 2

# 表示・検索で使う「有効値」= 手動値があればそれ、なければ自動値
# (SQL側では COALESCE(NULLIF(x_user,''), x_auto) で解決する)

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path        TEXT UNIQUE NOT NULL,   -- ルートからの相対パス(POSIX区切り)
    filename        TEXT NOT NULL,
    folder          TEXT NOT NULL,          -- 所属フォルダ(相対, ルート直下は "")
    ext             TEXT NOT NULL,
    size            INTEGER NOT NULL DEFAULT 0,
    mtime           REAL NOT NULL DEFAULT 0,
    content_hash    TEXT,                   -- 内容ハッシュ(重複/移動検出用)

    status          TEXT NOT NULL DEFAULT 'ok',   -- ok / missing / unreadable
    is_new          INTEGER NOT NULL DEFAULT 1,    -- NEW未確認フラグ
    first_seen_at   TEXT,
    last_seen_at    TEXT,

    -- メタデータ(自動値 _auto と手動値 _user を分離保持)
    title_auto      TEXT,
    title_user      TEXT,
    journal_auto    TEXT,
    journal_user    TEXT,
    doi_auto        TEXT,
    doi_user        TEXT,
    url_user        TEXT,          -- 参照URL(手動入力, バックアップ対象)
    category_auto   TEXT,
    category_user   TEXT,          -- 手動変更したら再スキャンで上書きしない
    authors_auto    TEXT,          -- 引用生成用(Crossref由来, セミコロン区切り)
    year_auto       TEXT,

    memo1           TEXT NOT NULL DEFAULT '',
    memo2           TEXT NOT NULL DEFAULT '',

    favorite        INTEGER NOT NULL DEFAULT 0,
    read_status     TEXT NOT NULL DEFAULT '未読',   -- 未読 / 読書中 / 読了

    meta_extracted  INTEGER NOT NULL DEFAULT 0,   -- メタ抽出済みか
    crossref_done   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_files_hash   ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

-- 全文検索(日本語対応: trigram)。rowid=files.id。
-- 標準FTS5(独立インデックス)。手動で reindex_fts により同期する。
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    title, journal, memo1, memo2, filename,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT,
    finished_at   TEXT,
    added         INTEGER DEFAULT 0,
    missing       INTEGER DEFAULT 0,
    total         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 将来のスキーマ変更はここに (from_version, sql) を追記する
MIGRATIONS: list[tuple[int, str]] = [
    # v1 -> v2: 参照URL列を追加
    (1, "ALTER TABLE files ADD COLUMN url_user TEXT;"),
]


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 複数コネクション(スキャン/抽出/編集)の同時書き込みでロック待ちする
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(BASE_SCHEMA)
    current = get_meta(conn, "schema_version")
    if current is None:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    else:
        cur_v = int(current)
        for from_v, sql in MIGRATIONS:
            if from_v >= cur_v:
                conn.executescript(sql)
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


# --- 有効値の解決に使う SQL 断片 -----------------------------------------
EFF_TITLE = "COALESCE(NULLIF(title_user,''), title_auto)"
EFF_JOURNAL = "COALESCE(NULLIF(journal_user,''), journal_auto)"
EFF_DOI = "COALESCE(NULLIF(doi_user,''), doi_auto)"
EFF_CATEGORY = "COALESCE(NULLIF(category_user,''), category_auto)"


def reindex_fts(conn: sqlite3.Connection, file_id: int) -> None:
    """1ファイル分の FTS 行を作り直す(有効タイトル+雑誌+メモ+ファイル名)。"""
    row = conn.execute(
        f"SELECT {EFF_TITLE} AS title, {EFF_JOURNAL} AS journal, "
        f"memo1, memo2, filename FROM files WHERE id=?",
        (file_id,),
    ).fetchone()
    conn.execute("DELETE FROM files_fts WHERE rowid=?", (file_id,))
    if row is not None:
        conn.execute(
            "INSERT INTO files_fts(rowid, title, journal, memo1, memo2, filename) "
            "VALUES(?,?,?,?,?,?)",
            (
                file_id,
                row["title"] or "",
                row["journal"] or "",
                row["memo1"] or "",
                row["memo2"] or "",
                row["filename"] or "",
            ),
        )
