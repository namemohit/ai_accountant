"""Google Places (New) lead provider.

Uses the Places API v1 Text Search, which returns name, address, phone, website,
rating and category in a single call (one billable request per page).

Setup:
  1. In Google Cloud (same project as Gemini) enable the "Places API (New)".
  2. Create an API key, restrict it to Places API.
  3. Put GOOGLE_PLACES_API_KEY=... in .env  (falls back to GEMINI_API_KEY if the
     same key is enabled for Places).
"""
import os
import requests

from .base import LeadProvider, LeadProviderUnavailable, normalize_lead

_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_FIELD_MASK = ",".join([
    "places.displayName",
    "places.formattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.primaryTypeDisplayName",
    "places.types",
])


def _api_key():
    return os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GEMINI_API_KEY")


def _city_from_address(addr):
    """Best-effort city guess: 2nd-from-last comma segment of the formatted address."""
    if not addr:
        return None
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[-3]
    if len(parts) == 2:
        return parts[0]
    return None


class GooglePlacesProvider(LeadProvider):
    name = "google_places"
    label = "Google Places"

    def search(self, params: dict, limit: int = 20) -> list:
        key = _api_key()
        if not key:
            raise LeadProviderUnavailable(
                "Google Places is not configured. Add GOOGLE_PLACES_API_KEY to .env "
                "(enable the Places API (New) in Google Cloud).")

        query = (params.get("query") or "").strip()
        if not query:
            bt = (params.get("business_type") or "").strip()
            loc = (params.get("location") or "").strip()
            query = " in ".join([x for x in (bt, loc) if x]) or "businesses"

        limit = max(1, min(int(limit or 20), 20))  # one page; v1 page max is 20
        try:
            resp = requests.post(
                _SEARCH_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": _FIELD_MASK,
                },
                json={"textQuery": query, "pageSize": limit},
                timeout=25,
            )
        except Exception as e:
            raise LeadProviderUnavailable(f"Google Places request failed: {e}")

        if resp.status_code == 403:
            raise LeadProviderUnavailable(
                "Google Places rejected the key (403). Enable 'Places API (New)' and "
                "check key restrictions.")
        if resp.status_code != 200:
            raise LeadProviderUnavailable(
                f"Google Places error {resp.status_code}: {resp.text[:200]}")

        places = (resp.json() or {}).get("places", []) or []
        leads = []
        for p in places[:limit]:
            disp = (p.get("displayName") or {}).get("text")
            addr = p.get("formattedAddress")
            leads.append(normalize_lead(
                self.name,
                name=disp,
                business_name=disp,
                category=p.get("primaryTypeDisplayName", {}).get("text")
                         if isinstance(p.get("primaryTypeDisplayName"), dict)
                         else (p.get("types") or [None])[0],
                address=addr,
                city=_city_from_address(addr),
                phone=p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber"),
                website=p.get("websiteUri"),
                email=None,  # Places does not expose email
                rating=p.get("rating"),
                raw_json=p,
            ))
        return leads
