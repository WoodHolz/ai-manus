from pydantic import BaseModel, Field
from typing import Optional

class ChatRequest(BaseModel):
    message: str = Field(..., description="User message")
    timestamp: Optional[int] = Field(None, description="Request timestamp")
    event_id: Optional[str] = Field(None, description="Event ID")

class FileViewRequest(BaseModel):
    file: str

class ShellViewRequest(BaseModel):
    session_id: str

class CreateAgentRequest(BaseModel):
    name: str 

class StartSessionRequest(BaseModel):
    url: str
    cookies: list[dict]

class NavigateRequest(BaseModel):
    url: str = Field(..., description="URL to navigate to")