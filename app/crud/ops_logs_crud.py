"""Queries for operational log lines (schema "pear-app-v1").

Kept apart from logs_crud.py on purpose: the audit queries there are not
touched (amendment A3). Ops lines are found by the schema marker plus
ops_type, never by log_type, which Logstash may overwrite.
"""
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from app.crud.logs_crud import _environment_condition
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_query import LogQuery
from app.schemas.ops_log import OpsLogDocument, OpsLogQuery

logger = logging.getLogger("uvicorn")

SCHEMA = "pear-app-v1"
OPS_TYPES = ("error", "request", "auth")
MAX_PAGE_SIZE = 100
MAX_RESULT_WINDOW = 10000
TOTAL_HITS_CAP = MAX_RESULT_WINDOW + 1
MAX_SPAN = timedelta(days=30)
DEFAULT_WINDOW = "now-24h"
SGT_OFFSET = timedelta(hours=8)

# Ops lines write the request path as "http_path" (Logstash strips a top-level
# "path"); "path" stays as an alternative for lines written before that rename.
PATH_FIELDS = ("http_path.keyword", "path.keyword")


def _path_rule(kind: str, value: Any) -> Dict[str, Any]:
    return {"bool": {"should": [{kind: {field: value}} for field in PATH_FIELDS], "minimum_should_match": 1}}


# The "Sensitive access" preset (spec 7.3). This is the one place the list lives.
SENSITIVE_REQUEST_RULES: Tuple[Dict[str, Any], ...] = (
    # Patient record reads: GET /patients/{id} and GET /api/v1/patients/{id}
    {"bool": {"must": [
        {"match_phrase": {"method": "GET"}},
        _path_rule("regexp", "(/api/v1)?/patients/[0-9]+/?"),
    ]}},
    # Unmasking: any request with mask=false in its query string
    {"wildcard": {"query.keyword": {"value": "*mask=false*", "case_insensitive": True}}},
    # The admin NRIC lookup: /admin/get_nric/{id}
    _path_rule("wildcard", {"value": "*/admin/get_nric/*", "case_insensitive": True}),
)


class OpsQueryError(ValueError):
    """A filter the caller can fix (bad date, page too deep). Maps to HTTP 400."""


def _to_utc(value: str, end_of_day: bool) -> datetime:
    """WebFE sends Singapore wall-clock dates; ES @timestamp is UTC."""
    text = value.strip()
    try:
        if "T" in text or " " in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed - SGT_OFFSET
        suffix = "T23:59:59.999999" if end_of_day else "T00:00:00"
        return datetime.fromisoformat(text + suffix) - SGT_OFFSET
    except (ValueError, OverflowError):
        raise OpsQueryError(f"Invalid date: {value!r}")


def build_ops_query(
    ops_type: str,
    query: OpsLogQuery,
    page_no: int = 0,
    page_size: int = 20,
    sensitive: bool = False,
) -> Dict[str, Any]:
    if ops_type not in OPS_TYPES:
        raise ValueError(f"Unknown ops_type {ops_type!r}")
    size = min(max(page_size, 1), MAX_PAGE_SIZE)
    offset = max(page_no, 0) * size
    if offset + size > MAX_RESULT_WINDOW:
        raise OpsQueryError("Page is too deep; narrow the filters or the time range")

    must: List[Dict[str, Any]] = [
        {"match_phrase": {"schema": SCHEMA}},
        {"match_phrase": {"ops_type": ops_type}},
    ]
    for field, value in (
        ("service", query.service),
        ("level", query.level),
        ("request_id", query.request_id),
        ("user_id", query.user_id),
    ):
        if value and value.strip():
            must.append({"match_phrase": {field: value.strip()}})

    env_condition = _environment_condition(LogQuery(environment=query.environment))
    if env_condition:
        must.append(env_condition)

    time_range: Dict[str, str] = {}
    start = _to_utc(query.start_date, end_of_day=False) if query.start_date else None
    end = _to_utc(query.end_date, end_of_day=True) if query.end_date else None
    if start and end and end < start:
        raise OpsQueryError("end_date is before start_date")
    try:
        if end and not start:
            start = end - timedelta(hours=24)
        if start and end and end - start > MAX_SPAN:
            raise OpsQueryError("The date range is at most 30 days")
        if start and not end and datetime.now(timezone.utc).replace(tzinfo=None) - start > MAX_SPAN:
            raise OpsQueryError("start_date is more than 30 days ago; give an end_date within 30 days of it")
    except OverflowError:
        raise OpsQueryError("Invalid date range")
    if start:
        time_range["gte"] = start.isoformat()
    if end:
        time_range["lte"] = end.isoformat()
    if not time_range and not (query.request_id and query.request_id.strip()):
        time_range["gte"] = DEFAULT_WINDOW
    if time_range:
        must.append({"range": {"@timestamp": time_range}})

    if ops_type == "request" and sensitive:
        must.append({"bool": {"should": list(SENSITIVE_REQUEST_RULES), "minimum_should_match": 1}})

    return {
        "query": {"bool": {"must": must}},
        "size": size,
        "from": offset,
        "sort": [{"@timestamp": {"order": query.timestamp_order}}, {"_doc": {"order": query.timestamp_order}}],
        "track_total_hits": TOTAL_HITS_CAP,
    }


def _last(value: Any) -> Any:
    # Logstash's add_field appends, so a field can arrive as a list.
    if isinstance(value, list):
        return value[-1] if value else None
    return value


def _text(value: Any) -> Optional[str]:
    value = _last(value)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _int(value: Any) -> Optional[int]:
    value = _last(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    value = _last(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _auth_event(source: Dict[str, Any]) -> Optional[str]:
    """The auth event name. Lines carry it as "auth_event"; "event" is the ECS
    object Filebeat adds, so it only counts when an old document has a string there."""
    name = _text(source.get("auth_event"))
    if name:
        return name
    legacy = _last(source.get("event"))
    return legacy if isinstance(legacy, str) else None


def _path(source: Dict[str, Any]) -> Optional[str]:
    """The request path: "http_path", else a string "path"."""
    for key in ("http_path", "path"):
        value = _last(source.get(key))
        if isinstance(value, str):
            return value
    return None


# Fields parse_hit reads from the top level of a stored document.
EXPECTED_FIELDS = (
    "timestamp", "ops_type", "message", "level", "service", "environment", "logger", "request_id",
    "user_id", "method", "query", "status", "duration_ms", "reason", "exception_type", "stack_trace",
    "auth_event",
)


def _original_line(source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The raw ops line Filebeat keeps in event.original, when it is one."""
    event = _last(source.get("event"))
    original = event.get("original") if isinstance(event, dict) else None
    if original is None:
        original = source.get("event.original")
    original = _last(original)
    if not isinstance(original, str):
        return None
    try:
        line = json.loads(original)
    except (ValueError, RecursionError):
        return None
    if isinstance(line, dict) and line.get("schema") == SCHEMA:
        return line
    return None


def _recover(source: Dict[str, Any]) -> Dict[str, Any]:
    """Fill fields Logstash dropped from the raw line in event.original."""
    missing = [key for key in EXPECTED_FIELDS if _last(source.get(key)) is None]
    if _path(source) is None:
        missing.append("http_path")
    if not missing:
        return source
    line = _original_line(source)
    if line is None:
        return source
    merged = dict(source)
    for key in missing:
        if key == "http_path":
            value = next((line[k] for k in ("http_path", "path") if isinstance(line.get(k), str)), None)
        else:
            value = line.get(key)
        if value is not None:
            merged[key] = value
    return merged


def parse_hit(source: Dict[str, Any]) -> Optional[OpsLogDocument]:
    source = _recover(source)
    timestamp = _text(source.get("timestamp")) or _text(source.get("@timestamp"))
    ops_type = _text(source.get("ops_type"))
    if not timestamp or not ops_type:
        return None
    try:
        return OpsLogDocument(
            timestamp=timestamp,
            ops_type=ops_type,
            message=_text(source.get("message")) or "",
            level=_text(source.get("level")),
            service=_text(source.get("service")),
            environment=_text(source.get("environment")),
            logger=_text(source.get("logger")),
            request_id=_text(source.get("request_id")),
            user_id=_text(source.get("user_id")),
            method=_text(source.get("method")),
            path=_path(source),
            query=_text(source.get("query")),
            status=_int(source.get("status")),
            duration_ms=_float(source.get("duration_ms")),
            event=_auth_event(source),
            reason=_text(source.get("reason")),
            exception_type=_text(source.get("exception_type")),
            stack_trace=_text(source.get("stack_trace")),
        )
    except ValidationError:
        return None


def search_ops_logs(
    ops_type: str,
    query: OpsLogQuery,
    page_no: int = 0,
    page_size: int = 20,
    sensitive: bool = False,
) -> Tuple[List[OpsLogDocument], int, int]:
    body = build_ops_query(ops_type, query, page_no, page_size, sensitive=sensitive)
    response = es_service.search_documents(index="*", body=body, headers={"Content-Type": "application/json"})
    hits = response.get("hits", {})
    total = hits.get("total", 0)
    total_records = int(total.get("value", 0)) if isinstance(total, dict) else int(total or 0)
    rows: List[OpsLogDocument] = []
    for hit in hits.get("hits", []):
        row = parse_hit(hit.get("_source", {}))
        if row is None:
            logger.warning("Skipping unparsable ops log document %s", hit.get("_id"))
            continue
        rows.append(row)
    total_pages = math.ceil(total_records / body["size"]) if total_records else 0
    return rows, total_records, total_pages
