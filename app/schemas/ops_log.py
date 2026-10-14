from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.log_document import Flag


class OpsLogDocument(BaseModel):
    """One operational log line (schema pear-app-v1) as returned to the admin console."""

    timestamp: str
    ops_type: str
    message: str = ""
    level: Optional[str] = None
    service: Optional[str] = None
    environment: Optional[str] = None
    logger: Optional[str] = None
    request_id: Optional[str] = None
    user_id: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    query: Optional[str] = None
    status: Optional[int] = None
    duration_ms: Optional[float] = None
    event: Optional[str] = None  # auth event name; stored as "auth_event" in Elasticsearch
    reason: Optional[str] = None
    exception_type: Optional[str] = None
    stack_trace: Optional[str] = None
    flags: List[Flag] = []


class OpsLogQuery(BaseModel):
    service: Optional[str] = Field(None, description="patient | activity | user | scheduler")
    level: Optional[str] = Field(None, description="INFO | WARNING | ERROR | CRITICAL")
    request_id: Optional[str] = Field(None, description="X-Request-ID; when given, no default time window applies")
    user_id: Optional[str] = Field(None, description="User ID of the caller")
    environment: Optional[str] = Field(None, description="Environment (staging or production)")
    start_date: Optional[str] = Field(None, description="SGT date or datetime; default: 24 hours ago")
    end_date: Optional[str] = Field(None, description="SGT date or datetime")
    timestamp_order: Literal["asc", "desc"] = Field("desc", description="Sort order")
