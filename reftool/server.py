"""FastAPI アプリ本体: API エンドポイントと静的UI配信。"""
from __future__ import annotations

import gzip as _gzip
import json as _json
import shutil
import threading
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders

from . import backup as backup_mod
from . import cite as cite_mod
from . import db
from . import metadata as meta_mod
from . import scanner as scanner_mod
from . import sync as sync_mod
from .config import (CONFIG_PATH, ROOT_DIR, STATIC_DIR, load_config, resolve_rel,
                     to_nfc, update_config_file)

cfg = load_config()
app = FastAPI(title="文献リファレンスツール")


# ==========================================================================
# gzip 圧縮(テキスト系のみ)。PDF等のバイナリ・巨大ファイルはバッファせず素通り。
# ==========================================================================
_COMPRESSIBLE_PREFIXES = (
    "text/", "application/json", "application/javascript",
    "application/xml", "image/svg+xml",
)


class GzipTextMiddleware:
    """テキスト系レスポンスだけを gzip 圧縮する pure-ASGI ミドルウェア。

    - Content-Type が text/* や application/json 等のときだけ圧縮する。
    - PDF/画像/動画(FileResponse のストリーム)はバッファせずそのまま流す
      (教科書フォルダには 23GB 分のファイルがあるため、ここは絶対にバッファしない)。
    - 既に Content-Encoding が付いているレスポンスは触らない。
    """

    def __init__(self, app, minimum_size: int = 500) -> None:
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get(
            "accept-encoding", ""
        ):
            await self.app(scope, receive, send)
            return

        state = {"start": None, "compressible": False, "buffer": bytearray(),
                 "passthrough": False}

        async def send_wrapper(message):
            t = message["type"]
            if t == "http.response.start":
                state["start"] = message
                h = Headers(raw=message["headers"])
                state["compressible"] = (
                    not h.get("content-encoding")
                    and h.get("content-type", "").startswith(_COMPRESSIBLE_PREFIXES)
                )
                return  # body を見てから送る
            if t != "http.response.body":
                await send(message)
                return

            body = message.get("body", b"")
            if not state["compressible"]:
                # 素通り: start をまだ送っていなければ先に送る
                if not state["passthrough"]:
                    state["passthrough"] = True
                    await send(state["start"])
                await send(message)
                return

            # 圧縮対象: 全 body をバッファ(テキスト系なので小さい)
            state["buffer"].extend(body)
            if message.get("more_body", False):
                return
            data = bytes(state["buffer"])
            start = state["start"]
            if len(data) < self.minimum_size:
                await send(start)
                await send({"type": "http.response.body", "body": data,
                            "more_body": False})
                return
            compressed = _gzip.compress(data)
            headers = MutableHeaders(raw=list(start["headers"]))
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(compressed))
            headers.add_vary_header("Accept-Encoding")
            start["headers"] = headers.raw
            await send(start)
            await send({"type": "http.response.body", "body": compressed,
                        "more_body": False})

        await self.app(scope, receive, send_wrapper)


app.add_middleware(GzipTextMiddleware, minimum_size=500)


# ---- 静的アセットのバージョン(内容ハッシュ)。起動時に計算し ?v= に使う ----
import hashlib as _hashlib


def _asset_version(name: str) -> str:
    """static/<name> の内容ハッシュ(先頭12桁)。起動時に1回だけ計算する。"""
    try:
        data = (STATIC_DIR / name).read_bytes()
        return _hashlib.sha1(data).hexdigest()[:12]
    except OSError:
        return "0"


ASSET_VERSIONS = {"app.js": _asset_version("app.js"), "style.css": _asset_version("style.css")}


@app.middleware("http")
async def _cache_policy(request: Request, call_next):
    """UIのキャッシュ制御。
    - HTML("/" と *.html)は常に no-store(?v= を必ず最新にするため)。
    - *.js / *.css は ?v= 付き(=index.htmlが埋め込むバージョン付きURL)のときだけ
      長期 immutable キャッシュ。素の(v=なし)アクセスは安全側で no-store。
    """
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    elif path.endswith((".js", ".css")):
        if "v=" in (request.url.query or ""):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


# ==========================================================================
# セキュリティ(ローカル/VPN内前提。認証は付けない)
#   1) Host 検証: DNSリバインディング対策。IPリテラル・loopback・自ホスト名のみ許可。
#      悪意あるWebページが evil.com→127.0.0.1 に再バインドしても Host: evil.com は弾く。
#   2) クロスサイトPOST遮断: Sec-Fetch-Site: cross-site の状態変更は403(CSRF対策)。
#      同一オリジンのUI(same-origin)や、ヘッダを送らない curl 等のツールは通す。
#   3) ボディサイズ上限: 巨大リクエストでメモリを食い潰されないようにする。
# ==========================================================================
import ipaddress as _ipaddress
import socket as _socket

_ALLOWED_HOST_NAMES = {"localhost", _socket.gethostname().lower()}
# ↑ IPアドレス・localhost・このPCのホスト名のみ許可(DNSリバインディング対策)。
#   Tailscale の MagicDNS 名(<ホスト名>.tailXXXX.ts.net のような形)で開きたい場合は、
#   その名前をこの集合に足す。IP直打ちで使う分には変更不要。
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_MAX_BODY = 10 * 1024 * 1024  # 10MB(アップロード機能は無い。JSON編集には十分)


def _host_allowed(host_header: str) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
    if host.startswith("[") and "]" in host:  # IPv6 リテラル [::1]:port
        host = host[1:host.index("]")]
    host = host.strip().lower()
    if host in _ALLOWED_HOST_NAMES:
        return True
    try:
        _ipaddress.ip_address(host)  # IPリテラルなら許可(127.0.0.1 / Tailscale / LAN IP)
        return True
    except ValueError:
        return False


@app.middleware("http")
async def _security(request: Request, call_next):
    if not _host_allowed(request.headers.get("host", "")):
        return JSONResponse({"detail": "invalid host"}, status_code=403)
    if request.method in _UNSAFE_METHODS:
        if request.headers.get("sec-fetch-site", "") == "cross-site":
            return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > _MAX_BODY:
            return JSONResponse({"detail": "request too large"}, status_code=413)
    return await call_next(request)


scan_state = scanner_mod.ScanState()
sync_state = sync_mod.SyncState()
extract_state = {"running": False, "processed": 0, "total": 0, "phase": "idle"}
_extract_lock = threading.Lock()
_write_lock = threading.Lock()  # 書き込みAPIの直列化(SQLite書き込み競合回避)


# ---- 接続ヘルパ(リクエストごとに開閉) ----------------------------------
def get_conn():
    return db.connect()


# ==========================================================================
# 起動処理
# ==========================================================================
@app.on_event("startup")
def _startup() -> None:
    conn = db.connect()
    db.init_db(conn)
    try:
        backup_mod.daily_backup(conn, cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[backup] 起動時バックアップ失敗: {e}")
    conn.close()


# ==========================================================================
# 一覧・検索
# ==========================================================================
def _short_query_tokens(q: str) -> bool:
    return any(len(t) < 3 for t in q.split())


def _build_filter(
    q: str = "",
    category: str = "",
    status: str = "",
    folder: str = "",
    favorite: str = "",
    read_status: str = "",
) -> tuple[list[str], list, str]:
    """一覧表示とNEW一括解除で共有するWHERE条件ビルダー。"""
    where = []
    params: list = []
    joins = ""

    # 検索語もNFCに揃える(macOSからコピーした語はNFDのことがある。索引側はNFC)
    q = to_nfc(q).strip()
    if q:
        if _short_query_tokens(q):
            like = f"%{q}%"
            where.append(
                f"(({db.EFF_TITLE}) LIKE ? OR ({db.EFF_JOURNAL}) LIKE ? "
                f"OR memo1 LIKE ? OR memo2 LIKE ? OR filename LIKE ?)"
            )
            params += [like, like, like, like, like]
        else:
            match = " ".join(f'"{t.replace(chr(34), chr(34)*2)}"' for t in q.split())
            joins += " JOIN files_fts ON files_fts.rowid = files.id"
            where.append("files_fts MATCH ?")
            params.append(match)

    if category:
        where.append(f"({db.EFF_CATEGORY}) = ?")
        params.append(category)
    if folder:
        where.append("(folder = ? OR folder LIKE ?)")
        params += [folder, folder + "/%"]
    if favorite == "1":
        where.append("favorite = 1")
    if read_status:
        where.append("read_status = ?")
        params.append(read_status)

    if status == "new":
        where.append("is_new = 1")
    elif status == "duplicate":
        where.append(
            "content_hash IN (SELECT content_hash FROM files "
            "WHERE content_hash IS NOT NULL GROUP BY content_hash HAVING COUNT(*)>1)"
        )
    elif status in ("missing", "unreadable"):
        where.append("status = ?")
        params.append(status)
    elif status == "attention":
        where.append("status IN ('missing','unreadable')")
    elif status == "ok":
        where.append("status = 'ok'")

    return where, params, joins


@app.get("/api/files")
def list_files(
    q: str = "",
    category: str = "",
    status: str = "",          # ok / missing / unreadable / new / duplicate
    folder: str = "",
    favorite: str = "",        # "1" で絞り込み
    read_status: str = "",
    sort: str = "folder",      # folder / title / new
    page: int = 1,
    per_page: int = 50,
):
    conn = get_conn()
    try:
        where, params, joins = _build_filter(q, category, status, folder, favorite, read_status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        order = {
            "title": f"{db.EFF_TITLE} COLLATE NOCASE",
            "new": "is_new DESC, last_seen_at DESC",
            "folder": "files.folder, files.filename",
        }.get(sort, "files.folder, files.filename")

        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM files{joins}{where_sql}", params
        ).fetchone()["c"]

        per_page = max(1, min(per_page, 500))
        page = max(1, page)
        offset = (page - 1) * per_page

        rows = conn.execute(
            f"""SELECT files.id, files.rel_path, files.filename, files.folder,
                   files.ext, files.size, files.status, files.is_new,
                   {db.EFF_TITLE} AS title, {db.EFF_JOURNAL} AS journal,
                   {db.EFF_DOI} AS doi, {db.EFF_CATEGORY} AS category,
                   files.favorite, files.read_status, files.memo1, files.memo2,
                   files.title_auto, files.title_user, files.journal_auto, files.journal_user,
                   files.doi_auto, files.doi_user, files.category_auto, files.category_user
                FROM files{joins}{where_sql}
                ORDER BY {order} LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            d["memo1_head"] = (d.pop("memo1") or "").strip().replace("\n", " ")[:80]
            d["memo2_head"] = (d.pop("memo2") or "").strip().replace("\n", " ")[:80]
            d["title_missing"] = not (d["title"] or "").strip()
            items.append(d)

        return {"total": total, "page": page, "per_page": per_page, "items": items}
    finally:
        conn.close()


@app.get("/api/files/{file_id}")
def get_file(file_id: int):
    conn = get_conn()
    try:
        r = conn.execute(
            f"""SELECT *, {db.EFF_TITLE} AS eff_title, {db.EFF_JOURNAL} AS eff_journal,
                   {db.EFF_DOI} AS eff_doi, {db.EFF_CATEGORY} AS eff_category
                FROM files WHERE id=?""",
            (file_id,),
        ).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        d = dict(r)
        d["move_candidates"] = (
            scanner_mod.find_move_candidates(conn, file_id) if d["status"] == "missing" else []
        )
        # 重複(同一ハッシュの別パス)
        d["duplicates"] = []
        if d["content_hash"]:
            d["duplicates"] = [
                dict(x) for x in conn.execute(
                    "SELECT id, rel_path FROM files WHERE content_hash=? AND id<>?",
                    (d["content_hash"], file_id),
                ).fetchall()
            ]
        return d
    finally:
        conn.close()


# ==========================================================================
# 編集
# ==========================================================================
_EDITABLE = {
    "title_user", "journal_user", "doi_user", "url_user", "category_user",
    "memo1", "memo2", "favorite", "read_status",
}


@app.patch("/api/files/{file_id}")
async def update_file(file_id: int, request: Request):
    payload = await request.json()
    fields = {k: v for k, v in payload.items() if k in _EDITABLE}
    if not fields:
        raise HTTPException(400, "no editable fields")
    with _write_lock:
        conn = get_conn()
        try:
            exists = conn.execute("SELECT id FROM files WHERE id=?", (file_id,)).fetchone()
            if not exists:
                raise HTTPException(404, "not found")
            if "favorite" in fields:
                fields["favorite"] = 1 if fields["favorite"] else 0
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE files SET {sets} WHERE id=?", list(fields.values()) + [file_id]
            )
            db.reindex_fts(conn, file_id)
            conn.commit()
            return get_file(file_id)
        finally:
            conn.close()


@app.post("/api/files/{file_id}/clear-new")
def clear_new(file_id: int):
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute("UPDATE files SET is_new=0 WHERE id=?", (file_id,))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()


@app.post("/api/clear-new")
def clear_new_filtered(
    q: str = "",
    category: str = "",
    status: str = "",
    folder: str = "",
    favorite: str = "",
    read_status: str = "",
):
    """現在の絞り込み条件に一致するNEWを一括解除する(条件なしなら全件)。"""
    with _write_lock:
        conn = get_conn()
        try:
            where, params, joins = _build_filter(q, category, status, folder, favorite, read_status)
            where = where + ["is_new = 1"]
            where_sql = " WHERE " + " AND ".join(where)
            count = conn.execute(
                f"SELECT COUNT(*) AS c FROM files{joins}{where_sql}", params
            ).fetchone()["c"]
            id_rows = conn.execute(
                f"SELECT files.id FROM files{joins}{where_sql}", params
            ).fetchall()
            ids = [r["id"] for r in id_rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(f"UPDATE files SET is_new=0 WHERE id IN ({placeholders})", ids)
                conn.commit()
            return {"ok": True, "count": count}
        finally:
            conn.close()


@app.post("/api/files/{file_id}/relink")
async def relink(file_id: int, request: Request):
    """missing なファイル(file_id)のメタ/メモを移動先(new_id)へ引き継ぎ、古い行を削除。"""
    payload = await request.json()
    new_id = int(payload.get("new_id", 0))
    with _write_lock:
        conn = get_conn()
        try:
            old = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            new = conn.execute("SELECT * FROM files WHERE id=?", (new_id,)).fetchone()
            if not old or not new:
                raise HTTPException(404, "not found")
            # 手動編集・メモ・状態を移送(空でない手動値のみ上書き)
            conn.execute(
                """UPDATE files SET
                     title_user   = CASE WHEN ?<>'' THEN ? ELSE title_user END,
                     journal_user = CASE WHEN ?<>'' THEN ? ELSE journal_user END,
                     doi_user     = CASE WHEN ?<>'' THEN ? ELSE doi_user END,
                     category_user= CASE WHEN ?<>'' THEN ? ELSE category_user END,
                     memo1 = CASE WHEN ?<>'' THEN ? ELSE memo1 END,
                     memo2 = CASE WHEN ?<>'' THEN ? ELSE memo2 END,
                     favorite = MAX(favorite, ?),
                     read_status = CASE WHEN ?<>'未読' THEN ? ELSE read_status END
                   WHERE id=?""",
                (
                    old["title_user"] or "", old["title_user"] or "",
                    old["journal_user"] or "", old["journal_user"] or "",
                    old["doi_user"] or "", old["doi_user"] or "",
                    old["category_user"] or "", old["category_user"] or "",
                    old["memo1"] or "", old["memo1"] or "",
                    old["memo2"] or "", old["memo2"] or "",
                    old["favorite"] or 0,
                    old["read_status"] or "未読", old["read_status"] or "未読",
                    new_id,
                ),
            )
            conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            conn.execute("DELETE FROM files_fts WHERE rowid=?", (file_id,))
            db.reindex_fts(conn, new_id)
            conn.commit()
            return {"ok": True, "new_id": new_id}
        finally:
            conn.close()


@app.post("/api/files/{file_id}/delete-missing")
def delete_missing_one(file_id: int):
    """欠落(missing)レコードをDBから完全に削除する。安全のため missing のときのみ許可。"""
    with _write_lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT status FROM files WHERE id=?", (file_id,)).fetchone()
            if not row:
                raise HTTPException(404, "not found")
            if row["status"] != "missing":
                raise HTTPException(409, "欠落状態のレコードのみ削除できます")
            conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            conn.execute("DELETE FROM files_fts WHERE rowid=?", (file_id,))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()


@app.post("/api/delete-missing")
def delete_missing_filtered(
    q: str = "",
    category: str = "",
    status: str = "",
    folder: str = "",
    favorite: str = "",
    read_status: str = "",
):
    """現在の絞り込み条件に一致する欠落(missing)レコードを一括削除する(条件なしなら全欠落)。"""
    with _write_lock:
        conn = get_conn()
        try:
            # 一覧の絞り込み条件に、欠落であることを必ず AND する(status絞りは上書き)
            where, params, joins = _build_filter(q, category, "", folder, favorite, read_status)
            where = where + ["files.status = 'missing'"]
            where_sql = " WHERE " + " AND ".join(where)
            id_rows = conn.execute(
                f"SELECT files.id FROM files{joins}{where_sql}", params
            ).fetchall()
            ids = [r["id"] for r in id_rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", ids)
                conn.execute(f"DELETE FROM files_fts WHERE rowid IN ({placeholders})", ids)
                conn.commit()
            return {"ok": True, "count": len(ids)}
        finally:
            conn.close()


# ==========================================================================
# メタデータ抽出 / Crossref
# ==========================================================================
def _run_extract(only_missing: bool = True) -> None:
    conn = db.connect()
    try:
        cond = "WHERE meta_extracted=0 AND status='ok'" if only_missing else "WHERE status='ok'"
        rows = conn.execute(f"SELECT id, rel_path, ext, doi_auto FROM files {cond}").fetchall()
        with _extract_lock:
            extract_state.update(running=True, phase="extracting", total=len(rows), processed=0)
        for r in rows:
            res = meta_mod.extract_file(r["rel_path"], r["ext"], cfg)
            title = res.get("title")
            journal = res.get("journal")
            doi = res.get("doi")
            status = "ok" if res.get("readable", True) else "unreadable"
            authors = year = None
            crossref_done = 0
            if doi and cfg.crossref_enabled:
                cr = meta_mod.crossref_lookup(doi, cfg)
                if cr:
                    title = cr.get("title") or title
                    journal = cr.get("journal") or journal
                    authors = cr.get("authors")
                    year = cr.get("year")
                    crossref_done = 1
            with _write_lock:
                conn.execute(
                    """UPDATE files SET title_auto=?, journal_auto=?, doi_auto=?,
                           authors_auto=?, year_auto=?, meta_extracted=1,
                           crossref_done=?, status=CASE WHEN status='missing' THEN status ELSE ? END
                       WHERE id=?""",
                    (title, journal, doi, authors, year, crossref_done, status, r["id"]),
                )
                db.reindex_fts(conn, r["id"])
                conn.commit()
            with _extract_lock:
                extract_state["processed"] += 1
        with _extract_lock:
            extract_state.update(running=False, phase="done")
    except Exception as e:  # noqa: BLE001
        with _extract_lock:
            extract_state.update(running=False, phase=f"error: {e}")
    finally:
        conn.close()


@app.post("/api/extract")
def start_extract(all: bool = False):
    with _extract_lock:
        if extract_state["running"]:
            return {"ok": False, "reason": "already running"}
    threading.Thread(target=_run_extract, kwargs={"only_missing": not all}, daemon=True).start()
    return {"ok": True}


@app.get("/api/extract/status")
def extract_status():
    with _extract_lock:
        return dict(extract_state)


@app.post("/api/files/{file_id}/crossref")
def crossref_one(file_id: int):
    conn = get_conn()
    try:
        r = conn.execute(
            f"SELECT id, {db.EFF_DOI} AS doi FROM files WHERE id=?", (file_id,)
        ).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        if not r["doi"]:
            return {"ok": False, "reason": "DOIがありません"}
        cr = meta_mod.crossref_lookup(r["doi"], cfg)
        if not cr:
            return {"ok": False, "reason": "Crossref取得に失敗(オフライン等)"}
        with _write_lock:
            conn.execute(
                """UPDATE files SET title_auto=COALESCE(?,title_auto),
                       journal_auto=COALESCE(?,journal_auto),
                       authors_auto=COALESCE(?,authors_auto),
                       year_auto=COALESCE(?,year_auto), crossref_done=1 WHERE id=?""",
                (cr.get("title"), cr.get("journal"), cr.get("authors"), cr.get("year"), file_id),
            )
            db.reindex_fts(conn, file_id)
            conn.commit()
        return {"ok": True, "crossref": cr}
    finally:
        conn.close()


# ==========================================================================
# 引用形式
# ==========================================================================
@app.get("/api/files/{file_id}/cite")
def cite(file_id: int, fmt: str = "plain"):
    conn = get_conn()
    try:
        r = conn.execute(
            f"""SELECT {db.EFF_TITLE} AS title, {db.EFF_JOURNAL} AS journal,
                   {db.EFF_DOI} AS doi, authors_auto AS authors, year_auto AS year
                FROM files WHERE id=?""",
            (file_id,),
        ).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        row = dict(r)
        text = cite_mod.to_bibtex(row) if fmt == "bibtex" else cite_mod.to_plain(row)
        return {"format": fmt, "text": text}
    finally:
        conn.close()


# ==========================================================================
# ファイルを開く
# ==========================================================================
import os as _os
import unicodedata as _unicodedata

_ROOT_RESOLVED = ROOT_DIR.resolve()


def _norm_for_compare(p: str) -> str:
    """接頭辞比較用の正規化: OSの大文字小文字同一視(normcase)+ NFC統一。"""
    return _unicodedata.normalize("NFC", _os.path.normcase(p))


def _safe_under_root(rel_path: str):
    """rel_path を ROOT 配下の絶対パスに解決し、ルート外へ出るなら 403。

    - resolve() で `..`・シンボリックリンクを解決してから判定(単純な startswith より堅い)。
    - 比較は normcase(Windowsの大文字小文字同一視)+ NFC 正規化(macOS由来のNFD差)を通す。
    - commonpath で「接頭辞が偶然一致する隣接ディレクトリ」(例 教科書 / 教科書bak)を弾く。
    ファイル解決自体は元の rel_path で行う(実ファイル名の正規化形を壊さない)。
    """
    path = resolve_rel(rel_path).resolve()  # NFC/NFDの表記ゆれを吸収してから安全判定
    root_n = _norm_for_compare(str(_ROOT_RESOLVED))
    targ_n = _norm_for_compare(str(path))
    try:
        if _os.path.commonpath([root_n, targ_n]) != root_n:
            raise HTTPException(403, "forbidden")
    except ValueError:  # 別ドライブ等で共通パスが無い
        raise HTTPException(403, "forbidden")
    return path


@app.get("/api/files/{file_id}/open")
def open_file(file_id: int, download: bool = False):
    conn = get_conn()
    try:
        r = conn.execute("SELECT rel_path, filename, ext FROM files WHERE id=?", (file_id,)).fetchone()
        if not r:
            raise HTTPException(404, "not found")
        path = _safe_under_root(r["rel_path"])
        if not path.exists():
            raise HTTPException(410, "file missing")
        media = "application/pdf" if r["ext"] == "pdf" else "application/octet-stream"
        disp = "attachment" if (download or r["ext"] != "pdf") else "inline"
        headers = {
            "Content-Disposition": f"{disp}; filename*=UTF-8''{quote(r['filename'])}"
        }
        return FileResponse(path, media_type=media, headers=headers)
    finally:
        conn.close()


# ==========================================================================
# スキャン
# ==========================================================================
def _run_scan() -> None:
    conn = db.connect()
    try:
        scanner_mod.scan(conn, cfg, scan_state)
    finally:
        conn.close()
    # スキャン後、新規/変更ファイルのメタ抽出を自動で走らせる
    _run_extract(only_missing=True)


@app.post("/api/scan")
def start_scan():
    if scan_state.running:
        return {"ok": False, "reason": "already running"}
    threading.Thread(target=_run_scan, daemon=True).start()
    return {"ok": True}


@app.get("/api/scan/status")
def scan_status():
    return scan_state.snapshot()


# ==========================================================================
# 重複
# ==========================================================================
@app.get("/api/duplicates")
def duplicates():
    conn = get_conn()
    try:
        groups = conn.execute(
            """SELECT content_hash, COUNT(*) AS n FROM files
               WHERE content_hash IS NOT NULL
               GROUP BY content_hash HAVING n>1 ORDER BY n DESC"""
        ).fetchall()
        out = []
        for g in groups:
            members = conn.execute(
                f"SELECT id, rel_path, {db.EFF_TITLE} AS title, size, status "
                "FROM files WHERE content_hash=? ORDER BY rel_path",
                (g["content_hash"],),
            ).fetchall()
            out.append({"hash": g["content_hash"], "count": g["n"],
                        "members": [dict(m) for m in members]})
        return {"groups": out}
    finally:
        conn.close()


# ==========================================================================
# 設定・統計・バックアップ
# ==========================================================================
@app.get("/api/config")
def get_config():
    conn = get_conn()
    try:
        folders = [r["folder"] for r in conn.execute(
            "SELECT DISTINCT folder FROM files WHERE folder<>'' ORDER BY folder"
        ).fetchall()]
        # トップレベルフォルダも抽出
        tops = sorted({f.split("/")[0] for f in folders})
        last_scan = db.get_meta(conn, "last_scan_at")
        counts = {
            "total": conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"],
            "new": conn.execute("SELECT COUNT(*) AS c FROM files WHERE is_new=1").fetchone()["c"],
            "attention": conn.execute(
                "SELECT COUNT(*) AS c FROM files WHERE status IN ('missing','unreadable')"
            ).fetchone()["c"],
        }
        return {
            "categories": cfg.categories,
            "memo1_label": cfg.memo1_label,
            "memo2_label": cfg.memo2_label,
            "read_statuses": ["未読", "読書中", "読了"],
            "folders": folders,
            "top_folders": tops,
            "last_scan_at": last_scan,
            "counts": counts,
            "root_name": ROOT_DIR.name,   # 初回起動の案内で「どこを読むか」を示すのに使う
        }
    finally:
        conn.close()


# ---- 設定(UIから変更できる項目) ----------------------------------------
# ⚠️port/host/スキャン対象拡張子は表示のみ。誤ると起動できなくなるため config.toml を直接編集させる。
_SET_LIMITS = {"label_max": 20, "category_max": 40, "categories_max": 30, "keep_range": (1, 365)}


def _toml_str(s: str) -> str:
    return _json.dumps(s, ensure_ascii=False)


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join(_toml_str(x) for x in items) + "]"


@app.get("/api/settings")
def get_settings():
    """設定画面の初期値。カテゴリは「何件で使われているか」も返す(消す前に気づけるように)。"""
    conn = get_conn()
    try:
        usage = {
            r["category_user"]: r["c"]
            for r in conn.execute(
                "SELECT category_user, COUNT(*) AS c FROM files "
                "WHERE category_user IS NOT NULL AND category_user<>'' GROUP BY category_user"
            ).fetchall()
        }
    finally:
        conn.close()
    return {
        "editable": {
            "categories": cfg.categories,
            "default_category": cfg.default_category,
            "memo1_label": cfg.memo1_label,
            "memo2_label": cfg.memo2_label,
            "backup_enabled": cfg.backup_enabled,
            "backup_keep": cfg.backup_keep,
            "crossref_enabled": cfg.crossref_enabled,
            "crossref_mailto": cfg.crossref_mailto,
        },
        "readonly": {
            "host": cfg.host,
            "port": cfg.port,
            "extensions": cfg.extensions,
            "metadata_pages": cfg.metadata_pages,
            "root_name": ROOT_DIR.name,
        },
        "category_usage": usage,
        "limits": _SET_LIMITS,
    }


@app.post("/api/settings")
async def save_settings(request: Request):
    """検証してから config.toml の該当キーだけを書き換え、設定を読み直す。"""
    global cfg
    p = await request.json()

    cats = [str(c).strip() for c in (p.get("categories") or [])]
    cats = [c for c in cats if c]
    if not cats:
        raise HTTPException(400, "カテゴリは1つ以上必要です")
    if len(cats) > _SET_LIMITS["categories_max"]:
        raise HTTPException(400, f"カテゴリは{_SET_LIMITS['categories_max']}個までです")
    if len(set(cats)) != len(cats):
        raise HTTPException(400, "同じ名前のカテゴリが複数あります")
    if any(len(c) > _SET_LIMITS["category_max"] for c in cats):
        raise HTTPException(400, f"カテゴリ名は{_SET_LIMITS['category_max']}文字までです")

    default = str(p.get("default_category") or "").strip()
    if default not in cats:
        raise HTTPException(400, "既定のカテゴリは一覧の中から選んでください")

    labels = {}
    for key in ("memo1_label", "memo2_label"):
        v = str(p.get(key) or "").strip()
        if not v:
            raise HTTPException(400, "メモ欄のラベルは空にできません")
        if len(v) > _SET_LIMITS["label_max"]:
            raise HTTPException(400, f"ラベルは{_SET_LIMITS['label_max']}文字までです")
        labels[key] = v

    lo, hi = _SET_LIMITS["keep_range"]
    try:
        keep = int(p.get("backup_keep"))
    except (TypeError, ValueError):
        raise HTTPException(400, "保持世代数は数字で入力してください")
    if not lo <= keep <= hi:
        raise HTTPException(400, f"保持世代数は{lo}〜{hi}の範囲で指定してください")

    mailto = str(p.get("crossref_mailto") or "").strip()
    if mailto and ("@" not in mailto or len(mailto) > 120):
        raise HTTPException(400, "連絡先メールの形式が正しくありません(空欄でも構いません)")

    backup_enabled = bool(p.get("backup_enabled"))
    crossref_enabled = bool(p.get("crossref_enabled"))

    updates = {
        ("categories", "list"): _toml_list(cats),
        ("categories", "default"): _toml_str(default),
        ("labels", "memo1"): _toml_str(labels["memo1_label"]),
        ("labels", "memo2"): _toml_str(labels["memo2_label"]),
        ("backup", "enabled"): "true" if backup_enabled else "false",
        ("backup", "keep"): str(keep),
        ("crossref", "enabled"): "true" if crossref_enabled else "false",
        ("crossref", "mailto"): _toml_str(mailto),
    }
    with _write_lock:
        bak = update_config_file(updates)
        try:
            cfg = load_config()
        except Exception as e:  # 壊れた設定で起動不能にしない
            shutil.copy2(bak, CONFIG_PATH)
            cfg = load_config()
            raise HTTPException(500, f"設定を書き込めませんでした(元に戻しました): {e}")
    # 一覧から外したカテゴリを手動指定しているファイルは、その値を持ったまま残る
    orphans = sorted({c for c in _category_user_values() if c not in cats})
    return {"ok": True, "backup": bak.name, "orphans": orphans}


def _category_user_values() -> set[str]:
    conn = get_conn()
    try:
        return {
            r["category_user"] for r in conn.execute(
                "SELECT DISTINCT category_user FROM files "
                "WHERE category_user IS NOT NULL AND category_user<>''"
            ).fetchall()
        }
    finally:
        conn.close()


@app.post("/api/backup")
def do_backup():
    try:
        name = backup_mod.manual_backup(cfg)
        return {"ok": True, "file": name}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))


# ==========================================================================
# 他端末同期(バックアップDBに合わせてファイル配置とDB内容を復元)
# ==========================================================================
def _run_sync(backup_name: str, apply: bool) -> None:
    conn = db.connect()
    try:
        sync_mod.run(conn, cfg, backup_name, apply, sync_state, write_lock=_write_lock)
    finally:
        conn.close()
    # 同期実行後は再スキャン済みなので、新規/変更ファイルのメタ抽出を続けて走らせる
    if apply and sync_state.snapshot().get("phase") == "done":
        _run_extract(only_missing=True)


def _start_sync(backup: str, apply: bool):
    if sync_state.snapshot()["running"]:
        return {"ok": False, "reason": "同期処理が既に実行中です"}
    if scan_state.running:
        return {"ok": False, "reason": "スキャン実行中は同期できません"}
    with _extract_lock:
        if extract_state["running"]:
            return {"ok": False, "reason": "メタ抽出実行中は同期できません"}
    try:
        sync_mod.resolve_backup(backup)
    except ValueError as e:
        raise HTTPException(400, str(e))
    threading.Thread(target=_run_sync, args=(backup, apply), daemon=True).start()
    return {"ok": True}


@app.get("/api/sync/backups")
def sync_backups():
    return {"backups": sync_mod.list_backups()}


@app.post("/api/sync/preview")
def sync_preview(backup: str):
    return _start_sync(backup, apply=False)


@app.post("/api/sync/apply")
def sync_apply(backup: str):
    return _start_sync(backup, apply=True)


@app.get("/api/sync/status")
def sync_status():
    return sync_state.snapshot()


# ==========================================================================
# 静的UI
# ==========================================================================
from fastapi.responses import HTMLResponse


def _render_index() -> str:
    """index.html の /app.js・/style.css 参照に内容ハッシュの ?v= を付与して返す。
    起動時にハッシュを計算しているので、ファイルを直すと再起動でキャッシュが自動で切り替わる。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/style.css"', f'href="/style.css?v={ASSET_VERSIONS["style.css"]}"')
    html = html.replace('src="/app.js"', f'src="/app.js?v={ASSET_VERSIONS["app.js"]}"')
    return html


@app.get("/", response_class=HTMLResponse)
def index():
    return _render_index()


# 上の明示ルートより後で catch-all マウント(/app.js /style.css 等を配信)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
