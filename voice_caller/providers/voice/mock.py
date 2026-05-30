"""Offline fallback used when SARVAM_API_KEY is absent.

Lets the simulation telephony path and the UI work end-to-end with no keys.
The brain is rule-based but still emits the structured [[OUTCOME ...]] markers
the conversation engine expects.
"""
from .base import BrainProvider, VoiceProvider


class MockVoiceProvider(VoiceProvider):
    name = "mock"
    label = "Mock (offline, no audio)"

    def transcribe(self, audio, language=None):
        try:
            return audio.decode("utf-8", "ignore").strip()
        except Exception:
            return ""

    def synthesize(self, text, language=None, voice=None):
        return text.encode("utf-8")


class MockBrainProvider(BrainProvider):
    name = "mock"
    label = "Mock (offline rule-based)"

    def chat(self, messages, temperature=0.3, max_tokens=512):
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = (m.get("content") or "").lower()
                break
        turns = sum(1 for m in messages if m.get("role") == "assistant")

        if not last_user:
            return ("Hello! This is Asha calling on behalf of our team. "
                    "Do you have a quick minute to chat?")
        if any(w in last_user for w in ("no", "not interested", "busy", "stop", "remove")):
            return ("No problem at all, thank you for your time. Have a great day! "
                    "[[OUTCOME status=not_interested interest=low]]")
        if any(w in last_user for w in ("yes", "sure", "interested", "tell me", "okay", "ok")):
            if turns >= 1:
                return ("Wonderful — would tomorrow at 11 AM work for a short call "
                        "with our specialist? [[OUTCOME status=booked interest=high "
                        "callback=tomorrow 11:00]]")
            return ("Great! We help businesses like yours save time and money. "
                    "Would you be open to a short follow-up call this week?")
        return ("I understand. To help you best, could you tell me if this is "
                "something you'd like to explore? [[OUTCOME status=contacted interest=medium]]")
