from typing import Optional
from fastapi import APIRouter
from app.schemas.log_document import LogDocument
from app.crud import logs_crud
from ..schemas.response import PaginatedResponse

router = APIRouter()

@router.get("/logs/", response_model=PaginatedResponse[LogDocument])
def get_logs_by_param(action: Optional[str] = None, user: Optional[str] = None, table:Optional[str] = None, pageNo: int = 0, pageSize: int = 10):
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param(action,user,table,pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)
