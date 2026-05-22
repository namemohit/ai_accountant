"""Sprint 18 backfill: re-derive Tally bank_transactions amount signs from the
authoritative source (tally_vouchers.ledger_entries). This corrects rows that
Sprint 17 mis-normalized for multi-bank-leg Receipt/Payment vouchers.

Rule: bank_transactions.amount = -1 * ledger_entry.amount
      (Tally Cr=+ve, Dr=-ve; bank perspective: in=+ve, out=-ve → simple negate)

Then re-run the linker with the new fallback (same-bank + same-day + same-amount).
Idempotent.
"""
import os, sys, json
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
    GROUP BY voucher_type ORDER BY voucher_type
""")
for r in cur.fetchall():
    print(f"  vt={r[0] or '(none)':10s}  +ve={r[1]:5d}  -ve={r[2]:5d}  total={r[3]}")

# Pull all Tally rows + their parent voucher's ledger_entries
cur.execute("""
    SELECT bt.id, bt.bank_ledger, bt.amount AS bt_amount,
           tv.voucher_type, tv.ledger_entries
    FROM bank_transactions bt
    JOIN tally_vouchers tv ON tv.id = bt.source_record_id
    WHERE bt.source = 'tally'
""")
rows = cur.fetchall()
print(f"\n[scan] {len(rows)} Tally bank_transactions rows")

fixed = 0
unchanged = 0
no_match = 0
for r in rows:
    bt_id, bank_ledger, bt_amount, vt, entries = r
    if isinstance(entries, str):
        try: entries = json.loads(entries)
        except: entries = []
    if not entries: continue
    # Find the bank leg that matches bank_ledger
    leg = None
    for e in entries:
        if (e.get("ledger_name") or e.get("ledger")) == bank_ledger:
            leg = e
            break
    if not leg:
        no_match += 1
        continue
    raw_amt = float(leg.get("amount") or 0)
    # Apply Sprint 18 logic: prefer is_debit, else negate, else fallback to vt
    is_debit = leg.get("is_debit")
    if is_debit is True:
        new_amt = abs(raw_amt)
    elif is_debit is False:
        new_amt = -abs(raw_amt)
    elif raw_amt != 0:
        new_amt = -raw_amt
    else:
        vtl = (vt or "").lower()
        if vtl == "payment":   new_amt = -abs(raw_amt)
        elif vtl == "receipt": new_amt = abs(raw_amt)
        else: new_amt = raw_amt
    new_amt = round(new_amt, 2)
    if abs(float(bt_amount) - new_amt) < 0.005:
        unchanged += 1
        continue
    cur.execute("UPDATE bank_transactions SET amount = %s WHERE id = %s",
                (new_amt, str(bt_id)))
    fixed += 1
conn.commit()
print(f"[fix] re-derived sign for {fixed} rows; unchanged {unchanged}; no parent leg match {no_match}")

print("\n[after]")
cur.execute("""
    SELECT voucher_type,
           SUM(CASE WHEN amount > 0 THEN 1 ELSE 0 END) AS pos_n,
           SUM(CASE WHEN amount < 0 THEN 1 ELSE 0 END) AS neg_n,
           COUNT(*) AS total
    FROM bank_transactions
    WHERE source = 'tally'
    GROUP BY voucher_type ORDER BY voucher_type
""")
for r in cur.fetchall():
    print(f"  vt={r[0] or '(none)':10s}  +ve={r[1]:5d}  -ve={r[2]:5d}  total={r[3]}")

# Re-run linker for every company
print("\n[linker] re-running with new same-bank-day fallback...")
cur.execute("SELECT DISTINCT company_id FROM bank_transactions WHERE company_id IS NOT NULL")
total_new = 0
for (cid,) in cur.fetchall():
    res = db.link_bank_transactions(str(cid))
    n = res.get("linked_pairs", 0) if isinstance(res, dict) else 0
    total_new += n
    if n: print(f"  company={cid}  +{n} pairs")
print(f"[linker] total NEW pairs linked: {total_new}")

cur.execute("SELECT COUNT(*) FROM bank_transactions WHERE linked_id IS NOT NULL")
print(f"\n[summary] rows with linked_id set: {cur.fetchone()[0]}")
cur.close(); conn.close()
