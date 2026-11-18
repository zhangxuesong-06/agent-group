import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("flag, enabled", [("0", False), ("1", True), ("true", True)])
def test_api_docs_visibility_and_authentication(tmp_path, flag, enabled):
    env = {
        **os.environ,
        "ENABLE_API_DOCS": flag,
        "ACCESS_TOKEN": "test-token",
        "DATA_DIR": str(tmp_path / "data"),
        "WORKSPACE_DIR": str(tmp_path / "workspace"),
    }
    script = '''
import json
from fastapi.testclient import TestClient
from mvp.app.main import app
paths = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]
with TestClient(app) as client:
    anonymous = [client.get(path).status_code for path in paths]
    bearer = [client.get(path, headers={"Authorization": "Bearer test-token"}).status_code for path in paths]
    client.post("/api/auth/login", json={"token": "test-token"})
    cookie = [client.get(path).status_code for path in paths]
    schema = client.get("/openapi.json").json()
print(json.dumps({"anonymous": anonymous, "bearer": bearer, "cookie": cookie, "schema": schema}))
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data["anonymous"] == [401 if enabled else 404] * 4
    assert data["bearer"] == [200 if enabled else 404] * 4
    assert data["cookie"] == [200 if enabled else 404] * 4
    if enabled:
        assert "/api/runs" in data["schema"]["paths"]
