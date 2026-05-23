#!/usr/bin/env python3
"""Sprint 47 — give every existing workspace (org) the free token grant once.

Idempotent: skips orgs that already received a 'grant' ledger entry. New orgs
created via onboard_user already get the grant at signup, so this only tops up
pre-existing/backfilled orgs.
"""
import db


def main():
    db._ensure_billing_schema()
    conn = db.get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT o.id, o.name
        FROM organizations o
        WHERE o.archived_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM token_ledger l WHERE l.org_id = o.id AND l.reason = 'grant')
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    print(f"Granting {db.SIGNUP_FREE_TOKENS} tokens to {len(rows)} org(s).")
    for org_id, name in rows:
        bal = db.credit_tokens(org_id, db.SIGNUP_FREE_TOKENS, reason="grant",
                               note="initial free grant", created_by="system")
        try: print(f"  OK {name}: balance {bal}")
        except Exception: print("  OK (encode)")
    print("Done.")


if __name__ == "__main__":
    main()
