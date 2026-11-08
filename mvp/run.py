from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
# Allow `python mvp/run.py` from any working directory.
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
try:  # Avoid UnicodeEncodeError on legacy Windows consoles.
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

from app.config import settings  # noqa: E402  (must come after sys.path setup)


def lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def print_banner() -> None:
    host, port = settings.host, settings.port
    print()
    print("  AI Cluster 已启动")
    print(f"  ├─ 本机访问   http://127.0.0.1:{port}")
    if host in ("0.0.0.0", "::"):
        for ip in lan_ips():
            print(f"  ├─ 局域网访问 http://{ip}:{port}")
        print("  └─ 其他设备请用上面的局域网地址（手机需与本机同一 Wi-Fi）")
        if not settings.access_token:
            print("  [!] 未设置 ACCESS_TOKEN：同一网络内任何人都能调用 API，建议设置后再对外开放")
    else:
        print(f"  └─ 监听地址   {host}:{port}（其他设备无法直接访问，改成 0.0.0.0 可开放）")
    if settings.access_token:
        print("  [lock] 已开启访问令牌（ACCESS_TOKEN）")
    print()


if __name__ == "__main__":
    print_banner()
    if os.getenv("OPEN_BROWSER", "0") == "1":
        threading.Timer(
            1.2, lambda: webbrowser.open(f"http://127.0.0.1:{settings.port}")
        ).start()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=os.getenv("RELOAD", "0") == "1",
    )
