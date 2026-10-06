import httpx
import pytest

from app.auth import token_verifier
from app.auth.token_verifier import Verdict, VerifiedUser, verify_token

USER = {"userId": "U1", "fullName": "Jane Admin", "roleName": "ADMIN", "email": "jane@example.com"}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("USER_SERVICE_URL", "http://user-svc")
    token_verifier.reset_cache()
    yield
    token_verifier.reset_cache()


def _respond(monkeypatch, status, body=None, calls=None):
    def fake(base_url, token):
        if calls is not None:
            calls.append((base_url, token))
        return httpx.Response(status, json=body if body is not None else {})
    monkeypatch.setattr(token_verifier, "_call_user_service", fake)


def test_valid_token_returns_verified_user(monkeypatch):
    calls = []
    _respond(monkeypatch, 200, USER, calls)
    verdict, user = verify_token("tok", now=lambda: 1000.0)
    assert verdict == Verdict.VALID
    assert user == VerifiedUser(userId="U1", fullName="Jane Admin", roleName="ADMIN", email="jane@example.com")
    assert calls == [("http://user-svc", "tok")]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_rejected_statuses(monkeypatch, status):
    _respond(monkeypatch, status)
    assert verify_token("tok", now=lambda: 1000.0) == (Verdict.REJECTED, None)


def test_server_error_is_unavailable(monkeypatch):
    _respond(monkeypatch, 500)
    assert verify_token("tok", now=lambda: 1000.0) == (Verdict.UNAVAILABLE, None)


def test_result_is_cached_for_60_seconds(monkeypatch):
    calls = []
    _respond(monkeypatch, 200, USER, calls)
    verify_token("tok", now=lambda: 1000.0)
    verify_token("tok", now=lambda: 1059.0)
    assert len(calls) == 1
    verify_token("tok", now=lambda: 1061.0)
    assert len(calls) == 2


def test_network_error_backs_off_for_30_seconds(monkeypatch):
    calls = []

    def boom(base_url, token):
        calls.append(token)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(token_verifier, "_call_user_service", boom)
    assert verify_token("a", now=lambda: 1000.0) == (Verdict.UNAVAILABLE, None)
    assert verify_token("b", now=lambda: 1029.0) == (Verdict.UNAVAILABLE, None)
    assert calls == ["a"]
    verify_token("c", now=lambda: 1031.0)
    assert calls == ["a", "c"]


def test_unconfigured_when_base_url_missing(monkeypatch):
    monkeypatch.delenv("USER_SERVICE_URL", raising=False)
    _respond(monkeypatch, 200, USER)
    assert verify_token("tok", now=lambda: 1000.0) == (Verdict.UNCONFIGURED, None)
