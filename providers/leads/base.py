"""Base contract for lead-data providers.

A provider takes structured search params and returns a list of *normalized*
lead dicts so the pipeline (server.py) and the UI never care which source the
data came from. Add a new source by subclassing LeadProvider and registering it
in registry.py — nothing else changes.
"""
from abc import ABC, abstractmethod


# Canonical keys every provider must emit (missing values -> None / "").
LEAD_FIELDS = (
    "name", "business_name", "category", "address", "city",
    "phone", "website", "email", "rating", "source", "raw_json",
)


class LeadProviderUnavailable(Exception):
    """Raised when a provider can't run — e.g. its API key isn't configured.

    The endpoint catches this and returns a friendly message instead of a 500.
    """
    pass


def normalize_lead(source, **kw):
    """Build a lead dict with every canonical key present."""
    lead = {k: kw.get(k) for k in LEAD_FIELDS}
    lead["source"] = source
    return lead


class LeadProvider(ABC):
    name = "base"
    label = "Base"

    @abstractmethod
    def search(self, params: dict, limit: int = 20) -> list:
        """Return up to `limit` normalized lead dicts for the given search params.

        `params` is the structured search spec produced from the user's free-text
        context, e.g. {"query": "...", "business_type": "...", "location": "..."}.
        Raise LeadProviderUnavailable if the provider cannot run.
        """
        raise NotImplementedError
