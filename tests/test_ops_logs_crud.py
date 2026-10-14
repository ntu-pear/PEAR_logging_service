import json

import pytest

from app.crud import ops_logs_crud
from app.schemas.ops_log import OpsLogQuery


def _must(body):
    return body["query"]["bool"]["must"]


def _range(body):
    return next(c["range"]["@timestamp"] for c in _must(body) if "range" in c)


@pytest.fixture(autouse=True)
def _no_env_scope(monkeypatch):
    monkeypatch.delenv("LOG_ENVIRONMENT", raising=False)


@pytest.mark.parametrize("ops_type", ["error", "request", "auth"])
def test_every_query_is_scoped_by_schema_and_ops_type(ops_type):
    body = ops_logs_crud.build_ops_query(ops_type, OpsLogQuery())
    assert {"match_phrase": {"schema": "pear-app-v1"}} in _must(body)
    assert {"match_phrase": {"ops_type": ops_type}} in _must(body)
    assert "log_type" not in json.dumps(body)


def test_unknown_ops_type_is_rejected():
    with pytest.raises(ValueError):
        ops_logs_crud.build_ops_query("audit", OpsLogQuery())


def test_default_window_is_last_24_hours():
    assert _range(ops_logs_crud.build_ops_query("error", OpsLogQuery())) == {"gte": "now-24h"}


def test_request_id_lookup_has_no_default_window():
    body = ops_logs_crud.build_ops_query("request", OpsLogQuery(request_id="abc-1"))
    assert not any("range" in c for c in _must(body))
    assert {"match_phrase": {"request_id": "abc-1"}} in _must(body)


def test_sgt_dates_are_converted_to_utc():
    body = ops_logs_crud.build_ops_query("error", OpsLogQuery(start_date="2026-10-14", end_date="2026-10-14T23:59:59"))
    assert _range(body) == {"gte": "2026-10-13T16:00:00", "lte": "2026-10-14T15:59:59"}


def test_explicit_utc_datetimes_are_kept():
    body = ops_logs_crud.build_ops_query("error", OpsLogQuery(start_date="2026-10-14T01:00:00Z"))
    assert _range(body) == {"gte": "2026-10-14T01:00:00"}


@pytest.mark.parametrize("query", [
    OpsLogQuery(start_date="not-a-date"),
    OpsLogQuery(start_date="2026-10-15", end_date="2026-10-14"),
])
def test_bad_dates_are_query_errors(query):
    with pytest.raises(ops_logs_crud.OpsQueryError):
        ops_logs_crud.build_ops_query("error", query)


def test_filters_are_added_and_blank_ones_ignored():
    body = ops_logs_crud.build_ops_query(
        "error", OpsLogQuery(service="patient", level="ERROR", user_id="U5", request_id="  ")
    )
    must = _must(body)
    assert {"match_phrase": {"service": "patient"}} in must
    assert {"match_phrase": {"level": "ERROR"}} in must
    assert {"match_phrase": {"user_id": "U5"}} in must
    assert not any("request_id" in json.dumps(c) for c in must)
    assert _range(body) == {"gte": "now-24h"}


def test_environment_scope_reuses_the_audit_rule(monkeypatch):
    monkeypatch.setenv("LOG_ENVIRONMENT", "staging")
    must = _must(ops_logs_crud.build_ops_query("auth", OpsLogQuery()))
    assert any("fields.environment" in json.dumps(c) for c in must)


def test_sensitive_preset_only_applies_to_requests():
    request_body = ops_logs_crud.build_ops_query("request", OpsLogQuery(), sensitive=True)
    error_body = ops_logs_crud.build_ops_query("error", OpsLogQuery(), sensitive=True)
    clause = {"bool": {"should": list(ops_logs_crud.SENSITIVE_REQUEST_RULES), "minimum_should_match": 1}}
    assert clause in _must(request_body)
    assert clause not in _must(error_body)
    assert clause not in _must(ops_logs_crud.build_ops_query("request", OpsLogQuery()))


def test_sensitive_rules_cover_the_three_spec_cases():
    text = json.dumps(ops_logs_crud.SENSITIVE_REQUEST_RULES)
    assert "/patients/[0-9]+" in text and "mask=false" in text and "/admin/get_nric/" in text


def test_sensitive_path_rules_match_http_path_or_path_keyword():
    rules = ops_logs_crud.SENSITIVE_REQUEST_RULES
    patients = rules[0]["bool"]["must"][1]["bool"]["should"]
    assert [next(iter(c["regexp"])) for c in patients] == ["http_path.keyword", "path.keyword"]
    assert rules[1] == {"wildcard": {"query.keyword": {"value": "*mask=false*", "case_insensitive": True}}}
    nric = rules[2]["bool"]["should"]
    assert [next(iter(c["wildcard"])) for c in nric] == ["http_path.keyword", "path.keyword"]
    assert all(c["wildcard"][next(iter(c["wildcard"]))]["value"] == "*/admin/get_nric/*" for c in nric)


def test_page_size_is_capped_and_paging_is_offset_based():
    body = ops_logs_crud.build_ops_query("error", OpsLogQuery(), page_no=3, page_size=500)
    assert body["size"] == 100 and body["from"] == 300
    assert body["sort"] == [{"@timestamp": {"order": "desc"}}, {"_doc": {"order": "desc"}}]


def test_negative_paging_is_clamped():
    body = ops_logs_crud.build_ops_query("error", OpsLogQuery(), page_no=-2, page_size=0)
    assert body["size"] == 1 and body["from"] == 0


def test_too_deep_page_is_a_query_error():
    with pytest.raises(ops_logs_crud.OpsQueryError):
        ops_logs_crud.build_ops_query("error", OpsLogQuery(), page_no=100, page_size=100)


def test_parse_hit_handles_logstash_shapes():
    row = ops_logs_crud.parse_hit({
        "@timestamp": "2026-10-14T01:02:03.000Z",
        "ops_type": ["request"],
        "message": {"unexpected": "object"},
        "status": "401",
        "duration_ms": "12.5",
        "path": "/api/v1/patients/1",
    })
    assert row.timestamp == "2026-10-14T01:02:03.000Z"
    assert row.ops_type == "request"
    assert json.loads(row.message) == {"unexpected": "object"}
    assert row.status == 401 and row.duration_ms == 12.5


def test_parse_hit_reads_http_path_and_falls_back_to_a_string_path():
    base = {"timestamp": "t", "ops_type": "request"}
    assert ops_logs_crud.parse_hit({**base, "http_path": "/a", "path": "/b"}).path == "/a"
    assert ops_logs_crud.parse_hit({**base, "path": "/b"}).path == "/b"
    assert ops_logs_crud.parse_hit({**base, "path": {"x": 1}}).path is None


RAW_LINE = {
    "schema": "pear-app-v1", "timestamp": "2026-10-14T01:02:03.000Z", "level": "WARNING", "service": "patient",
    "environment": "stg", "ops_type": "auth", "logger": "pear.ops", "message": "Auth http_401",
    "request_id": "r1", "user_id": "U1", "http_path": "/api/v1/patients/1", "method": "GET",
    "status": 401, "auth_event": "http_401", "reason": "Not authenticated",
}


def test_parse_hit_recovers_missing_fields_from_event_original():
    row = ops_logs_crud.parse_hit({
        "@timestamp": "2026-10-14T01:02:04.000Z", "ops_type": "auth", "service": "patient",
        "event": {"original": json.dumps(RAW_LINE), "dataset": "x"},
    })
    assert row.path == "/api/v1/patients/1" and row.event == "http_401"
    assert row.method == "GET" and row.status == 401 and row.reason == "Not authenticated"
    assert row.timestamp == "2026-10-14T01:02:03.000Z"  # no top-level "timestamp", so recovered
    assert row.request_id == "r1" and row.user_id == "U1"


def test_parse_hit_recovers_the_old_path_key_and_a_flat_event_original():
    raw = {key: value for key, value in RAW_LINE.items() if key != "http_path"}
    raw["path"] = "/old"
    row = ops_logs_crud.parse_hit({"@timestamp": "t", "ops_type": "auth", "event.original": json.dumps(raw)})
    assert row.path == "/old"


def test_parse_hit_prefers_top_level_values_over_event_original():
    row = ops_logs_crud.parse_hit({
        "timestamp": "t", "ops_type": "auth", "http_path": "/top", "auth_event": "top_event",
        "event": {"original": json.dumps(RAW_LINE)},
    })
    assert row.path == "/top" and row.event == "top_event"


@pytest.mark.parametrize("original", [
    "not json", "{broken", "[1, 2]", "null", json.dumps({**RAW_LINE, "schema": "other"}), 123, {"a": 1},
    pytest.param("[" * 100000, id="deeply-nested"),
])
def test_parse_hit_ignores_event_original_that_is_not_an_ops_line(original):
    row = ops_logs_crud.parse_hit({"timestamp": "t", "ops_type": "auth", "event": {"original": original}})
    assert row is not None and row.path is None and row.event is None


def test_parse_hit_reads_the_auth_event_name_from_auth_event():
    row = ops_logs_crud.parse_hit({
        "timestamp": "t", "ops_type": "auth", "auth_event": "login_failed",
        "event": {"original": "{...}", "dataset": "x"},
    })
    assert row.event == "login_failed"


def test_parse_hit_never_treats_the_ecs_event_object_as_the_auth_event():
    row = ops_logs_crud.parse_hit({"timestamp": "t", "ops_type": "auth", "event": {"original": "{...}"}})
    assert row.event is None


def test_parse_hit_falls_back_to_a_string_event_for_old_documents():
    assert ops_logs_crud.parse_hit({"timestamp": "t", "ops_type": "auth", "event": "http_401"}).event == "http_401"
    assert ops_logs_crud.parse_hit({"timestamp": "t", "ops_type": "auth", "event": ["a", "http_403"]}).event == "http_403"


@pytest.mark.parametrize("source", [{"ops_type": "error"}, {"timestamp": "2026-10-14T01:02:03Z"}])
def test_parse_hit_skips_rows_without_timestamp_or_type(source):
    assert ops_logs_crud.parse_hit(source) is None


def test_parse_hit_drops_bad_numbers():
    row = ops_logs_crud.parse_hit({"timestamp": "t", "ops_type": "request", "status": "abc", "duration_ms": True})
    assert row.status is None and row.duration_ms is None


def test_search_skips_unparsable_hits_and_counts_pages(monkeypatch):
    seen = {}

    def fake_search(index=None, body=None, headers=None):
        seen["index"], seen["body"] = index, body
        return {"hits": {"total": {"value": 41}, "hits": [
            {"_id": "1", "_source": {"timestamp": "2026-10-14T01:00:00Z", "ops_type": "error", "message": "boom"}},
            {"_id": "2", "_source": {"message": "no type"}},
        ]}}

    monkeypatch.setattr(ops_logs_crud.es_service, "search_documents", fake_search)
    rows, total, pages = ops_logs_crud.search_ops_logs("error", OpsLogQuery(), 0, 20)
    assert [r.message for r in rows] == ["boom"]
    assert total == 41 and pages == 3
    assert seen["index"] == "*"


def test_end_date_alone_gets_a_24_hour_lower_bound():
    body = ops_logs_crud.build_ops_query("error", OpsLogQuery(end_date="2026-10-14T12:00:00"))
    assert _range(body) == {"gte": "2026-10-13T04:00:00", "lte": "2026-10-14T04:00:00"}


def test_date_only_end_covers_the_whole_last_second():
    body = ops_logs_crud.build_ops_query("error", OpsLogQuery(start_date="2026-10-14", end_date="2026-10-14"))
    assert _range(body)["lte"] == "2026-10-14T15:59:59.999999"


def test_spans_over_30_days_are_query_errors():
    with pytest.raises(ops_logs_crud.OpsQueryError):
        ops_logs_crud.build_ops_query("error", OpsLogQuery(start_date="2026-01-01", end_date="2026-03-01"))
    body = ops_logs_crud.build_ops_query("error", OpsLogQuery(start_date="2026-09-01", end_date="2026-09-30"))
    assert "lte" in _range(body)


def test_request_id_lookup_with_dates_is_not_forced_into_a_window():
    body = ops_logs_crud.build_ops_query("request", OpsLogQuery(request_id="abc", end_date="2026-10-14"))
    assert _range(body)["lte"] == "2026-10-14T15:59:59.999999"


@pytest.mark.parametrize("value", ["0001-01-01", "0001-01-01T00:00:00Z"])
def test_extreme_dates_are_query_errors_not_overflow(value):
    for query in (OpsLogQuery(start_date=value), OpsLogQuery(end_date=value)):
        with pytest.raises(ops_logs_crud.OpsQueryError):
            ops_logs_crud.build_ops_query("error", query)


def test_total_hits_are_bounded_and_sort_has_a_tie_breaker():
    body = ops_logs_crud.build_ops_query("error", OpsLogQuery())
    assert body["track_total_hits"] == 10001
    assert body["sort"] == [{"@timestamp": {"order": "desc"}}, {"_doc": {"order": "desc"}}]
