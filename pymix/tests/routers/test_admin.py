import pytest
from fastapi import HTTPException

from pymix.routers.admin import require_admin_token


def test_require_admin_token_fails_closed_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("PYMIX_ADMIN_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        require_admin_token(x_admin_token="anything")

    assert exc_info.value.status_code == 503


def test_require_admin_token_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("PYMIX_ADMIN_TOKEN", "secret-token")

    with pytest.raises(HTTPException) as exc_info:
        require_admin_token(x_admin_token=None)

    assert exc_info.value.status_code == 401


def test_require_admin_token_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("PYMIX_ADMIN_TOKEN", "secret-token")

    with pytest.raises(HTTPException) as exc_info:
        require_admin_token(x_admin_token="wrong-token")

    assert exc_info.value.status_code == 401


def test_require_admin_token_accepts_correct_token(monkeypatch):
    monkeypatch.setenv("PYMIX_ADMIN_TOKEN", "secret-token")

    # must not raise
    require_admin_token(x_admin_token="secret-token")
