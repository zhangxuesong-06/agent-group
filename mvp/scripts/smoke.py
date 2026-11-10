"""End-to-end smoke test against a running server.

    python scripts/smoke.py [base_url]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def _access_token() -> str:
    token = os.getenv("ACCESS_TOKEN", "").strip()
    if token:
        return token
    env_path = Path(__file__).resolve().parents[1] / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


TOKEN = _access_token()
GOAL = "用中文测试：调研 AI 集群，分析风险，给出方案"


def main() -> int:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    client = httpx.Client(base_url=BASE, timeout=30, headers=headers)
    assert client.get("/api/health").json()["ok"] is True
    run_id = client.post(
        "/api/runs",
        json={"goal": GOAL, "budget": {"max_steps": 40, "max_usd": 1, "deadline_sec": 120}},
    ).json()["run_id"]
    for _ in range(200):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.1)
    assert run["status"] == "succeeded", run.get("error")
    assert run["goal"] == GOAL, run["goal"]
    assert [t["title"] for t in run["tasks"]] == ["背景调研", "要点分析", "方案撰写", "质量审校"]
    assert [t["assignee"] for t in run["tasks"]] == ["researcher", "analyst", "writer", "reviewer"]
    assert "集群执行完成" in run["result"]

    sse = client.get(f"/api/runs/{run_id}/events").text
    assert "event: end" in sse
    types = {
        json.loads(line[6:])["type"]
        for line in sse.splitlines()
        if line.startswith("data: ") and '"type"' in line
    }
    for expected in ("run.created", "run.planned", "task.completed", "run.completed", "tool.called"):
        assert expected in types, expected

    # DAG: downstream task must depend on the upstream one.
    analysis = run["tasks"][1]
    assert analysis["depends_on"] == [run["tasks"][0]["id"]]

    print(json.dumps({"ok": True, "run_id": run_id, "tokens": run["usage"]["tokens"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
