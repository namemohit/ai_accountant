#!/usr/bin/env python3
"""Sprint 48 — install Agent #1 (AI Accountant) for every existing workspace (org).

Idempotent: skips orgs that already have an 'ai-accountant' install row. New orgs
created via onboard_user already get the install at signup, so this only covers
pre-existing/backfilled orgs. Run once after deploying Sprint 48.
"""
import db


def main():
    db._ensure_billing_schema()   # ensures agents + org_agent_installs exist + seeded
    conn = db.get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT o.id, o.name
        FROM organizations o
        WHERE o.archived_at IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM org_agent_installs i
            WHERE i.org_id = o.id AND i.agent_slug = 'ai-accountant')
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    print(f"Installing Agent #1 (ai-accountant) for {len(rows)} org(s).")
    for org_id, name in rows:
        ok = db.install_agent(org_id, "ai-accountant", by_user_id=None)
        try:
            print(f"  {'OK' if ok else 'ERR'} {name}")
        except Exception:
            print(f"  {'OK' if ok else 'ERR'} (encode)")
    print("Done.")


if __name__ == "__main__":
    main()
