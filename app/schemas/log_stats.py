from typing import Dict, List, Literal, Optional

from pydantic import BaseModel

LinkHint = Literal["errors", "requests", "auth", "sensitive", "patient", "activity", "system"]


class StatsTotals(BaseModel):
    requests: int = 0
    errors: int = 0
    failed_logins: int = 0
    sensitive_access: int = 0
    audit_changes: int = 0
    flagged: int = 0


class TrafficPoint(BaseModel):
    t: str  # bucket start, ISO 8601 UTC ("...Z"); hourly for 24h, per SGT day otherwise
    service: str
    requests: int
    errors: int


class TopEndpoint(BaseModel):
    path: str
    count: int
    p95_ms: Optional[float] = None


class AuthPoint(BaseModel):
    t: str
    count: int


class TopAccount(BaseModel):
    user_id: str
    name: Optional[str] = None
    count: int


class AuthStats(BaseModel):
    failed_logins_over_time: List[AuthPoint] = []
    top_accounts: List[TopAccount] = []


class AuditDayAction(BaseModel):
    t: str  # SGT day start as ISO 8601 UTC
    action: str
    count: int


class TopTable(BaseModel):
    table: str
    count: int


class AuditStats(BaseModel):
    changes_per_day: List[AuditDayAction] = []
    top_tables: List[TopTable] = []
    top_actors: List[TopAccount] = []


class RecentFlag(BaseModel):
    time: str
    code: str
    severity: Literal["warning", "critical"]
    reason: str
    service: Optional[str] = None
    link_hint: LinkHint


class LogStats(BaseModel):
    window: Literal["24h", "7d", "30d"]
    totals: StatsTotals
    traffic: List[TrafficPoint] = []
    top_endpoints: List[TopEndpoint] = []
    status_mix: Dict[str, int]  # keys "2xx", "3xx", "4xx", "5xx"
    auth: AuthStats
    audit: AuditStats
    recent_flags: List[RecentFlag] = []
