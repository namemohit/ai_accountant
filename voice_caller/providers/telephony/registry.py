"""Telephony registry. Default = simulation, so the agent runs with no carrier.

Set TELEPHONY_PROVIDER=twilio (+ carrier credentials) for live calls.
"""
import os

from .simulation import SimulationTelephony
from .twilio_provider import TwilioTelephony

_PROVIDERS = {p.name: p for p in (SimulationTelephony, TwilioTelephony)}
DEFAULT_PROVIDER = SimulationTelephony.name


def get_provider(name=None):
    name = name or os.getenv("TELEPHONY_PROVIDER") or DEFAULT_PROVIDER
    return (_PROVIDERS.get(name) or _PROVIDERS[DEFAULT_PROVIDER])()


def list_providers():
    return [{"name": c.name, "label": c.label, "is_live": c.is_live}
            for c in _PROVIDERS.values()]
