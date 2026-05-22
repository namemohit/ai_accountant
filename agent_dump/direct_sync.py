"""Direct Tally → DB sync — bypasses WS entirely.
Fetches data from local Tally using the bridge agent functions,
saves directly to DB using db.py, then runs embeddings.
"""
import os, sys, time, json
from dotenv import load_dotenv
load_dotenv()

# Add current dir to path
sys.path.insert(0, os.path.dirname(__file__))

import db
import google.generativeai as genai
from tally_bridge_agent import (
    fetch_tally_company_info,
    fetch_rich_ledgers,
    fetch_groups,
    fetch_vouchers,
    fetch_stock_items,
)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

TALLY_URL = "http://localhost:9000"
COMPANY_ID = "2a0637fc-d446-4ab4-af06-021730a5768c"

def get_embedding(text):
    try:
        r = genai.embed_content(
            model="models/gemini-embedding-2",
            content=text,
            task_type="retrieval_document",
        )
        return r["embedding"]
    except Exception as e:
        print(f"  embed error: {e}")
        return None

# Step 1: Probe Tally
print("=" * 60)
info = fetch_tally_company_info(TALLY_URL)
if info["state"] != "ok":
    print(f"ERROR: Tally state={info['state']}"); sys.exit(1)
tally_company = info["company_name"]
print(f"Tally company: {tally_company}")

# Step 2: Fetch everything
print("\n--- Fetching from Tally ---")
t0 = time.time()

print("  Ledgers...", end=" ", flush=True)
ledgers = fetch_rich_ledgers(TALLY_URL)
print(f"{len(ledgers)}")

print("  Groups...", end=" ", flush=True)
groups = fetch_groups(TALLY_URL)
print(f"{len(groups)}")

print("  Vouchers (all history)...", end=" ", flush=True)
vouchers = fetch_vouchers(TALLY_URL)
print(f"{len(vouchers)}")

print("  Stock items...", end=" ", flush=True)
stock_items = fetch_stock_items(TALLY_URL)
print(f"{len(stock_items)}")

fetch_time = time.time() - t0
print(f"  Fetch took {fetch_time:.1f}s")

# Step 3: Save to DB
print("\n--- Saving to DB ---")
t1 = time.time()

print("  Saving vouchers...", end=" ", flush=True)
v_result = db.save_tally_vouchers(tally_company, vouchers)
print(f"upserted={v_result.get('upserted',0)}, skipped={v_result.get('skipped',0)}")

print("  Saving ledgers...", end=" ", flush=True)
ledger_count = db.save_tally_ledgers(tally_company, ledgers)
print(f"{ledger_count}")

print("  Saving groups...", end=" ", flush=True)
group_count = db.save_tally_groups(tally_company, groups)
print(f"{group_count}")

print("  Saving stock items...", end=" ", flush=True)
stock_count = db.save_tally_stock_items(tally_company, stock_items)
print(f"{stock_count}")

save_time = time.time() - t1
print(f"  Save took {save_time:.1f}s")

# Step 4: Log sync
db.log_tally_sync(tally_company, 'baseline',
                  records_in=len(vouchers)+len(ledgers)+len(groups)+len(stock_items),
                  records_upserted=v_result.get('upserted',0)+ledger_count+group_count+stock_count,
                  status='success')

# Step 5: Backfill company_id
print("\n--- Backfilling company_id ---")
conn_bf = db.get_conn()
cur_bf = conn_bf.cursor()
for tbl in ['tally_vouchers', 'tally_ledgers', 'tally_groups', 'tally_stock_items', 'tally_sync_log']:
    cur_bf.execute(f"UPDATE {tbl} SET company_id = %s WHERE company_name = %s AND company_id IS NULL", (COMPANY_ID, tally_company))
    print(f"  {tbl}: {cur_bf.rowcount} rows updated")
conn_bf.commit()
cur_bf.close()
conn_bf.close()

# Step 6: Sensitive ledgers
print("\n--- Sensitive ledger detection ---")
flagged = db.mark_sensitive_ledgers(COMPANY_ID)
print(f"  Flagged {flagged} ledgers")

# Step 7: Embeddings
print("\n--- Running embeddings ---")
t2 = time.time()
res = db.embed_tally_master(COMPANY_ID, tally_company, get_embedding, batch_log=lambda msg: print(f"  {msg}"))
embed_time = time.time() - t2
print(f"  Result: {res}")
print(f"  Embed took {embed_time:.1f}s")

# Step 8: Verify
print("\n--- Verification ---")
conn = db.get_conn()
cur = conn.cursor(cursor_factory=db.RealDictCursor)
cur.execute('SELECT MIN(date) as earliest, MAX(date) as latest, COUNT(*) as total FROM tally_vouchers WHERE company_id = %s', (COMPANY_ID,))
r = cur.fetchone()
print(f"  Vouchers: {r['total']} | {r['earliest']} to {r['latest']}")

cur.execute('SELECT COUNT(*) as n FROM tally_ledgers WHERE company_id = %s', (COMPANY_ID,))
print(f"  Ledgers: {cur.fetchone()['n']}")

cur.execute('SELECT COUNT(*) as n FROM tally_groups WHERE company_id = %s', (COMPANY_ID,))
print(f"  Groups: {cur.fetchone()['n']}")

cur.execute('SELECT COUNT(*) as n FROM tally_stock_items WHERE company_id = %s', (COMPANY_ID,))
print(f"  Stock items: {cur.fetchone()['n']}")

for kbtype in ['tally_master_ledger','tally_master_party','tally_master_narration','tally_master_item']:
    cur.execute("SELECT COUNT(*) FROM knowledge_base WHERE type = %s AND data->>'company_name' = %s", (kbtype, tally_company))
    print(f"  {kbtype:30s} {cur.fetchone()['count']} embeddings")

cur.close()
conn.close()
print(f"\n✓ Total time: {time.time()-t0:.1f}s")
