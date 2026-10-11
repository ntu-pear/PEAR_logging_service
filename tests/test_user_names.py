import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import dependencies
from app.auth.token_verifier import Verdict, VerifiedUser
from app.crud import logs_crud
from app.main import app
from app.schemas.log_document import LogDocument
from app.services import user_names

client = TestClient(app)


def _row(original=None, updated=None):
    return LogDocument(timestamp="t", method="UPDATE", table="T", user="U9", user_full_name="X",
                       message="m", original_data=original, updated_data=updated)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("USER_SERVICE_URL", "http://users")
    user_names.reset_cache()
    yield
    user_names.reset_cache()


@pytest.fixture
def calls(monkeypatch):
    seen = []
    directory = {"u1": "Alice Tan", "u2": "Bob Lee"}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append({"url": url, "ids": list(json["ids"]), "headers": headers, "timeout": timeout})
        names = {i: directory[i] for i in json["ids"] if i in directory}
        return httpx.Response(200, json={"names": names}, request=httpx.Request("POST", url))

    monkeypatch.setattr(user_names.httpx, "post", fake_post)
    return seen


def test_adds_names_for_snake_and_camel_keys(calls):
    rows = [_row(original={"created_by_id": "u1", "Name": "x"},
                 updated={"ModifiedById": "u2", "doctorId": "u1", "centre_id": "u1"})]
    out = user_names.resolve_user_names(rows, "tok")
    assert out[0].original_data == {"created_by_id": "u1", "created_by_name": "Alice Tan", "Name": "x"}
    assert out[0].updated_data == {"ModifiedById": "u2", "ModifiedByName": "Bob Lee",
                                   "doctorId": "u1", "doctorName": "Alice Tan", "centre_id": "u1"}
    assert len(calls) == 1
    assert calls[0]["url"] == "http://users/api/v1/user/names"
    assert sorted(calls[0]["ids"]) == ["u1", "u2"]
    assert calls[0]["headers"] == {"Authorization": "Bearer tok"}
    assert calls[0]["timeout"] == 2.0


def test_cached_ids_not_requested_again(calls):
    user_names.resolve_user_names([_row(updated={"created_by_id": "u1"})], "tok")
    out = user_names.resolve_user_names([_row(updated={"created_by_id": "u1"})], "tok")
    assert out[0].updated_data["created_by_name"] == "Alice Tan"
    assert len(calls) == 1


def test_existing_key_not_overwritten(calls):
    out = user_names.resolve_user_names([_row(updated={"created_by_id": "u1", "created_by_name": "Keep"})], "tok")
    assert out[0].updated_data["created_by_name"] == "Keep"


def test_unknown_id_adds_nothing(calls):
    out = user_names.resolve_user_names([_row(updated={"created_by_id": "zzz"})], "tok")
    assert out[0].updated_data == {"created_by_id": "zzz"}


def test_dict_rows_and_skips_non_dict(calls):
    rows = [{"original_data": None, "updated_data": {"created_by_id": "u1"}}, {"original_data": "str"}]
    out = user_names.resolve_user_names(rows, "tok")
    assert out[0]["updated_data"]["created_by_name"] == "Alice Tan"
    assert out[1] == {"original_data": "str"}


def test_chunks_of_100(monkeypatch):
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append(len(json["ids"]))
        return httpx.Response(200, json={"names": {}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(user_names.httpx, "post", fake_post)
    rows = [_row(updated={"created_by_id": "id%d" % i}) for i in range(150)]
    user_names.resolve_user_names(rows, "tok")
    assert seen == [100, 50]


@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"detail": "nf"}),
    httpx.Response(500),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"unexpected": 1}),
])
def test_bad_responses_leave_logs_unchanged(monkeypatch, response):
    monkeypatch.setattr(user_names.httpx, "post", lambda *a, **k: response)
    out = user_names.resolve_user_names([_row(updated={"created_by_id": "u1"})], "tok")
    assert out[0].updated_data == {"created_by_id": "u1"}


def test_network_error_leaves_logs_unchanged(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(user_names.httpx, "post", boom)
    out = user_names.resolve_user_names([_row(updated={"created_by_id": "u1"})], "tok")
    assert out[0].updated_data == {"created_by_id": "u1"}


def test_unconfigured_url_is_noop(monkeypatch):
    monkeypatch.delenv("USER_SERVICE_URL")
    out = user_names.resolve_user_names([_row(updated={"created_by_id": "u1"})], "tok")
    assert out[0].updated_data == {"created_by_id": "u1"}


def test_cache_bounded(monkeypatch, calls):
    monkeypatch.setattr(user_names, "MAX_CACHE_ENTRIES", 2)
    user_names.resolve_user_names([_row(updated={"created_by_id": "u1", "modified_by_id": "u2", "doctorid": "u3"})], "tok")
    assert len(user_names._cache) <= 2


def test_cache_expires(monkeypatch, calls):
    now = [0.0]
    monkeypatch.setattr(user_names.time, "monotonic", lambda: now[0])
    user_names.resolve_user_names([_row(updated={"created_by_id": "u1"})], "tok")
    now[0] = 601.0
    user_names.resolve_user_names([_row(updated={"created_by_id": "u1"})], "tok")
    assert len(calls) == 2


def _as(monkeypatch, role):
    user = VerifiedUser(userId="U1", fullName="X", roleName=role, email="x@x.com")
    monkeypatch.setattr(dependencies, "verify_token", lambda token: (Verdict.VALID, user))


def test_patient_route_shows_resolved_name(monkeypatch, calls):
    _as(monkeypatch, "SUPERVISOR")
    monkeypatch.setattr(logs_crud, "get_logs_by_param_patient",
                        lambda *a, **k: ([_row(updated={"created_by_id": "u1"})], 1, 1))
    r = client.get("/api/Logs/Patient", headers={"Authorization": "Bearer realtok"})
    assert r.status_code == 200
    assert r.json()["data"][0]["updated_data"] == {"created_by_id": "u1", "created_by_name": "Alice Tan"}
    assert calls[0]["headers"] == {"Authorization": "Bearer realtok"}


def test_admin_redacted_patient_logs_not_resolved(monkeypatch, calls):
    _as(monkeypatch, "ADMIN")
    monkeypatch.setattr(logs_crud, "get_logs_by_param_patient",
                        lambda *a, **k: ([_row(updated={"created_by_id": "u1"})], 1, 1))
    r = client.get("/api/Logs/Patient", headers={"Authorization": "Bearer t"})
    assert r.json()["data"][0]["updated_data"] is None
    assert calls == []


def test_history_activity_system_routes_resolve(monkeypatch, calls):
    row = lambda: _row(updated={"modified_by_id": "u2"})
    monkeypatch.setattr(logs_crud, "get_logs_by_param_patient", lambda *a, **k: ([row()], 1, 1))
    monkeypatch.setattr(logs_crud, "get_logs_by_param_activity", lambda *a, **k: ([row()], 1, 1))
    monkeypatch.setattr(logs_crud, "get_logs_by_param_system", lambda *a, **k: ([row()], 1, 1))
    h = {"Authorization": "Bearer t"}
    _as(monkeypatch, "DOCTOR")
    assert client.get("/api/Logs/Patient/1/history", headers=h).json()["data"][0]["updated_data"]["modified_by_name"] == "Bob Lee"
    _as(monkeypatch, "SUPERVISOR")
    assert client.get("/api/Logs/Activity", headers=h).json()["data"][0]["updated_data"]["modified_by_name"] == "Bob Lee"
    _as(monkeypatch, "ADMIN")
    assert client.get("/api/Logs/System", headers=h).json()["data"][0]["updated_data"]["modified_by_name"] == "Bob Lee"
