from typing import Optional
from fastapi import HTTPException
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_document import LogDocument
import math
    
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
        "query": {"bool": {"must": must_conditions}} if must_conditions else {"match_all": {}},
        "size": pageSize,
        "from": offset,
        "sort": [
            {"timestamp": {"order": "desc"}}
        ],
        "track_total_hits": True,
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
        totalRecords = response.get('hits', {}).get('total', {}).get('value', 0)
        totalPages = math.ceil(totalRecords/pageSize)
        return logs, totalRecords, totalPages
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")
    