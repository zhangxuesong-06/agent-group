from fastapi.testclient import TestClient
import pytest

from mvp.app import main as app_main
from mvp.app.main import app


def test_login_wrong_token_is_401(client):
    response = client.post(
        "/api/auth/login",
        json={"token": "wrong"},
        headers={"x-forwarded-for": "10.10.0.1"},
    )
    assert response.status_code == 401


def test_login_rate_limited_after_five_failures(client):
    headers = {"x-forwarded-for": "10.10.0.2"}
    for _ in range(5):
        client.post("/api/auth/login", json={"token": "wrong"}, headers=headers)
    response = client.post(
        "/api/auth/login", json={"token": "wrong"}, headers=headers
    )
    assert response.status_code == 429


def test_post_without_csrf_header_is_403(auth_client):
    response = auth_client.post("/api/settings", json={})
    assert response.status_code == 403


def test_post_with_csrf_header_is_allowed(auth_client):
    response = auth_client.post(
        "/api/settings",
        json={},
        headers={"x-requested-with": "XMLHttpRequest"},
    )
    assert response.status_code == 200


def test_revoked_session_is_401():
    # A dedicated client keeps this test from leaving cookies on the shared one.
    client = TestClient(app)
    first = client.post("/api/auth/login", json={"token": "test-token"})
    assert first.status_code == 200
    sessions = client.get("/api/auth/sessions").json()["sessions"]
    current = next(item for item in sessions if item["current"])
    response = client.delete(
        f"/api/auth/sessions/{current['id']}",
        headers={"x-requested-with": "XMLHttpRequest"},
    )
    assert response.status_code == 200
    assert client.get("/api/meta").status_code == 401


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer ", "Bearer\t", "bearer   "],
)
def test_empty_bearer_is_rejected(client, header):
    response = client.get("/api/meta", headers={"Authorization": header})
    assert response.status_code == 401


def test_non_bearer_authorization_falls_back_to_cookie(auth_client):
    response = auth_client.get(
        "/api/meta", headers={"Authorization": "Basic abc"}
    )
    assert response.status_code == 200


def test_bearer_helper_rejects_empty_expected():
    assert app_main._bearer_ok("Bearer ", "") is False
    assert app_main._bearer_ok("Bearer test-token", "test-token") is True
    assert app_main._bearer_ok("Bearer 中文", "test-token") is False


def test_non_ascii_login_is_401_not_500(client):
    response = client.post(
        "/api/auth/login",
        json={"token": "中文"},
        headers={"x-forwarded-for": "10.10.0.3"},
    )
    assert response.status_code == 401


def test_empty_server_token_never_authorizes(client, monkeypatch):
    monkeypatch.setattr(app_main.settings, "access_token", "")
    response = client.get("/api/meta", headers={"Authorization": "Bearer "})
    assert response.status_code == 401
