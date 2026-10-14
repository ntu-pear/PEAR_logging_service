import pytest
from elastic_transport import ConnectionError as ESConnectionError
from fastapi.testclient import TestClient

from app.auth import dependencies
from app.auth.token_verifier import Verdict, VerifiedUser
from app.crud import ops_logs_crud
from app.main import app
from app.schemas.ops_log import OpsLogDocument

client = TestClient(app)
AUTH = {"Authorization": "Bearer tok"}
ROUTES = {"/api/Logs/Errors": "error", "/api/Logs/Requests": "request", "/api/Logs/Auth": "auth"}
ROW = OpsLogDocument(
    timestamp="2026-10-14T01:02:03.000Z", ops_type="request", message="GET /x -> 200",
    service="patient", method="GET", path="/x", status=200, request_id="r1",
)


def _as(monkeypatch, role):
    user = VerifiedUser(userId="U1", fullName="X", roleName=role, email="x@x.com")
    monkeypatch.setattr(dependencies, "verify_token", lambda token: (Verdict.VALID, user))


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake(ops_type, query, page_no=0, page_size=20, sensitive=False):
        recorded.append({"ops_type": ops_type, "query": query, "page_no": page_no,
                         "page_size": page_size, "sensitive": sensitive})
        return [ROW], 1, 1

    monkeypatch.setattr(ops_logs_crud, "search_ops_logs", fake)
    return recorded


@pytest.mark.parametrize("path", ROUTES)
def test_no_token_is_401(path, calls):
    assert client.get(path).status_code == 401
    assert calls == []


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("role", ["SUPERVISOR", "DOCTOR", "CAREGIVER", "GUARDIAN", "GAME THERAPIST"])
def test_non_admin_is_403(monkeypatch, calls, path, role):
    _as(monkeypatch, role)
    assert client.get(path, headers=AUTH).status_code == 403
    assert calls == []


@pytest.mark.parametrize("path,ops_type", ROUTES.items())
def test_admin_gets_rows(monkeypatch, calls, path, ops_type):
    _as(monkeypatch, "ADMIN")
    response = client.get(path, headers=AUTH, params={"service": "patient", "request_id": "r1", "pageNo": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["request_id"] == "r1"
    assert body["pageNo"] == 2 and body["pageSize"] == 20
    assert calls[0]["ops_type"] == ops_type
    assert calls[0]["query"].service == "patient" and calls[0]["query"].request_id == "r1"


def test_sensitive_flag_reaches_only_the_requests_route(monkeypatch, calls):
    _as(monkeypatch, "ADMIN")
    client.get("/api/Logs/Requests", headers=AUTH, params={"sensitive": "true"})
    client.get("/api/Logs/Errors", headers=AUTH, params={"sensitive": "true"})
    assert [c["sensitive"] for c in calls] == [True, False]


def test_page_size_in_response_is_capped(monkeypatch, calls):
    _as(monkeypatch, "ADMIN")
    assert client.get("/api/Logs/Auth", headers=AUTH, params={"pageSize": 500}).json()["pageSize"] == 100


def test_overlong_ids_are_400(monkeypatch, calls):
    _as(monkeypatch, "ADMIN")
    response = client.get("/api/Logs/Requests", headers=AUTH, params={"request_id": "x" * 65})
    assert response.status_code == 400
    assert calls == []


def test_bad_filters_are_400_not_500(monkeypatch):
    _as(monkeypatch, "ADMIN")
    monkeypatch.setattr(ops_logs_crud.es_service, "search_documents",
                        lambda **kw: {"hits": {"total": {"value": 0}, "hits": []}})
    assert client.get("/api/Logs/Errors", headers=AUTH, params={"start_date": "yesterday"}).status_code == 400
    assert client.get("/api/Logs/Errors", headers=AUTH, params={"pageNo": 500, "pageSize": 100}).status_code == 400


def test_store_down_is_503_without_leaking_details(monkeypatch):
    _as(monkeypatch, "ADMIN")

    def down(**kwargs):
        raise ESConnectionError("es at 10.0.0.1:9200 refused, password=secret")

    monkeypatch.setattr(ops_logs_crud.es_service, "search_documents", down)
    response = client.get("/api/Logs/Errors", headers=AUTH)
    assert response.status_code == 503
    assert "secret" not in response.text and "10.0.0.1" not in response.text


def test_existing_audit_routes_are_unchanged(monkeypatch):
    from app.crud import logs_crud

    _as(monkeypatch, "ADMIN")
    seen = []
    monkeypatch.setattr(logs_crud, "get_logs_by_param_system",
                        lambda query, pageNo=0, pageSize=10: seen.append("system") or ([], 0, 0))
    assert client.get("/api/Logs/System", headers=AUTH).status_code == 200
    assert seen == ["system"]


@pytest.mark.parametrize("field", ["service", "level", "environment"])
def test_overlong_filter_strings_are_400(monkeypatch, calls, field):
    _as(monkeypatch, "ADMIN")
    response = client.get("/api/Logs/Errors", headers=AUTH, params={field: "x" * 65})
    assert response.status_code == 400
    assert calls == []


def test_store_failure_is_logged_but_not_leaked(monkeypatch, caplog):
    import logging

    _as(monkeypatch, "ADMIN")

    def down(**kwargs):
        raise ESConnectionError("refused")

    monkeypatch.setattr(ops_logs_crud.es_service, "search_documents", down)
    with caplog.at_level(logging.ERROR, logger="uvicorn"):
        assert client.get("/api/Logs/Errors", headers=AUTH).status_code == 503
    assert any("ops log search failed" in r.getMessage() and r.exc_info for r in caplog.records)


def test_unexpected_bugs_surface_as_500(monkeypatch):
    _as(monkeypatch, "ADMIN")

    def bug(**kwargs):
        raise KeyError("oops")

    monkeypatch.setattr(ops_logs_crud.es_service, "search_documents", bug)
    assert TestClient(app, raise_server_exceptions=False).get("/api/Logs/Errors", headers=AUTH).status_code == 500
