from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Tuple

from app.core.config import GuardrailSettings, get_settings
from app.logger import get_logger

logger = get_logger(__name__)


_PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),  # credit card-ish
    re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"),  # phone
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email
]


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    reason: str | None = None
    redacted_text: str | None = None


class Guardrails:
    """Lightweight PII and prompt-injection guardrails."""

    def __init__(self, settings: GuardrailSettings | None = None) -> None:
        self.settings = settings or get_settings().guardrails
        self._banned = [phrase.lower() for phrase in self.settings.banned_phrases]

    def inspect_user_input(self, text: str) -> GuardrailResult:
        normalized = text.lower()

        if len(text) > self.settings.max_input_chars:
            return GuardrailResult(False, reason="input_too_large")

        if self.settings.enable_prompt_injection_block:
            for phrase in self._banned:
                if phrase in normalized:
                    return GuardrailResult(False, reason="prompt_injection_detected")

        redacted = text
        if self.settings.enable_pii_redaction:
            redacted = self._redact(text)

        return GuardrailResult(True, redacted_text=redacted)

    def sanitize_output(self, text: str) -> str:
        if not self.settings.enable_pii_redaction:
            return text
        return self._redact(text)

    def _redact(self, text: str) -> str:
        redacted = text
        for pattern in _PII_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    @staticmethod
    def summarize_history(history: Iterable[Tuple[str, str]]) -> str:
        """Render short history lines for prompt conditioning."""
        lines = []
        for role, content in history:
            trimmed = content.strip()
            if len(trimmed) > 400:
                trimmed = trimmed[:400] + "…"
            lines.append(f"{role}: {trimmed}")
        return "\n".join(lines)
