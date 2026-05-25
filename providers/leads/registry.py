"""Pluggable lead-source registry.

Adding a new source = write a LeadProvider subclass and add one line here.
The endpoint and UI read from this registry, so nothing else needs to change.
"""
from .google_places import GooglePlacesProvider

_PROVIDERS = {
    p.name: p for p in (
        GooglePlacesProvider,
    )
}

DEFAULT_SOURCE = GooglePlacesProvider.name


def get_provider(name=None):
    """Return a provider instance for `name` (or the default). None if unknown."""
    cls = _PROVIDERS.get(name or DEFAULT_SOURCE)
    return cls() if cls else None


def list_sources():
    """[{'name','label'}] for populating the UI source dropdown."""
    return [{"name": c.name, "label": c.label} for c in _PROVIDERS.values()]
