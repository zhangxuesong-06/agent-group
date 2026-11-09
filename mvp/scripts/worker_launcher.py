from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(
    os.getenv("WORKER_CONFIG", str(BASE_DIR / "mvp" / "data" / "worker.json"))
).expanduser()

DEFAULT_SERVER = "http://127.0.0.1:8000"
DEFAULT_WORKSPACE = BASE_DIR / "mvp" / "data" / "workspace"

try:  # Legacy Windows consoles default to a non-UTF-8 code page.
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass


def mask_token(token: str) -> str:
    if not token:
        return "(空)"
    if len(token) <= 12:
        return token[:4] + "..."
    return token[:8] + "..." + token[-4:]


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[warn] 读取 {CONFIG_PATH} 失败：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:  # Best-effort: only meaningful on POSIX.
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def ask(label: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            raw = input(f"{label}{suffix}: ").strip()
        except EOFError:
            print()
            raise SystemExit("没有输入，已取消。")
        value = raw or default
        if value:
            return value
        if not required:
            return ""
        print("  不能为空，请重新输入。")


def ask_workspace(default: str) -> str:
    value = ask("工作区绝对路径", default, required=True)
    path = Path(value).expanduser()
    if not path.exists():
        create = ask(f"目录不存在，创建吗？(y/N)", "n").lower()
        if create in ("y", "yes"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            raise SystemExit(f"工作区不存在：{path}")
    return str(path.resolve())


def check_server(server: str) -> None:
    url = server.rstrip("/") + "/api/health"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            if response.status != 200:
                print(f"[warn] {url} 返回 {response.status}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"[warn] 连不上 {url}：{exc}")
        print("       服务器可能还没启动。worker 会持续重试，启动服务器后会自动连上。")


def build_config(args: argparse.Namespace) -> dict:
    existing = {} if args.reset else load_config()
    config = {
        "server": args.server or existing.get("server") or "",
        "token": args.token or existing.get("token") or "",
        "name": args.name or existing.get("name") or socket.gethostname(),
        "workspace": existing.get("workspace") or "",
        "allow_shell": existing.get("allow_shell", True),
    }
    if args.workspace:
        config["workspace"] = str(Path(args.workspace).expanduser())
    if args.no_shell:
        config["allow_shell"] = False

    fresh = not existing
    if fresh:
        print()
        print("  首次配置 worker。直接回车使用方括号里的默认值。")
        print(f"  配置会保存在：{CONFIG_PATH}")
        print()

    if not config["server"]:
        config["server"] = ask("服务器地址", DEFAULT_SERVER, required=True)
    if not config["token"]:
        config["token"] = ask("设备 token（设置 → 设备 → 配对设备）", required=True)
    if not config["name"]:
        config["name"] = ask("设备名称", socket.gethostname(), required=True)
    if not config["workspace"]:
        config["workspace"] = ask_workspace(str(DEFAULT_WORKSPACE))

    config["server"] = str(config["server"]).rstrip("/")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="交互式配置并启动 AI Cluster 设备 worker。"
    )
    parser.add_argument("--reset", action="store_true", help="忽略已保存配置，重新填写")
    parser.add_argument("--server", default="", help="http(s)://host[:port]")
    parser.add_argument("--token", default="", help="设备 token")
    parser.add_argument("--name", default="", help="设备显示名称")
    parser.add_argument("--workspace", default="", help="工作区绝对路径")
    parser.add_argument("--no-shell", action="store_true", help="禁止执行命令")
    args = parser.parse_args()

    config = build_config(args)
    save_config(config)

    print()
    print(f"  服务器   {config['server']}")
    print(f"  设备名   {config['name']}")
    print(f"  工作区   {config['workspace']}")
    print(f"  token    {mask_token(config['token'])}")
    print(f"  执行命令 {'是' if config['allow_shell'] else '否'}")
    print()
    check_server(config["server"])
    print("  正在连接；断开后会自动重试。按 Ctrl+C 停止。")
    print()

    sys.path.insert(0, str(BASE_DIR))
    argv = [
        "agent",
        "--server",
        config["server"],
        "--token",
        config["token"],
        "--name",
        config["name"],
        "--workspace",
        config["workspace"],
    ]
    if not config["allow_shell"]:
        argv.append("--no-shell")
    sys.argv = argv

    from mvp.worker.agent import main as worker_main

    worker_main()


if __name__ == "__main__":
    main()
