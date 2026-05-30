"""Pluggable voice + brain registry.

Swap by env: `VOICE_PROVIDER`, `BRAIN_PROVIDER`. Falls back to `mock` when
Sarvam isn't configured, so the agent boots and the simulation path works
out-of-the-box.
"""
import os

from .mock import MockBrainProvider, MockVoiceProvider
from .sarvam import SarvamBrainProvider, SarvamVoiceProvider

_VOICE = {p.name: p for p in (SarvamVoiceProvider, MockVoiceProvider)}
_BRAIN = {p.name: p for p in (SarvamBrainProvider, MockBrainProvider)}

DEFAULT_VOICE = SarvamVoiceProvider.name
DEFAULT_BRAIN = SarvamBrainProvider.name


def _has_sarvam():
    return bool(os.getenv("SARVAM_API_KEY", "").strip())


def get_voice(name=None):
    name = name or os.getenv("VOICE_PROVIDER") or DEFAULT_VOICE
    if name == "sarvam" and not _has_sarvam():
        name = "mock"
    return (_VOICE.get(name) or _VOICE["mock"])()


def get_brain(name=None):
    name = name or os.getenv("BRAIN_PROVIDER") or DEFAULT_BRAIN
    if name == "sarvam" and not _has_sarvam():
        name = "mock"
    return (_BRAIN.get(name) or _BRAIN["mock"])()


def list_voices():
    return [{"name": c.name, "label": c.label} for c in _VOICE.values()]
