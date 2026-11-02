import logging

from elasticsearch import ApiError, TransportError
from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import require_roles
from app.auth.token_verifier import VerifiedUser
from app.crud import ops_logs_crud
from app.services.log_flags import apply_ops_flags
from app.schemas.ops_log import OpsLogDocument, OpsLogQuery
from app.schemas.response import PaginatedResponse

router = APIRouter()

ADMIN_ONLY = require_roles("ADMIN")
MAX_ID_CHARS = 64
log = logging.getLogger("uvicorn")


def _effective_range(ops_type: str, query: OpsLogQuery):
    """The @timestamp range the page query used, or None (flags then derive it from the rows)."""
    try:
        must = ops_logs_crud.build_ops_query(ops_type, query)["query"]["bool"]["must"]
        for clause in must:
            if "range" in clause:
                return dict(clause["range"]["@timestamp"])
    except Exception:
        pass
    return None


def _page(ops_type: str, query: OpsLogQuery, page_no: int, page_size: int, sensitive: bool = False):
    for value in (query.request_id, query.user_id, query.service, query.level, query.environment):
        if value is not None and len(value) > MAX_ID_CHARS:
            raise HTTPException(
                status_code=400,
                detail="request_id, user_id, service, level and environment are at most 64 characters",
            )
    try:
        rows, total, pages = ops_logs_crud.search_ops_logs(ops_type, query, page_no, page_size, sensitive=sensitive)
    except ops_logs_crud.OpsQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ApiError, TransportError):
        log.exception("ops log search failed")
        raise HTTPException(status_code=503, detail="Log store unavailable")
    rows = apply_ops_flags(rows, _effective_range(ops_type, query))
    size = min(max(page_size, 1), ops_logs_crud.MAX_PAGE_SIZE)
    return PaginatedResponse(data=rows, pageNo=max(page_no, 0), pageSize=size, totalRecords=total, totalPages=pages)


@router.get(
    "/Logs/Errors",
    response_model=PaginatedResponse[OpsLogDocument],
    description="Operational error lines from every service (admin only). Default window: last 24 hours.",
)
def get_error_logs(query: OpsLogQuery = Depends(), pageNo: int = 0, pageSize: int = 20,
                   _user: VerifiedUser = Depends(ADMIN_ONLY)):
    return _page("error", query, pageNo, pageSize)


@router.get(
    "/Logs/Requests",
    response_model=PaginatedResponse[OpsLogDocument],
    description="API request lines (admin only). sensitive=true keeps only the Sensitive access preset.",
)
def get_request_logs(query: OpsLogQuery = Depends(), sensitive: bool = False, pageNo: int = 0, pageSize: int = 20,
                     _user: VerifiedUser = Depends(ADMIN_ONLY)):
    return _page("request", query, pageNo, pageSize, sensitive=sensitive)


@router.get(
    "/Logs/Auth",
    response_model=PaginatedResponse[OpsLogDocument],
    description="Auth events: failed logins, refresh failures, 401/403 responses, token-verification events (admin only).",
)
def get_auth_logs(query: OpsLogQuery = Depends(), pageNo: int = 0, pageSize: int = 20,
                  _user: VerifiedUser = Depends(ADMIN_ONLY)):
    return _page("auth", query, pageNo, pageSize)
