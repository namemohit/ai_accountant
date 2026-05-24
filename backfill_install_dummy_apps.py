#!/usr/bin/env python3
"""Sprint 65 — install the two dummy apps (demo-alpha, demo-beta) for every
existing workspace (org) so the iPhone-style swipeable Home pager has enough tiles
to span 2 pages. Idempotent: install_agent upserts (enabled=TRUE). Re-runnable.
Uninstall later from the Store; unseed by removing them from AGENT_SEED.
"""
import db

DUMMY_SLUGS = ["demo-alpha", "demo-beta"]


def main():
    db._ensure_billing_schema()   # ensures store_agents seeded (incl. dummies) + installs table
    conn = db.get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name FROM organizations WHERE archived_at IS NULL")
    rows = cur.fetchall()
    cur.close(); conn.close()
    print(f"Installing dummy apps {DUMMY_SLUGS} for {len(rows)} org(s).")
    for org_id, name in rows:
        for slug in DUMMY_SLUGS:
            ok = db.install_agent(org_id, slug, by_user_id=None)
            try:
                print(f"  {'OK' if ok else 'ERR'} {name} <- {slug}")
            except Exception:
                print(f"  {'OK' if ok else 'ERR'} (encode) <- {slug}")
    print("Done.")


if __name__ == "__main__":
    main()
