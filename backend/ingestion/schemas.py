from typing import List
from pydantic import BaseModel, Field

class AudioPayload(BaseModel):
    device_id: str
    timestamp: float = Field(default_factory=lambda: 0.0) # Will be set to server time if 0.0 or missing
    session_id: str
    samples: List[int]
