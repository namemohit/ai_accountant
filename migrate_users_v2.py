"""
Phase A migration: backfill multi-tenant schema from accounting_users.

For each accounting_users row:
  1. INSERT into public.users (carry username, password, name, email, phone)
  2. sadmin: is_super_admin=TRUE, skip org/membership creation
  3. Others: CREATE organization (type='firm', name=f"{username}'s Firm")
  4. For each company_name in user.companies: CREATE company under firm
  5. CREATE membership (user_id, org_id, role='owner', scope_company_ids=NULL)

Backfill company_id on all tenant tables:
  For each (table, company_name) row, find matching company_id by (org_id, name)
  UPDATE table SET company_id = matched_id

Run:
  python migrate_users_v2.py --dry-run     # show plan, no writes
  python migrate_users_v2.py --execute     # commit changes
"""
import sys
import json
import db
from psycopg2.extras import RealDictCursor

# Tables that have company_name and need company_id backfilled
TENANT_TABLES = [
    'invoices', 'parties', 'tally_vouchers', 'tally_ledgers',
    'tally_stock_items', 'tally_groups', 'tally_cost_centres',
    'tally_voucher_types', 'tally_sync_log', 'tasks',
    'recon_templates', 'recon_sessions', 'chat_sessions',
]


def get_accounting_users():
    conn = db.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM accounting_users ORDER BY username")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_distinct_company_names_in_data():
    """Find every company_name string actually used in tenant data tables."""
    conn = db.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    found = set()
    for tbl in TENANT_TABLES:
        try:
            cur.execute(f"SELECT DISTINCT company_name FROM {tbl} WHERE company_name IS NOT NULL")
            for r in cur.fetchall():
                if r['company_name']:
                    found.add(r['company_name'])
        except Exception as e:
            print(f"  skip {tbl}: {e}")
    cur.close()
    conn.close()
    return found


def plan_migration():
    """Build a plan dict of what would happen."""
    plan = {
        'users': [],
        'orgs': [],
        'companies': [],
        'memberships': [],
        'backfills': {},
        'orphan_companies': [],
    }

    accounting = get_accounting_users()
    data_companies = get_distinct_company_names_in_data()
    all_user_companies = set()

    for u in accounting:
        plan['users'].append({
            'username': u['username'],
            'role_legacy': u['role'],
            'is_super_admin': (u['role'] == 'super_admin'),
        })

        # Sadmin still gets an org so its data (YantrAI Platform Owner) has a company_id.
        # But also gets is_super_admin=TRUE for platform-level access.
        if u['role'] == 'super_admin':
            firm_name = "YantrAI Platform"
            plan['orgs'].append({'name': firm_name, 'type': 'firm', 'owner': u['username']})
            companies = u.get('companies') or [u.get('company_name', 'YantrAI Platform Owner')]
            if isinstance(companies, str):
                companies = json.loads(companies)
            for cn in companies:
                all_user_companies.add(cn)
                plan['companies'].append({'org': firm_name, 'name': cn})
            plan['memberships'].append({'user': u['username'], 'org': firm_name, 'role': 'owner'})
            continue

        firm_name = f"{u['username']}'s Firm"
        plan['orgs'].append({
            'name': firm_name,
            'type': 'firm',
            'owner': u['username'],
        })

        companies = u.get('companies') or [u.get('company_name', 'Acme Corp')]
        if isinstance(companies, str):
            companies = json.loads(companies)

        for cn in companies:
            all_user_companies.add(cn)
            plan['companies'].append({'org': firm_name, 'name': cn})

        plan['memberships'].append({
            'user': u['username'],
            'org': firm_name,
            'role': 'owner',
        })

    # Orphan: company_names in data but no user has them
    plan['orphan_companies'] = sorted(data_companies - all_user_companies)

    # Backfill: count rows per table that have company_name set
    conn = db.get_conn()
    cur = conn.cursor()
    for tbl in TENANT_TABLES:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE company_name IS NOT NULL AND company_id IS NULL")
            plan['backfills'][tbl] = cur.fetchone()[0]
        except Exception as e:
            plan['backfills'][tbl] = f"error: {e}"
    cur.close()
    conn.close()

    return plan


def print_plan(plan):
    print("=" * 70)
    print("MIGRATION PLAN — DRY RUN")
    print("=" * 70)
    print(f"\nUsers to backfill into public.users: {len(plan['users'])}")
    for u in plan['users']:
        flag = ' (SUPER ADMIN)' if u['is_super_admin'] else ''
        print(f"  - {u['username']} [legacy role: {u['role_legacy']}]{flag}")

    print(f"\nOrganizations to create: {len(plan['orgs'])}")
    for o in plan['orgs']:
        print(f"  - '{o['name']}' (type={o['type']}, owner={o['owner']})")

    print(f"\nCompanies to create: {len(plan['companies'])}")
    by_org = {}
    for c in plan['companies']:
        by_org.setdefault(c['org'], []).append(c['name'])
    for org, cs in by_org.items():
        print(f"  Under '{org}': {cs}")

    print(f"\nMemberships to create: {len(plan['memberships'])}")
    for m in plan['memberships']:
        print(f"  - {m['user']} -> {m['org']} as '{m['role']}'")

    print(f"\nOrphan companies (in data but no user has them): {len(plan['orphan_companies'])}")
    for c in plan['orphan_companies']:
        print(f"  - {c!r}  ⚠️  will be unassigned unless we map it")

    print("\nBackfill row counts (company_name → company_id):")
    for tbl, n in plan['backfills'].items():
        print(f"  {tbl:25s} {n} rows")

    print()
    print("=" * 70)


def execute_migration():
    plan = plan_migration()
    print_plan(plan)

    print("\n🚀 EXECUTING MIGRATION...\n")
    accounting = get_accounting_users()

    # Map username -> new user_id
    username_to_user_id = {}
    # Map (org_id, company_name) -> company_id
    org_company_to_id = {}
    # Map (username, company_name) -> company_id  (for backfill)
    user_company_to_company_id = {}

    # Step 1: create users in public.users
    conn = db.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    for u in accounting:
        # Insert or update
        cur.execute("""
            INSERT INTO users (username, password, name, email, phone, is_super_admin)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (username) DO UPDATE SET
                password = EXCLUDED.password,
                name = COALESCE(EXCLUDED.name, users.name),
                email = COALESCE(EXCLUDED.email, users.email),
                phone = COALESCE(EXCLUDED.phone, users.phone),
                is_super_admin = EXCLUDED.is_super_admin
            RETURNING id
        """, (u['username'], u['password'], u.get('name'), u.get('email'),
              u.get('phone'), u['role'] == 'super_admin'))
        new_id = cur.fetchone()['id']
        username_to_user_id[u['username']] = new_id
        print(f"  ✓ user {u['username']} -> {new_id}")
    conn.commit()

    # Step 2 + 3: create firm orgs + companies (sadmin also gets a 'YantrAI Platform' firm)
    for u in accounting:
        user_id = username_to_user_id[u['username']]
        if u['role'] == 'super_admin':
            firm_name = "YantrAI Platform"
        else:
            firm_name = f"{u['username']}'s Firm"

        org_id = db.create_organization(firm_name, 'firm', user_id, plan='free')
        print(f"  ✓ org '{firm_name}' -> {org_id}")

        companies = u.get('companies') or [u.get('company_name', 'Acme Corp')]
        if isinstance(companies, str):
            companies = json.loads(companies)

        for cn in companies:
            company_id = db.create_company(org_id, cn, is_primary=(cn == u.get('company_name')))
            org_company_to_id[(str(org_id), cn)] = str(company_id)
            user_company_to_company_id[(u['username'], cn)] = str(company_id)
            print(f"    ✓ company '{cn}' -> {company_id}")

        # Step 4: membership
        mem_id = db.create_membership(user_id, org_id, 'owner')
        print(f"  ✓ membership {u['username']} → {firm_name} (owner) {mem_id}")

    # Step 5: backfill company_id on tenant tables
    print("\n🔁 Backfilling company_id on tenant tables...")
    cur2 = conn.cursor()
    for tbl in TENANT_TABLES:
        # For each row with NULL company_id, find the matching company.
        # Strategy: match on company_name, then pick the company_id from the org owned by the
        # user whose `companies` array includes this name. If multiple users own this company,
        # we use the FIRST matching org (deterministic by org creation order).
        try:
            cur2.execute(f"""
                UPDATE {tbl} t
                SET company_id = sub.cid
                FROM (
                    SELECT DISTINCT ON (c.name) c.name AS cname, c.id AS cid
                    FROM companies c
                    ORDER BY c.name, c.created_at ASC
                ) sub
                WHERE t.company_id IS NULL AND t.company_name = sub.cname
            """)
            print(f"  ✓ {tbl}: {cur2.rowcount} rows updated")
        except Exception as e:
            print(f"  ✗ {tbl}: {e}")
            conn.rollback()
            cur2 = conn.cursor()
    conn.commit()

    # Auto-flag sensitive ledgers
    flagged = db.mark_sensitive_ledgers()
    print(f"\n🔒 Auto-flagged {flagged} sensitive ledgers")

    cur.close()
    cur2.close()
    conn.close()
    print("\n✅ MIGRATION COMPLETE\n")


def main():
    args = sys.argv[1:]
    if '--execute' in args:
        execute_migration()
    else:
        plan = plan_migration()
        print_plan(plan)
        print("\n(dry-run only — re-run with --execute to commit)")


if __name__ == '__main__':
    main()
