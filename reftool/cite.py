"""引用形式(BibTeX / プレーン)の生成。

登録済みの有効値(手動値優先)+ Crossref由来の著者・年から組み立てる。
DOIから著者・年が取れていない場合は、あるものだけで組み立てる。
"""
from __future__ import annotations

import re


def _bibkey(authors: str | None, year: str | None, title: str | None) -> str:
    first = ""
    if authors:
        first = re.split(r"[,;]", authors)[0].strip().split(" ")[0]
    first = re.sub(r"[^A-Za-z0-9]", "", first) or "ref"
    y = year or "n.d."
    return f"{first}{y}"


def to_bibtex(row: dict) -> str:
    title = row.get("title") or ""
    journal = row.get("journal") or ""
    doi = row.get("doi") or ""
    authors = row.get("authors") or ""
    year = row.get("year") or ""

    entry_type = "article" if journal else "misc"
    key = _bibkey(authors, year, title)
    # BibTeX の著者区切りは " and "
    bib_authors = " and ".join(a.strip() for a in re.split(r";", authors) if a.strip())

    fields = []
    if bib_authors:
        fields.append(f"  author = {{{bib_authors}}}")
    if title:
        fields.append(f"  title = {{{title}}}")
    if journal:
        fields.append(f"  journal = {{{journal}}}")
    if year:
        fields.append(f"  year = {{{year}}}")
    if doi:
        fields.append(f"  doi = {{{doi}}}")

    body = ",\n".join(fields)
    return f"@{entry_type}{{{key},\n{body}\n}}"


def to_plain(row: dict) -> str:
    """「著者. タイトル. 雑誌名. 年.」形式。欠けている部分は省く。"""
    authors = row.get("authors") or ""
    title = row.get("title") or ""
    journal = row.get("journal") or ""
    year = row.get("year") or ""
    doi = row.get("doi") or ""

    parts = []
    if authors:
        # "Fam Given; Fam Given" → "Fam Given, Fam Given"
        parts.append(", ".join(a.strip() for a in authors.split(";") if a.strip()))
    if title:
        parts.append(title)
    if journal:
        parts.append(journal)
    if year:
        parts.append(str(year))
    text = ". ".join(parts)
    if text and not text.endswith("."):
        text += "."
    if doi:
        text += f" doi:{doi}"
    return text
