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
        if query.table:  # the undone-dose lookup
            return [], 0, 0
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


def _dose(entity_id, method):
    return _row(table="PATIENT_MEDICATION_ADMINISTRATION", method=method, entity_id=entity_id,
                message=f"Medication administration {method.lower()}")


def test_undone_dose_is_hidden_but_other_doses_stay(monkeypatch):
    _as(monkeypatch, "CAREGIVER")
    lookups = []

    def fake(query, pageNo=0, pageSize=10, include_activity=False):
        if query.table:
            lookups.append((query.patient, query.table, query.action))
            return [_dose(5, "DELETE")], 1, 1
        # 5 was given then undone; 6 is the same slot marked again; 7 is another dose
        rows = [_dose(5, "CREATE"), _dose(5, "DELETE"), _dose(6, "CREATE"), _dose(7, "CREATE"), _row(entity_id=5)]
        return rows, len(rows), 1

    monkeypatch.setattr(logs_crud, "get_logs_by_param_patient", fake)
    r = client.get("/api/Logs/Patient/42/history", headers=AUTH)
    assert r.status_code == 200
    assert lookups == [("42", "PATIENT_MEDICATION_ADMINISTRATION", "delete")]
    shown = [(row["table"], row["entity_id"]) for row in r.json()["data"]]
    assert shown == [
        ("PATIENT_MEDICATION_ADMINISTRATION", 6),
        ("PATIENT_MEDICATION_ADMINISTRATION", 7),
        ("PATIENT_PRESCRIPTION", 5),
    ]


def test_admin_patient_logs_still_show_undone_dose(monkeypatch):
    _as(monkeypatch, "ADMIN")
    rows = [_dose(5, "CREATE"), _dose(5, "DELETE")]
    monkeypatch.setattr(logs_crud, "get_logs_by_param_patient",
                        lambda query, pageNo=0, pageSize=10, include_activity=False: (rows, 2, 1))
    r = client.get("/api/Logs/Patient?patient=42", headers=AUTH)
    assert r.status_code == 200
    assert [row["method"] for row in r.json()["data"]] == ["CREATE", "DELETE"]
