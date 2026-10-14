import logging

import pytest

from app.schemas.log_document import Flag, LogDocument
from app.schemas.ops_log import OpsLogDocument
from app.services import log_flags
from app.services.log_flags import FlagConfig

WINDOW_MS = 10 * 60 * 1000


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("FLAG_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LOG_ENVIRONMENT", raising=False)


def audit(ts="2026-10-14T10:00:00", method="update", table="Patient", user="u1", name="Lim Mei Ling",
          original=None, updated=None):
    return LogDocument(
        timestamp=ts, method=method, table=table, user=user, user_full_name=name, message="m",
        original_data=original or {}, updated_data=updated or {},
    )


def ops(ops_type="request", ts="2026-10-14T01:02:03.000Z", **kw):
    return OpsLogDocument(timestamp=ts, ops_type=ops_type, **kw)


def agg_response(buckets):
    """buckets: list of (bucket_start_ms, [(key, count[, distinct])])"""
    out = []
    for start, keys in buckets:
        out.append({
            "key": start,
            "doc_count": sum(k[1] for k in keys),
            "by_key": {"buckets": [
                {"key": k[0], "doc_count": k[1], **({"distinct": {"value": k[2]}} if len(k) > 2 else {})}
                for k in keys
            ]},
        })
    return {"aggregations": {"windows": {"buckets": out}}}


def bucket_start(iso_utc):
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return ms - ms % WINDOW_MS


# ---------- config ----------

def test_config_defaults():
    cfg = FlagConfig.from_env()
    assert (cfg.failed_login_count, cfg.failed_login_window_min) == (5, 10)
    assert (cfg.bulk_delete_count, cfg.bulk_delete_window_min) == (10, 10)
    assert (cfg.off_hours_start, cfg.off_hours_end) == ("22:00", "07:00")
    assert (cfg.mass_access_count, cfg.mass_access_window_min) == (30, 10)
    assert cfg.sensitive_fields == ("nric", "NRIC", "dateOfBirth", "DOB", "dob")
    assert (cfg.error_spike_count, cfg.error_spike_window_min) == (20, 5)


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("FLAG_FAILED_LOGIN_COUNT", "3")
    monkeypatch.setenv("FLAG_OFF_HOURS_START", "21:30")
    monkeypatch.setenv("FLAG_SENSITIVE_FIELDS", "nric, passport")
    cfg = FlagConfig.from_env()
    assert cfg.failed_login_count == 3
    assert cfg.off_hours_start == "21:30"
    assert cfg.sensitive_fields == ("nric", "passport")


@pytest.mark.parametrize("name,value", [
    ("FLAG_FAILED_LOGIN_COUNT", "abc"),
    ("FLAG_FAILED_LOGIN_COUNT", "0"),
    ("FLAG_BULK_DELETE_WINDOW_MIN", "-4"),
    ("FLAG_ERROR_SPIKE_COUNT", ""),
])
def test_config_invalid_int_falls_back_with_warning(monkeypatch, caplog, name, value):
    monkeypatch.setenv(name, value)
    with caplog.at_level(logging.WARNING):
        cfg = FlagConfig.from_env()
    default = FlagConfig()
    attr = {
        "FLAG_FAILED_LOGIN_COUNT": "failed_login_count",
        "FLAG_BULK_DELETE_WINDOW_MIN": "bulk_delete_window_min",
        "FLAG_ERROR_SPIKE_COUNT": "error_spike_count",
    }[name]
    assert getattr(cfg, attr) == getattr(default, attr)
    if value != "":
        assert any(name in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["25:00", "7pm", "22-00", "22:60"])
def test_config_invalid_time_falls_back(monkeypatch, caplog, value):
    monkeypatch.setenv("FLAG_OFF_HOURS_START", value)
    with caplog.at_level(logging.WARNING):
        cfg = FlagConfig.from_env()
    assert cfg.off_hours_start == "22:00"
    assert any("FLAG_OFF_HOURS_START" in r.getMessage() for r in caplog.records)


# ---------- off_hours ----------

@pytest.mark.parametrize("ts_utc,expected", [
    ("2026-10-14T13:59:59", False),   # 21:59:59 SGT
    ("2026-10-14T14:00:00", True),    # 22:00 SGT, boundary inclusive
    ("2026-10-14T16:30:00", True),    # 00:30 SGT, after midnight
    ("2026-10-14T22:59:59", True),    # 06:59:59 SGT
    ("2026-10-14T23:00:00", False),   # 07:00 SGT, end exclusive
    ("2026-10-14T04:00:00", False),   # noon SGT
])
def test_off_hours_across_midnight_sgt(ts_utc, expected):
    doc = audit(ts=ts_utc + "Z")
    flag = log_flags.off_hours(doc, FlagConfig())
    assert (flag is not None) == expected
    if flag:
        assert flag.code == "off_hours" and flag.severity == "warning"


def test_off_hours_naive_timestamp_is_sgt():
    assert log_flags.off_hours(audit(ts="2026-10-14T23:30:00"), FlagConfig()) is not None
    assert log_flags.off_hours(audit(ts="2026-10-14T12:00:00"), FlagConfig()) is None


def test_off_hours_same_day_window():
    cfg = FlagConfig(off_hours_start="01:00", off_hours_end="05:00")
    assert log_flags.off_hours(audit(ts="2026-10-14T02:00:00"), cfg) is not None
    assert log_flags.off_hours(audit(ts="2026-10-14T23:00:00"), cfg) is None


def test_off_hours_bad_timestamp_is_none():
    assert log_flags.off_hours(audit(ts="not a date"), FlagConfig()) is None


# ---------- sensitive_change ----------

def test_sensitive_update_changed_key_flags_without_values():
    doc = audit(original={"nric": "S1234567A", "name": "A"}, updated={"nric": "S7654321B", "name": "A"})
    flag = log_flags.sensitive_change(doc, FlagConfig())
    assert flag.code == "sensitive_change" and flag.severity == "critical"
    assert "nric" in flag.reason
    assert "S1234567A" not in flag.reason and "S7654321B" not in flag.reason


def test_sensitive_update_unchanged_key_not_flagged():
    doc = audit(original={"nric": "S1", "name": "A"}, updated={"nric": "S1", "name": "B"})
    assert log_flags.sensitive_change(doc, FlagConfig()) is None


def test_sensitive_delete_with_key_present():
    doc = audit(method="delete", original={"DOB": "1950-01-01"}, updated={})
    assert log_flags.sensitive_change(doc, FlagConfig()) is not None


def test_sensitive_create_not_flagged():
    doc = audit(method="create", updated={"nric": "S1"})
    assert log_flags.sensitive_change(doc, FlagConfig()) is None


def test_sensitive_keys_are_case_exact():
    doc = audit(original={"Nric": "a"}, updated={"Nric": "b"})
    assert log_flags.sensitive_change(doc, FlagConfig()) is None


def test_allergy_mapping_delete_flagged():
    doc = audit(method="delete", table="PatientAllergyMapping", original={"AllergyTypeID": 3})
    flag = log_flags.sensitive_change(doc, FlagConfig())
    assert flag is not None and "PatientAllergyMapping" in flag.reason


def test_allergy_mapping_update_not_flagged():
    doc = audit(method="update", table="PatientAllergyMapping", original={"a": 1}, updated={"a": 2})
    assert log_flags.sensitive_change(doc, FlagConfig()) is None


def test_sensitive_handles_missing_data():
    doc = LogDocument(timestamp="t", method="update", table="Patient", user="u", user_full_name="", message="m")
    assert log_flags.sensitive_change(doc, FlagConfig()) is None


# ---------- mask_false ----------

@pytest.mark.parametrize("query,expected", [
    ("mask=false", True), ("a=1&mask=false&b=2", True), ("MASK=FALSE", True),
    ("mask=true", False), (None, False), ("", False),
])
def test_mask_false(query, expected):
    flag = log_flags.mask_false(ops(query=query, user_id="7"))
    assert (flag is not None) == expected
    if flag:
        assert flag.code == "mass_access" and flag.severity == "critical"


# ---------- burst_buckets ----------

def test_burst_buckets_builds_bounded_query_and_parses():
    calls = []

    def es(index, body, headers=None):
        calls.append((index, body, headers))
        return agg_response([(1000 * WINDOW_MS, [("u1", 7)]), (1001 * WINDOW_MS, [("u2", 5)])])

    hot = log_flags.burst_buckets(
        es, "*", {"bool": {"must": [{"match_phrase": {"schema": "pear-app-v1"}}]}},
        "user_id.keyword", 10, 5, {"gte": "2026-10-14T00:00:00", "lte": "2026-10-14T06:00:00"},
    )
    assert hot == {("u1", 1000 * WINDOW_MS), ("u2", 1001 * WINDOW_MS)}
    assert hot.counts[("u1", 1000 * WINDOW_MS)] == 7
    index, body, headers = calls[0]
    assert body["size"] == 0 and body["track_total_hits"] is False
    windows = body["aggs"]["windows"]
    assert windows["date_histogram"]["field"] == "@timestamp"
    assert windows["date_histogram"]["fixed_interval"] == "10m"
    assert windows["date_histogram"]["min_doc_count"] == 1
    terms = windows["aggs"]["by_key"]["terms"]
    assert terms == {"field": "user_id.keyword", "size": 50, "min_doc_count": 5}
    must = body["query"]["bool"]["must"]
    assert {"range": {"@timestamp": {"gte": "2026-10-14T00:00:00", "lte": "2026-10-14T06:00:00"}}} in must
    assert headers == {"Content-Type": "application/json"}


def test_burst_buckets_distinct_field_filters_on_distinct_count():
    seen = {}

    def es(index, body, headers=None):
        seen["body"] = body
        return agg_response([(5 * WINDOW_MS, [("u1", 40, 31), ("u2", 40, 29)])])

    hot = log_flags.burst_buckets(es, "*", {"match_all": {}}, "user_id.keyword", 10, 30, {"gte": "now-1h"},
                                  distinct_field="http_path.keyword")
    sub = seen["body"]["aggs"]["windows"]["aggs"]["by_key"]
    assert sub["aggs"]["distinct"] == {"cardinality": {"field": "http_path.keyword"}}
    assert "bucket_selector" in sub["aggs"]["keep"]
    assert hot == {("u1", 5 * WINDOW_MS)}
    assert hot.counts[("u1", 5 * WINDOW_MS)] == 31


def test_burst_buckets_threshold_boundary_is_inclusive_by_min_doc_count():
    seen = {}

    def es(index, body, headers=None):
        seen["body"] = body
        return agg_response([])

    log_flags.burst_buckets(es, "*", {"match_all": {}}, "f", 5, 12, {"gte": "now-1h"})
    assert seen["body"]["aggs"]["windows"]["aggs"]["by_key"]["terms"]["min_doc_count"] == 12


def test_burst_buckets_span_over_30_days_is_rejected():
    with pytest.raises(ValueError):
        log_flags.burst_buckets(lambda **k: {}, "*", {"match_all": {}}, "f", 10, 5,
                                {"gte": "2026-08-01T00:00:00", "lte": "2026-10-01T00:00:00"})


@pytest.mark.parametrize("window_min,time_range,expected", [
    (10, {"gte": "now-1h"}, 10),
    (10, {"gte": "2026-10-14T00:00:00", "lte": "2026-10-14T06:00:00"}, 10),
    (10, {"gte": "now-7d"}, 11),                      # 10080 min / 1000 -> 11
    (10, {"gte": "now-30d"}, 44),                     # 43200 min / 1000 -> 44
    (45, {"gte": "now-30d"}, 45),                     # configured window already wider
    (10, {"gte": "2026-09-15T00:00:00Z", "lte": "2026-10-15T00:00:00Z"}, 44),
    (5, {"gte": "now-30d", "lte": "now-1d"}, 42),     # 41760 min / 1000 -> 42
    (10, {}, 44),                                     # unknown span -> assume 30 days
])
def test_effective_interval_min(window_min, time_range, expected):
    assert log_flags._effective_interval_min(window_min, time_range) == expected


def test_burst_buckets_30_day_range_caps_histogram_buckets():
    seen = {}

    def es(index, body, headers=None):
        seen["body"] = body
        return agg_response([])

    hot = log_flags.burst_buckets(es, "*", {"match_all": {}}, "f", 10, 5, {"gte": "now-30d"})
    windows = seen["body"]["aggs"]["windows"]
    assert windows["date_histogram"]["fixed_interval"] == "44m"
    assert hot.interval_min == 44
    minutes = 30 * 24 * 60
    histogram_buckets = -(-minutes // 44)
    assert histogram_buckets <= 1000
    assert histogram_buckets * windows["aggs"]["by_key"]["terms"]["size"] <= 50000


def test_burst_buckets_short_range_keeps_configured_window():
    hot = log_flags.burst_buckets(lambda **k: {}, "*", {"match_all": {}}, "f", 10, 5, {"gte": "now-1h"})
    assert hot.interval_min == 10


def test_flag_rows_uses_widened_interval_in_bucket_and_reason():
    interval_ms = 45 * 60 * 1000
    stamp = log_flags._epoch_ms(log_flags._parse_ts("2026-10-14T01:03:00Z"))
    start = stamp - stamp % interval_ms
    buckets = log_flags.HotBuckets({("u1", start)})
    buckets.counts[("u1", start)] = 41
    buckets.interval_min = 45
    rows = [ops("auth", ts="2026-10-14T01:03:00Z", user_id="u1")]
    log_flags.flag_rows(
        rows, buckets, key_fn=lambda r: r.user_id, window_min=10, code="failed_login_burst",
        severity="warning", reason_fn=lambda key, n, w: f"{n} failed logins for {key} in {w} min",
    )
    assert rows[0].flags[0].reason == "41 failed logins for u1 in 45 min"


def test_burst_buckets_missing_aggregations_is_empty():
    assert log_flags.burst_buckets(lambda **k: {}, "*", {"match_all": {}}, "f", 10, 5, {"gte": "now-1h"}) == set()


# ---------- flag_rows ----------

def test_flag_rows_marks_only_hot_bucket_rows_with_count():
    hot_start = bucket_start("2026-10-14T01:00:00Z")
    buckets = log_flags.HotBuckets({("u1", hot_start)})
    buckets.counts[("u1", hot_start)] = 12
    rows = [
        ops("auth", ts="2026-10-14T01:03:00Z", user_id="u1"),      # in hot bucket
        ops("auth", ts="2026-10-14T01:12:00Z", user_id="u1"),      # next bucket
        ops("auth", ts="2026-10-14T01:03:00Z", user_id="u2"),      # other key
    ]
    log_flags.flag_rows(
        rows, buckets, key_fn=lambda r: r.user_id, window_min=10, code="failed_login_burst",
        severity="warning", reason_fn=lambda key, n, w: f"{n} failed logins for {key} in {w} min",
    )
    assert [len(r.flags) for r in rows] == [1, 0, 0]
    assert rows[0].flags[0].reason == "12 failed logins for u1 in 10 min"
    assert rows[0].flags[0].code == "failed_login_burst"


def test_flag_rows_does_not_duplicate_and_skips_unparsable_rows():
    start = bucket_start("2026-10-14T01:00:00Z")
    b = log_flags.HotBuckets({("u1", start)})
    b.counts[("u1", start)] = 5
    rows = [ops("auth", ts="2026-10-14T01:03:00Z", user_id="u1"), ops("auth", ts="garbage", user_id="u1")]
    for _ in range(2):
        log_flags.flag_rows(rows, b, key_fn=lambda r: r.user_id, window_min=10, code="off_hours",
                            severity="warning", reason_fn=lambda k, n, w: "r")
    assert len(rows[0].flags) == 1 and rows[1].flags == []


# ---------- apply_*_flags ----------

class FakeEs:
    def __init__(self, responder):
        self.responder = responder
        self.bodies = []

    def __call__(self, index, body, headers=None):
        self.bodies.append(body)
        return self.responder(body)


def _field_of(body):
    return body["aggs"]["windows"]["aggs"]["by_key"]["terms"]["field"]


def test_apply_audit_flags_row_local_and_bulk_delete(monkeypatch):
    start = bucket_start("2026-10-14T04:00:00Z")

    def responder(body):
        return agg_response([(start, [("u1", 12)])])

    fake = FakeEs(responder)
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    rows = [
        audit(ts="2026-10-14T04:02:00Z", method="delete", user="u1", name="Lim Mei Ling"),
        audit(ts="2026-10-14T04:03:00Z", method="update", user="u1"),
        audit(ts="2026-10-14T15:00:00Z", method="create", user="u9"),  # 23:00 SGT
    ]
    out = log_flags.apply_audit_flags(rows, {"gte": "2026-10-14T00:00:00Z", "lte": "2026-10-14T23:00:00Z"},
                                      FlagConfig())
    assert out is rows
    delete_flags = [f.code for f in rows[0].flags]
    assert delete_flags == ["bulk_delete"]
    assert rows[0].flags[0].severity == "critical"
    assert "12 deletes by Lim Mei Ling in 10 min" == rows[0].flags[0].reason
    assert rows[1].flags == []  # update is not a delete
    assert [f.code for f in rows[2].flags] == ["off_hours"]
    assert _field_of(fake.bodies[0]) == "user.keyword"


def test_apply_audit_flags_skips_aggregation_without_deletes(monkeypatch):
    fake = FakeEs(lambda b: agg_response([]))
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    rows = [audit(ts="2026-10-14T04:00:00Z", method="update")]
    log_flags.apply_audit_flags(rows, {"gte": "2026-10-14T00:00:00Z"}, FlagConfig())
    assert fake.bodies == []


def test_apply_audit_flags_derives_range_when_missing(monkeypatch):
    fake = FakeEs(lambda b: agg_response([]))
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    rows = [audit(ts="2026-10-14T04:00:00Z", method="delete")]
    log_flags.apply_audit_flags(rows, None, FlagConfig())
    must = fake.bodies[0]["query"]["bool"]["must"]
    rng = next(c["range"]["@timestamp"] for c in must if "range" in c)
    assert "gte" in rng and "lte" in rng


def test_apply_audit_flags_es_failure_keeps_row_local_flags(monkeypatch):
    def boom(**kw):
        raise RuntimeError("es down")

    monkeypatch.setattr(log_flags.es_service, "search_documents", boom)
    rows = [
        audit(ts="2026-10-14T15:00:00Z", method="delete"),   # off_hours; the bulk_delete lookup fails
        audit(ts="2026-10-14T04:00:00Z"),
    ]
    out = log_flags.apply_audit_flags(rows, {"gte": "2026-10-14T00:00:00Z"}, FlagConfig())
    assert out is rows
    assert [f.code for f in rows[0].flags] == ["off_hours"]
    assert rows[1].flags == []


def test_apply_audit_flags_long_range_is_clamped_and_keeps_row_flags(monkeypatch):
    fake = FakeEs(lambda b: agg_response([]))
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    rows = [
        audit(ts="2026-08-01T04:00:00", method="delete", table="PatientAllergyMapping"),
        audit(ts="2026-10-14T04:00:00", method="update"),
    ]
    log_flags.apply_audit_flags(rows, None, FlagConfig())  # rows span 74 days
    assert "sensitive_change" in [f.code for f in rows[0].flags]
    must = fake.bodies[0]["query"]["bool"]["must"]
    rng = next(c["range"]["@timestamp"] for c in must if "range" in c)
    assert log_flags._parse_ts(rng["lte"]) - log_flags._parse_ts(rng["gte"]) <= log_flags.MAX_SPAN


def test_apply_audit_flags_empty_rows_makes_no_query(monkeypatch):
    fake = FakeEs(lambda b: agg_response([]))
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    assert log_flags.apply_audit_flags([], {"gte": "now-1h"}, FlagConfig()) == []
    assert fake.bodies == []


def test_apply_ops_flags_all_window_rules(monkeypatch):
    t_login = bucket_start("2026-10-14T01:00:00Z")
    t_access = bucket_start("2026-10-14T02:00:00Z")
    t_err = bucket_start("2026-10-14T03:00:00Z")
    err_ms = 5 * 60 * 1000
    t_err5 = int(__import__("datetime").datetime(2026, 10, 14, 3, 2, tzinfo=__import__("datetime").timezone.utc)
                 .timestamp() * 1000)
    t_err5 -= t_err5 % err_ms

    def responder(body):
        field = _field_of(body)
        if field == "user_id.keyword" and "distinct" in str(body):
            return agg_response([(t_access, [("u2", 60, 35)])])
        if field == "user_id.keyword":
            return agg_response([(t_login, [("u1", 6)])])
        if field == "service.keyword":
            return agg_response([(t_err5, [("patient", 25)])])
        raise AssertionError(field)

    fake = FakeEs(responder)
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    rows = [
        ops("auth", ts="2026-10-14T01:02:00Z", user_id="u1", event="login_failed"),
        ops("auth", ts="2026-10-14T01:02:00Z", user_id="u1", event="login_ok"),
        ops("request", ts="2026-10-14T02:03:00Z", user_id="u2", method="GET", path="/api/v1/patients/12"),
        ops("request", ts="2026-10-14T02:03:00Z", user_id="u2", method="GET", path="/api/v1/activities"),
        ops("request", ts="2026-10-14T03:02:30Z", service="patient", status=503, user_id="u3"),
        ops("error", ts="2026-10-14T03:03:00Z", service="patient"),
        ops("error", ts="2026-10-14T03:03:00Z", service="activity"),
        ops("request", ts="2026-10-14T05:00:00Z", service="patient", status=200, query="mask=false", user_id="u4",
            method="GET", path="/patients/5"),
    ]
    out = log_flags.apply_ops_flags(rows, {"gte": "2026-10-14T00:00:00Z", "lte": "2026-10-14T06:00:00Z"},
                                    FlagConfig())
    assert out is rows
    codes = [[f.code for f in r.flags] for r in rows]
    assert codes == [["failed_login_burst"], [], ["mass_access"], [], ["error_spike"], ["error_spike"], [],
                     ["mass_access"]]
    assert rows[0].flags[0].reason == "6 failed logins for u1 in 10 min"
    assert rows[2].flags[0].severity == "critical"
    assert rows[2].flags[0].reason == "35 distinct patient records read by u2 in 10 min"
    assert rows[4].flags[0].reason == "25 errors from patient in 5 min" and rows[4].flags[0].severity == "warning"


def test_apply_ops_flags_failed_login_excludes_unknown_bucket(monkeypatch):
    t = bucket_start("2026-10-14T01:00:00Z")
    fake = FakeEs(lambda b: agg_response([(t, [("unknown", 9)])]))
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    rows = [ops("auth", ts="2026-10-14T01:02:00Z", user_id="unknown", event="login_failed"),
            ops("auth", ts="2026-10-14T01:02:00Z", user_id="u1", event="login_failed")]
    log_flags.apply_ops_flags(rows, {"gte": "2026-10-14T00:00:00Z"}, FlagConfig())
    assert rows[0].flags == [] and rows[1].flags == []
    # and the query itself excludes the bucket
    must_not = fake.bodies[0]["query"]["bool"].get("must_not", [])
    assert any("unknown" in str(c) for c in must_not)


def test_apply_ops_flags_skips_queries_without_relevant_rows(monkeypatch):
    fake = FakeEs(lambda b: agg_response([]))
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    rows = [ops("request", ts="2026-10-14T03:02:30Z", service="patient", status=200, method="POST", path="/x")]
    log_flags.apply_ops_flags(rows, {"gte": "2026-10-14T00:00:00Z"}, FlagConfig())
    assert fake.bodies == []


def test_apply_ops_flags_es_failure_keeps_row_local_flags(monkeypatch):
    def boom(**kw):
        raise RuntimeError("bad agg")

    monkeypatch.setattr(log_flags.es_service, "search_documents", boom)
    rows = [
        ops("auth", ts="2026-10-14T01:02:00Z", user_id="u1", event="login_failed"),
        ops("request", ts="2026-10-14T05:00:00Z", query="mask=false", user_id="u4"),
    ]
    out = log_flags.apply_ops_flags(rows, {"gte": "2026-10-14T00:00:00Z"}, FlagConfig())
    assert out is rows
    assert rows[0].flags == []
    assert [f.code for f in rows[1].flags] == ["mass_access"]


def test_apply_ops_flags_one_failed_rule_does_not_stop_the_others(monkeypatch):
    t = bucket_start("2026-10-14T01:00:00Z")

    def responder(body):
        if _field_of(body) == "service.keyword":
            raise RuntimeError("bad agg")
        return agg_response([(t, [("u1", 6)])])

    monkeypatch.setattr(log_flags.es_service, "search_documents", FakeEs(responder))
    rows = [
        ops("error", ts="2026-10-14T03:03:00Z", service="patient"),
        ops("auth", ts="2026-10-14T01:02:00Z", user_id="u1", event="login_failed"),
    ]
    log_flags.apply_ops_flags(rows, {"gte": "2026-10-14T00:00:00Z", "lte": "2026-10-14T06:00:00Z"}, FlagConfig())
    assert rows[0].flags == []
    assert [f.code for f in rows[1].flags] == ["failed_login_burst"]


def test_apply_ops_flags_long_range_is_clamped_and_keeps_row_flags(monkeypatch):
    fake = FakeEs(lambda b: agg_response([]))
    monkeypatch.setattr(log_flags.es_service, "search_documents", fake)
    rows = [
        ops("request", ts="2026-10-14T05:00:00Z", query="mask=false", user_id="u4"),
        ops("auth", ts="2026-10-14T01:02:00Z", user_id="u1", event="login_failed"),
    ]
    log_flags.apply_ops_flags(rows, {"gte": "2026-07-01T00:00:00Z", "lte": "2026-10-14T06:00:00Z"}, FlagConfig())
    assert [f.code for f in rows[0].flags] == ["mass_access"]
    must = fake.bodies[0]["query"]["bool"]["must"]
    rng = next(c["range"]["@timestamp"] for c in must if "range" in c)
    assert rng["lte"] == "2026-10-14T06:00:00Z"
    assert log_flags._parse_ts(rng["lte"]) - log_flags._parse_ts(rng["gte"]) <= log_flags.MAX_SPAN


def test_apply_ops_flags_malformed_response_gives_empty_flags(monkeypatch):
    monkeypatch.setattr(log_flags.es_service, "search_documents",
                        lambda **kw: {"aggregations": {"windows": {"buckets": [{"nonsense": 1}]}}})
    rows = [ops("auth", ts="2026-10-14T01:02:00Z", user_id="u1", event="login_failed")]
    log_flags.apply_ops_flags(rows, {"gte": "2026-10-14T00:00:00Z"}, FlagConfig())
    assert rows[0].flags == []


def test_flag_model_and_default_flags_are_independent():
    a, b = ops("auth"), ops("auth")
    a.flags.append(Flag(code="off_hours", reason="r", severity="warning"))
    assert b.flags == []
    assert audit().flags == []
