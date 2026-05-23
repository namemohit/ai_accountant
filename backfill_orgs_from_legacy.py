#!/usr/bin/env python3
"""Sprint 45 — one-shot, idempotent migration of legacy accounting_users into the
Phase-B org model.

For each accounting_users row that has no users_id yet:
  - create a Phase-B `users` row (same username/password/identity),
  - create a `company`-type `organization` (owned by that user),
  - create `companies` rows (one per name in their `companies` list),
  - create an `owner` `membership`,
  - link accounting_users.users_id -> users.id.

Leaves `company_name`/`companies` on accounting_users intact, so all existing
data access (keyed by company_name) keeps working unchanged. Safe to re-run.
"""
import json
import db


def main():
    db._ensure_onboarding_columns()
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT username, password, name, email, phone, company_name, companies, user_type
        FROM accounting_users
        WHERE users_id IS NULL
        ORDER BY created_at NULLS FIRST
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} legacy user(s) to migrate.")
    migrated = 0
    for (username, password, name, email, phone, company_name, companies, user_type) in rows:
        try:
            comp_list = companies if isinstance(companies, list) else (json.loads(companies) if companies else [])
            if not comp_list and company_name:
                comp_list = [company_name]
            org_label = company_name or (comp_list[0] if comp_list else f"{username}'s organisation")

            c2 = conn.cursor()
            # 1) users row — reuse if a Phase-B row already exists for this username
            c2.execute("SELECT id FROM users WHERE username=%s", (username,))
            ex = c2.fetchone()
            if ex:
                users_id = ex[0]
            else:
                c2.execute("""INSERT INTO users (username, password, name, email, phone)
                              VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                           (username, password, name or username, email or f"{username}@yantrai.com", phone or ""))
                users_id = c2.fetchone()[0]

            # If this user already has an org/membership (prior Phase-B work), don't
            # create a duplicate org — just link the legacy row to their identity.
            c2.execute("SELECT org_id FROM memberships WHERE user_id=%s LIMIT 1", (users_id,))
            existing_mem = c2.fetchone()
            if existing_mem:
                org_id = existing_mem[0]
                reused_org = True
            else:
                c2.execute("""INSERT INTO organizations (name, type, plan, created_by_user_id)
                              VALUES (%s,'company','free',%s) RETURNING id""", (org_label, users_id))
                org_id = c2.fetchone()[0]
                reused_org = False
                # companies (one per name), first = primary
                for i, cname in enumerate(comp_list or [org_label]):
                    c2.execute("""INSERT INTO companies (org_id, name, is_primary)
                                  VALUES (%s,%s,%s)
                                  ON CONFLICT (org_id, name) DO NOTHING""",
                               (org_id, cname, i == 0))
                c2.execute("""INSERT INTO memberships (user_id, org_id, role)
                              VALUES (%s,%s,'owner')
                              ON CONFLICT (user_id, org_id) DO NOTHING""", (users_id, org_id))

            # link + default type
            c2.execute("""UPDATE accounting_users
                          SET users_id=%s, user_type=COALESCE(user_type,'business')
                          WHERE username=%s""", (users_id, username))
            conn.commit()
            c2.close()
            migrated += 1
            tag = "linked existing org" if reused_org else f"org '{org_label}' + {len(comp_list or [org_label])} company(ies)"
            print(f"  OK {username}: {tag}")
        except Exception as e:
            conn.rollback()
            try: print(f"  ERR {username}: {e}")
            except Exception: print("  ERR (encode)")
    cur.close()
    conn.close()
    print(f"Done. Migrated {migrated}/{len(rows)}.")


if __name__ == "__main__":
    main()
