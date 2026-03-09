from typing import Optional
from pydantic import BaseModel

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