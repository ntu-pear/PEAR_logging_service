from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from app.auth.dependencies import get_verified_user, require_roles
from app.auth.redaction import redact_clinical
from app.auth.token_verifier import VerifiedUser
from app.schemas.log_document import LogDocument
from app.schemas.log_query import LogQuery
from app.crud import logs_crud
from ..schemas.response import PaginatedResponse

router = APIRouter()

CARE_OVERSIGHT = require_roles("SUPERVISOR", "ADMIN")
ADMIN_ONLY = require_roles("ADMIN")
HISTORY_DENIED_ROLES = {"GUARDIAN", "ADMIN"}

@router.get("/Logs/Patient", response_model=PaginatedResponse[LogDocument], description="Gets all logs or logs filtered by params for patient")
def get_logs_by_param_patient(query: LogQuery = Depends(), pageNo: int = 0, pageSize: int = 10,
                              user: VerifiedUser = Depends(CARE_OVERSIGHT)):
    if pageSize > 100:
        pageSize = 100
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_patient(query, pageNo, pageSize)
    return PaginatedResponse(data=redact_clinical(db_logs, user.roleName), pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

@router.get(
    "/Logs/Patient/{patient_id}/history",
    response_model=PaginatedResponse[LogDocument],
    description="Change history for one patient (care roles only; excludes replica-sync entries)",
)
def get_patient_history(
    patient_id: str = Path(..., min_length=1, max_length=64),
    pageNo: int = 0,
    pageSize: int = 20,
    user: VerifiedUser = Depends(get_verified_user),
):
    if user.roleName in HISTORY_DENIED_ROLES:
        raise HTTPException(status_code=403, detail="Not permitted to view patient history")
    pageSize = min(max(pageSize, 1), 100)
    query = LogQuery(patient=patient_id)
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_patient(query, pageNo, pageSize)
    rows = [
        row for row in db_logs
        if not getattr(row, "is_system_config", False) and not str(getattr(row, "table", "")).startswith("REF_")
    ]
    return PaginatedResponse(data=rows, pageNo=pageNo, pageSize=pageSize,
                             totalRecords=totalRecords, totalPages=totalPages)

@router.get("/Logs/Activity", response_model=PaginatedResponse[LogDocument], description="Gets all logs or logs filtered by params for activity")
def get_logs_by_param_activity(query: LogQuery = Depends(), pageNo: int = 0, pageSize: int = 10,
                               user: VerifiedUser = Depends(CARE_OVERSIGHT)):
    if pageSize > 100:
        pageSize = 100
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_activity(query, pageNo, pageSize)
    return PaginatedResponse(data=redact_clinical(db_logs, user.roleName), pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

@router.get("/Logs/User", description="Get all user related logs")
def get_user_logs(query: LogQuery = Depends(),
    pageNo: int = 0,
    pageSize: int = 10,
    _user: VerifiedUser = Depends(ADMIN_ONLY),
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

@router.get("/Logs/System", response_model=PaginatedResponse[LogDocument], description="Get all system configuration logs from patient and activity services")
def get_logs_by_param_system(query: LogQuery = Depends(), pageNo: int = 0, pageSize: int = 10,
                             _user: VerifiedUser = Depends(ADMIN_ONLY)):
    if pageSize > 100:
        pageSize = 100
    try:
        db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_system(query, pageNo, pageSize)
        return PaginatedResponse(
            data=db_logs,
            pageNo=pageNo,
            pageSize=pageSize,
            totalRecords=totalRecords,
            totalPages=totalPages
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {e}")