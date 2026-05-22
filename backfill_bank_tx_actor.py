"""Sprint 11 one-shot backfill: populate ai_touched + human_touched on existing
bank_transactions rows from current evidence (source / status). Idempotent —
safe to re-run.
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))
import db

conn = db.get_conn()
cur = conn.cursor()

# Before snapshot
cur.execute("""
    SELECT
      SUM(CASE WHEN ai_touched THEN 1 ELSE 0 END) AS ai_n,
      SUM(CASE WHEN human_touched THEN 1 ELSE 0 END) AS human_n,
      SUM(CASE WHEN ai_touched AND human_touched THEN 1 ELSE 0 END) AS both_n,
      COUNT(*) AS total
    FROM bank_transactions
""")
b = cur.fetchone()
print(f"[before]  total={b[3]}  ai={b[0]}  human={b[1]}  both={b[2]}")

# Human-touched: anything that originates from a human-curated system or manual entry
cur.execute("UPDATE bank_transactions SET human_touched = TRUE WHERE source IN ('tally','manual') AND human_touched = FALSE")
print(f"  +human (source in tally/manual): {cur.rowcount}")

# Human-touched: user explicitly clicked Post to Tally
cur.execute("UPDATE bank_transactions SET human_touched = TRUE WHERE status = 'posted' AND human_touched = FALSE")
print(f"  +human (status=posted): {cur.rowcount}")

# AI-touched: AI engine actively produced suggestions
cur.execute("""
    UPDATE bank_transactions SET ai_touched = TRUE
    WHERE source IN ('bank_statement','invoice')
      AND status IN ('matched','ai_filled','posted')
      AND ai_touched = FALSE
""")
print(f"  +ai    (statement/invoice + matched/ai_filled/posted): {cur.rowcount}")

conn.commit()

# After snapshot
cur.execute("""
    SELECT
      SUM(CASE WHEN ai_touched AND NOT human_touched THEN 1 ELSE 0 END) AS ai_only,
      SUM(CASE WHEN human_touched AND NOT ai_touched THEN 1 ELSE 0 END) AS human_only,
      SUM(CASE WHEN ai_touched AND human_touched THEN 1 ELSE 0 END) AS both,
      SUM(CASE WHEN NOT ai_touched AND NOT human_touched THEN 1 ELSE 0 END) AS neither,
      COUNT(*) AS total
    FROM bank_transactions
""")
a = cur.fetchone()
print(f"\n[after]   total={a[4]}")
print(f"  🤖 AI only         : {a[0]}")
print(f"  👤 Human only      : {a[1]}")
print(f"  🤖+👤 AI+Human     : {a[2]}")
print(f"  —  Neither (untouched): {a[3]}")

# Breakdown by source × status for sanity
cur.execute("""
    SELECT source, status, ai_touched, human_touched, COUNT(*) AS n
    FROM bank_transactions
    GROUP BY source, status, ai_touched, human_touched
    ORDER BY source, status
""")
print("\n[breakdown source × status × flags]")
for r in cur.fetchall():
    print(f"  {r[0]:15s} {r[1]:10s} ai={int(r[2])} human={int(r[3])}   n={r[4]}")

cur.close()
conn.close()
