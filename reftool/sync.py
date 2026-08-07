"""他端末同期: バックアップDBの記録に合わせてファイル配置とDB内容を復元する。

2端末運用のワークフロー:
  メイン端末で「バックアップ」→ backups/ の library-*.db と新規文献ファイルを
  もう一方の端末(このフォルダ)へコピー → この機能で同期を実行すると、
  (1) 現DBを退避バックアップ (2) バックアップDBの内容を取り込み
  (3) ファイルを内容ハッシュで突き合わせて記録どおりの場所へ移動
  (4) 空になったフォルダを削除 (5) 再スキャン、まで一括で行う。

ファイルの同一性は sha1(スキャナと同じ)で判定するので、新規文献は
どこに置かれていてもメイン端末で整理した場所へ移動される。
"""
from __future__ import annotations

import errno
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import backup as backup_mod
from . import db
from . import scanner as scanner_mod
from .config import BACKUP_DIR, EXCLUDE_DIR_NAMES, REFTOOL_DIR, ROOT_DIR, Config, to_nfc

# 2相移動の一時退避先(_reftool 配下なのでスキャン対象外)
STAGE_DIR = REFTOOL_DIR / "_sync_stage"

# プレビュー応答に載せる一覧の上限(異常系で応答が巨大化しないための保険)
_LIST_LIMIT = 1000


class SyncState:
    """進捗を保持する共有オブジェクト(APIから参照)。"""

    def __init__(self) -> None:
        self.running = False
        self.mode = "preview"        # preview / apply
        self.backup: str | None = None
        self.phase = "idle"          # idle / reading / inventory / planning / backup
                                     # / db-import / moving / scanning / done / error
        self.processed = 0
        self.total = 0
        self.plan: dict | None = None
        self.result: dict | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.error: str | None = None
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "mode": self.mode,
                "backup": self.backup,
                "phase": self.phase,
                "processed": self.processed,
                "total": self.total,
                "plan": self.plan,
                "result": self.result,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
            }

    def set_phase(self, phase: str, total: int = 0) -> None:
        with self.lock:
            self.phase = phase
            self.processed = 0
            self.total = total

    def step(self) -> None:
        with self.lock:
            self.processed += 1


def list_backups() -> list[dict]:
    """backups/ にある DB ファイル一覧(新しい順)。"""
    out = []
    if BACKUP_DIR.exists():
        for p in sorted(BACKUP_DIR.glob("*.db"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            st = p.stat()
            out.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
    return out


def resolve_backup(name: str) -> Path:
    """backups/ 直下のファイル名のみ許可(パス区切り・別ディレクトリ参照は拒否)。"""
    if not name or Path(name).name != name or not name.endswith(".db"):
        raise ValueError("バックアップ名が不正です")
    path = BACKUP_DIR / name
    if not path.is_file():
        raise ValueError(f"バックアップが見つかりません: {name}")
    return path


def _safe_rel(rel: str) -> bool:
    """バックアップDB由来の相対パスの安全性チェック(ルート外参照を拒否)。"""
    if not rel or "\\" in rel:
        return False
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        return False
    if p.parts[0] in EXCLUDE_DIR_NAMES:
        return False
    return True


def _read_backup_records(backup_path: Path) -> list[dict]:
    """バックアップDBを読み取り専用で開き、files の配置記録を返す。"""
    import sqlite3
    uri = f"file:{backup_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        raise ValueError(f"バックアップDBを開けません: {e}")
    try:
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
        if not {"rel_path", "content_hash", "filename"} <= cols:
            raise ValueError("このファイルは文献リファレンスのバックアップDBではありません")
        rows = conn.execute(
            "SELECT rel_path, content_hash, filename FROM files ORDER BY rel_path"
        ).fetchall()
        return [dict(r) for r in rows]
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"バックアップDBの読み取りに失敗: {e}")
    finally:
        conn.close()


def _local_inventory(conn, cfg: Config, state: SyncState) -> dict[str, str]:
    """ローカル全対象ファイルの rel_path→sha1。既存DBの size/mtime が一致すれば
    ハッシュを再計算せず流用する(スキャナと同じ高速化)。"""
    known = {
        r["rel_path"]: r
        for r in conn.execute(
            "SELECT rel_path, size, mtime, content_hash FROM files "
            "WHERE content_hash IS NOT NULL"
        ).fetchall()
    }
    files = list(scanner_mod._iter_target_files(cfg))
    state.set_phase("inventory", total=len(files))
    inv: dict[str, str] = {}
    for p in files:
        rel = to_nfc(p.relative_to(ROOT_DIR).as_posix())  # scanner._rel と同じ正規化
        try:
            st = p.stat()
            prev = known.get(rel)
            if (prev is not None and prev["size"] == st.st_size
                    and abs((prev["mtime"] or 0) - st.st_mtime) <= 1e-6):
                inv[rel] = prev["content_hash"]
            else:
                inv[rel] = scanner_mod._hash_file(p)
        except OSError:
            pass  # 読めないファイルは照合対象外(スキャン時に unreadable 扱いになる)
        state.step()
    return inv


def _build_plan(records: list[dict], inv: dict[str, str]) -> dict:
    """バックアップ記録とローカル実ファイルの突き合わせ計画。

    - 配置一致(ok): 記録どおりの場所に同一内容のファイルがある
    - 移動(moves): 同一内容のファイルが別の場所にある → 記録の場所へ移す
    - 不足(missing): 同一内容のファイルがこの端末に無い(コピー漏れ)
    - この端末のみ(extra): 記録に無いファイル(そのまま残し、スキャンでNEW登録)
    - 競合(conflicts): 移動先を記録に無い別ファイルが塞いでいる → スキップ
    """
    local_by_hash: dict[str, list[str]] = {}
    for rel, h in inv.items():
        local_by_hash.setdefault(h, []).append(rel)
    for lst in local_by_hash.values():
        lst.sort()

    consumed: set[str] = set()
    moves: list[dict] = []
    missing: list[str] = []
    bad_paths: list[str] = []
    ok = 0

    # 1周目: 記録どおりの場所にあるものを確定(重複ハッシュでも位置優先で対応付け)
    pending = []
    for rec in records:
        rel, h = rec["rel_path"], rec["content_hash"]
        if not _safe_rel(rel):
            bad_paths.append(rel)
            continue
        lh = inv.get(rel)
        if lh is not None and (h is None or lh == h):
            consumed.add(rel)
            ok += 1
        else:
            pending.append(rec)

    # 2周目: 内容ハッシュで別の場所を探す(同名ファイルを優先)
    for rec in pending:
        h = rec["content_hash"]
        if not h:
            missing.append(rec["rel_path"])
            continue
        cands = [p for p in local_by_hash.get(h, []) if p not in consumed]
        if not cands:
            missing.append(rec["rel_path"])
            continue
        base = PurePosixPath(rec["rel_path"]).name
        cands.sort(key=lambda p: (0 if PurePosixPath(p).name == base else 1, p))
        src = cands[0]
        consumed.add(src)
        moves.append({"src": src, "dst": rec["rel_path"]})

    # 競合判定: 移動先に「今後も居座る」ローカルファイルがあるものは除外。
    # 移動元になっているファイルは退避されて空くので競合ではない(入替・玉突きOK)。
    srcs = {m["src"] for m in moves}
    conflicts = [m for m in moves if m["dst"] in inv and m["dst"] not in srcs]
    conflict_dsts = {m["dst"] for m in conflicts}
    moves = [m for m in moves if m["dst"] not in conflict_dsts]

    extra = sorted(p for p in inv if p not in consumed)

    def cap(lst):
        return lst[:_LIST_LIMIT]

    return {
        "backup_total": len(records),
        "ok": ok,
        "moves": cap(moves),
        "moves_total": len(moves),
        "missing": cap(missing),
        "missing_total": len(missing),
        "extra": cap(extra),
        "extra_total": len(extra),
        "conflicts": cap(conflicts),
        "conflicts_total": len(conflicts),
        "bad_paths": cap(bad_paths),
    }


def _import_db(conn, backup_path: Path) -> int:
    """バックアップDBの files 内容を現DBへ取り込む(共通列のみ・FTS再構築)。"""
    conn.execute("ATTACH DATABASE ? AS bkp", (str(backup_path),))
    try:
        local_cols = [r["name"] for r in conn.execute("PRAGMA table_info(files)")]
        bkp_cols = {r["name"] for r in conn.execute("PRAGMA bkp.table_info(files)")}
        cols = ",".join(c for c in local_cols if c in bkp_cols)
        conn.execute("DELETE FROM files")
        conn.execute(f"INSERT INTO files({cols}) SELECT {cols} FROM bkp.files")
        conn.execute("DELETE FROM files_fts")
        ids = [r["id"] for r in conn.execute("SELECT id FROM files").fetchall()]
        for fid in ids:
            db.reindex_fts(conn, fid)
        conn.commit()
        return len(ids)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("DETACH DATABASE bkp")


def _move_file(src: Path, dst: Path) -> None:
    """ファイルを1つ動かす。移動先が既にある場合は上書きしない。

    ⚠️shutil.move を直接使わないこと。失敗すると「コピーしてから元を消す」に
      切り替わるため、元を消せないとき(Windowsで開かれている等)に
      **コピーだけが残ってファイルが二重になる**(2026-08-08に実測)。
      まず名前の付け替えで試し、別ディスクのときだけコピーに頼る。
    """
    if dst.exists():
        raise OSError("移動先に別のファイルが存在します")
    try:
        os.rename(src, dst)          # 同じディスク内。中身は読まないので速く、途中状態も作らない
    except OSError as e:
        # 別ディスクをまたぐときだけ、コピー＋削除に頼る
        if getattr(e, "winerror", None) == 17 or e.errno == errno.EXDEV:
            shutil.move(str(src), str(dst))
        else:
            raise


def _apply_moves(moves: list[dict], state: SyncState) -> tuple[int, list[dict], list[str]]:
    """2相移動: 全移動元をいったんステージへ退避してから最終位置へ置く。
    (入替・玉突き移動でも安全。失敗したファイルは元の場所へ戻す。)

    戻り値の3つめは、退避フォルダに残ってしまったファイル。
    ⚠️黙って残すと、スキャン対象外の場所にファイルが隠れたまま「欠落」に見える。
      必ず報告して、利用者が取り戻せるようにすること。
    """
    STAGE_DIR.mkdir(exist_ok=True)
    state.set_phase("moving", total=len(moves) * 2)
    staged: list[tuple[Path, dict]] = []
    errors: list[dict] = []

    for i, m in enumerate(moves):
        src = ROOT_DIR / m["src"]
        tmp = STAGE_DIR / f"{i:05d}__{PurePosixPath(m['src']).name}"
        try:
            _move_file(src, tmp)
            staged.append((tmp, m))
        except OSError as e:
            errors.append({"path": m["src"], "error": str(e)})
        state.step()

    moved = 0
    for tmp, m in staged:
        dst = ROOT_DIR / m["dst"]
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            _move_file(tmp, dst)
            moved += 1
        except OSError as e:
            errors.append({"path": m["dst"], "error": str(e)})
            try:  # 元の場所へ戻す(ファイルを失わない)
                _move_file(tmp, ROOT_DIR / m["src"])
            except OSError as e2:
                errors.append({"path": m["src"], "error": f"元の場所へ戻せませんでした: {e2}"})
        state.step()

    # 戻しきれなかったものを数える。空でなければ rmdir は失敗するので、
    # 「消せたかどうか」ではなく中身を見て判断する。
    leftovers = sorted(p.name for p in STAGE_DIR.iterdir()) if STAGE_DIR.exists() else []
    if not leftovers:
        try:
            STAGE_DIR.rmdir()
        except OSError:
            pass
    return moved, errors, leftovers


def _prune_empty_dirs() -> int:
    """移動で空になったフォルダを削除(_reftool 配下は対象外)。"""
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(ROOT_DIR, topdown=False):
        p = Path(dirpath)
        if p == ROOT_DIR:
            continue
        try:
            rel_parts = p.relative_to(ROOT_DIR).parts
        except ValueError:
            continue
        if rel_parts and rel_parts[0] in EXCLUDE_DIR_NAMES:
            continue
        try:
            p.rmdir()  # 空のときだけ成功する
            removed += 1
        except OSError:
            pass
    return removed


def run(conn, cfg: Config, backup_name: str, apply: bool,
        state: SyncState, write_lock=None) -> None:
    """同期の本体。apply=False ならプレビュー(計画作成)まで。例外は state.error へ。"""
    now = datetime.now().isoformat(timespec="seconds")
    with state.lock:
        state.running = True
        state.mode = "apply" if apply else "preview"
        state.backup = backup_name
        state.phase = "reading"
        state.processed = 0
        state.total = 0
        state.plan = None
        state.result = None
        state.error = None
        state.started_at = now
        state.finished_at = None
    try:
        path = resolve_backup(backup_name)
        records = _read_backup_records(path)
        inv = _local_inventory(conn, cfg, state)
        state.set_phase("planning")
        plan = _build_plan(records, inv)
        with state.lock:
            state.plan = plan

        if apply:
            state.set_phase("backup")
            backup_mod.manual_backup(cfg)

            state.set_phase("db-import")
            if write_lock is not None:
                with write_lock:
                    imported = _import_db(conn, path)
            else:
                imported = _import_db(conn, path)

            moved, move_errors, leftovers = _apply_moves(plan["moves"], state)
            pruned = _prune_empty_dirs()

            state.set_phase("scanning")
            scanner_mod.scan(conn, cfg, scanner_mod.ScanState())

            with state.lock:
                state.result = {
                    "imported": imported,
                    "moved": moved,
                    "move_errors": move_errors,
                    "pruned_dirs": pruned,
                    # 退避フォルダに残ってしまったファイル(スキャン対象外の場所なので
                    # 報告しないと、利用者からは「消えた」ようにしか見えない)
                    "stage_leftovers": leftovers,
                    "stage_dir": str(STAGE_DIR.relative_to(ROOT_DIR)) if leftovers else None,
                }

        with state.lock:
            state.phase = "done"
            state.finished_at = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        with state.lock:
            state.phase = "error"
            state.error = f"{type(exc).__name__}: {exc}"
    finally:
        with state.lock:
            state.running = False
