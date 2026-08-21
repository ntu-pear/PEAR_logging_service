import json
import re
from typing import Optional, Literal, Tuple
from fastapi import HTTPException
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_document import LogDocument
from app.schemas.log_query import LogQuery
import math
import logging

logger = logging.getLogger("uvicorn")


def _clean_none_string(value, replacement=None):
    """
    The Patient/Activity log formatters render a Python None through
    %-style string templating, which produces the literal string "None"
    instead of JSON null. Normalize that back to `replacement` (None for
    Optional[...] LogDocument fields like patient_id/patient_full_name/
    log_type; "" for user_full_name, which LogDocument requires as a
    non-optional str) instead of leaking "None" as visible text or, for
    patient_id, failing Optional[int] validation and silently dropping
    the whole log entry.
    """
    # Logstash's "add_field" appends to a field that's already present
    # instead of overwriting it, so log_type (forced to "crud_operation"
    # for every CRUD line by a mutate rule server-side) can come back as
    # a single-element array like ["crud_operation"] rather than a plain
    # string -- Pydantic's Optional[str] rejects a list outright, silently
    # dropping the whole log entry. Unwrap to the last (most recently
    # added) element before the None-string check below.
    if isinstance(value, list):
        value = value[-1] if value else replacement
    if isinstance(value, str) and value.strip() == "None":
        return replacement
    return value


def get_logs_by_param_patient(query: LogQuery, pageNo: int = 0, pageSize: int = 10):
    offset = pageNo * pageSize
    must_conditions = []

    # Structural "is this a CRUD log" check. Dual-pathed: Logstash's json
    # filter (server-side, /etc/logstash/conf.d/) promotes fields to the
    # top level of the ES document for any log line that's valid JSON,
    # overwriting "message" in the process -- so a JSON-formatted Patient
    # log no longer contains these fields as text inside "message" at all.
    # Old (pre-migration) lines still do. Match either shape.
    must_conditions.append({
        "bool": {
            "should": [
                {
                    "bool": {
                        "must": [
                            {"match_phrase": {"message": "\"user\""}},
                            {"match_phrase": {"message": "\"user_full_name\""}},
                            {"match_phrase": {"message": "\"table\""}},
                            {"match_phrase": {"message": "\"action\""}},
                            {"match_phrase": {"message": "\"log_text\""}},
                        ]
                    }
                },
                {
                    "bool": {
                        "must": [
                            {"exists": {"field": "user"}},
                            {"exists": {"field": "table"}},
                            {"exists": {"field": "action"}},
                            {"exists": {"field": "log_text"}},
                        ]
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    })
    must_conditions.append({"wildcard": {"log.file.path": "*pear_patient_service*"}})

    # Make sure that action is either create, update or delete
    must_conditions.append({
        "bool": {
        "should": [
            {"match_phrase": {"message": f"\"action\": \"create\""}},
            {"match_phrase": {"message": f"\"action\": \"update\""}},
            {"match_phrase": {"message": f"\"action\": \"delete\""}},
            {"match_phrase": {"action": "create"}},
            {"match_phrase": {"action": "update"}},
            {"match_phrase": {"action": "delete"}},
        ],
        "minimum_should_match": 1
        }
    })

    # Exclude system config logs (ensure only patient logs are being retrieved)
    must_conditions.append({
        "bool": {
            "must_not": [
                {"match_phrase": {"message": f"\"is_system_config\": True"}},
                {"term": {"is_system_config": True}},
            ]
        }
    })

    if query.action:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"action\": \"{query.action}\""}},
                    {"match_phrase": {"action": query.action}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.user:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"user\": \"{query.user}\""}},
                    {"match_phrase": {"user": query.user}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.user_full_name:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"user_full_name\": \"{query.user_full_name}\""}},
                    {"match_phrase": {"user_full_name": query.user_full_name}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.table:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"table\": \"{query.table}\""}},
                    {"match_phrase": {"table": query.table}},
                ],
                "minimum_should_match": 1
            }
        })

    # Handle Patient ID search. Dual-pathed: legacy lines only have the id
    # buried in the raw "message" text (original_data/updated_data), while
    # new-shape lines carry a direct top-level "patient_id" field (see
    # logger_utils.py's log_crud_action, which always passes patient_id
    # through as its own field).
    if query.patient:
        must_conditions.append({
            "bool": {
                "should": [
                    # Legacy: For Patient table - look for 'id' field
                    {
                        "bool": {
                            "must": [
                                {"match_phrase": {"message": "\"table\": \"Patient\""}},
                                {
                                    "bool": {
                                        "should": [
                                            # CORRECTED: Escape the curly braces
                                            {"match_phrase": {"message": f"'updated_data': {{'id': {query.patient}}}"}},
                                            {"match_phrase": {
                                                "message": f"'original_data': {{'id': {query.patient}}}"}},
                                            {"match_phrase": {"message": f"'id': {query.patient}"}}
                                        ],
                                        "minimum_should_match": 1
                                    }
                                }
                            ]
                        }
                    },
                    # Legacy: For all other tables - look for patientId/PatientId/PatientID fields
                    {
                        "bool": {
                            "should": [
                                # The patterns we know work from your test results
                                {"match_phrase": {"message": f"'patientId': {query.patient}"}},
                                {"match_phrase": {"message": f"'PatientId': {query.patient}"}},
                                {"match_phrase": {"message": f"'PatientID': {query.patient}"}}
                            ],
                            "minimum_should_match": 1
                        }
                    },
                    # New shape: direct top-level field
                    {"match_phrase": {"patient_id": query.patient}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.patient_full_name:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"patient_full_name\": \"{query.patient_full_name}\""}},
                    {"match_phrase": {"patient_full_name": query.patient_full_name}},
                ],
                "minimum_should_match": 1
            }
        })

    if query.log_type:
        # NOTE: Logstash forcibly overwrites log_type to "crud_operation" for
        # any successfully JSON-parsed CRUD line (mutate add_field rule in
        # 02-beats-input.conf), so filtering by a real business log_type
        # value only works for legacy (pre-migration) lines.
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"log_type\": \"{query.log_type}\""}},
                    {"match_phrase": {"log_type": query.log_type}},
                ],
                "minimum_should_match": 1
            }
        })

    # Add timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            try:
                from datetime import datetime, timedelta
                start_str = query.start_date.strip()
                if 'T' in start_str or ' ' in start_str:
                    dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(start_str + 'T00:00:00')
                dt_utc = dt - timedelta(hours=8)
                range_filter["range"]["@timestamp"]["gte"] = dt_utc.isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse start date {query.start_date}: {e}")
                range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
            try:
                from datetime import datetime, timedelta
                end_str = query.end_date.strip()
                if 'T' in end_str or ' ' in end_str:
                    dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(end_str + 'T23:59:59')
                dt_utc = dt - timedelta(hours=8)
                range_filter["range"]["@timestamp"]["lte"] = dt_utc.isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse end date {query.end_date}: {e}")
                range_filter["range"]["@timestamp"]["lte"] = query.end_date

        must_conditions.append(range_filter)

    es_query = {
        "query": {"bool": {"must": must_conditions}} if must_conditions else {"match_all": {}},
        "size": pageSize,
        "from": offset,
        "sort": [
            {"@timestamp": {"order": query.timestamp_order}}
        ],
        "track_total_hits": True,
    }

    try:
        response = es_service.search_documents(index="*", body=es_query, headers={"Content-Type": "application/json"})
        hits = response.get('hits', {}).get('hits', [])
        logs = []
        for hit in hits:
            try:
                source = hit["_source"]
                message_str = source.get("message", "")

                # Dual-pathed like get_logs_by_param_activity: a JSON-formatted
                # Patient log has "table"/"action" promoted to the top level by
                # Logstash's json filter, and no longer has these fields as
                # text inside "message" at all. Old (pre-migration) lines
                # still do.
                is_new_shape = "table" in source and "action" in source
                if is_new_shape:
                    parsed_message = source
                elif isinstance(message_str, dict):
                    parsed_message = message_str
                else:
                    parsed_message = None

                    # Attempt 1: strict JSON
                    try:
                        parsed_message = json.loads(message_str)
                    except Exception:
                        pass

                    # Attempt 2: Python dict parsing
                    if parsed_message is None:
                        try:
                            import ast
                            parsed_message = ast.literal_eval(message_str)
                        except Exception:
                            pass

                    # Attempt 3: clean problematic Python syntax
                    if parsed_message is None:
                        try:
                            fixed = message_str

                            # Convert Python literals
                            fixed = fixed.replace("None", "null")
                            fixed = fixed.replace("True", "true")
                            fixed = fixed.replace("False", "false")

                            # Remove Enum objects like <PrivacyStatus.MEDIUM: 2>
                            fixed = re.sub(r"<[^>]+:\s*(\d+)>", r"\1", fixed)

                            # Replace single quotes with double quotes
                            fixed = fixed.replace("'", '"')

                            parsed_message = json.loads(fixed)

                        except Exception as parse_error:
                            logger.warning(f"Skipping unparsable log: {str(parse_error)}")
                            logger.warning(f"Message snippet: {message_str[:200]}")
                            continue

                # Extract data from parsed JSON
                timestamp = parsed_message.get("timestamp", "")
                # Filter by date range (using log timestamp, not @timestamp)
                if query.start_date or query.end_date:
                    try:
                        from datetime import datetime
                        log_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))

                        if query.start_date:
                            start_str = query.start_date.strip()
                            if 'T' in start_str or ' ' in start_str:
                                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                            else:
                                start_dt = datetime.fromisoformat(start_str + 'T00:00:00')
                            if log_dt < start_dt:
                                continue

                        if query.end_date:
                            end_str = query.end_date.strip()
                            if 'T' in end_str or ' ' in end_str:
                                end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                            else:
                                end_dt = datetime.fromisoformat(end_str + 'T23:59:59')
                            if log_dt > end_dt:
                                continue
                    except Exception as e:
                        logger.warning(f"Failed to filter by date: {e}")
                level = parsed_message.get("level", "")
                logger_name = parsed_message.get("logger", "")
                user = parsed_message.get("user", "")
                user_full_name = _clean_none_string(parsed_message.get("user_full_name", ""), replacement="")
                table = parsed_message.get("table", "")
                action = parsed_message.get("action", "")
                message = parsed_message.get("log_text", "")
                log_type = _clean_none_string(parsed_message.get("log_type", ""))
                is_system_config = parsed_message.get("is_system_config", False)
                patient_full_name = _clean_none_string(parsed_message.get("patient_full_name", ""))

                # Parse inner message field. Prefer "crud_payload" -- once the
                # Logstash filter is fixed (see spec §12: the ES field-type
                # collision between plain-text log lines and structured CRUD
                # payloads sharing the "message" field name), Logstash renames
                # the parsed payload to "crud_payload" for any CRUD-shaped
                # line. Falling back to "message" keeps this working exactly
                # as today until that server-side change lands -- no
                # coordinated deploy required between the two.
                inner_message = parsed_message.get("crud_payload", parsed_message.get("message", {}))
                if isinstance(inner_message, str):
                    try:
                        fixed = inner_message
                        fixed = re.sub(r"(?<!\\)'", '"', fixed)
                        fixed = fixed.replace('\\"', "'")
                        inner_message = json.loads(fixed)
                    except:
                        inner_message = {}

                original_data = inner_message.get("original_data", {})
                updated_data = inner_message.get("updated_data", {})
                entity_id = inner_message.get("entity_id")

                # Prefer the direct top-level "patient_id" field (always
                # emitted by logger_utils.py's log_crud_action now) over
                # digging through original_data/updated_data, which was the
                # only option for older callers that never passed patient_id
                # explicitly.
                patient_id = _clean_none_string(parsed_message.get("patient_id"))
                if patient_id is None and table == "Patient":
                    if original_data.get("id"):
                        patient_id = original_data.get("id")
                    elif updated_data.get("id"):
                        patient_id = updated_data.get("id")
                elif patient_id is None:
                    if original_data.get("PatientId"):
                        patient_id = original_data.get("PatientId")
                    elif updated_data.get("PatientId"):
                        patient_id = updated_data.get("PatientId")
                    elif original_data.get("patientId"):
                        patient_id = original_data.get("patientId")
                    elif updated_data.get("PatientID"):
                        patient_id = updated_data.get("PatientID")
                    elif original_data.get("PatientID"):
                        patient_id = original_data.get("PatientID")
                log = LogDocument(
                    timestamp=timestamp,
                    method=action,
                    table=table,
                    patient_id=patient_id,
                    patient_full_name=patient_full_name,
                    user=user,
                    user_full_name=user_full_name,
                    message=message,
                    log_type = log_type,
                    is_system_config=is_system_config,
                    original_data=original_data,
                    updated_data=updated_data
                )
                logs.append(log)
            except Exception as e:
                logger.error(f"Could not read log: {str(e)}")

        totalRecords = response.get('hits', {}).get('total', {}).get('value', 0)
        totalPages = math.ceil(totalRecords / pageSize) if pageSize > 0 else 0

        return logs, totalRecords, totalPages

    except Exception as e:
        logger.error(f"Error querying Elasticsearch: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")


def get_logs_by_param_activity(query: LogQuery, pageNo: int = 0, pageSize: int = 10):
    offset = pageNo * pageSize
    must_conditions = []

    # Structural "is this a CRUD log" check. Dual-pathed: Logstash's json
    # filter (server-side, /etc/logstash/conf.d/) promotes fields to the
    # top level of the ES document for any log line that's valid JSON,
    # overwriting "message" in the process -- so a JSON-formatted Activity
    # log no longer contains these fields as text inside "message" at all.
    # Old (pre-migration) lines still do. Match either shape.
    must_conditions.append({
        "bool": {
            "should": [
                {
                    "bool": {
                        "must": [
                            {"match_phrase": {"message": "\"user\""}},
                            {"match_phrase": {"message": "\"user_full_name\""}},
                            {"match_phrase": {"message": "\"table\""}},
                            {"match_phrase": {"message": "\"action\""}},
                            {"match_phrase": {"message": "\"log_text\""}},
                        ]
                    }
                },
                {
                    "bool": {
                        "must": [
                            {"exists": {"field": "user"}},
                            {"exists": {"field": "table"}},
                            {"exists": {"field": "action"}},
                            {"exists": {"field": "log_text"}},
                        ]
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    })
    must_conditions.append({"wildcard": {"log.file.path": "*pear_activity_service*"}})

    # Make sure that action is either create, update or delete
    must_conditions.append({
        "bool": {
            "should": [
                {"match_phrase": {"message": f"\"action\": \"create\""}},
                {"match_phrase": {"message": f"\"action\": \"update\""}},
                {"match_phrase": {"message": f"\"action\": \"delete\""}},
                {"match_phrase": {"action": "create"}},
                {"match_phrase": {"action": "update"}},
                {"match_phrase": {"action": "delete"}},
            ],
            "minimum_should_match": 1
        }
    })

    if query.action:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"action\": \"{query.action}\""}},
                    {"match_phrase": {"action": query.action}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.user:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"user\": \"{query.user}\""}},
                    {"match_phrase": {"user": query.user}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.table:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"table\": \"{query.table}\""}},
                    {"match_phrase": {"table": query.table}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.log_type:
        # NOTE: Logstash forcibly overwrites log_type to "crud_operation" for
        # any successfully JSON-parsed CRUD line (mutate add_field rule in
        # 02-beats-input.conf), so filtering by a real business log_type
        # value only works for legacy (pre-migration) lines. Left dual-pathed
        # for consistency; the new-shape branch just won't find anything
        # meaningful until that Logstash rule is fixed server-side.
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"log_type\": \"{query.log_type}\""}},
                    {"match_phrase": {"log_type": query.log_type}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.patient:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"patient_id\": \"{query.patient}\""}},
                    {"match_phrase": {"patient_id": query.patient}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.patient_full_name:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"patient_full_name\": \"{query.patient_full_name}\""}},
                    {"match_phrase": {"patient_full_name": query.patient_full_name}},
                ],
                "minimum_should_match": 1
            }
        })

    # Only patient-related activity logs (is_system_config = False)
    must_conditions.append({
        "bool": {
            "must_not": [
                {"match_phrase": {"message": f"\"is_system_config\": True"}},
                {"term": {"is_system_config": True}},
            ]
        }
    })

    # Handle Activity ID search (entity_id in the logs)
    if query.activity:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"'entity_id': {query.activity}"}},
                    {"match_phrase": {"message": f"\"entity_id\": {query.activity}"}},
                    {"match_phrase": {"message.entity_id": query.activity}},
                ],
                "minimum_should_match": 1
            }
        })

    # Add timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            try:
                from datetime import datetime, timedelta
                start_str = query.start_date.strip()
                if 'T' in start_str or ' ' in start_str:
                    dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(start_str + 'T00:00:00')
                dt_utc = dt - timedelta(hours=8)
                range_filter["range"]["@timestamp"]["gte"] = dt_utc.isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse start date {query.start_date}: {e}")
                range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
            try:
                from datetime import datetime, timedelta
                end_str = query.end_date.strip()
                if 'T' in end_str or ' ' in end_str:
                    dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(end_str + 'T23:59:59')
                dt_utc = dt - timedelta(hours=8)
                range_filter["range"]["@timestamp"]["lte"] = dt_utc.isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse end date {query.end_date}: {e}")
                range_filter["range"]["@timestamp"]["lte"] = query.end_date

        must_conditions.append(range_filter)

    es_query = {
        "query": {"bool": {"must": must_conditions}} if must_conditions else {"match_all": {}},
        "size": pageSize,
        "from": offset,
        "sort": [
            {"@timestamp": {"order": query.timestamp_order}}
        ],
        "track_total_hits": True,
    }

    logger.info(f"DEBUG Activity ES Query: {json.dumps(es_query, indent=2)}")

    try:
        response = es_service.search_documents(index="*", body=es_query, headers={"Content-Type": "application/json"})
        hits = response.get('hits', {}).get('hits', [])
        logs = []
        seen_messages = set()

        for hit in hits:
            try:
                source = hit["_source"]
                message_str = source.get("message", "")

                # Dedup by document id, not by "message" content: once
                # Logstash decomposes a JSON log line (see below), "message"
                # becomes a dict, which isn't hashable and can't go in a set().
                doc_id = hit.get("_id")
                if doc_id in seen_messages:
                    logger.debug(f"Skipping duplicate document: {doc_id}")
                    continue
                seen_messages.add(doc_id)

                # Skip logs unrelated to CRUD logs. Only relevant to the
                # legacy (not-yet-decomposed) shape, where "table"/"action"
                # need to already be present in the source. New-shape hits
                # are recognized directly by "table"/"action" already being
                # top-level fields (checked below) -- skip this filter for
                # them entirely, since once Logstash renames "message" away
                # (spec §12), message_str falls back to "" here, which would
                # otherwise incorrectly trip this check for every valid hit.
                is_new_shape = "table" in source and "action" in source
                if not is_new_shape and isinstance(message_str, str) and ('"action"' not in message_str or '"table"' not in message_str):
                    continue

                # Logstash's json filter (server-side, /etc/logstash/conf.d/)
                # runs unconditionally: if Activity's raw log line is valid
                # JSON, Logstash parses it and promotes every field to the
                # TOP LEVEL of the ES document -- overwriting "message" with
                # just the inner {entity_id, original_data, updated_data}
                # payload. If the raw line wasn't valid JSON (legacy shape),
                # "message" is left as the full untouched raw string.
                # is_new_shape (computed above) already tells us which case
                # this hit is in.
                if is_new_shape:
                    parsed_message = source
                elif isinstance(message_str, dict):
                    parsed_message = message_str
                else:
                    # Try parsing as JSON first (most logs are proper JSON)
                    try:
                        parsed_message = json.loads(message_str)
                    except:
                        # Try ast.literal_eval for Python dict syntax with single quotes
                        try:
                            import ast
                            parsed_message = ast.literal_eval(message_str)
                        except:
                            # Last resort: try to fix the JSON
                            try:
                                fixed = message_str.replace("None", "null")
                                fixed = fixed.replace("True", "true").replace("False", "false")
                                fixed = fixed.replace("'", '"')
                                fixed = fixed.replace('\\"', "'")
                                parsed_message = json.loads(fixed)
                            except Exception as parse_error:
                                logger.error(f"Failed to parse message: {str(parse_error)}")
                                logger.error(f"Message content: {message_str[:200]}")
                                continue

                # Extract data from parsed JSON
                timestamp = parsed_message.get("timestamp", "")

                # Filter by date range (using log timestamp, not @timestamp)
                if query.start_date or query.end_date:
                    try:
                        from datetime import datetime
                        log_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))

                        if query.start_date:
                            start_str = query.start_date.strip()
                            if 'T' in start_str or ' ' in start_str:
                                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                            else:
                                start_dt = datetime.fromisoformat(start_str + 'T00:00:00')
                            if log_dt < start_dt:
                                continue

                        if query.end_date:
                            end_str = query.end_date.strip()
                            if 'T' in end_str or ' ' in end_str:
                                end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                            else:
                                end_dt = datetime.fromisoformat(end_str + 'T23:59:59')
                            if log_dt > end_dt:
                                continue
                    except Exception as e:
                        logger.warning(f"Failed to filter by date: {e}")

                level = parsed_message.get("level", "")
                logger_name = parsed_message.get("logger", "")
                user = parsed_message.get("user", "")
                user_full_name = _clean_none_string(parsed_message.get("user_full_name", ""), replacement="")
                table = parsed_message.get("table", "")
                action = parsed_message.get("action", "")
                log_text = parsed_message.get("log_text", "")
                log_type = _clean_none_string(parsed_message.get("log_type", ""))
                is_system_config = parsed_message.get("is_system_config", False)

                # Parse inner message field. Prefer "crud_payload" -- once the
                # Logstash filter is fixed (see spec §12: the ES field-type
                # collision between plain-text log lines and structured CRUD
                # payloads sharing the "message" field name), Logstash renames
                # the parsed payload to "crud_payload" for any CRUD-shaped
                # line. Falling back to "message" keeps this working exactly
                # as today until that server-side change lands -- no
                # coordinated deploy required between the two.
                inner_message = parsed_message.get("crud_payload", parsed_message.get("message", {}))
                if isinstance(inner_message, str):
                    try:
                        fixed = inner_message
                        fixed = re.sub(r"(?<!\\)'", '"', fixed)
                        fixed = fixed.replace('\\"', "'")
                        inner_message = json.loads(fixed)
                    except:
                        inner_message = {}

                original_data = inner_message.get("original_data", {})
                updated_data = inner_message.get("updated_data", {})
                entity_id = inner_message.get("entity_id")

                # Extract patient_id and patient_full_name from root level.
                # _clean_none_string is required here: the formatter renders a
                # missing patient_id as the string "None", which Pydantic's
                # Optional[int] can't coerce -- that was silently dropping every
                # log for patient-independent tables (ACTIVITY, CARE_CENTRE, ...).
                patient_id = _clean_none_string(parsed_message.get("patient_id"))
                patient_full_name = _clean_none_string(parsed_message.get("patient_full_name", ""))

                log = LogDocument(
                    timestamp=timestamp,
                    method=action,
                    table=table,
                    patient_id=patient_id,
                    patient_full_name=patient_full_name,
                    entity_id=entity_id,  # Use entity_id for activity/other entities
                    user=user,
                    user_full_name=user_full_name,
                    message=log_text,
                    log_type=log_type,
                    is_system_config=is_system_config,
                    original_data=original_data,
                    updated_data=updated_data
                )
                logs.append(log)

            except Exception as e:
                logger.error(f"Could not read log: {str(e)}")

        totalRecords = response.get('hits', {}).get('total', {}).get('value', 0)
        totalPages = math.ceil(totalRecords / pageSize) if pageSize > 0 else 0

        return logs, totalRecords, totalPages

    except Exception as e:
        logger.error(f"Error querying Elasticsearch: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")

def get_logs_by_param_user(
        query: LogQuery,
        pageNo: int = 0,
        pageSize: int = 10
):
    """Get user service logs (auth events and user data changes)"""
    offset = pageNo * pageSize
    must_conditions = []

    must_conditions.append({
        "wildcard": {"log.file.path": "*pear_user_service*"}
    })

    # User actions (login, logout, password_change, create, update, delete)
    # Fields are at top level in ES, not inside message
    if query.action:
        must_conditions.append({
            "match_phrase": {"action": query.action}
        })

    if query.user:
        must_conditions.append({
            "match_phrase": {"user": query.user}
        })

    if query.user_full_name:
        must_conditions.append({
            "wildcard": {
                "user_full_name": {
                    "value": f"*{query.user_full_name}*",
                    "case_insensitive": True
                }
            }
        })
    if query.role:
        must_conditions.append({
            "wildcard": {
                "role": {
                    "value": f"*{query.role}*",
                    "case_insensitive": True
                }
            }
        })

    # Note: log_type is inferred from action, not stored in ES
    # We filter by log_type in Python code after parsing

    # Handle timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            try:
                from datetime import datetime, timedelta
                start_str = query.start_date.strip()
                if 'T' in start_str or ' ' in start_str:
                    dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(start_str + 'T00:00:00')
                dt_utc = dt - timedelta(hours=8)
                range_filter["range"]["@timestamp"]["gte"] = dt_utc.isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse start date {query.start_date}: {e}")
                range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
            try:
                from datetime import datetime, timedelta
                end_str = query.end_date.strip()
                if 'T' in end_str or ' ' in end_str:
                    dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(end_str + 'T23:59:59')
                dt_utc = dt - timedelta(hours=8)
                range_filter["range"]["@timestamp"]["lte"] = dt_utc.isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse end date {query.end_date}: {e}")
                range_filter["range"]["@timestamp"]["lte"] = query.end_date

        must_conditions.append(range_filter)

    # Build Elasticsearch query
    es_query = {
        "query": {
            "bool": {"must": must_conditions}
        } if must_conditions else {"match_all": {}},
        "size": pageSize,
        "from": offset,
        "sort": [
            {"@timestamp": {"order": query.timestamp_order}}
        ],
        "track_total_hits": True,
    }

    try:
        response = es_service.search_documents(
            index="logs-*",
            body=es_query,
            headers={"Content-Type": "application/json"}
        )

        hits = response.get('hits', {}).get('hits', [])
        logs = []
        seen_messages = set()

        for hit in hits:
            try:
                source = hit["_source"]

                # Skip duplicates by document ID
                doc_id = hit.get("_id")
                if doc_id in seen_messages:
                    continue
                seen_messages.add(doc_id)

                # Extract fields directly from source (top-level fields in ES)
                # Convert UTC timestamp to SG time
                raw_timestamp = source.get("timestamp", "")
                timestamp = raw_timestamp
                # Filter by date range (using log timestamp, not @timestamp)
                if query.start_date or query.end_date:
                    try:
                        from datetime import datetime
                        log_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))

                        if query.start_date:
                            start_str = query.start_date.strip()
                            if 'T' in start_str or ' ' in start_str:
                                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                            else:
                                start_dt = datetime.fromisoformat(start_str + 'T00:00:00')
                            if log_dt < start_dt:
                                continue

                        if query.end_date:
                            end_str = query.end_date.strip()
                            if 'T' in end_str or ' ' in end_str:
                                end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                            else:
                                end_dt = datetime.fromisoformat(end_str + 'T23:59:59')
                            if log_dt > end_dt:
                                continue
                    except Exception as e:
                        logger.warning(f"Failed to filter by date: {e}")

                user = source.get("user", "")
                user_full_name = source.get("user_full_name", "")
                role = source.get("role", "")
                action = source.get("action", "")
                log_text = source.get("log_text", "")
                table = source.get("table", "User")

                # Infer log_type from action if not present
                log_type = source.get("log_type", "")
                if not log_type and action:
                    auth_actions = ["login", "logout", "password_change"]
                    log_type = "auth" if action.lower() in auth_actions else "data"

                # Parse message field for original_data, updated_data
                message_str = source.get("message", "")
                original_data = None
                updated_data = None

                if message_str:
                    try:
                        if isinstance(message_str, dict):
                            msg_data = message_str
                        else:
                            msg_data = json.loads(message_str)
                        original_data = msg_data.get("original_data")
                        updated_data = msg_data.get("updated_data")
                    except Exception as parse_error:
                        logger.debug(f"Could not parse message field: {str(parse_error)}")

                # Skip if log_type filter doesn't match (when filter is provided)
                if query.log_type and log_type != query.log_type:
                    continue

                # Create standardized LogDocument
                # Note: entity_id is not set for user logs (user ID is string, not int)
                log = LogDocument(
                    timestamp=timestamp,
                    method=action,
                    table=table,
                    user=user,
                    user_full_name=user_full_name,
                    message=log_text,
                    role=role,
                    log_type=log_type,
                    original_data=original_data,
                    updated_data=updated_data
                )
                logs.append(log)

            except Exception as e:
                logger.error(f"Could not read user log: {str(e)}")
                continue

        totalRecords = response.get('hits', {}).get('total', {}).get('value', 0)
        totalPages = math.ceil(totalRecords / pageSize) if pageSize > 0 else 0

        return logs, totalRecords, totalPages

    except Exception as e:
        logger.error(f"Error querying Elasticsearch for user logs: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error querying Elasticsearch: {str(e)}")

def get_logs_by_param_system(
        query: LogQuery,
        pageNo: int = 0,
        pageSize: int = 10
):
    """Get system configuration logs from both patient and activity services"""
    offset = pageNo * pageSize
    must_conditions = []

    # Match logs from patient or activity service with CRUD fields.
    # Dual-pathed: a JSON-formatted producer (Logstash promotes fields to
    # the document root, see get_logs_by_param_activity for the full
    # explanation) no longer has these as text inside "message" at all.
    must_conditions.append({
        "bool": {
            "should": [
                {
                    "bool": {
                        "must": [
                            {"match_phrase": {"message": "\"user\""}},
                            {"match_phrase": {"message": "\"table\""}},
                            {"match_phrase": {"message": "\"action\""}},
                            {"match_phrase": {"message": "\"log_text\""}},
                            {"match_phrase": {"message": "\"is_system_config\""}},
                        ]
                    }
                },
                {
                    "bool": {
                        "must": [
                            {"exists": {"field": "user"}},
                            {"exists": {"field": "table"}},
                            {"exists": {"field": "action"}},
                            {"exists": {"field": "log_text"}},
                            {"exists": {"field": "is_system_config"}},
                        ]
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    })
    must_conditions.append({
        "bool": {
            "should": [
                {"wildcard": {"log.file.path": "*pear_patient_service*"}},
                {"wildcard": {"log.file.path": "*pear_activity_service*"}}
            ],
            "minimum_should_match": 1
        }
    })

    # Only system config logs (is_system_config = True)
    must_conditions.append({
        "bool": {
            "should": [
                {"match_phrase": {"message": "\"is_system_config\": True"}},
                {"term": {"is_system_config": True}},
            ],
            "minimum_should_match": 1
        }
    })

    # Make sure that action is either create, update or delete
    must_conditions.append({
        "bool": {
            "should": [
                {"match_phrase": {"message": "\"action\": \"create\""}},
                {"match_phrase": {"message": "\"action\": \"update\""}},
                {"match_phrase": {"message": "\"action\": \"delete\""}},
                {"match_phrase": {"action": "create"}},
                {"match_phrase": {"action": "update"}},
                {"match_phrase": {"action": "delete"}},
            ],
            "minimum_should_match": 1
        }
    })

    # Apply filters
    if query.action:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"action\": \"{query.action}\""}},
                    {"match_phrase": {"action": query.action}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.user:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"user\": \"{query.user}\""}},
                    {"match_phrase": {"user": query.user}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.user_full_name:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"user_full_name\": \"{query.user_full_name}\""}},
                    {"match_phrase": {"user_full_name": query.user_full_name}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.table:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"table\": \"{query.table}\""}},
                    {"match_phrase": {"table": query.table}},
                ],
                "minimum_should_match": 1
            }
        })
    if query.log_type:
        # NOTE: unreliable for JSON-formatted producers -- see the identical
        # note in get_logs_by_param_activity. Logstash overwrites log_type
        # server-side for any successfully-parsed CRUD line.
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"log_type\": \"{query.log_type}\""}},
                    {"match_phrase": {"log_type": query.log_type}},
                ],
                "minimum_should_match": 1
            }
        })

    # Handle Entity ID search
    if query.activity:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"entity_id\": {query.activity}"}},
                    {"match_phrase": {"message": f"'entity_id': {query.activity}"}},
                    {"match_phrase": {"message.entity_id": query.activity}},
                ],
                "minimum_should_match": 1
            }
        })

    # Add timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            try:
                from datetime import datetime, timedelta
                start_str = query.start_date.strip()
                if 'T' in start_str or ' ' in start_str:
                    dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(start_str + 'T00:00:00')
                dt_utc = dt - timedelta(hours=8)
                range_filter["range"]["@timestamp"]["gte"] = dt_utc.isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse start date {query.start_date}: {e}")
                range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
            try:
                from datetime import datetime, timedelta
                end_str = query.end_date.strip()
                if 'T' in end_str or ' ' in end_str:
                    dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                else:
                    dt = datetime.fromisoformat(end_str + 'T23:59:59')
                dt_utc = dt - timedelta(hours=8)
                range_filter["range"]["@timestamp"]["lte"] = dt_utc.isoformat()
            except Exception as e:
                logger.warning(f"Failed to parse end date {query.end_date}: {e}")
                range_filter["range"]["@timestamp"]["lte"] = query.end_date

        must_conditions.append(range_filter)

    es_query = {
        "query": {"bool": {"must": must_conditions}} if must_conditions else {"match_all": {}},
        "size": pageSize,
        "from": offset,
        "sort": [
            {"@timestamp": {"order": query.timestamp_order}}
        ],
        "track_total_hits": True,
    }

    try:
        response = es_service.search_documents(
            index="*",
            body=es_query,
            headers={"Content-Type": "application/json"}
        )
        hits = response.get('hits', {}).get('hits', [])
        logs = []
        seen_messages = set()

        for hit in hits:
            try:
                source = hit["_source"]
                message_str = source.get("message", "")

                # Dedup by document id, not "message" content -- see the
                # identical note in get_logs_by_param_activity.
                doc_id = hit.get("_id")
                if doc_id in seen_messages:
                    continue
                seen_messages.add(doc_id)

                # Same shape detection as get_logs_by_param_activity: if
                # Logstash's json filter already promoted fields to the
                # document root, use it directly instead of re-parsing
                # "message" (which is no longer the outer envelope).
                if "table" in source and "action" in source:
                    parsed_message = source
                elif isinstance(message_str, dict):
                    parsed_message = message_str
                else:
                    # Try parsing as JSON first
                    try:
                        parsed_message = json.loads(message_str)
                    except:
                        # Try ast.literal_eval for Python dict syntax
                        try:
                            import ast
                            parsed_message = ast.literal_eval(message_str)
                        except:
                            # Last resort: try to fix the JSON
                            try:
                                fixed = message_str.replace("None", "null")
                                fixed = fixed.replace("True", "true").replace("False", "false")
                                fixed = fixed.replace("'", '"')
                                fixed = fixed.replace('\\"', "'")
                                parsed_message = json.loads(fixed)
                            except Exception as parse_error:
                                logger.error(f"Failed to parse message: {str(parse_error)}")
                                continue

                # Extract data from parsed JSON
                timestamp = parsed_message.get("timestamp", "")
                # Filter by date range (using log timestamp, not @timestamp)
                if query.start_date or query.end_date:
                    try:
                        from datetime import datetime
                        log_dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))

                        if query.start_date:
                            start_str = query.start_date.strip()
                            if 'T' in start_str or ' ' in start_str:
                                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                            else:
                                start_dt = datetime.fromisoformat(start_str + 'T00:00:00')
                            if log_dt < start_dt:
                                continue

                        if query.end_date:
                            end_str = query.end_date.strip()
                            if 'T' in end_str or ' ' in end_str:
                                end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                            else:
                                end_dt = datetime.fromisoformat(end_str + 'T23:59:59')
                            if log_dt > end_dt:
                                continue
                    except Exception as e:
                        logger.warning(f"Failed to filter by date: {e}")
                user = parsed_message.get("user", "")
                user_full_name = _clean_none_string(parsed_message.get("user_full_name", ""), replacement="")
                table = parsed_message.get("table", "")
                action = parsed_message.get("action", "")
                log_text = parsed_message.get("log_text", "")
                log_type = _clean_none_string(parsed_message.get("log_type", ""))
                is_system_config = parsed_message.get("is_system_config", True)

                # Parse inner message field. Prefer "crud_payload" -- once the
                # Logstash filter is fixed (see spec §12: the ES field-type
                # collision between plain-text log lines and structured CRUD
                # payloads sharing the "message" field name), Logstash renames
                # the parsed payload to "crud_payload" for any CRUD-shaped
                # line. Falling back to "message" keeps this working exactly
                # as today until that server-side change lands -- no
                # coordinated deploy required between the two.
                inner_message = parsed_message.get("crud_payload", parsed_message.get("message", {}))
                if isinstance(inner_message, str):
                    try:
                        fixed = inner_message
                        fixed = re.sub(r"(?<!\\)'", '"', fixed)
                        fixed = fixed.replace('\\"', "'")
                        inner_message = json.loads(fixed)
                    except:
                        inner_message = {}

                original_data = inner_message.get("original_data", {})
                updated_data = inner_message.get("updated_data", {})
                entity_id = inner_message.get("entity_id")

                log = LogDocument(
                    timestamp=timestamp,
                    method=action,
                    table=table,
                    patient_id=None,  # System logs don't have patient info
                    entity_id=entity_id,
                    user=user,
                    user_full_name=user_full_name,
                    message=log_text,
                    log_type=log_type,
                    is_system_config=is_system_config,
                    original_data=original_data,
                    updated_data=updated_data
                )
                logs.append(log)

            except Exception as e:
                logger.error(f"Could not read log: {str(e)}")

        totalRecords = response.get('hits', {}).get('total', {}).get('value', 0)
        totalPages = math.ceil(totalRecords / pageSize) if pageSize > 0 else 0

        return logs, totalRecords, totalPages

    except Exception as e:
        logger.error(f"Error querying Elasticsearch: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")