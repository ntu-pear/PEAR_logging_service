import pytest
from elastic_transport import ConnectionError as ESConnectionError
from fastapi.testclient import TestClient

from app.auth import dependencies
from app.auth.token_verifier import Verdict, VerifiedUser
from app.crud import log_stats_crud
from app.main import app
from app.routers import log_stats as log_stats_router
from app.services import user_names

client = TestClient(app)
AUTH = {"Authorization": "Bearer tok"}
URL = "/api/Logs/Stats"

T0 = 1791939600000  # 2026-10-14T01:00:00Z
MIN10 = 600000


def _as(monkeypatch, role):
    user = VerifiedUser(userId="U1", fullName="X", roleName=role, email="x@x.com")
    monkeypatch.setattr(dependencies, "verify_token", lambda token: (Verdict.VALID, user))


def _windows(*buckets):
    return {"aggregations": {"windows": {"buckets": list(buckets)}}}


def _hot(start, key, n, **extra):
    bucket = {"key": key, "doc_count": n}
    bucket.update(extra)
    return {"key": start, "by_key": {"buckets": [bucket]}}


def _answer(body):
    """A canned Elasticsearch reply for each query the stats endpoint sends."""
    aggs = body.get("aggs", {})
    if "windows" in aggs:
        terms = aggs["windows"]["aggs"]["by_key"]
        field = terms["terms"]["field"]
        if field == "service.keyword":
            return _windows(_hot(T0 + MIN10, "patient", 25))
        if field == "user.keyword":
            return _windows(_hot(T0 + 3 * MIN10, "U9", 12))
        if "aggs" in terms:
            return _windows(_hot(T0 + 2 * MIN10, "U7", 40, distinct={"value": 31}))
        return _windows(_hot(T0, "U5", 7))
    if "requests" in aggs and "sensitive_access" in aggs:
        return {"aggregations": {"requests": {"doc_count": 100}, "errors": {"doc_count": 7},
                                 "failed_logins": {"doc_count": 9}, "sensitive_access": {"doc_count": 3}}}
    if "over_time" in aggs:
        return {"aggregations": {"over_time": {"buckets": [
            {"key": T0, "by_service": {"buckets": [
                {"key": "patient", "requests": {"doc_count": 40}, "errors": {"doc_count": 2}},
                {"key": "user", "requests": {"doc_count": 10}, "errors": {"doc_count": 0}},
            ]}},
            {"key": T0 + 3600000, "by_service": {"buckets": []}},
        ]}}}
    if "endpoints" in aggs:
        return {"aggregations": {"endpoints": {"buckets": [
            {"key": "/api/v1/patients", "doc_count": 50, "p95": {"values": {"95.0": 123.4}}},
            {"key": "/api/v1/login", "doc_count": 20, "p95": {"values": {"95.0": None}}},
        ]}}}
    if "status" in aggs:
        return {"aggregations": {"status": {"buckets": [
            {"key": "2xx", "doc_count": 90}, {"key": "3xx", "doc_count": 0},
            {"key": "4xx", "doc_count": 8}, {"key": "5xx", "doc_count": 2}]}}}
    if "failed_over_time" in aggs:
        return {"aggregations": {
            "failed_over_time": {"buckets": [{"key": T0, "doc_count": 4}]},
            "top_users": {"buckets": [{"key": "U5", "doc_count": 6}, {"key": "U6", "doc_count": 3}]}}}
    if "per_day" in aggs:
        return {"hits": {"total": {"value": 25, "relation": "eq"}, "hits": []}, "aggregations": {
            "per_day": {"buckets": [{"key": T0, "by_action": {"buckets": [
                {"key": "update", "doc_count": 10}, {"key": "delete", "doc_count": 5}]}}]},
            "tables": {"buckets": [{"key": "Patient", "doc_count": 12}]},
            "actors": {"buckets": [{"key": "U9", "doc_count": 8}]}}}
    if "sort" in body:  # the mask=false lookup
        return {"hits": {"total": {"value": 2, "relation": "eq"}, "hits": [
            {"_source": {"@timestamp": "2026-10-14T01:50:00.000Z", "user_id": "U3", "service": "patient"}}]}}
    raise AssertionError("unexpected query: %r" % (body,))


@pytest.fixture
def es(monkeypatch):
    bodies = []

    def fake(**kwargs):
        bodies.append(kwargs["body"])
        return _answer(kwargs["body"])

    monkeypatch.setattr(log_stats_crud, "_search", fake)
    monkeypatch.setattr(log_stats_router, "lookup_names", lambda ids, token: {})
    return bodies


def _walk(node, key):
    """Every value stored under `key` anywhere in a nested query body."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            for found in _walk(v, key):
                yield found
    elif isinstance(node, list):
        for item in node:
            for found in _walk(item, key):
                yield found


# ---------------------------------------------------------------- access

def test_no_token_is_401(es):
    assert client.get(URL).status_code == 401
    assert es == []


@pytest.mark.parametrize("role", ["SUPERVISOR", "DOCTOR", "CAREGIVER", "GUARDIAN", "GAME THERAPIST"])
def test_non_admin_is_403(monkeypatch, es, role):
    _as(monkeypatch, role)
    assert client.get(URL, headers=AUTH).status_code == 403
    assert es == []


@pytest.mark.parametrize("window", ["1h", "90d", "", "24H", "all"])
def test_bad_window_is_400(monkeypatch, es, window):
    _as(monkeypatch, "ADMIN")
    response = client.get(URL, headers=AUTH, params={"window": window})
    assert response.status_code == 400
    assert response.json()["detail"] == "window must be one of 24h, 7d, 30d"
    assert es == []


# ---------------------------------------------------------------- query bodies

@pytest.mark.parametrize("window,start,interval", [
    ("24h", "now-24h", {"fixed_interval": "1h"}),
    ("7d", "now-7d", {"calendar_interval": "1d"}),
    ("30d", "now-30d", {"calendar_interval": "1d"}),
])
def test_queries_are_bounded_and_scoped_to_the_window(monkeypatch, es, window, start, interval):
    _as(monkeypatch, "ADMIN")
    assert client.get(URL, headers=AUTH, params={"window": window}).status_code == 200
    assert len(es) >= 8
    for body in es:
        assert body["size"] <= 20
        ranges = [r["@timestamp"] for r in _walk(body["query"], "range") if "@timestamp" in r]
        assert ranges and all(r["gte"] == start for r in ranges)
        assert all(size <= 50 for size in _walk(body.get("aggs", {}), "size"))
        assert all(not isinstance(t, dict) or "size" in t for t in _walk(body.get("aggs", {}), "terms"))
        assert "scroll" not in body
    traffic = [b for b in es if "over_time" in b.get("aggs", {})][0]
    histogram = traffic["aggs"]["over_time"]["date_histogram"]
    assert histogram["time_zone"] == "Asia/Singapore"
    for key, value in interval.items():
        assert histogram[key] == value
    assert traffic["size"] == 0


def test_default_window_is_24h(monkeypatch, es):
    _as(monkeypatch, "ADMIN")
    assert client.get(URL, headers=AUTH).json()["window"] == "24h"


def test_environment_scope_is_applied(monkeypatch, es):
    monkeypatch.setenv("LOG_ENVIRONMENT", "staging")
    _as(monkeypatch, "ADMIN")
    client.get(URL, headers=AUTH)
    assert all("fields.environment" in str(body["query"]) for body in es)


def test_top_endpoints_ask_for_p95_of_duration(monkeypatch, es):
    _as(monkeypatch, "ADMIN")
    client.get(URL, headers=AUTH)
    body = [b for b in es if "endpoints" in b.get("aggs", {})][0]
    agg = body["aggs"]["endpoints"]
    assert agg["terms"] == {"field": "http_path.keyword", "size": 10}
    assert agg["aggs"]["p95"]["percentiles"] == {"field": "duration_ms", "percents": [95]}


# ---------------------------------------------------------------- response

def test_response_shape_and_values(monkeypatch, es):
    _as(monkeypatch, "ADMIN")
    response = client.get(URL, headers=AUTH, params={"window": "24h"})
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"window", "totals", "traffic", "top_endpoints", "status_mix", "auth", "audit", "recent_flags"}
    assert data["totals"] == {"requests": 100, "errors": 7, "failed_logins": 9, "sensitive_access": 3,
                              "audit_changes": 25, "flagged": 6}
    assert data["traffic"] == [
        {"t": "2026-10-14T01:00:00.000Z", "service": "patient", "requests": 40, "errors": 2},
        {"t": "2026-10-14T01:00:00.000Z", "service": "user", "requests": 10, "errors": 0},
    ]
    assert data["top_endpoints"] == [
        {"path": "/api/v1/patients", "count": 50, "p95_ms": 123.4},
        {"path": "/api/v1/login", "count": 20, "p95_ms": None},
    ]
    assert data["status_mix"] == {"2xx": 90, "3xx": 0, "4xx": 8, "5xx": 2}
    assert data["auth"] == {
        "failed_logins_over_time": [{"t": "2026-10-14T01:00:00.000Z", "count": 4}],
        "top_accounts": [{"user_id": "U5", "name": None, "count": 6}, {"user_id": "U6", "name": None, "count": 3}],
    }
    assert data["audit"] == {
        "changes_per_day": [
            {"t": "2026-10-14T01:00:00.000Z", "action": "update", "count": 10},
            {"t": "2026-10-14T01:00:00.000Z", "action": "delete", "count": 5},
        ],
        "top_tables": [{"table": "Patient", "count": 12}],
        "top_actors": [{"user_id": "U9", "name": None, "count": 8}],
    }


def test_status_mix_always_has_the_four_classes(monkeypatch, es):
    _as(monkeypatch, "ADMIN")
    original = log_stats_crud._search

    def sparse(**kwargs):
        if "status" in kwargs["body"].get("aggs", {}):
            return {"aggregations": {"status": {"buckets": [{"key": "2xx", "doc_count": 4}]}}}
        return original(**kwargs)

    monkeypatch.setattr(log_stats_crud, "_search", sparse)
    assert client.get(URL, headers=AUTH).json()["status_mix"] == {"2xx": 4, "3xx": 0, "4xx": 0, "5xx": 0}


def test_empty_store_gives_zeroes(monkeypatch):
    _as(monkeypatch, "ADMIN")
    monkeypatch.setattr(log_stats_crud, "_search", lambda **kw: {})
    monkeypatch.setattr(log_stats_router, "lookup_names", lambda ids, token: {})
    data = client.get(URL, headers=AUTH).json()
    assert data["totals"] == {"requests": 0, "errors": 0, "failed_logins": 0, "sensitive_access": 0,
                              "audit_changes": 0, "flagged": 0}
    assert data["traffic"] == [] and data["top_endpoints"] == [] and data["recent_flags"] == []
    assert data["status_mix"] == {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}


def test_recent_flags_come_from_the_flag_detectors_newest_first(monkeypatch, es):
    _as(monkeypatch, "ADMIN")
    flags = client.get(URL, headers=AUTH).json()["recent_flags"]
    assert flags == [
        {"time": "2026-10-14T01:50:00.000Z", "code": "mass_access", "severity": "critical",
         "reason": "Unmasked patient data requested by U3", "service": "patient", "link_hint": "sensitive"},
        {"time": "2026-10-14T01:30:00.000Z", "code": "bulk_delete", "severity": "critical",
         "reason": "12 deletes by U9 in 10 min", "service": None, "link_hint": "patient"},
        {"time": "2026-10-14T01:20:00.000Z", "code": "mass_access", "severity": "critical",
         "reason": "31 distinct patient records read by U7 in 10 min", "service": "patient",
         "link_hint": "sensitive"},
        {"time": "2026-10-14T01:10:00.000Z", "code": "error_spike", "severity": "warning",
         "reason": "25 errors from patient in 5 min", "service": "patient", "link_hint": "errors"},
        {"time": "2026-10-14T01:00:00.000Z", "code": "failed_login_burst", "severity": "warning",
         "reason": "7 failed logins for U5 in 10 min", "service": "user", "link_hint": "auth"},
    ]


def test_recent_flags_are_capped_at_20_but_flagged_counts_all(monkeypatch, es):
    _as(monkeypatch, "ADMIN")
    many = [_hot(T0 + i * MIN10, "U5", 7) for i in range(30)]
    original = log_stats_crud._search

    def fake(**kwargs):
        aggs = kwargs["body"].get("aggs", {})
        if "windows" in aggs and aggs["windows"]["aggs"]["by_key"]["terms"]["field"] == "user_id.keyword" \
                and "aggs" not in aggs["windows"]["aggs"]["by_key"]:
            return _windows(*many)
        return original(**kwargs)

    monkeypatch.setattr(log_stats_crud, "_search", fake)
    data = client.get(URL, headers=AUTH).json()
    assert len(data["recent_flags"]) == 20
    assert data["totals"]["flagged"] == 30 + 1 + 1 + 1 + 2


def test_flag_detector_failure_degrades_to_no_flags(monkeypatch, es):
    _as(monkeypatch, "ADMIN")
    original = log_stats_crud._search

    def fake(**kwargs):
        body = kwargs["body"]
        if "windows" in body.get("aggs", {}) or "sort" in body:
            raise RuntimeError("aggregation blew up")
        return original(**kwargs)

    monkeypatch.setattr(log_stats_crud, "_search", fake)
    response = client.get(URL, headers=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert data["recent_flags"] == [] and data["totals"]["flagged"] == 0
    assert data["totals"]["requests"] == 100


def test_names_are_resolved_for_accounts_and_actors(monkeypatch, es):
    _as(monkeypatch, "ADMIN")
    seen = {}

    def lookup(ids, token):
        seen["ids"], seen["token"] = sorted(ids), token
        return {"U5": "Lim Mei Ling", "U9": "Tan Wei"}

    monkeypatch.setattr(log_stats_router, "lookup_names", lookup)
    data = client.get(URL, headers=AUTH).json()
    assert seen == {"ids": ["U5", "U6", "U9"], "token": "tok"}
    assert data["auth"]["top_accounts"][0]["name"] == "Lim Mei Ling"
    assert data["auth"]["top_accounts"][1]["name"] is None
    assert data["audit"]["top_actors"][0]["name"] == "Tan Wei"


def test_lookup_names_failure_returns_nothing(monkeypatch):
    monkeypatch.setenv("USER_SERVICE_URL", "http://user")

    def boom(base_url, token, ids):
        raise RuntimeError("user service down")

    monkeypatch.setattr(user_names, "_fetch", boom)
    assert user_names.lookup_names(["U1"], "tok") == {}
    monkeypatch.delenv("USER_SERVICE_URL")
    assert user_names.lookup_names(["U1"], "tok") == {}


def test_lookup_names_returns_fetched_names(monkeypatch):
    monkeypatch.setenv("USER_SERVICE_URL", "http://user/")
    monkeypatch.setattr(user_names, "_fetch", lambda base_url, token, ids: {"U1": "Ann"})
    assert user_names.lookup_names(["U1", "U1", "U2"], "tok") == {"U1": "Ann"}


# ---------------------------------------------------------------- failures

@pytest.mark.parametrize("error", [ESConnectionError("down"), RuntimeError("bad aggregation")])
def test_store_failure_is_503_with_a_fixed_message_and_is_logged(monkeypatch, es, error):
    _as(monkeypatch, "ADMIN")

    def fake(**kwargs):
        raise error

    monkeypatch.setattr(log_stats_crud, "_search", fake)
    logged = []
    monkeypatch.setattr(log_stats_router.log, "exception", lambda *a, **k: logged.append(a))
    response = client.get(URL, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": "Log store unavailable"}
    assert len(logged) == 1
