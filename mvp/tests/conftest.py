import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Every test run gets its own data directory. This must happen before the app
# modules are imported, otherwise config.py creates and uses the real database.
_TMP = Path(tempfile.mkdtemp(prefix="cluster-test-"))
os.environ["ACCESS_TOKEN"] = "test-token"
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["WORKSPACE_DIR"] = str(_TMP / "workspace")
os.environ["ENABLE_SHELL"] = "0"
os.environ["COMMAND_APPROVAL"] = "0"
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["CORS_ORIGINS"] = ""

import pytest
from fastapi.testclient import TestClient

from mvp.app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def auth_client():
    with TestClient(app) as test_client:
        response = test_client.post("/api/auth/login", json={"token": "test-token"})
        assert response.status_code == 200
        yield test_client


@pytest.fixture()
def tmp_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


@pytest.fixture(scope="session")
def orchestrator():
    from mvp.app.main import orchestrator as instance

    return instance


@pytest.fixture()
def store(tmp_path):
    from mvp.app.store import Store

    return Store(str(tmp_path / "test.db"))
