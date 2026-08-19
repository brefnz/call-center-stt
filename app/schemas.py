from typing import Optional
from pydantic import BaseModel, Field


class KBArticleCreate(BaseModel):
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    category: Optional[str] = None


class KBArticleUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    category: Optional[str] = None


class TranscriptEvent(BaseModel):
    call_id: str
    speaker: str  # "agent" | "customer" | "unknown"
    text: str


class KBSuggestion(BaseModel):
    id: str
    title: str
    content: str
    score: float
