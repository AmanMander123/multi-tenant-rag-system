from __future__ import annotations

from typing import Iterable, Iterator, List, Tuple

from fastapi import HTTPException
from langchain_openai import ChatOpenAI
from langsmith import traceable

from app.core.config import get_settings
from app.core.secrets import fetch_secret
from app.logger import get_logger, log_context
from app.services.guardrails import Guardrails
from app.services.prompt_registry import PromptRegistry
from app.services.retrieval_engine import HybridRetriever

logger = get_logger(__name__)
settings = get_settings()


class ChatOrchestrator:
    """Routes chat requests through guardrails, retrieval, prompts, and an LLM."""

    def __init__(self) -> None:
        self._retriever = HybridRetriever()
        self._prompts = PromptRegistry()
        self._guardrails = Guardrails(settings.guardrails)
        self._api_key = (
            settings.openai_api_key
            or fetch_secret(settings.openai_secret_name, settings.openai_secret_version)
        )
        if not self._api_key:
            raise RuntimeError("OpenAI API key missing; cannot initialize orchestrator.")

    def _make_llm(self, model: str) -> ChatOpenAI:
        return ChatOpenAI(
            api_key=self._api_key,
            model=model,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
            timeout=settings.retrieval.reranker_timeout_seconds,
        )

    def _build_history(self, history: Iterable[Tuple[str, str]]) -> str:
        return self._guardrails.summarize_history(history)

    def _format_context(self, results: List[dict]) -> str:
        lines = []
        for idx, res in enumerate(results, start=1):
            content = res.get("content") or ""
            source = res.get("source_uri") or res.get("document_id") or "unknown"
            lines.append(f"[{idx}] source={source}\n{content}")
        return "\n\n".join(lines)

    @traceable(run_type="chain", name="chat_orchestrator")
    def chat(
        self,
        *,
        tenant_id: str,
        message: str,
        history: List[Tuple[str, str]] | None = None,
        prompt_name: str | None = None,
        prompt_version: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Return a full response (no streaming) with diagnostics."""
        with log_context(request_id=request_id, tenant_id=tenant_id):
            guard = self._guardrails.inspect_user_input(message)
            if not guard.allowed:
                logger.warning("Guardrail blocked request.", extra={"reason": guard.reason})
                raise HTTPException(status_code=400, detail=guard.reason or "guardrail_blocked")

            sanitized_message = guard.redacted_text or message

            retrieval = self._retriever.retrieve(query=sanitized_message, tenant_id=tenant_id)
            context_text = self._format_context(retrieval["results"])

            prompt = self._prompts.get(name=prompt_name, version=prompt_version)
            history_pairs = history or []
            rendered_history = self._build_history(history_pairs)
            messages = prompt.render(
                question=sanitized_message,
                context=context_text,
                history=rendered_history,
            )

            answer, model_used = self._invoke_with_fallbacks(messages)
            answer = self._guardrails.sanitize_output(answer)

            response = {
                "answer": answer,
                "model": model_used,
                "prompt_name": prompt.name,
                "prompt_version": prompt.version,
                "retrieval": retrieval,
            }

            logger.info(
                "Chat orchestration completed.",
                extra={
                    "tenant_id": tenant_id,
                    "model_used": model_used,
                    "prompt_version": prompt.version,
                    "returned": len(retrieval["results"]),
                },
            )
            return response

    def stream(
        self,
        *,
        tenant_id: str,
        message: str,
        history: List[Tuple[str, str]] | None = None,
        prompt_name: str | None = None,
        prompt_version: str | None = None,
        request_id: str | None = None,
    ) -> Iterator[str]:
        """Yield response text chunks for streaming clients."""
        guard = self._guardrails.inspect_user_input(message)
        if not guard.allowed:
            logger.warning("Guardrail blocked streaming request.", extra={"reason": guard.reason})
            raise HTTPException(status_code=400, detail=guard.reason or "guardrail_blocked")

        sanitized_message = guard.redacted_text or message

        retrieval = self._retriever.retrieve(query=sanitized_message, tenant_id=tenant_id)
        context_text = self._format_context(retrieval["results"])
        prompt = self._prompts.get(name=prompt_name, version=prompt_version)
        history_pairs = history or []
        rendered_history = self._build_history(history_pairs)
        messages = prompt.render(
            question=sanitized_message,
            context=context_text,
            history=rendered_history,
        )

        llm = self._make_llm(settings.llm.default_model)
        try:
            for chunk in llm.stream(messages):
                text = getattr(chunk, "content", "") if chunk else ""
                if text:
                    yield self._guardrails.sanitize_output(text)
        except Exception:
            logger.exception("Streaming failed.")
            raise HTTPException(status_code=500, detail="llm_stream_failed")

    def _invoke_with_fallbacks(self, messages: list[dict]) -> tuple[str, str]:
        last_error: Exception | None = None
        for model in [settings.llm.default_model, *settings.llm.fallback_models]:
            try:
                llm = self._make_llm(model)
                response = llm.invoke(messages)
                text = getattr(response, "content", "") if response else ""
                return text, model
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("Model failed; trying fallback.", extra={"model": model, "error": str(exc)})
                continue
        raise HTTPException(status_code=502, detail=f"llm_failed: {last_error}")
