"""Rule-based flags for abnormal log activity.

Row-local rules look at one log row; window rules ask Elasticsearch for the
(key, time bucket) pairs that crossed a threshold in a single bounded
date_histogram x terms aggregation, then mark the rows that fall in them.

Windows use fixed time buckets aligned to the epoch, so a burst that straddles
a bucket boundary can be missed (documented approximation).

Flag reasons carry actor names, ids, table and field names and counts only,
never the values of any record field.
"""
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from app.crud.logs_crud import _environment_condition
from app.crud.ops_logs_crud import SCHEMA, _path_rule
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_document import Flag, LogDocument
from app.schemas.log_query import LogQuery
from app.schemas.ops_log import OpsLogDocument

logger = logging.getLogger("uvicorn")

SGT = timezone(timedelta(hours=8))
MAX_SPAN = timedelta(days=30)
MAX_TERMS = 50
JSON_HEADERS = {"Content-Type": "application/json"}

DEFAULT_SENSITIVE_FIELDS = ("nric", "NRIC", "dateOfBirth", "DOB", "dob")
PATIENT_READ_PATH = "(/api/v1)?/patients/[0-9]+/?"
PATIENT_READ_RE = re.compile(r"^(/api/v1)?/patients/[0-9]+/?$")
_TIME_RE = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")
_NOW_RE = re.compile(r"^now-(\d+)([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
        if value > 0:
            return value
    except ValueError:
        pass
    logger.warning("Invalid %s=%r; using default %s", name, raw, default)
    return default


def _env_time(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    if _TIME_RE.match(raw.strip()):
        return raw.strip()
    logger.warning("Invalid %s=%r; using default %s", name, raw, default)
    return default


@dataclass(frozen=True)
class FlagConfig:
    failed_login_count: int = 5
    failed_login_window_min: int = 10
    bulk_delete_count: int = 10
    bulk_delete_window_min: int = 10
    off_hours_start: str = "22:00"
    off_hours_end: str = "07:00"
    mass_access_count: int = 30
    mass_access_window_min: int = 10
    sensitive_fields: Tuple[str, ...] = DEFAULT_SENSITIVE_FIELDS
    error_spike_count: int = 20
    error_spike_window_min: int = 5

    @classmethod
    def from_env(cls) -> "FlagConfig":
        d = cls()
        raw_fields = os.getenv("FLAG_SENSITIVE_FIELDS")
        fields = tuple(f.strip() for f in raw_fields.split(",") if f.strip()) if raw_fields else ()
        return cls(
            failed_login_count=_env_int("FLAG_FAILED_LOGIN_COUNT", d.failed_login_count),
            failed_login_window_min=_env_int("FLAG_FAILED_LOGIN_WINDOW_MIN", d.failed_login_window_min),
            bulk_delete_count=_env_int("FLAG_BULK_DELETE_COUNT", d.bulk_delete_count),
            bulk_delete_window_min=_env_int("FLAG_BULK_DELETE_WINDOW_MIN", d.bulk_delete_window_min),
            off_hours_start=_env_time("FLAG_OFF_HOURS_START", d.off_hours_start),
            off_hours_end=_env_time("FLAG_OFF_HOURS_END", d.off_hours_end),
            mass_access_count=_env_int("FLAG_MASS_ACCESS_COUNT", d.mass_access_count),
            mass_access_window_min=_env_int("FLAG_MASS_ACCESS_WINDOW_MIN", d.mass_access_window_min),
            sensitive_fields=fields or d.sensitive_fields,
            error_spike_count=_env_int("FLAG_ERROR_SPIKE_COUNT", d.error_spike_count),
            error_spike_window_min=_env_int("FLAG_ERROR_SPIKE_WINDOW_MIN", d.error_spike_window_min),
        )


# ---------------------------------------------------------------- time helpers

def _parse_ts(value: Any, naive_tz: timezone = timezone.utc) -> Optional[datetime]:
    """An aware datetime, or None. Naive values are read in naive_tz."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _minutes_of_day(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _iso_z(dt: datetime) -> str:
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (utc.microsecond // 1000)


def _range_bound_seconds_ago(value: Any) -> Optional[int]:
    match = _NOW_RE.match(value) if isinstance(value, str) else None
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)] if match else None


def _check_span(time_range: Dict[str, Any]) -> None:
    """Keep every flag aggregation inside the 30 day bound."""
    gte, lte = time_range.get("gte"), time_range.get("lte")
    ago = _range_bound_seconds_ago(gte)
    if ago is not None and ago > MAX_SPAN.total_seconds():
        raise ValueError("time range is longer than 30 days")
    start = _parse_ts(gte) if isinstance(gte, str) and ago is None else None
    end = _parse_ts(lte) if isinstance(lte, str) else None
    if start and end and end - start > MAX_SPAN:
        raise ValueError("time range is longer than 30 days")
    if start and not end and datetime.now(timezone.utc) - start > MAX_SPAN:
        raise ValueError("time range is longer than 30 days")


def _range_for(rows: Iterable[Any], time_range: Optional[Dict[str, Any]],
               naive_tz: timezone) -> Optional[Dict[str, Any]]:
    """The request's time range, or one derived from the rows when it has none."""
    if time_range:
        return dict(time_range)
    stamps = [t for t in (_parse_ts(r.timestamp, naive_tz) for r in rows) if t]
    if not stamps:
        return None
    return {"gte": _iso_z(min(stamps)), "lte": _iso_z(max(stamps) + timedelta(seconds=1))}


# ---------------------------------------------------------------- row-local rules

def off_hours(doc: LogDocument, cfg: FlagConfig) -> Optional[Flag]:
    """An audit change made inside the off-hours window, read in Singapore time.
    The window may cross midnight (22:00 - 07:00). Start is inclusive, end exclusive."""
    stamp = _parse_ts(doc.timestamp, SGT)
    if stamp is None:
        return None
    local = stamp.astimezone(SGT)
    minute = local.hour * 60 + local.minute
    start, end = _minutes_of_day(cfg.off_hours_start), _minutes_of_day(cfg.off_hours_end)
    inside = (start <= minute < end) if start < end else (minute >= start or minute < end)
    if not inside:
        return None
    return Flag(code="off_hours", severity="warning",
                reason="Change made at %s SGT, outside working hours" % local.strftime("%H:%M"))


def sensitive_change(doc: LogDocument, cfg: FlagConfig) -> Optional[Flag]:
    """An update/delete where a sensitive key changed, or any PatientAllergyMapping delete.
    Key names only: values never go into the reason."""
    action = (doc.method or "").lower()
    if action not in ("update", "delete"):
        return None
    if action == "delete" and doc.table == "PatientAllergyMapping":
        return Flag(code="sensitive_change", severity="critical",
                    reason="Delete on PatientAllergyMapping")
    original = doc.original_data if isinstance(doc.original_data, dict) else {}
    updated = doc.updated_data if isinstance(doc.updated_data, dict) else {}
    if action == "delete":
        changed = [k for k in cfg.sensitive_fields if k in original]
    else:
        changed = [k for k in cfg.sensitive_fields
                   if (k in original or k in updated) and original.get(k) != updated.get(k)]
    if not changed:
        return None
    return Flag(code="sensitive_change", severity="critical",
                reason="Sensitive field changed (%s) on %s" % (", ".join(changed), action) +
                       (" in %s" % doc.table if doc.table else ""))


def mask_false(doc: OpsLogDocument) -> Optional[Flag]:
    """A request that asked for unmasked data (mask=false in the query string)."""
    if doc.query and "mask=false" in doc.query.lower():
        who = doc.user_id or "unknown user"
        return Flag(code="mass_access", severity="critical",
                    reason="Unmasked patient data requested by %s" % who)
    return None


# ---------------------------------------------------------------- window rules

class HotBuckets(set):
    """The hot (key, bucket_start_ms) pairs, with the count that made each one hot."""

    def __init__(self, iterable: Iterable[Tuple[Any, int]] = ()):
        super().__init__(iterable)
        self.counts: Dict[Tuple[Any, int], int] = {}


def burst_buckets(
    es_search: Callable[..., Dict[str, Any]],
    index: str,
    query: Dict[str, Any],
    field: str,
    window_min: int,
    threshold: int,
    time_range: Dict[str, Any],
    distinct_field: Optional[str] = None,
) -> HotBuckets:
    """(key, bucket_start_ms) pairs with at least `threshold` hits in one fixed bucket.

    One date_histogram(fixed_interval=<window>m) x terms(field, size 50) aggregation,
    no hits returned. With distinct_field, the count is the number of distinct values
    of that field instead of the number of documents.
    """
    _check_span(time_range)
    base = dict(query["bool"]) if isinstance(query, dict) and "bool" in query else {"must": [query]}
    must = base.get("must", [])
    must = list(must) if isinstance(must, list) else [must]
    must.append({"range": {"@timestamp": time_range}})
    base["must"] = must

    by_key: Dict[str, Any] = {"terms": {"field": field, "size": MAX_TERMS, "min_doc_count": threshold}}
    if distinct_field:
        by_key["aggs"] = {
            "distinct": {"cardinality": {"field": distinct_field}},
            "keep": {"bucket_selector": {
                "buckets_path": {"d": "distinct"},
                "script": "params.d >= %d" % threshold,
            }},
        }
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": base},
        "aggs": {"windows": {
            "date_histogram": {
                "field": "@timestamp",
                "fixed_interval": "%dm" % window_min,
                "min_doc_count": 1,
            },
            "aggs": {"by_key": by_key},
        }},
    }
    response = es_search(index=index, body=body, headers=JSON_HEADERS)
    hot = HotBuckets()
    for window in ((response or {}).get("aggregations", {}).get("windows", {}).get("buckets", [])):
        start = int(window["key"])
        for bucket in window["by_key"]["buckets"]:
            count = int(bucket["distinct"]["value"]) if distinct_field else int(bucket["doc_count"])
            if count < threshold:
                continue
            pair = (bucket["key"], start)
            hot.add(pair)
            hot.counts[pair] = count
    return hot


def flag_rows(
    rows: List[Any],
    buckets: HotBuckets,
    key_fn: Callable[[Any], Any],
    window_min: int,
    code: str,
    severity: str,
    reason_fn: Callable[[Any, int, int], str],
    row_filter: Optional[Callable[[Any], bool]] = None,
    naive_tz: timezone = timezone.utc,
) -> None:
    """Add a flag to each row whose (key, bucket) is hot. Mutates rows."""
    if not buckets:
        return
    interval = window_min * 60 * 1000
    for row in rows:
        if row_filter and not row_filter(row):
            continue
        key = key_fn(row)
        stamp = _parse_ts(row.timestamp, naive_tz)
        if key is None or stamp is None:
            continue
        ms = _epoch_ms(stamp)
        pair = (key, ms - ms % interval)
        if pair not in buckets or any(f.code == code for f in row.flags):
            continue
        row.flags.append(Flag(code=code, severity=severity, reason=reason_fn(key, buckets.counts.get(pair, 0), window_min)))


def _add(row: Any, flag: Optional[Flag]) -> None:
    if flag and not any(f.code == flag.code for f in row.flags):
        row.flags.append(flag)


def _search(**kwargs: Any) -> Dict[str, Any]:
    # Resolved at call time so tests (and a reconnected client) can swap es_service.
    return es_service.search_documents(**kwargs)


def _env_scope() -> List[Dict[str, Any]]:
    condition = _environment_condition(LogQuery())
    return [condition] if condition else []


# ---------------------------------------------------------------- audit

_AUDIT_SHAPE = {"bool": {"should": [
    {"bool": {"must": [{"match_phrase": {"message": '"%s"' % k}}
                       for k in ("user", "user_full_name", "table", "action", "log_text")]}},
    {"bool": {"must": [{"exists": {"field": k}} for k in ("user", "table", "action", "log_text")]}},
], "minimum_should_match": 1}}


def _audit_delete_query() -> Dict[str, Any]:
    return {"bool": {"must": [
        _AUDIT_SHAPE,
        {"bool": {"should": [
            {"match_phrase": {"message": '"action": "delete"'}},
            {"match_phrase": {"action": "delete"}},
        ], "minimum_should_match": 1}},
        {"exists": {"field": "user"}},
    ] + _env_scope()}}


def apply_audit_flags(rows: List[LogDocument], time_range: Optional[Dict[str, Any]],
                      cfg: Optional[FlagConfig] = None) -> List[LogDocument]:
    """Attach flags to audit rows in place. Any failure leaves every row with flags=[]."""
    try:
        cfg = cfg or FlagConfig.from_env()
        for row in rows:
            _add(row, sensitive_change(row, cfg))
            _add(row, off_hours(row, cfg))
        deletes = [r for r in rows if (r.method or "").lower() == "delete" and r.user]
        if deletes:
            window = _range_for(rows, time_range, SGT)
            if window:
                names = {r.user: (r.user_full_name or r.user) for r in deletes}
                hot = burst_buckets(_search, "*", _audit_delete_query(), "user.keyword",
                                    cfg.bulk_delete_window_min, cfg.bulk_delete_count, window)
                flag_rows(
                    deletes, hot, key_fn=lambda r: r.user, window_min=cfg.bulk_delete_window_min,
                    code="bulk_delete", severity="critical", naive_tz=SGT,
                    reason_fn=lambda key, n, w: "%d deletes by %s in %d min" % (n, names.get(key, key), w),
                )
    except Exception:
        logger.exception("Computing audit log flags failed; returning rows without flags")
        for row in rows:
            row.flags = []
    return rows


# ---------------------------------------------------------------- ops

def _ops_base(ops_type: str) -> List[Dict[str, Any]]:
    return [{"match_phrase": {"schema": SCHEMA}}, {"match_phrase": {"ops_type": ops_type}}] + _env_scope()


def _is_failed_login(r: OpsLogDocument) -> bool:
    return r.ops_type == "auth" and r.event == "login_failed" and bool(r.user_id) and r.user_id != "unknown"


def _is_patient_read(r: OpsLogDocument) -> bool:
    return (r.ops_type == "request" and (r.method or "").upper() == "GET" and bool(r.user_id)
            and bool(r.path) and PATIENT_READ_RE.match(r.path) is not None)


def _is_server_error(r: OpsLogDocument) -> bool:
    return bool(r.service) and (r.ops_type == "error" or (r.ops_type == "request" and (r.status or 0) >= 500))


def apply_ops_flags(rows: List[OpsLogDocument], time_range: Optional[Dict[str, Any]],
                    cfg: Optional[FlagConfig] = None) -> List[OpsLogDocument]:
    """Attach flags to ops rows in place. Any failure leaves every row with flags=[]."""
    try:
        cfg = cfg or FlagConfig.from_env()
        for row in rows:
            _add(row, mask_false(row))
        window = _range_for(rows, time_range, timezone.utc)
        if window and any(_is_failed_login(r) for r in rows):
            query = {"bool": {
                "must": _ops_base("auth") + [{"match_phrase": {"auth_event": "login_failed"}},
                                             {"exists": {"field": "user_id"}}],
                "must_not": [{"match_phrase": {"user_id": "unknown"}}],
            }}
            hot = burst_buckets(_search, "*", query, "user_id.keyword", cfg.failed_login_window_min,
                                cfg.failed_login_count, window)
            flag_rows(rows, hot, key_fn=lambda r: r.user_id, window_min=cfg.failed_login_window_min,
                      code="failed_login_burst", severity="warning", row_filter=_is_failed_login,
                      reason_fn=lambda key, n, w: "%d failed logins for %s in %d min" % (n, key, w))
        if window and any(_is_patient_read(r) for r in rows):
            query = {"bool": {"must": _ops_base("request") + [
                {"match_phrase": {"method": "GET"}},
                _path_rule("regexp", PATIENT_READ_PATH),
                {"exists": {"field": "user_id"}},
            ]}}
            hot = burst_buckets(_search, "*", query, "user_id.keyword", cfg.mass_access_window_min,
                                cfg.mass_access_count, window, distinct_field="http_path.keyword")
            flag_rows(rows, hot, key_fn=lambda r: r.user_id, window_min=cfg.mass_access_window_min,
                      code="mass_access", severity="critical", row_filter=_is_patient_read,
                      reason_fn=lambda key, n, w: "%d distinct patient records read by %s in %d min" % (n, key, w))
        if window and any(_is_server_error(r) for r in rows):
            query = {"bool": {
                "must": [{"match_phrase": {"schema": SCHEMA}}] + _env_scope(),
                "should": [
                    {"match_phrase": {"ops_type": "error"}},
                    {"bool": {"must": [{"match_phrase": {"ops_type": "request"}},
                                       {"range": {"status": {"gte": 500}}}]}},
                ],
                "minimum_should_match": 1,
            }}
            hot = burst_buckets(_search, "*", query, "service.keyword", cfg.error_spike_window_min,
                                cfg.error_spike_count, window)
            flag_rows(rows, hot, key_fn=lambda r: r.service, window_min=cfg.error_spike_window_min,
                      code="error_spike", severity="warning", row_filter=_is_server_error,
                      reason_fn=lambda key, n, w: "%d errors from %s in %d min" % (n, key, w))
    except Exception:
        logger.exception("Computing ops log flags failed; returning rows without flags")
        for row in rows:
            row.flags = []
    return rows
