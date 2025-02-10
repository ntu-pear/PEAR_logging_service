from typing import List, Optional
from fastapi import APIRouter, HTTPException
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_document import LogDocument
import logging
import math
logger = logging.getLogger("uvicorn")

def get_all_logs(pageNo: int = 0, pageSize: int = 10):
    offset = pageNo * pageSize
    query = {
        "query": {
            "match": {
                "logger": {
                    "query": "app.logger.config"
                }
            }
        },
        "size": pageSize,
        "from": offset,
        "sort": [
            {"timestamp": {"order": "desc"}}
        ]
    }
    try:
        response = es_service.search_documents(index="*", body=query, headers={"Content-Type": "application/json"})
        hits = response.get('hits', {}).get('hits',[])
        logs = []
        for hit in hits:
            source = hit["_source"]
            message_data = source.get("message", "{}")
            log = LogDocument(
                    timestamp=source.get("timestamp", ""),
                    method=source.get("action", ""),
                    table=source.get("table", ""),
                    user=source.get("user", ""),
                    original_data=message_data.get("original_data"),
                    updated_data=message_data.get("updated_data")
                )
            logs.append(log)
        totalRecords = len(hits)
        totalPages = math.ceil(totalRecords/pageSize)
        return logs, totalRecords, totalPages
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")
    
def get_logs_by_param(action: Optional[str] = None, user: Optional[str] = None, table: Optional[str] = None, pageNo: int = 0, pageSize: int = 10):
    offset = pageNo * pageSize
    must_conditions = []
    
    if action:
        must_conditions.append({"match": {"action": action}})
    if user:
        must_conditions.append({"match": {"user": user}})
    if table:
        must_conditions.append({"match": {"table": table}})
    query = {
        "query": {
            "bool": {
                "must": must_conditions
            }
        },
        "size": pageSize,
        "from": offset,
        "sort": [
            {"timestamp": {"order": "desc"}}
        ]
    }
    try:
        response = es_service.search_documents(index="*", body=query, headers={"Content-Type": "application/json"})
        hits = response.get('hits', {}).get('hits',[])
        logs = []
        for hit in hits:
            source = hit["_source"]
            message_data = source.get("message", "{}")
            log = LogDocument(
                    timestamp=source.get("timestamp", ""),
                    method=source.get("action", ""),
                    table=source.get("table", ""),
                    user=source.get("user", ""),
                    original_data=message_data.get("original_data"),
                    updated_data=message_data.get("updated_data")
                )
            logs.append(log)
        totalRecords = len(hits)
        totalPages = math.ceil(totalRecords/pageSize)
        return logs, totalRecords, totalPages
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")
    