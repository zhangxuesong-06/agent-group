from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv ships with uvicorn[standard]; keep it optional.
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # noqa: BLE001
    pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


_ACCESS_TOKEN_LINE = re.compile(r"^\s*(?:export\s+)?ACCESS_TOKEN\s*=\s*(.*?)\s*$")


def _parse_env_value(raw: str) -> str:
    """Parse one .env value the same way python-dotenv does for our cases."""
    value = raw.strip()
    if not value:
        return ""
    if value[0] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return (value[1:end] if end != -1 else value[1:]).strip()
    if " #" in value:
        value = value.split(" #", 1)[0]
    return value.strip()


def _read_env_token(env_text: str) -> str:
    """Return the last effective ACCESS_TOKEN= value, ignoring comments."""
    token = ""
    for line in env_text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _ACCESS_TOKEN_LINE.match(line)
        if not match:
            continue
        value = _parse_env_value(match.group(1))
        # A value starting with '#' is a placeholder, not a credential.
        token = "" if not value or value.startswith("#") else value
    return token


def _resolve_access_token(base_dir: Path) -> str:
    """Authentication is mandatory: never return an empty token."""
    env_path = base_dir / ".env"
    try:
        existing = (
            env_path.read_text(encoding="utf-8-sig", errors="replace")
            if env_path.exists()
            else ""
        )
    except OSError as exc:
        print(f"[auth] 警告：无法读取 {env_path}：{exc}")
        existing = ""

    token = _read_env_token(existing)
    if token:
        return token

    token = secrets.token_urlsafe(32)
    kept = [
        line for line in existing.splitlines() if not _ACCESS_TOKEN_LINE.match(line)
    ]
    if not any("generated on first start" in line for line in kept):
        kept.append("# generated on first start; keep it secret")
    kept.append(f"ACCESS_TOKEN={token}")
    try:
        env_path.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
        os.environ["ACCESS_TOKEN"] = token
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        print(f"[auth] ACCESS_TOKEN 未设置或为空，已生成并写入 {env_path}")
        print(f"[auth] 首次登录令牌：{token}")
    except OSError as exc:
        print(f"[auth] 警告：无法写入 {env_path}：{exc}")
        print(f"[auth] 本次使用临时令牌，重启后会变化：{token}")
    return token


@dataclass
class Settings:
    base_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    data_dir: Path = field(init=False)
    db_path: Path = field(init=False)
    artifact_dir: Path = field(init=False)
    workspace_dir: Path = field(init=False)

    # Network. 0.0.0.0 makes the console reachable from phones / other machines.
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    # If set, every /api/* call (except health) needs this bearer token.
    access_token: str = field(default_factory=lambda: os.getenv("ACCESS_TOKEN", ""))
    enable_api_docs: bool = field(
        default_factory=lambda: os.getenv("ENABLE_API_DOCS", "0").strip().lower()
        in ("1", "true", "yes", "on")
    )
    session_days: int = field(
        default_factory=lambda: max(1, _env_int("SESSION_DAYS", 180))
    )
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
        ]
    )

    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    llm_timeout: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT", 300))
    price_in_per_m: float = field(default_factory=lambda: _env_float("LLM_PRICE_IN", 0.15))
    price_out_per_m: float = field(default_factory=lambda: _env_float("LLM_PRICE_OUT", 0.60))

    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", ""))
    brave_api_key: str = field(default_factory=lambda: os.getenv("BRAVE_API_KEY", ""))
    searxng_url: str = field(default_factory=lambda: os.getenv("SEARXNG_URL", "").rstrip("/"))

    # Local tools. Agents may read/write files under workspace_dir and, when
    # shell_enabled is on, run commands there with the app's user permissions.
    shell_enabled: bool = field(
        default_factory=lambda: os.getenv("ENABLE_SHELL", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    shell_timeout: int = field(default_factory=lambda: _env_int("SHELL_TIMEOUT", 60))
    command_max_output: int = field(
        default_factory=lambda: _env_int("COMMAND_MAX_OUTPUT", 12000)
    )
    # When on, run_command waits for an explicit approval in the web UI.
    command_approval: bool = field(
        default_factory=lambda: os.getenv("COMMAND_APPROVAL", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    approval_timeout: int = field(
        default_factory=lambda: _env_int("APPROVAL_TIMEOUT", 300)
    )

    max_concurrency: int = field(default_factory=lambda: _env_int("MAX_CONCURRENCY", 4))
    max_steps_per_task: int = field(default_factory=lambda: _env_int("MAX_STEPS_PER_TASK", 10))
    # Derive a compressed prompt view once a task's conversation grows past
    # this size. The raw transcript stays in SQLite for replay/audit.
    context_compact_tokens: int = field(
        default_factory=lambda: _env_int("CONTEXT_COMPACT_TOKENS", 40000)
    )
    max_total_steps: int = field(default_factory=lambda: _env_int("MAX_TOTAL_STEPS", 60))
    max_tokens: int = field(default_factory=lambda: _env_int("MAX_TOKENS", 2_000_000))
    max_usd: float = field(default_factory=lambda: _env_float("MAX_USD", 1.0))
    deadline_sec: int = field(default_factory=lambda: _env_int("DEADLINE_SEC", 3600))

    def __post_init__(self) -> None:
        self.data_dir = Path(os.getenv("DATA_DIR", str(self.base_dir / "data")))
        self.db_path = self.data_dir / "cluster.db"
        self.artifact_dir = self.data_dir / "artifacts"
        self.workspace_dir = Path(
            os.getenv("WORKSPACE_DIR", str(self.data_dir / "workspace"))
        ).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if not self.access_token.strip():
            self.access_token = _resolve_access_token(self.base_dir)
        self.access_token = self.access_token.strip()
        if not self.access_token:
            raise RuntimeError("ACCESS_TOKEN 不能为空；请在 mvp/.env 中设置有效令牌")

    @property
    def mock_mode(self) -> bool:
        return not (self.llm_base_url and self.llm_api_key)


settings = Settings()
