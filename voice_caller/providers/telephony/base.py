"""Telephony provider contract — the layer that actually dials a PSTN number.

Kept separate from the voice layer because Sarvam supplies STT/TTS/LLM but does
not place phone calls. A carrier (Twilio / Plivo / Exotel) places the call and
streams audio to/from the conversation engine.

For local development and the pilot UI we ship a SimulationTelephony that does
not dial — the conversation is driven turn-by-turn from typed text, so the full
qualify+book flow is exercisable without a carrier account.
"""
from abc import ABC, abstractmethod


class TelephonyUnavailable(Exception):
    """Raised when a telephony provider can't run (missing credentials)."""


class TelephonyProvider(ABC):
    name = "base"
    label = "Base"
    is_live = False  # True if placing a call reaches a real phone

    @abstractmethod
    def place_call(self, to_number: str, call_id: str, webhook_base: str) -> str:
        """Initiate an outbound call. Returns a provider-side reference. Raise
        TelephonyUnavailable if credentials are missing."""

    def hangup(self, provider_call_id: str) -> None:
        """Best-effort end an in-progress call. Optional."""
