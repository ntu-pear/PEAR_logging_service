from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException,Query
from app.schemas.log_document import LogDocument
from app.schemas.log_query import LogQuery
from app.crud import logs_crud
from ..schemas.response import PaginatedResponse

router = APIRouter()

@router.get("/Logs/Patient", response_model=PaginatedResponse[LogDocument], description="Gets all logs or logs filtered by params for patient")
def get_logs_by_param_patient(query: LogQuery = Depends(), pageNo: int = 0, pageSize: int = 10):
    if pageSize > 100:
        pageSize = 100
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_patient(query, pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

@router.get("/Logs/Activity", response_model=PaginatedResponse[LogDocument], description="Gets all logs or logs filtered by params for activity")
def get_logs_by_param_activity(query: LogQuery = Depends(), pageNo: int = 0, pageSize: int = 10):
    if pageSize > 100:
        pageSize = 100
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_activity(query, pageNo, pageSize)
    return PaginatedResponse(data=db_logs, pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

@router.get("/Logs/User", description="Get all user related logs")
def get_user_logs(query: LogQuery = Depends(),
    pageNo: int = 0,
    pageSize: int = 10
):
    if pageSize > 100:
        pageSize = 100
    try:
        db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_user(query, pageNo, pageSize)
        return PaginatedResponse(
            data=db_logs,
            pageNo=pageNo,
            pageSize=pageSize,
            totalRecords=totalRecords,
            totalPages=totalPages
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {e}")