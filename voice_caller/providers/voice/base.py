"""Voice + brain provider contracts.

The voice layer is split so each piece is swappable independently:
  - VoiceProvider : speech I/O (STT + TTS) — the source of the Indian accent.
  - BrainProvider : the LLM that decides the next reply.

Add a new provider by subclassing the relevant ABC and registering it in
`registry.py`. Nothing else changes.
"""
from abc import ABC, abstractmethod
from typing import Optional


class VoiceProviderUnavailable(Exception):
    """Raised when a provider can't run (e.g. missing API key)."""


class VoiceProvider(ABC):
    name = "base"
    label = "Base"
    default_language = "en-IN"

    # NOTE: Optional[str] (not `str | None`) — PEP 604 union syntax requires
    # Python 3.10+, but the Cloud Run container runs python:3.9-slim. The
    # `str | None` annotation at module load raised TypeError before the
    # uvicorn process could even open port 8080, which was the *actual*
    # root cause of the startup-probe timeouts on revisions 00074 + 00075
    # + 00076 + 00077. Bumping to Python 3.11 in the Dockerfile would also
    # work; this surgical fix avoids that broader change.
    @abstractmethod
    def transcribe(self, audio: bytes, language: Optional[str] = None) -> str: ...

    @abstractmethod
    def synthesize(self, text: str, language: Optional[str] = None,
                   voice: Optional[str] = None) -> bytes: ...


class BrainProvider(ABC):
    name = "base"
    label = "Base"

    @abstractmethod
    def chat(self, messages: list[dict], temperature: float = 0.3,
             max_tokens: int = 512) -> str: ...
