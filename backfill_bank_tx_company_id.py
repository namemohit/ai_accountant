"""Sprint 10 one-shot backfill: populate company_id on orphan bank_transactions
rows by joining on company_name. Idempotent — safe to re-run.
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))
import db

conn = db.get_conn()
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM bank_transactions WHERE company_id IS NULL")
before = cur.fetchone()[0]
print(f"[before] NULL company_id rows: {before}")

cur.execute("""
    UPDATE bank_transactions bt
    SET company_id = c.id
    FROM companies c
    WHERE bt.company_id IS NULL
      AND bt.company_name = c.name
""")
updated = cur.rowcount
conn.commit()

cur.execute("SELECT COUNT(*) FROM bank_transactions WHERE company_id IS NULL")
after = cur.fetchone()[0]
print(f"[updated] {updated} rows backfilled")
print(f"[after]  NULL company_id rows: {after}")

# Per-company / per-source breakdown for sanity
cur.execute("""
    SELECT source, company_name, COUNT(*) AS n,
           SUM(CASE WHEN company_id IS NULL THEN 1 ELSE 0 END) AS still_null
    FROM bank_transactions
    GROUP BY source, company_name
    ORDER BY source, company_name
""")
print("\n[breakdown]")
for r in cur.fetchall():
    print(f"  {r[0]:18s} {r[1] or '(no name)':40s} n={r[2]:5d}  still_null={r[3]}")

cur.close()
conn.close()
