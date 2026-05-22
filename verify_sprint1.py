"""Sprint 1 verification — check DB state after sync + embeddings."""
import db

conn = db.get_conn()
cur = conn.cursor(cursor_factory=db.RealDictCursor)
cid = '2a0637fc-d446-4ab4-af06-021730a5768c'
co_name = 'Jai Mata Kalka Enterprises'

print('=== Master tables under JMK ===')
for tbl in ['tally_vouchers','tally_ledgers','tally_groups','tally_stock_items','tally_sync_log']:
    cur.execute(f'SELECT COUNT(*) as n FROM {tbl} WHERE company_id = %s', (cid,))
    print(f'  {tbl:22s} {cur.fetchone()["n"]} rows')

print('\n=== Sample stock items (NEW) ===')
cur.execute('SELECT name, hsn_code, gst_rate, unit, closing_qty, closing_value FROM tally_stock_items WHERE company_id = %s LIMIT 10', (cid,))
for r in cur.fetchall():
    print(f'  {(r["name"] or "")[:35]:35s} HSN:{(r["hsn_code"] or "-"):8s} GST:{r["gst_rate"] or 0}% qty:{r["closing_qty"]} val:{r["closing_value"]}')

print('\n=== HTML entity check ===')
cur.execute("SELECT name, parent_group FROM tally_ledgers WHERE company_id = %s AND (name LIKE %s OR parent_group LIKE %s OR name LIKE %s) LIMIT 5", (cid, '%&amp;%', '%&amp;%', '%&#%'))
rows = cur.fetchall()
print(f'  Ledgers with stale HTML entities: {len(rows)} (should be 0 after Sprint 1)')
for r in rows:
    print(f'    {r["name"]!r} | {r["parent_group"]!r}')

print('\n=== Cost centres extracted? ===')
cur.execute("""
SELECT COUNT(*) as n FROM tally_vouchers
WHERE company_id = %s
  AND jsonb_path_exists(ledger_entries::jsonb, '$[*].cost_centres')
""", (cid,))
print(f'  Vouchers with cost_centres: {cur.fetchone()["n"]}')

print('\n=== Sensitive ledgers ===')
cur.execute('SELECT name, parent_group FROM tally_ledgers WHERE company_id = %s AND is_sensitive = TRUE', (cid,))
for r in cur.fetchall():
    print(f'  🔒 {r["name"]:30s} (under {r["parent_group"]})')

print('\n=== Vector embeddings (knowledge_base) ===')
for kbtype in ['tally_master_ledger','tally_master_party','tally_master_narration','tally_master_item']:
    cur.execute("SELECT COUNT(*) FROM knowledge_base WHERE type = %s AND data->>'company_name' = %s", (kbtype, co_name))
    print(f'  {kbtype:30s} {cur.fetchone()["count"]} embeddings')

# Total knowledge_base for this company
cur.execute("SELECT COUNT(*) FROM knowledge_base WHERE data->>'company_name' = %s", (co_name,))
print(f'  {"TOTAL kb rows":30s} {cur.fetchone()["count"]}')

cur.close()
conn.close()
