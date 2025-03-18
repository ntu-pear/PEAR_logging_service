from typing import Optional, Literal
from fastapi import APIRouter, Depends
from app.schemas.log_document import LogDocument
from app.schemas.log_query import LogQuery
from app.crud import logs_crud
from ..schemas.response import PaginatedResponse

router = APIRouter()

@router.get("/Logs/", response_model=PaginatedResponse[LogDocument], description="Gets all logs or logs filtered by params")
def get_logs_by_param(query: LogQuery = Depends(), pageNo: int = 0, pageSize: int = 10):
    if pageSize > 100:
        pageSize = 100
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param(query,pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)
