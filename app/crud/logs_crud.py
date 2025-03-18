from typing import Optional, Literal
from fastapi import HTTPException
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_document import LogDocument
from app.schemas.log_query import LogQuery
import math
    
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
        must_conditions.append({"match": {"message.entity_id": query.patient}})
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
                log = LogDocument(
                        timestamp=source.get("timestamp", ""),
                        method=source.get("action", ""),
                        table=source.get("table", ""),
                        patient_id=message_data.get("entity_id"),
                        user=source.get("user", ""),
                        user_full_name=source.get("user_full_name", ""),
                        message=source.get("log_text", ""),
                        original_data=message_data.get("original_data"),
                        updated_data=message_data.get("updated_data")
                    )
                logs.append(log)
            except e:
                print("Could not read log")
        totalRecords = response.get('hits', {}).get('total', {}).get('value', 0)
        totalPages = math.ceil(totalRecords/pageSize)
        return logs, totalRecords, totalPages
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")
    