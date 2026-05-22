"""Sprint 19 — reset existing linked_id values + re-run scored linker.

This clears the 175 pairs created by earlier first-match-wins logic and lets
the new scored linker reassign them based on evidence strength. Statement rows
that were Phase-1 marked `status='matched'` still appear as "Linked" in the
column (the Sprint 16 fallback handles that), so no visible regression even
for rows the scored linker doesn't re-pair.
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))
import db

conn = db.get_conn()
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM bank_transactions WHERE linked_id IS NOT NULL")
before = cur.fetchone()[0]
print(f"[before] linked rows: {before}")

cur.execute("UPDATE bank_transactions SET linked_id = NULL WHERE linked_id IS NOT NULL")
reset_n = cur.rowcount
conn.commit()
print(f"[reset]  cleared linked_id on {reset_n} rows")

# Re-run linker per company
cur.execute("SELECT DISTINCT company_id FROM bank_transactions WHERE company_id IS NOT NULL")
total_new = 0
for (cid,) in cur.fetchall():
    res = db.link_bank_transactions(str(cid))
    n = res.get("linked_pairs", 0) if isinstance(res, dict) else 0
    total_new += n
    if n: print(f"  company={cid}  {n} pairs linked")
print(f"\n[after]  total pairs: {total_new}  (rows linked = {total_new*2})")

cur.execute("SELECT COUNT(*) FROM bank_transactions WHERE linked_id IS NOT NULL")
print(f"[verify] linked rows in DB: {cur.fetchone()[0]}")
cur.close(); conn.close()
