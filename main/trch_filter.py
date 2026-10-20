"""Trench T01 Token-Lite prompt compression layer.

This module intentionally contains only the public filter API used by trch.py.
"""

from __future__ import annotations

import re
from typing import Any


class TokenFilter:
    """Conservative prompt/context compressor used by Trench.

    It removes common conversational filler while preserving URLs, code fences,
    identifiers, and normal sentence structure. Token counts are estimates, not
    tokenizer-specific billing numbers.
    """

    _REDUNDANT_PATTERNS = (
        r"\bplease\s+provide\s+(?:a\s+)?comprehensive\s+",
        r"\bprovide\s+detailed\s+explanations?\s*",
        r"\bmake\s+sure\s+to\s+",
        r"\bbe\s+sure\s+to\s+",
        r"\bfor\s+your\s+reference\s*",
        r"\bas\s+a\s+reminder\s*",
        r"\bi\s+would\s+appreciate\s+if\s+you\s+",
        r"\bcould\s+you\s+please\s+",
        r"\bwould\s+you\s+be\s+so\s+kind\s+as\s+to\s+",
        r"\bif\s+possible\s*",
        r"\bthanks\s+in\s+advance\s*",
        r"\bkind\s+regards\s*",
        r"\bwell[- ]structured\s*",
        r"\bwell[- ]formatted\s*",
        r"\bclear\s+and\s+concise\s*",
        r"\bcomprehensive\s+and\s+detailed\s*",
    )

    def __init__(self, enabled: bool = False):
        self.enabled = bool(enabled)
        self.mode_name = "⚡ Token-Lite" if self.enabled else "Standard"

    def toggle(self) -> str:
        self.enabled = not self.enabled
        self.mode_name = "⚡ Token-Lite" if self.enabled else "Standard"
        return f"Filter mode: {self.mode_name}"

    @staticmethod
    def _protect_code_and_urls(text: str):
        protected: list[str] = []

        def stash(match: re.Match[str]) -> str:
            protected.append(match.group(0))
            return f"\x00TRCHPROTECT{len(protected) - 1}\x00"

        # Protect fenced code first, then URLs that may occur outside code.
        text = re.sub(r"```[\s\S]*?```", stash, text)
        text = re.sub(r"https?://[^\s)]+", stash, text)
        return text, protected

    @staticmethod
    def _restore(text: str, protected: list[str]) -> str:
        for index, value in enumerate(protected):
            text = text.replace(f"\x00TRCHPROTECT{index}\x00", value)
        return text

    def compress_prompt(self, prompt: str) -> str:
        if not self.enabled or not isinstance(prompt, str):
            return prompt
        text, protected = self._protect_code_and_urls(prompt)
        text = re.sub(r"\n\s*\n+", "\n", text)
        for pattern in self._REDUNDANT_PATTERNS:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        # Collapse whitespace without destroying line structure or indentation inside
        # protected code blocks.
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return self._restore(text, protected).strip()

    def create_compact_system_prompt(self, base_prompt: str) -> str:
        if not self.enabled:
            return base_prompt
        return "Trench: direct, accurate, concise. Follow the user's request. Put code in blocks."

    def compress_context(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled or not isinstance(context, dict):
            return context
        compressed: dict[str, Any] = {}
        for key, value in context.items():
            if isinstance(value, str) and len(value) > 400:
                compressed[key] = self.compress_prompt(value[:400])
            elif isinstance(value, list) and len(value) > 5:
                compressed[key] = value[-5:]
            else:
                compressed[key] = value
        return compressed

    def strip_metadata(self, text: str) -> str:
        if not self.enabled or not isinstance(text, str):
            return text
        text = re.sub(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b", "", text)
        text = re.sub(r"\b[a-f0-9]{32}\b", "", text, flags=re.IGNORECASE)
        return re.sub(r"[ \t]{2,}", " ", text).strip()

    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not isinstance(text, str):
            return 0
        # Character-based estimate only. Real token counts vary by provider/model.
        return max(1, (len(text) + 3) // 4) if text else 0


TOKEN_FILTER = TokenFilter(enabled=False)
