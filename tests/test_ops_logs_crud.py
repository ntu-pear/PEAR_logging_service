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
