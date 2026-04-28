from typing import Literal, Optional

from pydantic import BaseModel, Field


class TaskData(BaseModel):
    title: str
    description: str
    client_request: str
    deadline: Optional[str] = None
    priority: Literal["P0", "P1", "P2"]
    tag: Literal["Gestion de catalogue", "QFIX", "Demande SEO", "Custom DEV"]
    subtasks: list[str] = Field(default_factory=list)


class ProcessMessageRequest(BaseModel):
    sender_id: str
    message: Optional[str] = None
