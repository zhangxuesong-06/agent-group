from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

_CACHE: tuple[float, dict[str, Any]] | None = None
_CACHE_TTL = 10.0


def _tailscale_exe() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Tailscale\tailscale.exe",
        r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        "/usr/bin/tailscale",
        "/usr/local/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def _tailscale_info() -> dict[str, Any]:
    exe = _tailscale_exe()
    if not exe:
        return {
            "installed": False,
            "online": False,
            "ips": [],
            "dns_name": "",
            "error": "",
        }
    try:
        proc = subprocess.run(
            [exe, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if proc.returncode != 0:
            return {
                "installed": True,
                "online": False,
                "ips": [],
                "dns_name": "",
                "error": (proc.stderr or proc.stdout or "").strip()[:300],
            }
        data = json.loads(proc.stdout or "{}")
        self_info = data.get("Self") or {}
        ips = [
            str(item)
            for item in self_info.get("TailscaleIPs") or []
            if ":" not in str(item)
        ]
        dns_name = str(self_info.get("DNSName") or "").rstrip(".")
        return {
            "installed": True,
            "online": data.get("BackendState") == "Running",
            "ips": ips,
            "dns_name": dns_name,
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - status is best-effort
        return {
            "installed": True,
            "online": False,
            "ips": [],
            "dns_name": "",
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


def _lan_ips() -> list[str]:
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


def network_info(port: int, current_origin: str = "") -> dict[str, Any]:
    global _CACHE
    now = time.time()
    if _CACHE is not None and now - _CACHE[0] < _CACHE_TTL:
        base = dict(_CACHE[1])
    else:
        base = {
            "hostname": socket.gethostname(),
            "lan_ips": _lan_ips(),
            "tailscale": _tailscale_info(),
        }
        _CACHE = (now, base)
        base = dict(base)

    tailscale = dict(base["tailscale"])
    urls: list[dict[str, str]] = []
    for ip in tailscale.get("ips") or []:
        urls.append(
            {
                "label": "Tailscale",
                "url": f"http://{ip}:{port}",
                "kind": "tailscale",
            }
        )
    dns_name = tailscale.get("dns_name") or ""
    if dns_name:
        urls.append(
            {
                "label": "Tailscale HTTPS",
                "url": f"https://{dns_name}",
                "kind": "tailscale-https",
            }
        )
    if current_origin:
        urls.append(
            {"label": "当前访问地址", "url": current_origin, "kind": "current"}
        )
    for ip in base["lan_ips"]:
        urls.append({"label": "局域网", "url": f"http://{ip}:{port}", "kind": "lan"})
    recommended = ""
    for candidate in urls:
        if candidate["kind"] == "tailscale":
            recommended = candidate["url"]
            break
    if not recommended:
        for candidate in urls:
            if candidate["kind"] == "lan":
                recommended = candidate["url"]
                break
    if not recommended and current_origin:
        if not any(
            host in current_origin
            for host in ("127.0.0.1", "localhost", "[::1]")
        ):
            recommended = current_origin
    if not recommended:
        recommended = current_origin or (urls[0]["url"] if urls else "")
    return {
        **base,
        "port": port,
        "urls": urls,
        "recommended": recommended,
    }
