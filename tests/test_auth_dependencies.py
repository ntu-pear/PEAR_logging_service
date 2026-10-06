import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import dependencies
from app.auth.dependencies import require_roles
from app.auth.token_verifier import Verdict, VerifiedUser

ADMIN = VerifiedUser(userId="U1", fullName="Jane Admin", roleName="ADMIN", email="j@x.com")


def _app():
    app = FastAPI()

    @app.get("/admin-only")
    def admin_only(user: VerifiedUser = Depends(require_roles("ADMIN"))):
        return {"userId": user.userId}

    return TestClient(app)


def _verdict(monkeypatch, verdict, user=None):
    monkeypatch.setattr(dependencies, "verify_token", lambda token: (verdict, user))


def test_allowed_role_passes(monkeypatch):
    _verdict(monkeypatch, Verdict.VALID, ADMIN)
    r = _app().get("/admin-only", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.json() == {"userId": "U1"}


def test_wrong_role_is_403(monkeypatch):
    _verdict(monkeypatch, Verdict.VALID, VerifiedUser("U2", "Sam", "SUPERVISOR", "s@x.com"))
    assert _app().get("/admin-only", headers={"Authorization": "Bearer tok"}).status_code == 403


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer "}, {"Authorization": "Basic abc"}])
def test_missing_or_malformed_header_is_401(monkeypatch, headers):
    _verdict(monkeypatch, Verdict.VALID, ADMIN)
    assert _app().get("/admin-only", headers=headers).status_code == 401


def test_rejected_token_is_401(monkeypatch):
    _verdict(monkeypatch, Verdict.REJECTED)
    assert _app().get("/admin-only", headers={"Authorization": "Bearer tok"}).status_code == 401


@pytest.mark.parametrize("verdict", [Verdict.UNAVAILABLE, Verdict.UNCONFIGURED])
def test_unavailable_is_503(monkeypatch, verdict):
    _verdict(monkeypatch, verdict)
    assert _app().get("/admin-only", headers={"Authorization": "Bearer tok"}).status_code == 503
