from __future__ import annotations

import os
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


def _ensure_access_token(base_dir: Path) -> str:
    """Authentication is mandatory: generate a token on first start."""
    token = secrets.token_urlsafe(32)
    env_path = base_dir / ".env"
    try:
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        if "ACCESS_TOKEN" in existing:
            for line in existing.splitlines():
                line = line.strip()
                if line.startswith("ACCESS_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
            return token
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with env_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{prefix}# generated on first start; keep it secret\n"
                f"ACCESS_TOKEN={token}\n"
            )
        os.environ["ACCESS_TOKEN"] = token
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        print(f"[auth] 未设置 ACCESS_TOKEN，已生成并写入 {env_path}")
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
        if not self.access_token:
            self.access_token = _ensure_access_token(self.base_dir)

    @property
    def mock_mode(self) -> bool:
        return not (self.llm_base_url and self.llm_api_key)


settings = Settings()
