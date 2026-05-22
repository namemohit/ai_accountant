"""Read-only outbox state for the Sun Pharma test."""
import sys, os
sys.path.insert(0, '.')
import db
conn=db.get_conn(); cur=conn.cursor()
cur.execute("""SELECT id, state, attempts, last_error, tally_voucher_guid, pushed_at
                FROM tally_outbox
                WHERE payload->>'invoice_number'='JMK/2026-27/047-TEST'
                ORDER BY enqueued_at DESC LIMIT 5""")
for r in cur.fetchall():
    print(f"  {str(r[0])[:14]}  state={r[1]:<8}  attempts={r[2]}  guid={r[4]}  err={r[3]}")
cur.close(); conn.close()
