from typing import List, Literal, Optional
from pydantic import BaseModel


class Flag(BaseModel):
    """A rule-based marker for abnormal activity on a log row (see app/services/log_flags.py)."""

    code: Literal["failed_login_burst", "bulk_delete", "off_hours", "mass_access", "sensitive_change", "error_spike"]
    reason: str
    severity: Literal["warning", "critical"]


class LogDocument(BaseModel):
    timestamp: str
    method: str
    table: str
    user: str
    user_full_name: str
    patient_id: Optional[int] = None
    patient_full_name: Optional[str] = None
    entity_id: Optional[int] = None # For activity and other entity logs
    original_data: Optional[dict] = None
    updated_data: Optional[dict] = None
    message: str
    role: str = None # User logs
    log_type: Optional[str] = None
    is_system_config: Optional[bool] = None
    flags: List[Flag] = []