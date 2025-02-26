from typing import Optional
from pydantic import BaseModel

class LogDocument(BaseModel):
    timestamp: str
    method: str
    table: str
    user: str
    user_full_name: str
    original_data: Optional[dict] = None
    updated_data: Optional[dict] = None
    message: str