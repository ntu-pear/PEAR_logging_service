import pytest
from fastapi.testclient import TestClient

from app.auth import dependencies
from app.auth.token_verifier import Verdict, VerifiedUser
from app.crud import logs_crud
from app.main import app
from app.schemas.log_document import LogDocument

ROW = LogDocument(
    timestamp="2026-10-01T10:00:00", method="UPDATE", table="PATIENT_PRESCRIPTION",
    user="U9", user_full_name="Dr Lim", patient_id=123, patient_full_name="John Tan",
    entity_id=7, original_data={"Dosage": "1"}, updated_data={"Dosage": "2"},
    message="Updated prescription: Panadol for John Tan", role="DOCTOR",
)
client = TestClient(app)


def _as(monkeypatch, role):
    user = VerifiedUser(userId="U1", fullName="X", roleName=role, email="x@x.com")
    monkeypatch.setattr(dependencies, "verify_token", lambda token: (Verdict.VALID, user))


@pytest.fixture(autouse=True)
def _fake_crud(monkeypatch):
    for name in ("get_logs_by_param_patient", "get_logs_by_param_activity",
                 "get_logs_by_param_user", "get_logs_by_param_system"):
        monkeypatch.setattr(logs_crud, name, lambda query, pageNo=0, pageSize=10: ([ROW], 1, 1))


AUTH = {"Authorization": "Bearer tok"}


@pytest.mark.parametrize("path", ["/api/Logs/Patient", "/api/Logs/Activity", "/api/Logs/User", "/api/Logs/System"])
def test_no_token_is_401(path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path,role,expected", [
    ("/api/Logs/Patient", "SUPERVISOR", 200), ("/api/Logs/Patient", "ADMIN", 200), ("/api/Logs/Patient", "DOCTOR", 403),
    ("/api/Logs/Activity", "SUPERVISOR", 200), ("/api/Logs/Activity", "ADMIN", 200), ("/api/Logs/Activity", "CAREGIVER", 403),
    ("/api/Logs/User", "ADMIN", 200), ("/api/Logs/User", "SUPERVISOR", 403),
    ("/api/Logs/System", "ADMIN", 200), ("/api/Logs/System", "SUPERVISOR", 403),
])
def test_role_matrix(monkeypatch, path, role, expected):
    _as(monkeypatch, role)
    assert client.get(path, headers=AUTH).status_code == expected


@pytest.mark.parametrize("path", ["/api/Logs/Patient", "/api/Logs/Activity"])
def test_admin_gets_metadata_only(monkeypatch, path):
    _as(monkeypatch, "ADMIN")
    row = client.get(path, headers=AUTH).json()["data"][0]
    assert row["message"] == ""
    assert row["original_data"] is None and row["updated_data"] is None
    assert row["patient_full_name"] is None
    assert row["patient_id"] == 123 and row["user_full_name"] == "Dr Lim" and row["table"] == "PATIENT_PRESCRIPTION"


def test_supervisor_gets_full_rows(monkeypatch):
    _as(monkeypatch, "SUPERVISOR")
    row = client.get("/api/Logs/Patient", headers=AUTH).json()["data"][0]
    assert row["message"].startswith("Updated prescription")
    assert row["updated_data"] == {"Dosage": "2"}
