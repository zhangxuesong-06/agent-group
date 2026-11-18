import asyncio
import copy
import json
import os
import shlex
import subprocess
import sys

import pytest

from mvp.app import tools
from mvp.app.config import settings
from mvp.worker import agent


@pytest.mark.parametrize("module", [tools, agent], ids=["server", "worker"])
def test_child_environment_filters_case_insensitively_without_mutating_source(module):
    base = {
        "Path": "/bin", "HOME": "/home/test", "TEMP": "/tmp",
        "access_token": "secret", "LLM_API_KEY": "secret",
        "TAVILY_API_KEY": "secret", "BRAVE_API_KEY": "secret",
        "SEARXNG_URL": "secret", "CLUSTER_WORKER_TOKEN": "secret",
        "SERVICE_TOKEN": "secret", "SERVICE_SECRET": "secret",
        "SERVICE_PASSWORD": "secret", "OTHER_API_KEY": "secret",
        "SERVICE_CREDENTIALS": "secret", "DATABASE_URL": "secret",
        "AWS_SECRET_ACCESS_KEY": "secret", "AWS_SESSION_TOKEN": "secret",
        "HTTP_PROXY": "secret", "https_proxy": "secret", "ALL_PROXY": "secret",
    }
    before = dict(base)
    assert module._child_env(base) == {"Path": "/bin", "HOME": "/home/test", "TEMP": "/tmp"}
    assert base == before


def test_server_and_worker_share_the_same_policy():
    assert tools._SECRET_ENV_NAMES == agent._SECRET_ENV_NAMES
    assert tools._SECRET_ENV_SUFFIXES == agent._SECRET_ENV_SUFFIXES


@pytest.mark.parametrize("target", ["server", "worker"])
def test_real_shell_cannot_read_service_credentials(target, tmp_workspace, monkeypatch):
    keys = ["ACCESS_TOKEN", "LLM_API_KEY", "CLUSTER_WORKER_TOKEN", "CUSTOM_SECRET", "HTTPS_PROXY"]
    for key in keys:
        monkeypatch.setenv(key, "test-secret-never-inherit")
    probe = tmp_workspace / "probe.py"
    probe.write_text(
        "import json, os\n"
        f"keys = {keys!r}\n"
        "print(json.dumps({'leaked': [k for k in keys if os.getenv(k)], 'path': bool(os.getenv('PATH')), 'cwd': os.getcwd()}))\n",
        encoding="utf-8",
    )
    argv = [sys.executable, str(probe)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    if target == "server":
        config = copy.copy(settings)
        config.shell_enabled = True
        config.workspace_dir = tmp_workspace
        result = json.loads(asyncio.run(tools._run_command({"command": command}, {"settings": config})))
    else:
        worker = agent.Worker([tmp_workspace])
        result = asyncio.run(worker.run_command(str(tmp_workspace), {"command": command}))
    assert result["exit_code"] == 0, result["output"]
    data = json.loads(result["output"])
    assert data["leaked"] == []
    assert data["path"] is True
    assert os.path.samefile(data["cwd"], tmp_workspace)
