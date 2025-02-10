from typing import Optional
from pydantic import BaseModel

class LogDocument(BaseModel):
    timestamp: str
    method: str
    table: str
    user: str
    original_data: Optional[dict] = None
    updated_data: Optional[dict] = None