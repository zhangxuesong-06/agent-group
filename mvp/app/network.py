from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


def _origin_kind(origin: str) -> str:
    """Classify the origin the browser used: public / private / loopback."""
    try:
        host = (urlsplit(origin).hostname or "").strip()
    except ValueError:
        return "public"
    if not host or host.lower() == "localhost":
        return "loopback" if host else "public"
    try:
        ip = ip_address(host)
    except ValueError:
        return "public"  # a domain name
    if ip.is_loopback:
        return "loopback"
    if ip.is_private or ip.is_link_local:
        return "private"
    return "public"


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
    current_kind = _origin_kind(current_origin) if current_origin else ""
    urls: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: str, url: str, kind: str) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        urls.append({"label": label, "url": url, "kind": kind})

    if current_origin:
        add("当前访问地址", current_origin, current_kind or "current")
    for ip in tailscale.get("ips") or []:
        add("Tailscale", f"http://{ip}:{port}", "tailscale")
    dns_name = tailscale.get("dns_name") or ""
    if dns_name:
        add("Tailscale HTTPS", f"https://{dns_name}", "tailscale-https")
    for ip in base["lan_ips"]:
        add("局域网", f"http://{ip}:{port}", "lan")

    recommended = ""
    # 1) Public origin (cloud domain / public IP): that is what the user
    #    actually opened, so it is the best address to hand to a phone.
    if current_origin and current_kind == "public":
        recommended = current_origin
    # 2) Tailscale works across networks, so prefer it over LAN.
    if not recommended:
        for candidate in urls:
            if candidate["kind"] == "tailscale":
                recommended = candidate["url"]
                break
    # 3) Current LAN origin, then any detected LAN address.
    if not recommended and current_origin and current_kind == "private":
        recommended = current_origin
    if not recommended:
        for candidate in urls:
            if candidate["kind"] == "lan":
                recommended = candidate["url"]
                break
    # 4) Last resort: loopback (local testing only).
    if not recommended:
        recommended = current_origin or (urls[0]["url"] if urls else "")
    return {
        **base,
        "port": port,
        "urls": urls,
        "recommended": recommended,
    }
