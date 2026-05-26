#!/usr/bin/env python3
"""Publish the L2 agents (bluestone-cam-ai, fieldai, voice-caller) into the store
as first-party, public, chargeable REMOTE apps.

Unlike _seed_agents (which only writes 11 columns), remote+chargeable apps also
need client_id + signing_key (for SSO scoping / usage metering) and visibility.
This script does a full upsert and is idempotent: re-runs update metadata and the
remote_url but PRESERVE existing client_id/signing_key (rotating them would break
live sessions and metering).

Set the live deployment URLs via env before running:
    BLUESTONE_REMOTE_URL=https://...   FIELDAI_REMOTE_URL=https://...
    VOICECALLER_REMOTE_URL=https://...
Defaults are placeholders under yantrailabs.com — update if the hosts differ.

After running, copy each printed client_id/signing_key into that agent's env.
"""
import os
import db

AGENTS = [
    {
        "slug": "bluestone-cam-ai",
        "name": "Bluestone Cam AI",
        "tagline": "Walk-ins, staff presence & store intelligence from your cameras.",
        "description": "Turns daily multi-camera store footage into walk-in counts, "
                       "staff-presence tracking, demography and dashboards — automatically.",
        "icon": "📹",
        "category": "retail",
        "remote_url_env": "BLUESTONE_REMOTE_URL",
        "default_url": "https://bluestone.yantrailabs.com",
        "sort_order": 30,
    },
    {
        "slug": "fieldai",
        "name": "FieldAI",
        "tagline": "Automated field inspection of trenches, cables & manholes by photo.",
        "description": "Computer-vision field inspection: measures trench depth/width, "
                       "detects cables, manholes, ducts and warning tape from site photos.",
        "icon": "🛠️",
        "category": "field-inspection",
        "remote_url_env": "FIELDAI_REMOTE_URL",
        "default_url": "https://fieldai.yantrailabs.com",
        "sort_order": 35,
    },
    {
        "slug": "voice-caller",
        "name": "Voice Caller",
        "tagline": "AI voice agent that calls your leads and books follow-ups.",
        "description": "Calls the leads you generate in an Indian voice, qualifies "
                       "interest and books a follow-up, then syncs the outcome back "
                       "to each lead's CRM status. Pull from Lead Gen, CSV or manual.",
        "icon": "📞",
        "category": "sales",
        "remote_url_env": "VOICECALLER_REMOTE_URL",
        "default_url": "https://voice-caller.yantrailabs.com",
        "sort_order": 40,
    },
]


def main():
    db._ensure_billing_schema()   # ensures store_agents + Sprint-50 columns exist
    for a in AGENTS:
        remote_url = os.getenv(a["remote_url_env"], a["default_url"]).strip()
        # Same code path as the developer portal — just first-party + public + chargeable.
        res = db.create_app(
            org_id=None, user_id=None, name=a["name"], remote_url=remote_url,
            slug=a["slug"], tagline=a["tagline"], description=a["description"],
            icon=a["icon"], category=a["category"],
            publisher="first-party", visibility="public",
            token_policy={"chargeable": True}, sort_order=a["sort_order"],
        )
        if not res.get("ok"):
            print(f"{a['slug']}: ERROR {res.get('error')}")
            continue
        print(f"{a['slug']}")
        print(f"  remote_url   = {remote_url}")
        print(f"  client_id    = {res['client_id']}")
        print(f"  signing_key  = {res['client_secret']}")
    print("Done. Copy each client_id/signing_key into the matching agent's env.")


if __name__ == "__main__":
    main()
