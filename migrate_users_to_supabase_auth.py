#!/usr/bin/env python3
"""Sprint 51 — migrate existing users into Supabase Auth (seamless).

For every accounting_users row not yet linked (auth_uid IS NULL), create a
Supabase auth user with the SAME email the app will use at login + the user's
CURRENT plaintext password, then store the returned auth uid on both identity
tables. Idempotent: skips already-linked rows; reuses an existing auth user if
the email is already registered.

Run ONCE after providing the Supabase env vars:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (required)
Then flip AUTH_ENFORCE=1 on the server to require verified sessions.
"""
import os
import requests
try:
    from dotenv import load_dotenv
    load_dotenv()   # read keys from the local .env (same file the server uses)
except Exception:
    pass
import db

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://vxnflumpectzqdamjqsc.supabase.co").rstrip("/")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SYNTH_DOMAIN = "yantrai.app"


def _login_email(username, email):
    return (email or "").strip() or f"{username}@{SYNTH_DOMAIN}"


def _admin_create(email, password):
    """Create (or find) a Supabase auth user. Returns the auth uid or None."""
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
         "Content-Type": "application/json"}
    r = requests.post(f"{SUPABASE_URL}/auth/v1/admin/users", headers=h,
                      json={"email": email, "password": password, "email_confirm": True},
                      timeout=20)
    if r.status_code in (200, 201):
        return (r.json() or {}).get("id")
    if r.status_code == 422:  # already registered → look it up
        g = requests.get(f"{SUPABASE_URL}/auth/v1/admin/users", headers=h,
                         params={"email": email}, timeout=20)
        if g.status_code == 200:
            j = g.json() or {}
            users = j.get("users") or j.get("data") or []
            if users:
                return users[0].get("id")
    print(f"    ERR {r.status_code}: {r.text[:160]}")
    return None


def main():
    if not SERVICE_KEY:
        print("Missing SUPABASE_SERVICE_ROLE_KEY env var — aborting.")
        return
    db._ensure_billing_schema()  # ensure auth_uid columns exist
    conn = db.get_conn(); cur = conn.cursor()
    cur.execute("""SELECT username, password, email FROM accounting_users
                   WHERE auth_uid IS NULL ORDER BY id""")
    rows = cur.fetchall()
    cur.close(); conn.close()
    print(f"Migrating {len(rows)} user(s) into Supabase Auth.")
    ok = skip = fail = 0
    for username, password, email in rows:
        em = _login_email(username, email)
        uid = _admin_create(em, password or "changeme123!")
        if uid:
            db.link_auth_uid(username, uid)
            ok += 1
            print(f"  OK  {username}  ({em})")
        else:
            fail += 1
            print(f"  FAIL {username}  ({em})")
    print(f"Done. linked={ok} failed={fail}")


if __name__ == "__main__":
    main()
