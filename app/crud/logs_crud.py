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


def get_logs_by_param_patient(query: LogQuery, pageNo: int = 0, pageSize: int = 10):
    offset = pageNo * pageSize
    must_conditions = []

    must_conditions.append({
        "bool": {
            "must": [
                {"match_phrase": {"message": "\"user\""}},
                {"match_phrase": {"message": "\"user_full_name\""}},
                {"match_phrase": {"message": "\"table\""}},
                {"match_phrase": {"message": "\"action\""}},
                {"match_phrase": {"message": "\"log_text\""}},
                {"match_phrase": {"log.file.path": "PEAR_patient_service"}}
            ]
        }
    })

    # Make sure that action is either create, update or delete
    must_conditions.append({
        "bool": {
        "should": [
            {"match_phrase": {"message": f"\"action\": \"create\""}},
            {"match_phrase": {"message": f"\"action\": \"update\""}},
            {"match_phrase": {"message": f"\"action\": \"delete\""}},
        ],
        "minimum_should_match": 1
        }
    })

    # Exclude system config logs (ensure only patient logs are being retrieved)
    must_conditions.append({
        "bool": {
            "must_not": [
                {"match_phrase": {"message": f"\"is_system_config\": True"}}
            ]
        }
    })

    if query.action:
        must_conditions.append({"match_phrase": {"message": f"\"action\": \"{query.action}\""}})
    if query.user:
        must_conditions.append({"match_phrase": {"message": f"\"user\": \"{query.user}\""}})
    if query.user_full_name:
        must_conditions.append({"match_phrase": {"message": f"\"user_full_name\": \"{query.user_full_name}\""}})
    if query.table:
        must_conditions.append({"match_phrase": {"message": f"\"table\": \"{query.table}\""}})

    # Handle Patient ID search
    if query.patient:
        must_conditions.append({
            "bool": {
                "should": [
                    # For Patient table - look for 'id' field
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
                    # For all other tables - look for patientId/PatientId/PatientID fields
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
                    }
                ],
                "minimum_should_match": 1
            }
        })
    if query.patient_full_name:
        must_conditions.append({
            "match_phrase": {"message": f"'patient_full_name': {query.patient_full_name}"}
        })

    if query.log_type:
        must_conditions.append({
            "match_phrase": {"message": f"'log_type': {query.log_type}"}
        })

    # Add timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
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

                if isinstance(message_str, dict):
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
                level = parsed_message.get("level", "")
                logger_name = parsed_message.get("logger", "")
                user = parsed_message.get("user", "")
                user_full_name = parsed_message.get("user_full_name", "")
                table = parsed_message.get("table", "")
                action = parsed_message.get("action", "")
                message = parsed_message.get("log_text", "")
                log_type = parsed_message.get("log_type", "")
                is_system_config = parsed_message.get("is_system_config", False)
                patient_full_name = parsed_message.get("patient_full_name", "")

                # Parse inner message field
                inner_message = parsed_message.get("message", {})
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

                patient_id = None
                if table == "Patient":
                    if original_data.get("id"):
                        patient_id = original_data.get("id")
                    elif updated_data.get("id"):
                        patient_id = updated_data.get("id")
                else:
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

    must_conditions.append({
        "bool": {
            "must": [
                {"match_phrase": {"message": "\"user\""}},
                {"match_phrase": {"message": "\"user_full_name\""}},
                {"match_phrase": {"message": "\"table\""}},
                {"match_phrase": {"message": "\"action\""}},
                {"match_phrase": {"message": "\"log_text\""}},
                {"match_phrase": {"log.file.path": "PEAR_activity_service"}}
            ]
        }
    })

    # Make sure that action is either create, update or delete
    must_conditions.append({
        "bool": {
            "should": [
                {"match_phrase": {"message": f"\"action\": \"create\""}},
                {"match_phrase": {"message": f"\"action\": \"update\""}},
                {"match_phrase": {"message": f"\"action\": \"delete\""}},
            ],
            "minimum_should_match": 1
        }
    })

    if query.action:
        must_conditions.append({"match_phrase": {"message": f"\"action\": \"{query.action}\""}})
    if query.user:
        must_conditions.append({"match_phrase": {"message": f"\"user\": \"{query.user}\""}})
    if query.table:
        must_conditions.append({"match_phrase": {"message": f"\"table\": \"{query.table}\""}})
    if query.log_type:
        must_conditions.append({"match_phrase": {"message": f"\"log_type\": \"{query.log_type}\""}})
    if query.patient:
        must_conditions.append({"match_phrase": {"message": f"\"patient_id\": \"{query.patient}\""}})
    if query.patient_full_name:
        must_conditions.append({"match_phrase": {"message": f"\"patient_full_name\": \"{query.patient_full_name}\""}})

    # Only patient-related activity logs (is_system_config = False)
    must_conditions.append({
        "bool": {
            "must_not": [
                {"match_phrase": {"message": f"\"is_system_config\": True"}}
            ]
        }
    })

    # Handle Activity ID search (entity_id in the logs)
    if query.activity:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"'entity_id': {query.activity}"}},
                    {"match_phrase": {"message": f"\"entity_id\": {query.activity}"}}
                ],
                "minimum_should_match": 1
            }
        })

    # Add timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
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
        seen_messages = set()

        for hit in hits:
            try:
                source = hit["_source"]
                message_str = source.get("message", "")

                if message_str in seen_messages:
                    logger.debug(f"Skipping duplicate message: {message_str[:100]}...")
                    continue
                seen_messages.add(message_str)

                # Skip logs unrelated to CRUD logs
                if isinstance(message_str, str) and ('"action"' not in message_str or '"table"' not in message_str):
                    continue

                if isinstance(message_str, dict):
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
                level = parsed_message.get("level", "")
                logger_name = parsed_message.get("logger", "")
                user = parsed_message.get("user", "")
                user_full_name = parsed_message.get("user_full_name", "")
                table = parsed_message.get("table", "")
                action = parsed_message.get("action", "")
                log_text = parsed_message.get("log_text", "")
                log_type = parsed_message.get("log_type", "")
                is_system_config = parsed_message.get("is_system_config", False)

                # Parse inner message field
                inner_message = parsed_message.get("message", {})
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

                # Extract patient_id and patient_full_name from root level
                patient_id = parsed_message.get("patient_id")
                patient_full_name = parsed_message.get("patient_full_name", "")

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
        "match_phrase": {"log.file.path": "PEAR_user_service"}
    })

    # Match against top-level fields now, not inside message JSON
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
            "match_phrase": {"user_full_name": query.user_full_name}
        })

    if query.log_type:
        must_conditions.append({
            "match_phrase": {"log_type": query.log_type}
        })

    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}
        if query.start_date:
            range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
            range_filter["range"]["@timestamp"]["lte"] = query.end_date
        must_conditions.append(range_filter)

    es_query = {
        "query": {
            "bool": {"must": must_conditions}
        } if must_conditions else {"match_all": {}},
        "size": pageSize,
        "from": offset,
        "sort": [{"@timestamp": {"order": query.timestamp_order}}],
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
        seen_ids = set()

        for hit in hits:
            try:
                # Deduplicate by document ID instead of message string
                doc_id = hit.get("_id")
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)

                source = hit["_source"]

                # Fields are now top-level in _source
                timestamp = source.get("timestamp", "")
                user = source.get("user", "")
                user_full_name = source.get("user_full_name", "")
                role = source.get("role", "")
                action = source.get("action", "")
                log_text = source.get("log_text", "")
                table = source.get("table", "User")
                log_type = source.get("log_type", "")

                # message now holds entity data: entity_id, original_data, updated_data
                entity_id = None
                original_data = None
                updated_data = None

                message_str = source.get("message", "")
                if message_str:
                    try:
                        if isinstance(message_str, dict):
                            msg_data = message_str
                        else:
                            msg_data = json.loads(message_str)
                        entity_id = msg_data.get("entity_id")
                        original_data = msg_data.get("original_data")
                        updated_data = msg_data.get("updated_data")
                    except Exception as parse_error:
                        logger.warning(f"Could not parse message JSON: {str(parse_error)}")

                log = LogDocument(
                    timestamp=timestamp,
                    method=action,
                    table=table,
                    user=user,
                    user_full_name=user_full_name,
                    message=log_text,
                    role=role,
                    log_type=log_type,
                    entity_id=entity_id,
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

    # Match logs from patient or activity service with CRUD fields
    must_conditions.append({
        "bool": {
            "must": [
                {"match_phrase": {"message": "\"user\""}},
                {"match_phrase": {"message": "\"table\""}},
                {"match_phrase": {"message": "\"action\""}},
                {"match_phrase": {"message": "\"log_text\""}},
                {"match_phrase": {"message": "\"is_system_config\""}}
            ],
            "should": [
                {"match_phrase": {"log.file.path": "PEAR_patient_service"}},
                {"match_phrase": {"log.file.path": "PEAR_activity_service"}}
            ],
            "minimum_should_match": 1
        }
    })

    # Only system config logs (is_system_config = True)
    must_conditions.append({
        "match_phrase": {"message": "\"is_system_config\": True"}
    })

    # Make sure that action is either create, update or delete
    must_conditions.append({
        "bool": {
            "should": [
                {"match_phrase": {"message": "\"action\": \"create\""}},
                {"match_phrase": {"message": "\"action\": \"update\""}},
                {"match_phrase": {"message": "\"action\": \"delete\""}},
            ],
            "minimum_should_match": 1
        }
    })

    # Apply filters
    if query.action:
        must_conditions.append({"match_phrase": {"message": f"\"action\": \"{query.action}\""}})
    if query.user:
        must_conditions.append({"match_phrase": {"message": f"\"user\": \"{query.user}\""}})
    if query.user_full_name:
        must_conditions.append({"match_phrase": {"message": f"\"user_full_name\": \"{query.user_full_name}\""}})
    if query.table:
        must_conditions.append({"match_phrase": {"message": f"\"table\": \"{query.table}\""}})
    if query.log_type:
        must_conditions.append({"match_phrase": {"message": f"\"log_type\": \"{query.log_type}\""}})

    # Handle Entity ID search
    if query.activity:
        must_conditions.append({
            "bool": {
                "should": [
                    {"match_phrase": {"message": f"\"entity_id\": {query.activity}"}},
                    {"match_phrase": {"message": f"'entity_id': {query.activity}"}}
                ],
                "minimum_should_match": 1
            }
        })

    # Add timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
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

                if message_str in seen_messages:
                    continue
                seen_messages.add(message_str)

                if isinstance(message_str, dict):
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
                user = parsed_message.get("user", "")
                user_full_name = parsed_message.get("user_full_name", "")
                table = parsed_message.get("table", "")
                action = parsed_message.get("action", "")
                log_text = parsed_message.get("log_text", "")
                log_type = parsed_message.get("log_type", "")
                is_system_config = parsed_message.get("is_system_config", True)

                # Parse inner message field
                inner_message = parsed_message.get("message", {})
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