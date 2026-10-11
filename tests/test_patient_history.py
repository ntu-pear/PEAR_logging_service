import pytest
from fastapi.testclient import TestClient

from app.auth import dependencies
from app.auth.token_verifier import Verdict, VerifiedUser
from app.crud import logs_crud
from app.main import app
from app.schemas.log_document import LogDocument

client = TestClient(app)
AUTH = {"Authorization": "Bearer tok"}


def _row(**overrides):
    data = dict(timestamp="2026-10-01T10:00:00", method="UPDATE", table="PATIENT_PRESCRIPTION",
                user="U9", user_full_name="Dr Lim", patient_id=42, message="Updated prescription")
    data.update(overrides)
    return LogDocument(**data)


def _as(monkeypatch, role):
    user = VerifiedUser(userId="U1", fullName="X", roleName=role, email="x@x.com")
    monkeypatch.setattr(dependencies, "verify_token", lambda token: (Verdict.VALID, user))


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def fake(query, pageNo=0, pageSize=10, include_activity=False):
        seen["include_activity"] = include_activity
        seen["patient"] = query.patient
        seen["pageSize"] = pageSize
        rows = [_row(), _row(table="REF_PATIENT"), _row(is_system_config=True)]
        return rows, 3, 1

    monkeypatch.setattr(logs_crud, "get_logs_by_param_patient", fake)
    return seen


def test_no_token_is_401():
    assert client.get("/api/Logs/Patient/42/history").status_code == 401


@pytest.mark.parametrize("role", ["GUARDIAN", "ADMIN"])
def test_guardian_and_admin_forbidden(monkeypatch, captured, role):
    _as(monkeypatch, role)
    assert client.get("/api/Logs/Patient/42/history", headers=AUTH).status_code == 403


@pytest.mark.parametrize("role", ["SUPERVISOR", "DOCTOR", "CAREGIVER", "GAME THERAPIST"])
def test_care_roles_get_patient_scoped_history(monkeypatch, captured, role):
    _as(monkeypatch, role)
    r = client.get("/api/Logs/Patient/42/history?pageSize=500", headers=AUTH)
    assert r.status_code == 200
    assert captured["patient"] == "42"
    assert captured["pageSize"] == 100
    tables = [row["table"] for row in r.json()["data"]]
    assert tables == ["PATIENT_PRESCRIPTION"]


def test_history_route_includes_activity(monkeypatch, captured):
    _as(monkeypatch, "DOCTOR")
    assert client.get("/api/Logs/Patient/42/history", headers=AUTH).status_code == 200
    assert captured["include_activity"] is True
