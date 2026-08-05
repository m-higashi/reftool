"""文献ファイルからのメタデータ抽出。

PDF : 埋め込みメタデータ(/Title) + 先頭数ページのテキストから DOI/タイトル
pptx: 1枚目のスライドタイトル + コアプロパティ
DOI が取れたら Crossref で正式タイトル・雑誌名・著者・年を補完(オフライン時は
ローカル抽出値のままにフォールバック)。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config, resolve_rel

# 末尾の余計な記号を含めないよう、DOIとして自然な文字集合に限定
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)

# タイトルとして採用しない自明なゴミ
_JUNK_TITLES = {
    "", "untitled", "microsoft word", "pdf", "document", "presentation",
    "powerpoint presentation", "slide 1",
}

# ==========================================================================
# 取り出した文字列の後始末
#   2026-08-05、手入力1354件と突き合わせた実測にもとづく(→ memory の
#   reftool-extraction-quality)。多い順に「雑誌の柱」「文字化け」「空白」「ゴミ」。
# ==========================================================================

# PDFから取り出すと語間が空白の塊になることが多い。全角空白や特殊な空白も潰す
_SPACES = re.compile("[\\s\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+")
_EDGE_JUNK = " \t\u2013\u2014\u2012\u2015-_=*.,:;|\u30fb"


def tidy(s: str | None) -> str:
    """表示と検索に耐える形に整える(空白の潰し込みと前後の飾り落とし)。"""
    if not s:
        return ""
    return _SPACES.sub(" ", s).strip(_EDGE_JUNK)


# CIDフォント等のPDFからテキストを取ると読めない文字列になる。化けた値は採らない
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PUA = re.compile("[\ue000-\uf8ff]")   # 私用領域(化けた文字が入りやすい)
_EXPECTED = re.compile(
    "[\u3040-\u30ff\u3400-\u9fff\uff00-\uffef"   # かな・漢字・全角英数記号
    "A-Za-z0-9"
    "\u00a0-\u024f\u2000-\u206f\u2100-\u214f\u2190-\u21ff\u2200-\u22ff\u25a0-\u26ff"
    "\\s\\-\u2013\u2014_.,:;!?()\\[\\]{}'\"/\\\\+=*&%#@~`^|<>]"
)


def looks_garbled(s: str | None) -> bool:
    """文字化けしていそうなら True。想定外の字種が15%以上、または制御文字・私用領域。"""
    if not s:
        return False
    if _CTRL.search(s) or _PUA.search(s):
        return True
    body = s.strip()
    if not body:
        return False
    odd = sum(1 for ch in body if not _EXPECTED.match(ch))
    return odd / len(body) >= 0.15


# ページ上下の「柱」(誌名＋巻号)をタイトルと取り違えるのが最も多い外し方だった
_ISSUE_RE = re.compile(
    r"(?:vol\.?\s*\d+|no\.?\s*\d+|第?\s*\d+\s*巻|\d+\s*号|\d{4}\s*年\s*\d{1,2}\s*月号)",
    re.IGNORECASE,
)
_MONTHS = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.IGNORECASE)


def issue_line(s: str | None) -> tuple[bool, str | None]:
    """『画像診断 Vol.34 No.6 2014』のような柱かどうかを見る。

    戻り値 (柱である, 雑誌名)。巻号を取り除いた残りが短ければ柱とみなす
    (残りが長い場合は Vol. を含む本物のタイトルなので手を出さない)。
    雑誌名は化けていたら None を返す(柱の判定自体は化けていても有効)。
    """
    t = tidy(s)
    if not t or not _ISSUE_RE.search(t):
        return False, None
    rest = _ISSUE_RE.sub(" ", t)
    rest = re.sub(r"\b(?:19|20)\d{2}\b", " ", rest)   # 発行年
    rest = _MONTHS.sub(" ", rest)
    rest = re.sub(r"^\s*\d+\s*", " ", rest)           # 先頭に付くページ番号
    rest = tidy(rest)
    if len(rest) > 24:          # 巻号以外の中身が多い = 本物のタイトル
        return False, None
    return True, _clean_journal(rest)


def _clean_journal(name: str) -> str | None:
    """柱から取り出した雑誌名の後始末。使えないと判断したら None。"""
    # 巻号の言い回しの残骸(『第』『Vol』『No』)を前後から落とす
    n = re.sub(r"[\s第・,、]+$", "", name)
    n = re.sub(r"(?i)\s*(?:vol|no)\.?\s*$", "", n)
    n = re.sub(r"^[\s・,、]+", "", n)
    # 端に紛れ込んだ化け文字を落とす(1〜2文字だけ混ざると全体判定では拾えない)
    while n and not _EXPECTED.match(n[-1]):
        n = n[:-1].rstrip()
    while n and not _EXPECTED.match(n[0]):
        n = n[1:].lstrip()
    # 柱には誌名のあとに引用情報(『, , (87)339』のような巻頁)が続くことがある
    n = re.split(r"[,，、]", n)[0]
    n = re.sub(r"\s*[(（]\s*\d+\s*[)）]\s*\d*\s*$", "", n)
    n = tidy(n)
    if len(n) < 2:
        return None
    if n.lower().strip(".") in {"no", "vol", "第", "巻", "号"}:
        return None
    if re.fullmatch(r"[\d\W_]+", n):      # 数字や記号だけ
        return None
    # 『2024S4』『2021 A5』のような発行年＋判型の残骸を落とす
    if sum(1 for ch in n if ch.isalpha()) < 2:
        return None
    if sum(1 for ch in n if ch.isdigit()) >= len(n) * 0.4:
        return None
    if _FILENAME_LIKE.search(n):          # 『….indd』のような組版ファイル名
        return None
    if looks_garbled(n):
        return None
    return n


# そのほか採用しないもの
_JUNK_RE = re.compile(
    r"^(?:"
    "[\\d\\s‐-―\\-_=*.,:;|・]+"        # ページ番号や飾りだけ
    r"|powerpoint\s*プレゼンテーション|microsoft\s*word.*|スライド\s*\d+|slide\s*\d+|無題"
    r"|はじめに|目次|序|序文|まえがき|あとがき|参考文献|謝辞|contents?|introduction|abstract"
    r")$",
    re.IGNORECASE,
)
_FILENAME_LIKE = re.compile(r"\.(?:indd|docx?|pptx?|pdf|ai|xlsx?)$", re.IGNORECASE)
_BODY_OPENING = re.compile(r"(?:について(?:発表|お話)|を始めます|いたします)[。.]?$")


def _clean_doi(raw: str) -> str:
    doi = raw.strip().rstrip(".,;)]}>")
    # 末尾に紛れ込みがちな語を除去
    doi = re.sub(r"(?i)(pmid|received|accepted|©|copyright).*$", "", doi).strip().rstrip(".,;")
    return doi


def _looks_like_title(s: str | None) -> bool:
    """タイトルとして採用してよい文字列か。迷ったら採らない(誤りを見せるより空欄がよい)。"""
    t = tidy(s)
    if len(t) < 4 or t.lower() in _JUNK_TITLES:
        return False
    if _JUNK_RE.match(t) or _FILENAME_LIKE.search(t) or _BODY_OPENING.search(t):
        return False
    if looks_garbled(t):
        return False
    if issue_line(t)[0]:
        return False
    return True


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def extract_pdf(path: Path, pages: int) -> dict:
    from pypdf import PdfReader

    result: dict = {"title": None, "journal": None, "doi": None, "readable": True}
    try:
        reader = PdfReader(str(path))
    except Exception:
        result["readable"] = False
        return result

    # 埋め込みメタデータ。柱(誌名＋巻号)が入っていることがあるので、その場合は雑誌名へ回す
    try:
        info = reader.metadata
        if info and info.title:
            if _looks_like_title(info.title):
                result["title"] = tidy(info.title)
            else:
                is_issue, name = issue_line(info.title)
                if is_issue and name:
                    result["journal"] = name
    except Exception:
        pass

    # 先頭ページのテキスト
    text_parts = []
    try:
        n = min(pages, len(reader.pages))
        for i in range(n):
            try:
                text_parts.append(reader.pages[i].extract_text() or "")
            except Exception:
                continue
    except Exception:
        pass
    head_text = "\n".join(text_parts)

    # DOI: 本文 → 改行で分断された本文 → メタデータ の順で探す
    m = DOI_RE.search(head_text)
    if not m and head_text:
        # 組版の都合で `10.1111/\nneup.…` のように途中で改行されることがある
        m = DOI_RE.search(re.sub(r"[\r\n]+", "", head_text))
    if not m:
        try:
            raw_meta = " ".join(str(v) for v in (reader.metadata or {}).values())
            m = DOI_RE.search(raw_meta)
        except Exception:
            m = None
    if m:
        result["doi"] = _clean_doi(m.group(0))

    # 本文の先頭を1行ずつ見て、タイトルに使える行と雑誌の柱を拾い分ける。
    # 柱は「タイトルらしく見えるのに実は誌名＋巻号」という最も多い外し方なので、
    # タイトルには採らず雑誌名の候補にする。
    if head_text:
        for line in head_text.splitlines():
            line = tidy(line)
            if not line:
                continue
            if not result["journal"]:
                is_issue, name = issue_line(line)
                if is_issue:
                    if name:
                        result["journal"] = name
                    continue          # 柱の行はタイトル候補にしない
            if not result["title"] and _looks_like_title(line) and not DOI_RE.search(line):
                result["title"] = line[:300]
            if result["title"] and result["journal"]:
                break

    return result


# --------------------------------------------------------------------------
# PDF しおり(目次抽出タスクでも使う)
# --------------------------------------------------------------------------
def extract_pdf_outline(path: Path) -> list[tuple[int, str]]:
    """埋め込みしおりを (深さ, タイトル) の並びで返す。無ければ空リスト。"""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        outlines = reader.outline
    except Exception:
        return []

    items: list[tuple[int, str]] = []

    def walk(node, depth: int) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, depth + 1 if not isinstance(child, list) else depth)
            return
        title = getattr(node, "title", None)
        if title:
            items.append((depth, str(title).strip()))

    try:
        for top in outlines:
            walk(top, 0)
    except Exception:
        return []
    return items


# --------------------------------------------------------------------------
# pptx
# --------------------------------------------------------------------------
def extract_pptx(path: Path) -> dict:
    from pptx import Presentation

    result: dict = {"title": None, "journal": None, "doi": None, "readable": True}
    try:
        prs = Presentation(str(path))
    except Exception:
        result["readable"] = False
        return result

    # コアプロパティのタイトル
    try:
        if prs.core_properties.title and _looks_like_title(prs.core_properties.title):
            result["title"] = tidy(prs.core_properties.title)
    except Exception:
        pass

    # 1枚目のタイトルプレースホルダ or 最初のテキスト
    if not result["title"] and prs.slides:
        slide = prs.slides[0]
        title_text = None
        try:
            if slide.shapes.title and slide.shapes.title.text.strip():
                title_text = slide.shapes.title.text.strip()
        except Exception:
            pass
        if not title_text:
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    title_text = shape.text_frame.text.strip().splitlines()[0]
                    break
        if _looks_like_title(title_text):
            result["title"] = tidy(title_text)[:300]

    return result


def extract_file(rel_path: str, ext: str, cfg: Config) -> dict:
    """拡張子に応じて抽出。結果は title/journal/doi/readable/authors/year を含む dict。"""
    path = resolve_rel(rel_path)  # NFC/NFDの表記ゆれを吸収して実ファイルを探す
    if not path.exists():
        return {"title": None, "journal": None, "doi": None, "readable": False}
    if ext == "pdf":
        res = extract_pdf(path, cfg.metadata_pages)
    elif ext in ("ppt", "pptx"):
        res = extract_pptx(path)
    else:
        res = {"title": None, "journal": None, "doi": None, "readable": True}
    res.setdefault("authors", None)
    res.setdefault("year", None)
    return res


# --------------------------------------------------------------------------
# Crossref
# --------------------------------------------------------------------------
def crossref_lookup(doi: str, cfg: Config) -> dict | None:
    """DOIから {title, journal, authors, year} を返す。失敗時 None(=フォールバック)。"""
    if not cfg.crossref_enabled or not doi:
        return None
    headers = {"User-Agent": f"reftool/1.0 (mailto:{cfg.crossref_mailto})"} if cfg.crossref_mailto else {}
    # 標準ライブラリのみで問い合わせる(依存を増やさない方針)。DOIはパスに載るので
    # `/` だけ残してエスケープする。ネットワーク不通・タイムアウト・不正JSONは
    # すべて None を返してローカル抽出値へフォールバックする。
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=cfg.crossref_timeout) as r:
            if r.status != 200:
                return None
            msg = json.loads(r.read().decode("utf-8", "replace")).get("message", {})
    except urllib.error.HTTPError as e:
        e.close()  # HTTPErrorは応答本体を持つので閉じる(ResourceWarning対策)
        return None
    except Exception:
        return None

    title = None
    if msg.get("title"):
        title = tidy(msg["title"][0])   # Crossrefの値にも改行や連続空白が入ることがある
    journal = None
    if msg.get("container-title"):
        journal = msg["container-title"][0]

    authors = None
    if msg.get("author"):
        names = []
        for a in msg["author"]:
            fam = a.get("family", "")
            given = a.get("given", "")
            names.append((f"{fam} {given}").strip() or a.get("name", ""))
        authors = "; ".join(n for n in names if n)

    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = msg.get(key, {}).get("date-parts")
        if parts and parts[0] and parts[0][0]:
            year = str(parts[0][0])
            break

    return {"title": title, "journal": journal, "authors": authors, "year": year}
