import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.dependencies import get_bearer_token, require_roles
from app.auth.token_verifier import VerifiedUser
from app.crud import log_stats_crud
from app.schemas.log_stats import LogStats
from app.services.user_names import lookup_names

router = APIRouter()

ADMIN_ONLY = require_roles("ADMIN")
log = logging.getLogger("uvicorn")


@router.get(
    "/Logs/Stats",
    response_model=LogStats,
    description="Log statistics for the admin dashboard over the last 24h, 7d or 30d (admin only).",
)
def get_log_stats(window: str = Query("24h"), _user: VerifiedUser = Depends(ADMIN_ONLY),
                  token: str = Depends(get_bearer_token)):
    if window not in log_stats_crud.WINDOWS:
        raise HTTPException(status_code=400, detail="window must be one of 24h, 7d, 30d")
    try:
        stats = log_stats_crud.compute_stats(window)
    except Exception:
        log.exception("log statistics failed")
        raise HTTPException(status_code=503, detail="Log store unavailable")
    accounts = stats.auth.top_accounts + stats.audit.top_actors
    names = lookup_names([a.user_id for a in accounts], token)
    for account in accounts:
        account.name = names.get(account.user_id)
    return stats
