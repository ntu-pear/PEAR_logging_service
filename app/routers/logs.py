from typing import List, Optional
from fastapi import APIRouter, HTTPException
from app.elasticsearch.elasticsearch import es_service
from app.schemas.log_document import LogDocument
from app.crud import logs_crud
from ..schemas.response import SingleResponse, PaginatedResponse
import logging

logger = logging.getLogger("uvicorn")
router = APIRouter()

@router.get("/health")
def check_health():
    if es_service.ping():
        return {"status": "Elasticsearch is healthy"}
    else:
        raise HTTPException(status_code=500, detail="Elasticsearch is not reachable")

@router.get("/logs/", response_model=PaginatedResponse[LogDocument])
def get_all_logs(pageNo: int = 0, pageSize: int = 10):
    db_logs, totalRecords, totalPages = logs_crud.get_all_logs(pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

@router.get("/logs/filter", response_model=PaginatedResponse[LogDocument])
def get_logs_by_param(action: Optional[str] = None, user: Optional[str] = None, table:Optional[str] = None, pageNo: int = 0, pageSize: int = 10):
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param(action,user,table,pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

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
