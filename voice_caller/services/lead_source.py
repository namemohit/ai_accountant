"""Normalize leads from the three supported sources into call-ready dicts.

Sources:
  - platform : rows from db.list_leads(...) (the L1 Lead Gen agent)
  - csv      : an uploaded CSV (header row mapped by name)
  - manual   : a single lead from the UI form

Every source yields the same shape consumed by db.add_voice_calls:
  {id?, name, business_name, phone, why_fit}
`id` (the platform leads.id) is only present for the platform source and is
what lets the call's outcome write back onto the originating lead.
"""
import csv
import io

_CSV_ALIASES = {
    "phone": "phone", "mobile": "phone", "number": "phone", "phone_number": "phone",
    "name": "name", "contact": "name", "contact_name": "name", "person": "name",
    "business": "business_name", "business_name": "business_name",
    "company": "business_name", "company_name": "business_name",
    "why_fit": "why_fit", "notes": "why_fit", "reason": "why_fit",
}


def from_platform_leads(rows):
    """`rows` is the list returned by db.list_leads (dict-like)."""
    out = []
    for l in rows or []:
        phone = (l.get("phone") or "").strip()
        if not phone:
            continue
        out.append({"id": str(l.get("id")) if l.get("id") is not None else None,
                    "name": l.get("name"),
                    "business_name": l.get("business_name"),
                    "phone": phone,
                    "why_fit": l.get("why_fit")})
    return out


def from_csv(content: bytes):
    text = content.decode("utf-8-sig", "ignore")
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        mapped = {}
        for raw_key, val in row.items():
            if raw_key is None:
                continue
            canon = _CSV_ALIASES.get(raw_key.strip().lower())
            if canon and val:
                mapped[canon] = val.strip()
        if mapped.get("phone"):
            out.append({"id": None, "name": mapped.get("name"),
                        "business_name": mapped.get("business_name"),
                        "phone": mapped.get("phone"),
                        "why_fit": mapped.get("why_fit")})
    return out


def from_manual(name=None, business_name=None, phone=None, why_fit=None):
    if not (phone or "").strip():
        return []
    return [{"id": None, "name": name, "business_name": business_name,
             "phone": phone.strip(), "why_fit": why_fit}]
