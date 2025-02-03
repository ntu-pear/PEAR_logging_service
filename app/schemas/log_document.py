from typing import Optional
from pydantic import BaseModel

class LogDocument(BaseModel):
    timestamp: str
    log_type: str
    method: str
    url: str
    user: str
    status: str
    request_body: Optional[str] = None