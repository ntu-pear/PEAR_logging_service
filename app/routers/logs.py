from typing import List, Optional
from fastapi import APIRouter, HTTPException
from app.schemas.log_document import LogDocument
from app.crud import logs_crud
from ..schemas.response import SingleResponse, PaginatedResponse
import logging

logger = logging.getLogger("uvicorn")
router = APIRouter()

@router.get("/logs/", response_model=PaginatedResponse[LogDocument])
def get_all_logs(pageNo: int = 0, pageSize: int = 10):
    db_logs, totalRecords, totalPages = logs_crud.get_all_logs(pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

@router.get("/logs/filter", response_model=PaginatedResponse[LogDocument])
def get_logs_by_param(action: Optional[str] = None, user: Optional[str] = None, table:Optional[str] = None, pageNo: int = 0, pageSize: int = 10):
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param(action,user,table,pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)
