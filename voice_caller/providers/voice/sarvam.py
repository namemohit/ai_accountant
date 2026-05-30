"""Sarvam AI — the default Indian-voice + brain stack.

  - Saarika  (STT)               -> SarvamVoiceProvider.transcribe
  - Bulbul   (TTS)               -> SarvamVoiceProvider.synthesize
  - Sarvam-M (chat, OpenAI-shape)-> SarvamBrainProvider.chat

One key (`SARVAM_API_KEY`) covers all three, sent as `api-subscription-key`.
Docs: https://docs.sarvam.ai
"""
import base64
import os

import httpx

from .base import BrainProvider, VoiceProvider, VoiceProviderUnavailable

_API = "https://api.sarvam.ai"


def _key() -> str:
    k = os.getenv("SARVAM_API_KEY", "").strip()
    if not k:
        raise VoiceProviderUnavailable("SARVAM_API_KEY is not set.")
    return k


def _headers() -> dict:
    return {"api-subscription-key": _key()}


class SarvamVoiceProvider(VoiceProvider):
    name = "sarvam"
    label = "Sarvam AI (Indian voices)"
    default_language = os.getenv("SARVAM_LANGUAGE", "en-IN")
    default_speaker = os.getenv("SARVAM_SPEAKER", "anushka")
    tts_model = os.getenv("SARVAM_TTS_MODEL", "bulbul:v2")
    stt_model = os.getenv("SARVAM_STT_MODEL", "saarika:v2")

    def transcribe(self, audio, language=None):
        try:
            with httpx.Client(timeout=30) as c:
                r = c.post(f"{_API}/speech-to-text", headers=_headers(),
                    data={"model": self.stt_model,
                          "language_code": language or self.default_language},
                    files={"file": ("audio.wav", audio, "audio/wav")})
                r.raise_for_status()
                return (r.json() or {}).get("transcript", "").strip()
        except VoiceProviderUnavailable:
            raise
        except Exception as e:
            raise VoiceProviderUnavailable(f"Sarvam STT failed: {e}")

    def synthesize(self, text, language=None, voice=None):
        try:
            with httpx.Client(timeout=30) as c:
                r = c.post(f"{_API}/text-to-speech",
                    headers={**_headers(), "Content-Type": "application/json"},
                    json={"inputs": [text],
                          "target_language_code": language or self.default_language,
                          "speaker": voice or self.default_speaker,
                          "model": self.tts_model})
                r.raise_for_status()
                audios = (r.json() or {}).get("audios") or []
                return base64.b64decode(audios[0]) if audios else b""
        except VoiceProviderUnavailable:
            raise
        except Exception as e:
            raise VoiceProviderUnavailable(f"Sarvam TTS failed: {e}")


class SarvamBrainProvider(BrainProvider):
    name = "sarvam"
    label = "Sarvam-M"
    model = os.getenv("SARVAM_CHAT_MODEL", "sarvam-m")

    def chat(self, messages, temperature=0.3, max_tokens=512):
        try:
            with httpx.Client(timeout=45) as c:
                r = c.post(f"{_API}/v1/chat/completions",
                    headers={**_headers(), "Content-Type": "application/json"},
                    json={"model": self.model, "messages": messages,
                          "temperature": temperature, "max_tokens": max_tokens})
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
        except VoiceProviderUnavailable:
            raise
        except Exception as e:
            raise VoiceProviderUnavailable(f"Sarvam chat failed: {e}")
