from mvp.app.config import settings


def test_settings_use_temp_data_dir():
    assert "cluster-test-" in str(settings.data_dir)
    assert settings.workspace_dir.exists()


def test_health_is_public(client):
    assert client.get("/api/health").status_code == 200


def test_meta_requires_auth(client):
    assert client.get("/api/meta").status_code == 401


def test_login_then_meta_ok(auth_client):
    assert auth_client.get("/api/meta").status_code == 200
