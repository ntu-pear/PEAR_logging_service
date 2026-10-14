import pytest
from fastapi.testclient import TestClient

from app.auth import dependencies
from app.auth.token_verifier import Verdict, VerifiedUser
from app.crud import logs_crud, ops_logs_crud
from app.main import app
from app.schemas.log_document import LogDocument
from app.schemas.ops_log import OpsLogDocument
from app.services import log_flags

client = TestClient(app)
AUTH = {"Authorization": "Bearer tok"}

CLINICAL = "Penicillin allergy for S1234567A"


def _as(monkeypatch, role):
    user = VerifiedUser(userId="U1", fullName="X", roleName=role, email="x@x.com")
    monkeypatch.setattr(dependencies, "verify_token", lambda token: (Verdict.VALID, user))


def _audit_row(**overrides):
    data = dict(timestamp="2026-10-01T23:30:00", method="DELETE", table="PatientAllergyMapping",
                user="U9", user_full_name="Lim Mei Ling", patient_id=42, message=CLINICAL,
                original_data={"allergy": CLINICAL, "nric": "S1234567A"})
    data.update(overrides)
    return LogDocument(**data)


def _ops_row(**overrides):
    data = dict(timestamp="2026-10-14T01:02:03.000Z", ops_type="auth", message="login failed",
                service="user", event="login_failed", user_id="U5", request_id="r1")
    data.update(overrides)
    return OpsLogDocument(**data)


def _hot(monkeypatch, user="U9"):
    """One hot bulk-delete bucket for `user` covering the row timestamp (2026-10-01 15:30Z)."""
    ms = 1790868600000  # 2026-10-01T15:30:00Z
    ms -= ms % 600000

    def fake(**kwargs):
        return {"aggregations": {"windows": {"buckets": [
            {"key": ms, "by_key": {"buckets": [{"key": user, "doc_count": 12}]}}]}}}

    monkeypatch.setattr(log_flags, "_search", fake)


def _boom(monkeypatch):
    def fake(**kwargs):
        raise RuntimeError("es down")

    monkeypatch.setattr(log_flags, "_search", fake)


AUDIT_ROUTES = [
    ("/api/Logs/Patient", "get_logs_by_param_patient", "SUPERVISOR"),
    ("/api/Logs/Activity", "get_logs_by_param_activity", "SUPERVISOR"),
    ("/api/Logs/System", "get_logs_by_param_system", "ADMIN"),
    ("/api/Logs/Patient/42/history", "get_logs_by_param_patient", "DOCTOR"),
]


@pytest.fixture
def audit_rows(monkeypatch):
    seen = {}

    def make(name):
        def fake(query, pageNo=0, pageSize=10, include_activity=False):
            seen["query"] = query
            return [_audit_row()], 1, 1
        monkeypatch.setattr(logs_crud, name, fake)

    for _, name, _ in AUDIT_ROUTES:
        make(name)
    return seen


@pytest.mark.parametrize("path,fn,role", AUDIT_ROUTES)
def test_audit_routes_return_flags(monkeypatch, audit_rows, path, fn, role):
    _as(monkeypatch, role)
    _hot(monkeypatch)
    response = client.get(path, headers=AUTH)
    assert response.status_code == 200
    codes = {f["code"] for f in response.json()["data"][0]["flags"]}
    assert {"bulk_delete", "off_hours", "sensitive_change"} <= codes
    bulk = [f for f in response.json()["data"][0]["flags"] if f["code"] == "bulk_delete"][0]
    assert bulk["reason"] == "12 deletes by Lim Mei Ling in 10 min"
    assert bulk["severity"] == "critical"


@pytest.mark.parametrize("path,fn,role", AUDIT_ROUTES)
def test_flag_failure_leaves_audit_page_working(monkeypatch, audit_rows, path, fn, role):
    _as(monkeypatch, role)
    _boom(monkeypatch)
    response = client.get(path, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["totalRecords"] == 1
    assert response.json()["data"][0]["flags"] == []


def test_admin_flags_carry_no_clinical_values(monkeypatch, audit_rows):
    _as(monkeypatch, "ADMIN")
    _hot(monkeypatch)
    response = client.get("/api/Logs/Patient", headers=AUTH)
    row = response.json()["data"][0]
    assert row["message"] == "" and row["original_data"] is None
    assert row["flags"], "ADMIN still sees flags"
    text = " ".join(f["reason"] for f in row["flags"])
    assert "Penicillin" not in text and "S1234567A" not in text and CLINICAL not in text


def test_audit_flags_use_the_request_time_range(monkeypatch, audit_rows):
    _as(monkeypatch, "SUPERVISOR")
    seen = {}

    def fake(**kwargs):
        seen["body"] = kwargs["body"]
        return {}

    monkeypatch.setattr(log_flags, "_search", fake)
    client.get("/api/Logs/Patient", headers=AUTH,
               params={"start_date": "2026-10-01", "end_date": "2026-10-02"})
    rng = [c for c in seen["body"]["query"]["bool"]["must"] if "range" in c][0]["range"]["@timestamp"]
    assert rng == {"gte": "2026-09-30T16:00:00", "lte": "2026-10-02T15:59:59"}


@pytest.fixture
def ops_rows(monkeypatch):
    seen = {}

    def fake(ops_type, query, page_no=0, page_size=20, sensitive=False):
        seen["ops_type"] = ops_type
        return [_ops_row()], 1, 1

    monkeypatch.setattr(ops_logs_crud, "search_ops_logs", fake)
    return seen


@pytest.mark.parametrize("path", ["/api/Logs/Errors", "/api/Logs/Requests", "/api/Logs/Auth"])
def test_ops_routes_return_flags(monkeypatch, ops_rows, path):
    _as(monkeypatch, "ADMIN")
    ms = 1791939600000  # 2026-10-14T01:00:00Z
    ms -= ms % 600000

    def fake(**kwargs):
        return {"aggregations": {"windows": {"buckets": [
            {"key": ms, "by_key": {"buckets": [{"key": "U5", "doc_count": 7}]}}]}}}

    monkeypatch.setattr(log_flags, "_search", fake)
    response = client.get(path, headers=AUTH)
    assert response.status_code == 200
    flags = response.json()["data"][0]["flags"]
    assert [f["code"] for f in flags] == ["failed_login_burst"]
    assert flags[0]["reason"] == "7 failed logins for U5 in 10 min"


def test_ops_sensitive_preset_returns_flags(monkeypatch, ops_rows):
    _as(monkeypatch, "ADMIN")
    ops_rows_row = _ops_row(ops_type="request", event=None, query="mask=false", user_id="U5")
    monkeypatch.setattr(ops_logs_crud, "search_ops_logs",
                        lambda *a, **k: ([ops_rows_row], 1, 1))
    monkeypatch.setattr(log_flags, "_search", lambda **kw: {})
    response = client.get("/api/Logs/Requests", headers=AUTH, params={"sensitive": "true"})
    assert [f["code"] for f in response.json()["data"][0]["flags"]] == ["mass_access"]


def test_ops_flags_use_the_request_time_range(monkeypatch, ops_rows):
    _as(monkeypatch, "ADMIN")
    seen = {}

    def fake(**kwargs):
        seen["body"] = kwargs["body"]
        return {}

    monkeypatch.setattr(log_flags, "_search", fake)
    client.get("/api/Logs/Auth", headers=AUTH,
               params={"start_date": "2026-10-13", "end_date": "2026-10-14"})
    rng = [c for c in seen["body"]["query"]["bool"]["must"] if "range" in c][0]["range"]["@timestamp"]
    assert rng["gte"] == "2026-10-12T16:00:00"
    assert rng["lte"].startswith("2026-10-14T15:59:59")


def test_ops_flag_failure_leaves_page_working(monkeypatch, ops_rows):
    _as(monkeypatch, "ADMIN")
    _boom(monkeypatch)
    response = client.get("/api/Logs/Auth", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["data"][0]["flags"] == []
