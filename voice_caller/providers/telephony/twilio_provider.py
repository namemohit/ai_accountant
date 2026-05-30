"""Twilio telephony (production path) — outbound call + media-stream stub.

Wiring point for real calls. Twilio dials the lead and connects a bidirectional
Media Stream (websocket) back to the platform; the websocket handler bridges
that audio to the Sarvam STT/TTS + brain in services/conversation.py.

Left as an explicit, documented stub so the agent runs out-of-the-box in
simulation mode. Mirror this module as plivo_provider.py / exotel_provider.py
for India-first calling — the contract (place_call + a media websocket) is the
same.
"""
import os

from .base import TelephonyProvider, TelephonyUnavailable


class TwilioTelephony(TelephonyProvider):
    name = "twilio"
    label = "Twilio"
    is_live = True

    def place_call(self, to_number, call_id, webhook_base):
        sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
        if not (sid and token and from_number):
            raise TelephonyUnavailable(
                "Twilio not configured — set TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, and deploy with a public "
                "HTTPS URL for media streaming.")
        raise TelephonyUnavailable(
            "Twilio media-stream bridge not implemented yet — use the "
            "'simulation' provider for the pilot.")
