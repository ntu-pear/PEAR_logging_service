from typing import List
from fastapi import APIRouter, HTTPException
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_document import LogDocument
import logging

logger = logging.getLogger("uvicorn")
router = APIRouter()

@router.get("/health")
def check_health():
    if es_service.ping():
        return {"status": "Elasticsearch is healthy"}
    else:
        raise HTTPException(status_code=500, detail="Elasticsearch is not reachable")

@router.get("/logs", response_model=List[LogDocument])
def get_all_logs():
    query = {
        "query": {
            "match": {
                "log_type": {
                    "query": "http_response"
                }
            }
        },
        "size": 100
        # "sort": [
        #     {"timestamp": {"order": "desc"}}
        # ]
    }
    
    try:
        # Query all indices for logs with log_type = "http_request"
        response = es_service.search_documents(index="*", body=query, headers={"Content-Type": "application/json"})
        hits = response.get('hits', {}).get('hits',[])
        logs = [
            LogDocument(
                timestamp=hit["_source"].get("@timestamp", ""),
                log_type=hit["_source"].get("log_type", ""),
                method=hit["_source"].get("method", ""),
                url=hit["_source"].get("url", ""),
                user=hit["_source"].get("user", ""),
                status=hit["_source"].get("status", ""),
                request_body=hit["_source"].get("request_body")
            )
            for hit in hits
        ]
        logger.info(f"Hits: {len(hits)}")
        logger.info(f"Logs: {logs}")
        return logs
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {str(e)}")


# # Read a log entry by ID
# @router.get("/logs/{index_name}/{document_id}")
# def read_log(index_name: str, document_id: str):
#     try:
#         result = es_service.get_document(index_name=index_name, document_id=document_id)
#         return result["_source"]
#     except Exception as e:
#         raise HTTPException(status_code=404, detail="Log not found")

# # Search for logs with a query
# @router.post("/logs/{index_name}/search")
# def search_logs(index_name: str, query: dict):
#     try:
#         results = es_service.search_documents(index_name=index_name, query=query)
#         return results["hits"]["hits"]
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))

# # Delete a log entry
# @router.delete("/logs/{index_name}/{document_id}")
# def delete_log(index_name: str, document_id: str):
#     try:
#         es_service.delete_document(index_name=index_name, document_id=document_id)
#         return {"message": "Log entry deleted successfully"}
#     except Exception as e:
#         raise HTTPException(status_code=404, detail="Log not found")
