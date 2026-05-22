"""Manually run embed_tally_master for JMK — picks up the fixed db.py code."""
import os, time
from dotenv import load_dotenv
load_dotenv()

import db
import google.generativeai as genai

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))


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


COMPANY_ID = "2a0637fc-d446-4ab4-af06-021730a5768c"
COMPANY_NAME = "Jai Mata Kalka Enterprises"

print(f"Running embed_tally_master for {COMPANY_NAME}…")
t0 = time.time()
res = db.embed_tally_master(COMPANY_ID, COMPANY_NAME, get_embedding,
                            batch_log=lambda msg: None)
elapsed = time.time() - t0
print(f"Result: {res}")
print(f"Elapsed: {elapsed:.1f}s")

# Verify
import db as db2
conn = db2.get_conn()
cur = conn.cursor(cursor_factory=db2.RealDictCursor)
for kbtype in ['tally_master_ledger', 'tally_master_party', 'tally_master_narration', 'tally_master_item']:
    cur.execute("SELECT COUNT(*) FROM knowledge_base WHERE type = %s AND data->>'company_name' = %s",
                (kbtype, COMPANY_NAME))
    print(f"  {kbtype:30s} {cur.fetchone()['count']} embeddings")
cur.close()
conn.close()
