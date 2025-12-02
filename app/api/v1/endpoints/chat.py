from __future__ import annotations

from typing import List, Literal, Optional, Tuple

from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.logger import get_logger
from app.services.orchestrator import ChatOrchestrator

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()
orchestrator = ChatOrchestrator()


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., description="User message to send to the assistant.")
    history: List[ChatHistoryItem] = Field(
        default_factory=list,
        description="Optional short chat history to ground the response.",
    )
    prompt_name: Optional[str] = Field(
        default=None,
        description="Prompt pack to use (defaults to configured prompt).",
    )
    prompt_version: Optional[str] = Field(
        default=None,
        description="Prompt version to load (defaults to configured version).",
    )
    stream: bool = Field(
        default=False,
        description="If true, return a streaming response.",
    )


@router.post(
    "/chat",
    status_code=status.HTTP_200_OK,
    summary="Run chat orchestration with retrieval, prompts, and guardrails.",
)
async def chat(request: Request, payload: ChatRequest):
    auth_context = getattr(request.state, "auth_context", None)
    tenant_id = getattr(auth_context, "tenant_id", None) or settings.default_tenant_id
    request_id = getattr(auth_context, "request_id", None) or getattr(request.state, "request_id", None)

    history_pairs: List[Tuple[str, str]] = [(item.role, item.content) for item in payload.history]

    if payload.stream:
        stream = orchestrator.stream(
            tenant_id=tenant_id,
            message=payload.message,
            history=history_pairs,
            prompt_name=payload.prompt_name,
            prompt_version=payload.prompt_version,
            request_id=request_id,
        )
        return StreamingResponse(stream, media_type="text/plain")

    response = orchestrator.chat(
        tenant_id=tenant_id,
        message=payload.message,
        history=history_pairs,
        prompt_name=payload.prompt_name,
        prompt_version=payload.prompt_version,
        request_id=request_id,
    )
    return response
