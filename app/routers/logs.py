from typing import Optional, Literal
from fastapi import APIRouter
from app.schemas.log_document import LogDocument
from app.crud import logs_crud
from ..schemas.response import PaginatedResponse

router = APIRouter()

@router.get("/Logs/", response_model=PaginatedResponse[LogDocument], description="Gets all logs or logs filtered by params")
def get_logs_by_param(action: Optional[str] = None, user: Optional[str] = None, table:Optional[str] = None, timestamp_order: Literal["asc", "desc"] = "desc", pageNo: int = 0, pageSize: int = 10):
    if pageSize > 100:
        pageSize = 100
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param(action,user,table, timestamp_order,pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)
