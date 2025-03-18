from typing import Optional, Literal
from fastapi import HTTPException
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_document import LogDocument
from app.schemas.log_query import LogQuery
import math
import logging
 
logger = logging.getLogger("uvicorn")

def get_logs_by_param(query: LogQuery, pageNo: int = 0, pageSize: int = 10):
    offset = pageNo * pageSize
    must_conditions = []
    
    if query.action:
        must_conditions.append({"match": {"action": query.action}})
    if query.user:
        must_conditions.append({"match": {"user": query.user}})
    if query.table:
        must_conditions.append({"match": {"table": query.table}})
    if query.patient:
        must_conditions.append({
            "bool": {
                "should": [
                    {
                        "bool": {
                            "must": [
                                {"match": {"table": "Patient"}},
                                {
                                    "bool": {
                                        "should": [
                                            {"match": {"message.updated_data.id": query.patient}},
                                            {"match": {"message.original_data.id": query.patient}}
                                        ],
                                        "minimum_should_match": 1  # At least one ID should match
                                    }
                                }
                            ]
                        }
                    },
                    {"match": {"message.updated_data.PatientID": query.patient}},
                    {"match": {"message.updated_data.PatientId": query.patient}},
                    {"match": {"message.updated_data.patientId": query.patient}},
                    {"match": {"message.original_data.PatientID": query.patient}},
                    {"match": {"message.original_data.PatientId": query.patient}},
                    {"match": {"message.original_data.patientId": query.patient}}
                ],
                "minimum_should_match": 1  # Ensures at least one match
            }
    })
    
    # Add timestamp range filter
    if query.start_date or query.end_date:
        range_filter = {"range": {"timestamp": {}}}
        
        if query.start_date:
            range_filter["range"]["timestamp"]["gte"] = query.start_date
        if query.end_date:
            range_filter["range"]["timestamp"]["lte"] = query.end_date
            
        must_conditions.append(range_filter)

    query = {
        "query": {"bool": {"must": must_conditions}} if must_conditions else {"match_all": {}},
        "size": pageSize,
        "from": offset,
        "sort": [
            {"timestamp": {"order": query.timestamp_order}}
        ],
        "track_total_hits": True,
    }

    try:
        response = es_service.search_documents(index="*", body=query, headers={"Content-Type": "application/json"})
        hits = response.get('hits', {}).get('hits',[])
        logs = []
        for hit in hits:
            try:
                source = hit["_source"]
                message_data = source.get("message", "{}")
                original_data=message_data.get("original_data")
                updated_data=message_data.get("updated_data")
                table=source.get("table", "")
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
                    elif updated_data.get("PatientID"):
                        patient_id = updated_data.get("PatientID")
                log = LogDocument(
                        timestamp=source.get("timestamp", ""),
                        method=source.get("action", ""),
                        table=source.get("table", ""),
                        patient_id=patient_id,
                        user=source.get("user", ""),
                        user_full_name=source.get("user_full_name", ""),
                        message=source.get("log_text", ""),
                        original_data=original_data,
                        updated_data=updated_data
                    )
                logs.append(log)
                logger.info(f"Log : {log}")
            except Exception as e:
                print(f"Could not read log, {e}")
        totalRecords = response.get('hits', {}).get('total', {}).get('value', 0)
        totalPages = math.ceil(totalRecords/pageSize)
        return logs, totalRecords, totalPages
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")
    