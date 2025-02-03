from typing import List
from fastapi import APIRouter, HTTPException, Depends
from app.elasticsearch.elasticsearch import ElasticsearchService
from app.elasticsearch.config import settings
from app.schemas.log_document import LogDocument
from elasticsearch import Elasticsearch
import logging

logger = logging.getLogger("uvicorn")

router = APIRouter()
es_service = ElasticsearchService(
    host=settings.ES_HOST,
    port=settings.ES_PORT,
    username=settings.ES_USERNAME,
    password=settings.ES_PASSWORD,
)
#es = Elasticsearch(hosts="http://elastic:BCYTUXDk2uiCrHWWpzQ+@10.96.188.172:9200/")
# Check if Elasticsearch is connected
@router.get("/health")
def check_health():
    if es.ping():
        return {"status": "Elasticsearch is healthy"}
    else:
        raise HTTPException(status_code=500, detail="Elasticsearch is not reachable")
#response_model=List[LogDocument]
@router.get("/logs")
def get_all_logs():
    query = {
        "query": {
            "match": {
                "log_type": {
                    "query": "http_response"
                }
            }
        },
        "size": 100  # Adjust the number of logs you want to retrieve
        # "sort": [
        #     {"timestamp": {"order": "desc"}}
        # ]
    }
    
    try:
        # Query all indices for logs with log_type = "http_request"
        response = es.search(index="*", body=query, headers={"Content-Type": "application/json"})
        hits = response.get('hits', {}).get('hits', [])
        logger.info(response)
        logger.info(f"Hits: {hits}")
        return [response]
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
