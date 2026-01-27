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

    if query.action:
        must_conditions.append({"match_phrase": {"message": f"\"action\": \"{query.action}\""}})
    if query.user:
        must_conditions.append({"match_phrase": {"message": f"\"user\": \"{query.user}\""}})
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

    # Add timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
            range_filter["range"]["@timestamp"]["lte"] = query.end_date

        must_conditions.append(range_filter)

    query = {
        "query": {"bool": {"must": must_conditions}} if must_conditions else {"match_all": {}},
        "size": pageSize,
        "from": offset,
        "sort": [
            {"@timestamp": {"order": query.timestamp_order}}
        ],
        "track_total_hits": True,
    }

    try:
        response = es_service.search_documents(index="*", body=query, headers={"Content-Type": "application/json"})
        hits = response.get('hits', {}).get('hits', [])
        logs = []
        for hit in hits:
            try:
                source = hit["_source"]
                message_str = source.get("message", "")

                if isinstance(message_str, dict):
                    # If it's already a dict, use it directly
                    parsed_message = message_str
                else:
                    # Use ast.literal_eval which handles Python dict syntax with single quotes and None
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
                    user=user,
                    user_full_name=user_full_name,
                    message=log_text,
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
                    try:
                        import ast
                        parsed_message = ast.literal_eval(message_str)
                    except:
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
                    patient_id=None,  # No patient_id for activity logs
                    entity_id=entity_id,  # Use entity_id for activity/other entities
                    user=user,
                    user_full_name=user_full_name,
                    message=log_text,
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
    offset = pageNo * pageSize
    must_conditions = []

    must_conditions.append({
        "match_phrase": {"log.file.path": "PEAR_user_service"}
    })

    # User actions (e.g. login / logout/ change password)
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

    # Handle timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"@timestamp": {}}}

        if query.start_date:
            range_filter["range"]["@timestamp"]["gte"] = query.start_date
        if query.end_date:
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
        # Assuming es_service is available globally
        response = es_service.search_documents(
            index="logs-*",
            body=es_query,
            headers={"Content-Type": "application/json"}
        )

        hits = response.get('hits', {}).get('hits', [])
        logs = []

        for hit in hits:
            try:
                source = hit["_source"]

                timestamp = source.get("timestamp", "")
                level = source.get("level", "")
                logger_name = source.get("logger", "")
                user = source.get("user", "")
                user_full_name = source.get("user_full_name", "")
                action = source.get("action", "")
                log_text = source.get("log_text", "")
                role = source.get("role", "")

                # Create standardized LogDocument
                log = LogDocument(
                    timestamp=timestamp,
                    method=action,
                    table="User",
                    user=user,
                    user_full_name=user_full_name,
                    message=log_text,
                    role=role,
                )
                logs.append(log)

            except Exception as e:
                logger.error(f"Could not read login log: {str(e)}")
                continue

        totalRecords = response.get('hits', {}).get('total', {}).get('value', 0)
        totalPages = math.ceil(totalRecords / pageSize) if pageSize > 0 else 0

        return logs, totalRecords, totalPages

    except Exception as e:
        logger.error(f"Error querying Elasticsearch for login logs: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Error querying Elasticsearch: {str(e)}"
        )
