"""設定ファイル(config.toml)の読み込み。

すべてのパスは _reftool/ の親(=文献ルートフォルダ)からの相対で扱う。
絶対パスはこのモジュール内で実行時に解決するだけで、DBや設定には保存しない。
"""
from __future__ import annotations

import re
import shutil
import tomllib
import unicodedata
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

# _reftool/ ディレクトリ
REFTOOL_DIR = Path(__file__).resolve().parent.parent
# 文献ルート(スキャン対象の最上位)= _reftool の親
ROOT_DIR = REFTOOL_DIR.parent
CONFIG_PATH = REFTOOL_DIR / "config.toml"
DB_PATH = REFTOOL_DIR / "library.db"
BACKUP_DIR = REFTOOL_DIR / "backups"
STATIC_DIR = REFTOOL_DIR / "static"

# 自分自身のディレクトリはスキャンから除外する
EXCLUDE_DIR_NAMES = {"_reftool"}


def to_nfc(text: str) -> str:
    """パス文字列を NFC(合成形)に統一する。

    macOS はファイル名を NFD(分解形。「が」が「か」+濁点)で返すため、Windows で作った
    DB をそのまま Mac で使うと全ファイルが「欠落」+「新規」に見えてしまう。
    rel_path は保存も比較も必ずこの関数を通した NFC で扱う。
    """
    return unicodedata.normalize("NFC", text)


def resolve_rel(rel_path: str) -> Path:
    """DB の rel_path(NFC)から実ファイルの絶対パスを作る。

    macOS はファイル名の正規化差を吸収するのでそのまま開けるが、Windows は吸収しない。
    NFD のまま置かれたファイルが Windows にある場合に備えて、NFC で見つからなければ
    NFD でも探す。
    """
    path = ROOT_DIR / rel_path
    if not path.exists():
        alt = ROOT_DIR / unicodedata.normalize("NFD", rel_path)
        if alt.exists():
            return alt
    return path


@dataclass
class CategoryRule:
    keyword: str
    category: str


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8585
    allow: str = "tailscale"   # 接続を許す相手: tailscale / lan / any
    memo1_label: str = "メモ"
    memo2_label: str = "目次"
    categories: list[str] = field(default_factory=lambda: ["それ以外"])
    default_category: str = "それ以外"
    category_rules: list[CategoryRule] = field(default_factory=list)
    crossref_enabled: bool = True
    crossref_mailto: str = ""
    crossref_timeout: int = 8
    extensions: list[str] = field(default_factory=lambda: ["pdf", "ppt", "pptx"])
    metadata_pages: int = 3
    backup_enabled: bool = True
    backup_keep: int = 30

    def categorize(self, rel_path: str) -> str:
        """相対パス(フォルダ名を含む)からカテゴリを自動判定する。"""
        lower = rel_path.lower()
        for rule in self.category_rules:
            if rule.keyword.lower() in lower:
                return rule.category
        return self.default_category


def _split_trailing_comment(rest: str) -> tuple[str, str]:
    """`30   # 保持世代数` を ("30", "   # 保持世代数") に分ける。

    値が文字列で `#` を含む場合を壊さないよう、最後の引用符より後ろの `#` だけを
    コメントの開始とみなす。
    """
    start = rest.rfind('"') + 1
    idx = rest.find("#", start)
    if idx < 0:
        return rest.rstrip(), ""
    return rest[:idx].rstrip(), rest[idx:]


def update_config_file(updates: dict[tuple[str, str], str]) -> Path:
    """config.toml のうち指定したキーの値だけを差し替える。

    updates は {(セクション名, キー名): TOMLとして正しい値の文字列}。
    コメント・並び・対象外のキーはそのまま残す。`[[categories.rules]]` のような
    配列表は書き換え対象にしない(意図しない破壊を避けるため)。
    書き換える前に backups/ へ控えを取り、そのパスを返す。
    """
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # 各行がどのセクションに属するかを先に決める
    owners: list[str] = []
    cur = ""
    for ln in lines:
        s = ln.strip()
        if s.startswith("[["):
            cur = "\0array"  # 配列表(判定ルール等)は触らない
        elif s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
        owners.append(cur)

    remaining = dict(updates)
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*=\s*)(.*?)(\r?\n)?$", ln)
        if not m:
            continue
        key = (owners[i], m.group(2))
        if key not in remaining:
            continue
        _value, comment = _split_trailing_comment(m.group(4))
        eol = m.group(5) or "\n"
        lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{remaining.pop(key)}{comment}{eol}"

    # 元々書かれていなかったキーは、そのセクションの末尾に足す
    for (section, key), value in remaining.items():
        last = max((i for i, o in enumerate(owners) if o == section), default=-1)
        if last < 0:
            lines.append(f"\n[{section}]\n{key} = {value}\n")
            owners.append(section)
            continue
        lines.insert(last + 1, f"{key} = {value}\n")
        owners.insert(last + 1, section)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = BACKUP_DIR / f"config-{stamp}.toml"
    shutil.copy2(CONFIG_PATH, bak)
    CONFIG_PATH.write_text("".join(lines), encoding="utf-8")
    return bak


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    with open(CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)

    server = data.get("server", {})
    labels = data.get("labels", {})
    cats = data.get("categories", {})
    crossref = data.get("crossref", {})
    scan = data.get("scan", {})
    backup = data.get("backup", {})

    rules = [
        CategoryRule(keyword=r["keyword"], category=r["category"])
        for r in cats.get("rules", [])
        if r.get("keyword") and r.get("category")
    ]

    return Config(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8585)),
        allow=str(server.get("allow", "tailscale")).strip().lower(),
        memo1_label=labels.get("memo1", "メモ"),
        memo2_label=labels.get("memo2", "目次"),
        categories=cats.get("list", ["それ以外"]),
        default_category=cats.get("default", "それ以外"),
        category_rules=rules,
        crossref_enabled=bool(crossref.get("enabled", True)),
        crossref_mailto=crossref.get("mailto", ""),
        crossref_timeout=int(crossref.get("timeout_seconds", 8)),
        extensions=[e.lower().lstrip(".") for e in scan.get("extensions", ["pdf", "ppt", "pptx"])],
        metadata_pages=int(scan.get("metadata_pages", 3)),
        backup_enabled=bool(backup.get("enabled", True)),
        backup_keep=int(backup.get("keep", 30)),
    )
