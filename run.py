"""エントリポイント: uvicorn を起動する。start.bat から呼ばれる。"""
from __future__ import annotations

import logging
import socket

import uvicorn

from reftool.config import load_config

# pypdf は「壊れているが読めた」PDFに出会うと logging で警告を出す
# (例: "EOF marker not found")。ルートロガーに handler が無いので、この行だけが
# 素のままコンソールへ落ちる——しかもどのファイルの話かは出ないので、見ても直せない。
# 開けなかったファイルは一覧の「読込不可」で分かるため、警告は止めてエラーだけ残す。
logging.getLogger("pypdf").setLevel(logging.ERROR)


class _QuietStartupLines(logging.Filter):
    """uvicorn の起動・終了ログのうち、下のバナーや start.bat と重複する行だけを落とす。

    残すのは「Application startup complete.」「Shutting down」と、警告・エラーの全部。
    落とすのは既知の文言だけなので、uvicorn 側が文言を変えたときは元どおり表示される
    (知らないメッセージを黙って握りつぶさない)。
    """

    _DROP = (
        "Started server process",          # PIDだけの行
        "Waiting for application startup",
        "Uvicorn running on",              # バナーと重複。0.0.0.0 は開けないURLで紛らわしい
        "Waiting for application shutdown",
        "Application shutdown complete",
        "Finished server process",         # 直後に start.bat が "Server stopped." を出す
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO:
            return True
        return not str(record.msg).startswith(self._DROP)


def _local_ips() -> list[str]:
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            # IPv4のみ。127.x はこの下の行で別に案内するので重複させない
            if ":" not in ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main() -> None:
    cfg = load_config()
    print("=" * 64)
    print("  文献リファレンスツール")
    print("=" * 64)
    print(f"  bind        : {cfg.host}:{cfg.port}")
    print(f"  このPCから  : http://127.0.0.1:{cfg.port}/")
    if cfg.host == "0.0.0.0":
        # 接続元の制限(allow)によって、実際に開けるアドレスが変わる
        for ip in _local_ips():
            tailscale = ip.startswith("100.")
            usable = cfg.allow == "any" or tailscale or cfg.allow == "lan"
            label = "Tailscale   " if tailscale else "LAN         "
            mark = "" if usable else "  ← 現在は接続できません"
            print(f"  {label}: http://{ip}:{cfg.port}/{mark}")
    else:
        print(f"  アクセスURL : http://{cfg.host}:{cfg.port}/")
    note = {"tailscale": "このPCとTailscaleのみ(LANの他の端末からは開けません)",
            "lan": "このPC・Tailscale・同じLANの端末",
            "any": "制限なし(信頼できるネットワークでのみ)"}.get(cfg.allow, cfg.allow)
    print(f"  接続を許す先: {note}")
    print("-" * 64)
    print("  停止するには Ctrl+C を押すか、このウィンドウを閉じてください。")
    print("=" * 64)
    # access_log=False: 操作のたびに出る "GET /api/... 200 OK" を抑止(起動情報とエラーは出る)
    logging.getLogger("uvicorn.error").addFilter(_QuietStartupLines())
    uvicorn.run("reftool.server:app", host=cfg.host, port=cfg.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
