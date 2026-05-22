"""Sprint 17 one-shot backfill:
1. Flip signs on existing source='tally' rows so Payment = -ve, Receipt = +ve.
2. Clear Head where it duplicates Party on Payment/Receipt rows.
3. Re-run db.link_bank_transactions() for every company to persist linked_id
   on Phase-1 matched pairs that never got the FK written.
Idempotent — safe to re-run.
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))
import db

conn = db.get_conn()
cur = conn.cursor()

print("[before]")
cur.execute("""
    SELECT voucher_type,
           SUM(CASE WHEN amount > 0 THEN 1 ELSE 0 END) AS pos_n,
           SUM(CASE WHEN amount < 0 THEN 1 ELSE 0 END) AS neg_n,
           COUNT(*) AS total
    FROM bank_transactions
    WHERE source = 'tally'
    GROUP BY voucher_type
    ORDER BY voucher_type
""")
for r in cur.fetchall():
    print(f"  voucher_type={r[0] or '(none)':12s}  +ve={r[1]:5d}  -ve={r[2]:5d}  total={r[3]:5d}")

# 1. Payment vouchers should have negative amount; flip positives
cur.execute("""
    UPDATE bank_transactions
    SET amount = -ABS(amount)
    WHERE source = 'tally'
      AND voucher_type ILIKE 'Payment'
      AND amount > 0
""")
flipped_payments = cur.rowcount
print(f"\n[fix] Payment +ve -> -ve: {flipped_payments} rows")

# 2. Receipt vouchers should have positive amount; flip negatives
cur.execute("""
    UPDATE bank_transactions
    SET amount = ABS(amount)
    WHERE source = 'tally'
      AND voucher_type ILIKE 'Receipt'
      AND amount < 0
""")
flipped_receipts = cur.rowcount
print(f"[fix] Receipt -ve -> +ve: {flipped_receipts} rows")

# 3. Clear Head where it duplicates Party on Payment/Receipt rows
cur.execute("""
    UPDATE bank_transactions
    SET head = NULL
    WHERE source = 'tally'
      AND voucher_type ILIKE ANY (ARRAY['Payment','Receipt'])
      AND head = party
""")
cleared_heads = cur.rowcount
print(f"[fix] Cleared duplicate Heads on Payment/Receipt: {cleared_heads} rows")

conn.commit()

print("\n[after]")
cur.execute("""
    SELECT voucher_type,
           SUM(CASE WHEN amount > 0 THEN 1 ELSE 0 END) AS pos_n,
           SUM(CASE WHEN amount < 0 THEN 1 ELSE 0 END) AS neg_n,
           COUNT(*) AS total
    FROM bank_transactions
    WHERE source = 'tally'
    GROUP BY voucher_type
    ORDER BY voucher_type
""")
for r in cur.fetchall():
    print(f"  voucher_type={r[0] or '(none)':12s}  +ve={r[1]:5d}  -ve={r[2]:5d}  total={r[3]:5d}")

# 4. Re-link cross-source pairs
print("\n[linker] running link_bank_transactions per company...")
cur.execute("SELECT DISTINCT company_id FROM bank_transactions WHERE company_id IS NOT NULL")
total_linked = 0
for (cid,) in cur.fetchall():
    cur.execute("SELECT name FROM companies WHERE id = %s", (cid,))
    cname_row = cur.fetchone()
    cname = cname_row[0] if cname_row else str(cid)
    res = db.link_bank_transactions(str(cid))
    n = res.get("linked_pairs", 0) if isinstance(res, dict) else 0
    total_linked += n
    if n:
        print(f"  {cname}: +{n} pairs linked")
print(f"[linker] total new pairs linked: {total_linked}")

print("\n[summary]")
cur.execute("SELECT COUNT(*) FROM bank_transactions WHERE source='tally' AND linked_id IS NOT NULL")
print(f"  Tally rows with linked_id set: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM bank_transactions WHERE source='bank_statement' AND linked_id IS NOT NULL")
print(f"  Statement rows with linked_id set: {cur.fetchone()[0]}")

cur.close()
conn.close()
