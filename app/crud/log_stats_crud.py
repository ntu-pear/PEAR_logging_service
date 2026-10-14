"""Aggregated statistics over the logs, for the admin dashboard.

One bounded Elasticsearch query per section, each with size 0 aggregations
(terms never above 20), over a window of at most 30 days. Ops lines are found
by schema "pear-app-v1" plus ops_type, audit lines by the audit shape from
log_flags. Times are bucket starts in ISO 8601 UTC; hourly buckets for 24h and
Singapore calendar days for 7d and 30d.

A failing section raises (the router answers 503). Only recent_flags degrades:
each flag detector that fails is logged and skipped.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.crud.ops_logs_crud import SCHEMA, SENSITIVE_REQUEST_RULES, _text
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_stats import (
    AuditDayAction, AuditStats, AuthPoint, AuthStats, LogStats, RecentFlag, StatsTotals,
    TopAccount, TopEndpoint, TopTable, TrafficPoint,
)
from app.services import log_flags
from app.services.log_flags import FlagConfig, burst_buckets

logger = logging.getLogger("uvicorn")

WINDOWS: Dict[str, Tuple[str, str]] = {
    "24h": ("now-24h", "hour"),
    "7d": ("now-7d", "day"),
    "30d": ("now-30d", "day"),
}
TIME_ZONE = "Asia/Singapore"
JSON_HEADERS = {"Content-Type": "application/json"}
MAX_SERVICES = 20
MAX_FLAGS = 20
TOP_ENDPOINTS = 10
TOP_N = 5
STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx")

_REQUEST = {"match_phrase": {"ops_type": "request"}}
_ERROR_OR_5XX = {"bool": {"should": [
    {"match_phrase": {"ops_type": "error"}},
    {"bool": {"must": [_REQUEST, {"range": {"status": {"gte": 500}}}]}},
], "minimum_should_match": 1}}
_FAILED_LOGIN = {"bool": {"must": [
    {"match_phrase": {"ops_type": "auth"}},
    {"match_phrase": {"auth_event": "login_failed"}},
]}}


def _search(**kwargs: Any) -> Dict[str, Any]:
    # Resolved at call time so tests (and a reconnected client) can swap es_service.
    return es_service.search_documents(**kwargs)


def _run(body: Dict[str, Any]) -> Dict[str, Any]:
    return _search(index="*", body=body, headers=JSON_HEADERS) or {}


def _aggs(response: Dict[str, Any]) -> Dict[str, Any]:
    return response.get("aggregations") or {}


def _buckets(agg: Any) -> List[Dict[str, Any]]:
    return (agg or {}).get("buckets") or []


def _iso(ms: Any) -> str:
    return log_flags._iso_z(datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc))


def _ops(start: str, *extra: Dict[str, Any]) -> Dict[str, Any]:
    must = [{"match_phrase": {"schema": SCHEMA}}] + log_flags._env_scope() + list(extra)
    must.append({"range": {"@timestamp": {"gte": start}}})
    return {"bool": {"must": must}}


def _audit(start: str) -> Dict[str, Any]:
    must = [log_flags._AUDIT_SHAPE] + log_flags._env_scope() + [{"range": {"@timestamp": {"gte": start}}}]
    return {"bool": {"must": must, "must_not": [{"prefix": {"table.keyword": "REF_"}}]}}


def _histogram(unit: str) -> Dict[str, Any]:
    interval = {"fixed_interval": "1h"} if unit == "hour" else {"calendar_interval": "1d"}
    return dict({"field": "@timestamp", "time_zone": TIME_ZONE, "min_doc_count": 1}, **interval)


def _size_zero(query: Dict[str, Any], aggs: Dict[str, Any], total: bool = False) -> Dict[str, Any]:
    return {"size": 0, "track_total_hits": total, "query": query, "aggs": aggs}


# ---------------------------------------------------------------- sections

def _totals(start: str) -> StatsTotals:
    body = _size_zero(_ops(start), {
        "requests": {"filter": _REQUEST},
        "errors": {"filter": _ERROR_OR_5XX},
        "failed_logins": {"filter": _FAILED_LOGIN},
        "sensitive_access": {"filter": {"bool": {"must": [
            _REQUEST, {"bool": {"should": list(SENSITIVE_REQUEST_RULES), "minimum_should_match": 1}}]}}},
    })
    aggs = _aggs(_run(body))
    count = lambda name: int((aggs.get(name) or {}).get("doc_count", 0))  # noqa: E731
    return StatsTotals(requests=count("requests"), errors=count("errors"),
                       failed_logins=count("failed_logins"), sensitive_access=count("sensitive_access"))


def _traffic(start: str, unit: str) -> List[TrafficPoint]:
    body = _size_zero(
        _ops(start, {"bool": {"should": [_REQUEST, {"match_phrase": {"ops_type": "error"}}],
                              "minimum_should_match": 1}}),
        {"over_time": {"date_histogram": _histogram(unit), "aggs": {
            "by_service": {"terms": {"field": "service.keyword", "size": MAX_SERVICES}, "aggs": {
                "requests": {"filter": _REQUEST},
                "errors": {"filter": _ERROR_OR_5XX},
            }},
        }}},
    )
    points = []
    for hour in _buckets(_aggs(_run(body)).get("over_time")):
        for svc in _buckets(hour.get("by_service")):
            points.append(TrafficPoint(
                t=_iso(hour["key"]), service=str(svc["key"]),
                requests=int(svc["requests"]["doc_count"]), errors=int(svc["errors"]["doc_count"]),
            ))
    return points


def _p95(bucket: Dict[str, Any]) -> Optional[float]:
    values = (bucket.get("p95") or {}).get("values") or {}
    for value in values.values():
        return float(value) if value is not None else None
    return None


def _top_endpoints(start: str) -> List[TopEndpoint]:
    body = _size_zero(_ops(start, _REQUEST), {
        "endpoints": {"terms": {"field": "http_path.keyword", "size": TOP_ENDPOINTS}, "aggs": {
            "p95": {"percentiles": {"field": "duration_ms", "percents": [95]}},
        }},
    })
    return [TopEndpoint(path=str(b["key"]), count=int(b["doc_count"]), p95_ms=_p95(b))
            for b in _buckets(_aggs(_run(body)).get("endpoints"))]


def _status_mix(start: str) -> Dict[str, int]:
    ranges = [{"key": "%dxx" % n, "from": n * 100, "to": n * 100 + 100} for n in (2, 3, 4, 5)]
    body = _size_zero(_ops(start, _REQUEST), {"status": {"range": {"field": "status", "ranges": ranges}}})
    mix = {name: 0 for name in STATUS_CLASSES}
    for bucket in _buckets(_aggs(_run(body)).get("status")):
        if bucket.get("key") in mix:
            mix[bucket["key"]] = int(bucket["doc_count"])
    return mix


def _auth(start: str, unit: str) -> AuthStats:
    body = _size_zero(_ops(start, _FAILED_LOGIN), {
        "failed_over_time": {"date_histogram": _histogram(unit)},
        "top_users": {"terms": {"field": "user_id.keyword", "size": TOP_N, "exclude": ["unknown"]}},
    })
    aggs = _aggs(_run(body))
    return AuthStats(
        failed_logins_over_time=[AuthPoint(t=_iso(b["key"]), count=int(b["doc_count"]))
                                 for b in _buckets(aggs.get("failed_over_time"))],
        top_accounts=[TopAccount(user_id=str(b["key"]), count=int(b["doc_count"]))
                      for b in _buckets(aggs.get("top_users"))],
    )


def _audit_stats(start: str) -> Tuple[AuditStats, int]:
    body = _size_zero(_audit(start), {
        "per_day": {"date_histogram": _histogram("day"), "aggs": {
            "by_action": {"terms": {"field": "action.keyword", "size": 10}},
        }},
        "tables": {"terms": {"field": "table.keyword", "size": TOP_N}},
        "actors": {"terms": {"field": "user.keyword", "size": TOP_N}},
    }, total=True)
    response = _run(body)
    aggs = _aggs(response)
    total = (response.get("hits") or {}).get("total", 0)
    total = int(total.get("value", 0)) if isinstance(total, dict) else int(total or 0)
    stats = AuditStats(
        changes_per_day=[AuditDayAction(t=_iso(day["key"]), action=str(a["key"]), count=int(a["doc_count"]))
                         for day in _buckets(aggs.get("per_day")) for a in _buckets(day.get("by_action"))],
        top_tables=[TopTable(table=str(b["key"]), count=int(b["doc_count"])) for b in _buckets(aggs.get("tables"))],
        top_actors=[TopAccount(user_id=str(b["key"]), count=int(b["doc_count"]))
                    for b in _buckets(aggs.get("actors"))],
    )
    return stats, total


# ---------------------------------------------------------------- flags

def _burst_flags(
    start: str, index_query: Dict[str, Any], field: str, window_min: int, threshold: int,
    code: str, severity: str, link_hint: str, reason_fn: Callable[[Any, int, int], str],
    service_fn: Callable[[Any], Optional[str]], distinct_field: Optional[str] = None,
) -> List[RecentFlag]:
    hot = burst_buckets(_search, "*", index_query, field, window_min, threshold, {"gte": start},
                        distinct_field=distinct_field)
    return [
        RecentFlag(time=_iso(bucket_start), code=code, severity=severity, link_hint=link_hint,
                   reason=reason_fn(key, hot.counts.get((key, bucket_start), 0), window_min),
                   service=service_fn(key))
        for key, bucket_start in hot
    ]


def _mask_false_flags(start: str) -> Tuple[List[RecentFlag], int]:
    body = {
        "size": MAX_FLAGS,
        "track_total_hits": True,
        "_source": ["@timestamp", "timestamp", "user_id", "service"],
        "query": _ops(start, _REQUEST, {"wildcard": {"query.keyword": {"value": "*mask=false*",
                                                                       "case_insensitive": True}}}),
        "sort": [{"@timestamp": {"order": "desc"}}],
    }
    hits = (_run(body).get("hits") or {})
    total = hits.get("total", 0)
    total = int(total.get("value", 0)) if isinstance(total, dict) else int(total or 0)
    flags = []
    for hit in hits.get("hits") or []:
        source = hit.get("_source") or {}
        stamp = _text(source.get("@timestamp")) or _text(source.get("timestamp"))
        parsed = log_flags._parse_ts(stamp)
        if not stamp:
            continue
        who = _text(source.get("user_id")) or "unknown user"
        flags.append(RecentFlag(
            time=log_flags._iso_z(parsed) if parsed else stamp, code="mass_access", severity="critical",
            reason="Unmasked patient data requested by %s" % who,
            service=_text(source.get("service")), link_hint="sensitive",
        ))
    return flags, max(total, len(flags))


def _recent_flags(start: str, cfg: FlagConfig) -> Tuple[List[RecentFlag], int]:
    """Every flag the burst detectors and the mask=false lookup find in the window,
    newest first (at most 20), plus how many were found in all."""
    found: List[RecentFlag] = []
    extra = 0
    detectors = [
        lambda: _burst_flags(
            start, log_flags.failed_login_query(), "user_id.keyword", cfg.failed_login_window_min,
            cfg.failed_login_count, "failed_login_burst", "warning", "auth",
            lambda key, n, w: "%d failed logins for %s in %d min" % (n, key, w), lambda key: "user"),
        lambda: _burst_flags(
            start, log_flags.mass_access_query(), "user_id.keyword", cfg.mass_access_window_min,
            cfg.mass_access_count, "mass_access", "critical", "sensitive",
            lambda key, n, w: "%d distinct patient records read by %s in %d min" % (n, key, w),
            lambda key: "patient", distinct_field="http_path.keyword"),
        lambda: _burst_flags(
            start, log_flags.error_spike_query(), "service.keyword", cfg.error_spike_window_min,
            cfg.error_spike_count, "error_spike", "warning", "errors",
            lambda key, n, w: "%d errors from %s in %d min" % (n, key, w), lambda key: str(key)),
        lambda: _burst_flags(
            start, log_flags._audit_delete_query(), "user.keyword", cfg.bulk_delete_window_min,
            cfg.bulk_delete_count, "bulk_delete", "critical", "patient",
            lambda key, n, w: "%d deletes by %s in %d min" % (n, key, w), lambda key: None),
    ]
    for detect in detectors:
        try:
            found.extend(detect())
        except Exception:
            logger.exception("A flag detector failed for the log statistics; skipping it")
    try:
        masked, total = _mask_false_flags(start)
        found.extend(masked)
        extra = total - len(masked)
    except Exception:
        logger.exception("The mask=false lookup failed for the log statistics; skipping it")
    found.sort(key=lambda flag: flag.time, reverse=True)
    return found[:MAX_FLAGS], len(found) + extra


# ---------------------------------------------------------------- entry point

def compute_stats(window: str, cfg: Optional[FlagConfig] = None) -> LogStats:
    """Statistics for "24h", "7d" or "30d". Raises when a section's query fails."""
    start, unit = WINDOWS[window]
    cfg = cfg or FlagConfig.from_env()
    totals = _totals(start)
    traffic = _traffic(start, unit)
    endpoints = _top_endpoints(start)
    mix = _status_mix(start)
    auth = _auth(start, unit)
    audit, audit_total = _audit_stats(start)
    flags, flagged = _recent_flags(start, cfg)
    totals.audit_changes = audit_total
    totals.flagged = flagged
    return LogStats(window=window, totals=totals, traffic=traffic, top_endpoints=endpoints,
                    status_mix=mix, auth=auth, audit=audit, recent_flags=flags)
