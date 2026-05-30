"""Voice + brain provider contracts.

The voice layer is split so each piece is swappable independently:
  - VoiceProvider : speech I/O (STT + TTS) — the source of the Indian accent.
  - BrainProvider : the LLM that decides the next reply.

Add a new provider by subclassing the relevant ABC and registering it in
`registry.py`. Nothing else changes.
"""
from abc import ABC, abstractmethod


class VoiceProviderUnavailable(Exception):
    """Raised when a provider can't run (e.g. missing API key)."""


class VoiceProvider(ABC):
    name = "base"
    label = "Base"
    default_language = "en-IN"

    @abstractmethod
    def transcribe(self, audio: bytes, language: str | None = None) -> str: ...

    @abstractmethod
    def synthesize(self, text: str, language: str | None = None,
                   voice: str | None = None) -> bytes: ...


class BrainProvider(ABC):
    name = "base"
    label = "Base"

    @abstractmethod
    def chat(self, messages: list[dict], temperature: float = 0.3,
             max_tokens: int = 512) -> str: ...
