from typing import Optional, Literal
from pydantic import BaseModel, Field

class LogQuery(BaseModel):
    action: Optional[str] = Field(None, description="Action type")
    user: Optional[str] = Field(None, description="User performing the action")
    table: Optional[str] = Field(None, description="Target database table")
    patient: Optional[str] = Field(None, description="Patient ID")
    timestamp_order: Literal["asc", "desc"] = Field("desc", description="Sort order")
