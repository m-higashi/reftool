"""フォルダスキャン: 追加/欠落/移動の検出とハッシュ計算。

再スキャンは冪等。手動修正値(_user)・メモ・お気に入り・読書ステータスは保持する。
ハッシュは size+mtime が変わらないファイルでは再計算せず既存値を使う(高速化)。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from pathlib import Path

from . import db
from .config import EXCLUDE_DIR_NAMES, ROOT_DIR, Config, to_nfc

_HASH_CHUNK = 1 << 20  # 1MB


class ScanState:
    """進捗を保持する共有オブジェクト(APIから参照)。"""

    def __init__(self) -> None:
        self.running = False
        self.phase = "idle"          # idle / scanning / hashing / finalizing / done / error
        self.processed = 0
        self.total = 0
        self.added = 0
        self.missing = 0
        self.moved_candidates = 0
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.error: str | None = None
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "phase": self.phase,
                "processed": self.processed,
                "total": self.total,
                "added": self.added,
                "missing": self.missing,
                "moved_candidates": self.moved_candidates,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
            }


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _iter_target_files(cfg: Config):
    exts = {"." + e for e in cfg.extensions}
    for p in ROOT_DIR.rglob("*"):
        # 除外ディレクトリ(_reftool 等)配下はスキップ
        if any(part in EXCLUDE_DIR_NAMES for part in p.relative_to(ROOT_DIR).parts):
            continue
        if p.is_file() and p.suffix.lower() in exts:
            yield p


def _rel(path: Path) -> str:
    # NFC統一: macOSはファイル名をNFDで返すため、正規化しないとWindowsで作ったDBと
    # 突き合わせたときに全件が別ファイル扱いになる(to_nfc のdocstring参照)
    return to_nfc(path.relative_to(ROOT_DIR).as_posix())


def scan(conn, cfg: Config, state: ScanState) -> None:
    """全走査を実行して DB を更新する。例外は state.error に格納。"""
    now = datetime.now().isoformat(timespec="seconds")
    with state.lock:
        state.running = True
        state.phase = "scanning"
        state.processed = 0
        state.total = 0
        state.added = 0
        state.missing = 0
        state.moved_candidates = 0
        state.started_at = now
        state.finished_at = None
        state.error = None

    run_id = conn.execute(
        "INSERT INTO scan_runs(started_at) VALUES(?)", (now,)
    ).lastrowid
    conn.commit()

    try:
        # 既存レコードを読み込む
        existing = {
            r["rel_path"]: r
            for r in conn.execute(
                "SELECT id, rel_path, filename, size, mtime, content_hash, status FROM files"
            ).fetchall()
        }
        seen_paths: set[str] = set()

        files = list(_iter_target_files(cfg))
        with state.lock:
            state.total = len(files)
            state.phase = "hashing"

        added = 0
        for path in files:
            rel = _rel(path)
            seen_paths.add(rel)
            try:
                st = path.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                with state.lock:
                    state.processed += 1
                continue

            prev = existing.get(rel)
            if prev is None:
                # 新規ファイル
                try:
                    content_hash = _hash_file(path)
                    status = "ok"
                except OSError:
                    content_hash = None
                    status = "unreadable"
                folder = str(Path(rel).parent.as_posix())
                folder = "" if folder == "." else folder
                category_auto = cfg.categorize(rel)
                conn.execute(
                    """INSERT INTO files
                       (rel_path, filename, folder, ext, size, mtime, content_hash,
                        status, is_new, first_seen_at, last_seen_at, category_auto)
                       VALUES (?,?,?,?,?,?,?,?,1,?,?,?)""",
                    # ⚠️filename も rel と同じ NFC で入れる。path.name のまま入れると
                    # macOS では NFD で索引され、日本語IMEで打った語(NFC)で検索できなくなる
                    (rel, Path(rel).name, folder, path.suffix.lower().lstrip("."),
                     size, mtime, content_hash, status, now, now, category_auto),
                )
                fid = conn.execute("SELECT id FROM files WHERE rel_path=?", (rel,)).fetchone()["id"]
                db.reindex_fts(conn, fid)
                added += 1
            else:
                changed = (prev["size"] != size) or (abs((prev["mtime"] or 0) - mtime) > 1e-6)
                if changed or prev["content_hash"] is None:
                    try:
                        content_hash = _hash_file(path)
                        status = "ok"
                    except OSError:
                        content_hash = prev["content_hash"]
                        status = "unreadable"
                    # フォルダが変わっていれば category_auto を更新(手動値は別列なので影響なし)
                    new_folder = str(Path(rel).parent.as_posix())
                    new_folder = "" if new_folder == "." else new_folder
                    conn.execute(
                        """UPDATE files SET size=?, mtime=?, content_hash=?, status=?,
                           last_seen_at=?, folder=?, category_auto=?,
                           meta_extracted=CASE WHEN ?=1 THEN 0 ELSE meta_extracted END
                           WHERE id=?""",
                        (size, mtime, content_hash, status, now, new_folder,
                         cfg.categorize(rel), 1 if changed else 0, prev["id"]),
                    )
                else:
                    # 変化なし: last_seen だけ更新し、以前 missing/unreadable なら ok に戻す
                    conn.execute(
                        "UPDATE files SET last_seen_at=?, status='ok' WHERE id=?",
                        (now, prev["id"]),
                    )
                # 以前のバージョンが NFD のまま入れた filename を直す(検索に効く)
                if prev["filename"] != Path(rel).name:
                    conn.execute("UPDATE files SET filename=? WHERE id=?",
                                 (Path(rel).name, prev["id"]))
                db.reindex_fts(conn, prev["id"])

            with state.lock:
                state.processed += 1
            if state.processed % 50 == 0:
                conn.commit()

        # 欠落検出: DBにあるが今回見つからなかったもの
        missing = 0
        for rel, prev in existing.items():
            if rel not in seen_paths:
                conn.execute(
                    "UPDATE files SET status='missing' WHERE id=?", (prev["id"],)
                )
                missing += 1

        conn.commit()

        # 移動候補件数の集計(missing のハッシュと一致する ok ファイルが別パスにある)
        moved = conn.execute(
            """SELECT COUNT(*) AS c FROM files m
               WHERE m.status='missing' AND m.content_hash IS NOT NULL
               AND EXISTS (SELECT 1 FROM files o
                           WHERE o.status='ok' AND o.content_hash=m.content_hash
                           AND o.id<>m.id)"""
        ).fetchone()["c"]

        finished = datetime.now().isoformat(timespec="seconds")
        total = conn.execute("SELECT COUNT(*) AS c FROM files WHERE status='ok'").fetchone()["c"]
        conn.execute(
            "UPDATE scan_runs SET finished_at=?, added=?, missing=?, total=? WHERE id=?",
            (finished, added, missing, total, run_id),
        )
        db.set_meta(conn, "last_scan_at", finished)
        conn.commit()

        with state.lock:
            state.added = added
            state.missing = missing
            state.moved_candidates = moved
            state.phase = "done"
            state.finished_at = finished
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        with state.lock:
            state.phase = "error"
            state.error = f"{type(exc).__name__}: {exc}"
    finally:
        with state.lock:
            state.running = False


def find_move_candidates(conn, file_id: int) -> list[dict]:
    """missing なファイルに対し、同一ハッシュで存在する別パスを返す。"""
    row = conn.execute("SELECT content_hash FROM files WHERE id=?", (file_id,)).fetchone()
    if not row or not row["content_hash"]:
        return []
    rows = conn.execute(
        "SELECT id, rel_path, filename FROM files "
        "WHERE content_hash=? AND id<>? AND status='ok'",
        (row["content_hash"], file_id),
    ).fetchall()
    return [dict(r) for r in rows]
