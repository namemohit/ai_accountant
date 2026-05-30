"""Simulation telephony — the default. Places no real call.

Marks the call as in_progress and lets the conversation engine be driven via
POST /api/voice/calls/{id}/say. Powers the pilot UI's "test call" mode.
"""
import uuid

from .base import TelephonyProvider


class SimulationTelephony(TelephonyProvider):
    name = "simulation"
    label = "Simulation (no real call)"
    is_live = False

    def place_call(self, to_number, call_id, webhook_base):
        return f"sim_{uuid.uuid4().hex[:12]}"
