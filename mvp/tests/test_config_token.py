from pathlib import Path

import pytest

from mvp.app import config


def _write_env(tmp_path, text):
    env_path = tmp_path / ".env"
    env_path.write_text(text, encoding="utf-8")
    return env_path


def test_empty_token_line_is_regenerated(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "ACCESS_TOKEN=\n")
    token = config._resolve_access_token(tmp_path)
    assert token
    assert f"ACCESS_TOKEN={token}" in env_path.read_text(encoding="utf-8")


def test_commented_token_is_not_used(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "#ACCESS_TOKEN=old\n")
    token = config._resolve_access_token(tmp_path)
    assert token and token != "old"
    assert f"ACCESS_TOKEN={token}" in env_path.read_text(encoding="utf-8")


def test_valid_token_is_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    _write_env(tmp_path, "ACCESS_TOKEN=abc123\n")
    assert config._resolve_access_token(tmp_path) == "abc123"


def test_quoted_token_is_unwrapped(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    _write_env(tmp_path, 'ACCESS_TOKEN="abc123"\n')
    assert config._resolve_access_token(tmp_path) == "abc123"


def test_no_token_in_file_generates_and_writes(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "")
    token = config._resolve_access_token(tmp_path)
    assert token
    assert token in env_path.read_text(encoding="utf-8")


def test_other_env_entries_are_preserved(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "OTHER_KEY=keep-me\nACCESS_TOKEN=\n")
    token = config._resolve_access_token(tmp_path)
    text = env_path.read_text(encoding="utf-8")
    assert "OTHER_KEY=keep-me" in text
    assert f"ACCESS_TOKEN={token}" in text
    assert "ACCESS_TOKEN=\n" not in text


def test_settings_regenerates_empty_env_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN", "")
    _write_env(tmp_path, "ACCESS_TOKEN=\n")
    settings = config.Settings(base_dir=tmp_path)
    assert settings.access_token
    assert settings.access_token in (tmp_path / ".env").read_text(encoding="utf-8")


def test_settings_regenerates_whitespace_env_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN", "   ")
    _write_env(tmp_path, "ACCESS_TOKEN=\n")
    settings = config.Settings(base_dir=tmp_path)
    assert settings.access_token
    assert settings.access_token != "   "


def test_settings_reuses_valid_file_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN", "")
    _write_env(tmp_path, "ACCESS_TOKEN=abc123\n")
    settings = config.Settings(base_dir=tmp_path)
    assert settings.access_token == "abc123"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ACCESS_TOKEN= # placeholder\n", None),
        ("ACCESS_TOKEN=#\n", None),
        ("ACCESS_TOKEN=abc # note\n", "abc"),
        ('ACCESS_TOKEN="abc" # note\n', "abc"),
        ('ACCESS_TOKEN="   "\n', None),
        ('ACCESS_TOKEN=" abc "\n', "abc"),
        ("ACCESS_TOKEN=abc123\r\n", "abc123"),
        ("ACCESS_TOKEN=old\nACCESS_TOKEN=new\n", "new"),
        ("export ACCESS_TOKEN=abc123\n", "abc123"),
        ("\ufeffACCESS_TOKEN=abc123\n", "abc123"),
        ("access_token=abc123\n", None),
    ],
)
def test_env_value_parsing(tmp_path, monkeypatch, text, expected):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, text)
    token = config._resolve_access_token(tmp_path)
    body = env_path.read_text(encoding="utf-8")
    if expected is None:
        assert token
        assert token not in ("abc", "#", "# placeholder")
        assert f"ACCESS_TOKEN={token}" in body
    else:
        assert token == expected


def test_gbk_env_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_bytes("ACCESS_TOKEN=abc123\n# 中文注释\n".encode("gbk"))
    assert config._resolve_access_token(tmp_path) == "abc123"


def test_rewrite_keeps_one_token_line(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    env_path = _write_env(tmp_path, "OTHER=1\nACCESS_TOKEN= # todo\n")
    first = config._resolve_access_token(tmp_path)
    second = config._resolve_access_token(tmp_path)
    assert first == second
    lines = [
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("ACCESS_TOKEN=")
    ]
    assert len(lines) == 1
    assert "OTHER=1" in env_path.read_text(encoding="utf-8")


def test_write_failure_returns_non_empty_token(tmp_path, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    _write_env(tmp_path, "ACCESS_TOKEN=\n")

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    assert config._resolve_access_token(tmp_path)
