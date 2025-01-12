from fastapi import APIRouter, HTTPException, Depends
from app.elasticsearch.elasticsearch import ElasticsearchService
from app.elasticsearch.config import settings

router = APIRouter()
es_service = ElasticsearchService(
    host=settings.ES_HOST,
    port=settings.ES_PORT,
    username=settings.ES_USERNAME,
    password=settings.ES_PASSWORD,
)

# Check if Elasticsearch is connected
@router.get("/health")
def check_health():
    if es_service.is_connected():
        return {"status": "Elasticsearch is healthy"}
    else:
        raise HTTPException(status_code=500, detail="Elasticsearch is not reachable")

# Create a new log entry
@router.post("/logs/{index_name}")
def create_log(index_name: str, document_id: str, log: dict):
    try:
        es_service.insert_document(index_name=index_name, document_id=document_id, document=log)
        return {"message": "Log entry created successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Read a log entry by ID
@router.get("/logs/{index_name}/{document_id}")
def read_log(index_name: str, document_id: str):
    try:
        result = es_service.get_document(index_name=index_name, document_id=document_id)
        return result["_source"]
    except Exception as e:
        raise HTTPException(status_code=404, detail="Log not found")

# Search for logs with a query
@router.post("/logs/{index_name}/search")
def search_logs(index_name: str, query: dict):
    try:
        results = es_service.search_documents(index_name=index_name, query=query)
        return results["hits"]["hits"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Delete a log entry
@router.delete("/logs/{index_name}/{document_id}")
def delete_log(index_name: str, document_id: str):
    try:
        es_service.delete_document(index_name=index_name, document_id=document_id)
        return {"message": "Log entry deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=404, detail="Log not found")
