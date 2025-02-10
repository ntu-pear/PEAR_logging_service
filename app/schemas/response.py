from pydantic import BaseModel
from typing import List, Optional, Generic, TypeVar

T = TypeVar("T")
class SingleResponse(BaseModel, Generic[T]):
    data: T

class PaginatedResponse(BaseModel, Generic[T]):
    data: List[T]
    pageNo: int
    pageSize: int
    totalRecords: int
    totalPages: int
