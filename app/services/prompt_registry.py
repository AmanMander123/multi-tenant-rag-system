from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import yaml

from app.core.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PromptTemplate:
    """Represents a single prompt version loaded from YAML."""

    name: str
    version: str
    system: str
    user: str
    description: str | None = None

    def render(
        self,
        *,
        question: str,
        context: str,
        history: str,
    ) -> list[dict]:
        """Render to an OpenAI-style message list."""
        rendered_user = self.user.format(
            question=question,
            context=context or "No additional context provided.",
            history=history or "No prior messages.",
        )
        return [
            {"role": "system", "content": self.system.strip()},
            {"role": "user", "content": rendered_user.strip()},
        ]


class PromptRegistry:
    """Loads and resolves prompt templates by name/version."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = Path(self.settings.prompts.path).resolve()
        self._prompts: Dict[str, Dict[str, PromptTemplate]] = {}
        self._load_prompts()

    def _load_prompts(self) -> None:
        if not self.root.exists():
            logger.warning("Prompt path missing; creating it.", extra={"path": str(self.root)})
            self.root.mkdir(parents=True, exist_ok=True)

        for path in self.root.glob("*.yml"):
            try:
                with path.open("r", encoding="utf-8") as file:
                    data = yaml.safe_load(file) or {}
            except Exception:
                logger.exception("Failed to load prompt file.", extra={"path": str(path)})
                continue

            # Accept either a mapping for a single prompt or a list of prompts.
            records: List[dict] = []
            if isinstance(data, dict) and "name" in data:
                records = [data]
            elif isinstance(data, list):
                records = [item for item in data if isinstance(item, dict)]

            for record in records:
                name = str(record.get("name") or self.settings.prompts.default_name)
                version = str(record.get("version") or self.settings.prompts.default_version)
                system = str(record.get("system") or "").strip()
                user = str(record.get("user") or "").strip()
                description = record.get("description")

                if not system or not user:
                    logger.warning(
                        "Prompt missing required fields; skipping.",
                        extra={"path": str(path), "record": json.dumps(record)},
                    )
                    continue

                template = PromptTemplate(
                    name=name,
                    version=version,
                    system=system,
                    user=user,
                    description=description,
                )

                self._prompts.setdefault(name, {})[version] = template

        if not self._prompts:
            logger.warning("No prompts loaded; ensure prompt YAML files exist.")

    def get(self, *, name: str | None = None, version: str | None = None) -> PromptTemplate:
        """Return a prompt template; defaults to configured name/version."""
        resolved_name = name or self.settings.prompts.default_name
        versions = self._prompts.get(resolved_name, {})
        if not versions:
            raise RuntimeError(f"No prompts loaded for name '{resolved_name}'.")

        if version:
            template = versions.get(version)
            if not template:
                raise RuntimeError(f"Prompt '{resolved_name}' with version '{version}' not found.")
            return template

        # Fallback to the default version, otherwise pick the highest lexical version.
        default_version = self.settings.prompts.default_version
        if default_version in versions:
            return versions[default_version]

        latest_version = sorted(versions.keys())[-1]
        return versions[latest_version]
