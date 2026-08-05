"""エントリポイント: uvicorn を起動する。start.bat から呼ばれる。"""
from __future__ import annotations

import socket

import uvicorn

from reftool.config import load_config


def _local_ips() -> list[str]:
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ip = info[4][0]
            if ":" not in ip:  # IPv4のみ
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
        for ip in _local_ips():
            print(f"  LAN/Tailscale: http://{ip}:{cfg.port}/")
    else:
        print(f"  アクセスURL : http://{cfg.host}:{cfg.port}/")
    print("-" * 64)
    print("  停止するには Ctrl+C を押すか、このウィンドウを閉じてください。")
    print("=" * 64)
    # access_log=False: 操作のたびに出る "GET /api/... 200 OK" を抑止(起動情報とエラーは出る)
    uvicorn.run("reftool.server:app", host=cfg.host, port=cfg.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
