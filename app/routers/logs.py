from datetime import datetime, timedelta
from typing import Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from app.auth.dependencies import get_bearer_token, get_verified_user, require_roles
from app.auth.redaction import redact_clinical
from app.auth.token_verifier import VerifiedUser
from app.schemas.log_document import LogDocument
from app.schemas.log_query import LogQuery
from app.crud import logs_crud
from app.services.log_flags import apply_audit_flags
from app.services.user_names import resolve_user_names
from ..schemas.response import PaginatedResponse

router = APIRouter()

CARE_OVERSIGHT = require_roles("SUPERVISOR", "ADMIN")
ADMIN_ONLY = require_roles("ADMIN")
HISTORY_DENIED_ROLES = {"GUARDIAN", "ADMIN"}
ADMINISTRATION_TABLE = "PATIENT_MEDICATION_ADMINISTRATION"
UNDONE_LOOKUP_LIMIT = 1000


def _undone_administration_ids(patient_id):
    """Record ids of doses a supervisor undid for this patient. History hides both the
    "given" and the "undone" entry for these; the admin logs still show them. A dose
    marked again after an undo is a new record with its own id, so it stays visible."""
    query = LogQuery(patient=patient_id, table=ADMINISTRATION_TABLE, action="delete")
    rows, _, _ = logs_crud.get_logs_by_param_patient(query, 0, UNDONE_LOOKUP_LIMIT)
    return {row.entity_id for row in rows if row.table == ADMINISTRATION_TABLE and row.entity_id is not None}


def _sgt_bound(value, end_of_day):
    """One start/end date the way the audit queries read it: SGT wall clock, sent to ES as UTC."""
    text = value.strip()
    if "T" in text or " " in text:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        parsed = datetime.fromisoformat(text + ("T23:59:59" if end_of_day else "T00:00:00"))
    return (parsed - timedelta(hours=8)).isoformat()


def _audit_range(query):
    """The query's effective @timestamp range, or None (flags then derive it from the rows)."""
    try:
        window = {}
        if query.start_date:
            window["gte"] = _sgt_bound(query.start_date, False)
        if query.end_date:
            window["lte"] = _sgt_bound(query.end_date, True)
        return window or None
    except Exception:
        return None

@router.get("/Logs/Patient", response_model=PaginatedResponse[LogDocument], description="Gets all logs or logs filtered by params for patient")
def get_logs_by_param_patient(query: LogQuery = Depends(), pageNo: int = 0, pageSize: int = 10,
                              user: VerifiedUser = Depends(CARE_OVERSIGHT), token: str = Depends(get_bearer_token)):
    if pageSize > 100:
        pageSize = 100
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_patient(query, pageNo, pageSize)
    # Flags first: their reasons hold field/table names, actor names and counts, never values,
    # so ADMIN keeps them while the clinical data below is redacted.
    rows = redact_clinical(apply_audit_flags(db_logs, _audit_range(query)), user.roleName)
    return PaginatedResponse(data=resolve_user_names(rows, token), pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

@router.get(
    "/Logs/Patient/{patient_id}/history",
    response_model=PaginatedResponse[LogDocument],
    description="Change history for one patient across Patient and Activity service changes (care roles only; excludes replica-sync entries)",
)
def get_patient_history(
    patient_id: str = Path(..., min_length=1, max_length=64),
    pageNo: int = 0,
    pageSize: int = 20,
    user: VerifiedUser = Depends(get_verified_user),
    token: str = Depends(get_bearer_token),
):
    if user.roleName in HISTORY_DENIED_ROLES:
        raise HTTPException(status_code=403, detail="Not permitted to view patient history")
    pageSize = min(max(pageSize, 1), 100)
    query = LogQuery(patient=patient_id)
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_patient(query, pageNo, pageSize, include_activity=True)
    undone = _undone_administration_ids(patient_id)
    rows = [
        row for row in db_logs
        if not getattr(row, "is_system_config", False) and not str(getattr(row, "table", "")).startswith("REF_")
        and not (row.table == ADMINISTRATION_TABLE and row.entity_id in undone)
    ]
    rows = apply_audit_flags(rows, _audit_range(query))
    return PaginatedResponse(data=resolve_user_names(rows, token), pageNo=pageNo, pageSize=pageSize,
                             totalRecords=totalRecords, totalPages=totalPages)

@router.get("/Logs/Activity", response_model=PaginatedResponse[LogDocument], description="Gets all logs or logs filtered by params for activity")
def get_logs_by_param_activity(query: LogQuery = Depends(), pageNo: int = 0, pageSize: int = 10,
                               user: VerifiedUser = Depends(CARE_OVERSIGHT), token: str = Depends(get_bearer_token)):
    if pageSize > 100:
        pageSize = 100
    db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_activity(query, pageNo, pageSize)
    # Flags first: their reasons hold field/table names, actor names and counts, never values,
    # so ADMIN keeps them while the clinical data below is redacted.
    rows = redact_clinical(apply_audit_flags(db_logs, _audit_range(query)), user.roleName)
    return PaginatedResponse(data=resolve_user_names(rows, token), pageNo=pageNo,pageSize=pageSize,totalRecords=totalRecords, totalPages=totalPages)

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
                             _user: VerifiedUser = Depends(ADMIN_ONLY), token: str = Depends(get_bearer_token)):
    if pageSize > 100:
        pageSize = 100
    try:
        db_logs, totalRecords, totalPages = logs_crud.get_logs_by_param_system(query, pageNo, pageSize)
        return PaginatedResponse(
            data=resolve_user_names(apply_audit_flags(db_logs, _audit_range(query)), token),
            pageNo=pageNo,
            pageSize=pageSize,
            totalRecords=totalRecords,
            totalPages=totalPages
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying Elasticsearch: {e}")