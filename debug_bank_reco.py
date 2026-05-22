"""Run the AI bank reconciler on your real ICICI bank statement and print
EVERY step of the RAG pipeline so you can see how the engine works.
"""
import os, sys, json, time
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from google import generativeai as genai
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

BANK_FILE = r"C:\Users\namem\Desktop\jaimatakalkaenterprisesdatafy202627\Bank Statement FY 2026-27\Bank Statement FY 2026-27\ICICI Bank\JMK_ICICI_Apr'26.xlsx"
COMPANY_NAME = "Jai Mata Kalka Enterprises"
COMPANY_ID = "2a0637fc-d446-4ab4-af06-021730a5768c"

print("=" * 80)
print("STEP 1 — READ XLSX → CONVERT TO CSV TEXT FOR GEMINI")
print("=" * 80)
if not os.path.exists(BANK_FILE):
    print(f"FILE NOT FOUND: {BANK_FILE}"); sys.exit(1)

xl = pd.ExcelFile(BANK_FILE)
print(f"  Sheets in file: {xl.sheet_names}")
pieces = []
for sh in xl.sheet_names:
    df = xl.parse(sh)
    pieces.append(f"--- Sheet: {sh} ---\n" + df.to_csv(index=False))
text_content = "\n\n".join(pieces)
print(f"  Total CSV-text length: {len(text_content):,} chars")
print(f"  First 600 chars preview:")
print("  | " + text_content[:600].replace("\n", "\n  | "))

print("\n" + "=" * 80)
print("STEP 2 — PARSE XLSX WITH PANDAS (no Gemini — direct column read)")
print("=" * 80)
from datetime import datetime as _dt
t0 = time.time()
transactions = []
for sh in xl.sheet_names:
    raw = xl.parse(sh, header=None)
    header_row_idx = None
    for ridx in range(min(15, len(raw))):
        row_str = ' '.join(str(c).lower() for c in raw.iloc[ridx].values if str(c) != 'nan')
        hits = sum(1 for k in ['date', 'description', 'amount', 'cr', 'dr', 'particulars', 'narration'] if k in row_str)
        if hits >= 3:
            header_row_idx = ridx; break
    if header_row_idx is None: continue
    headers = [str(c).strip() for c in raw.iloc[header_row_idx].values]
    body = raw.iloc[header_row_idx + 1:].copy()
    body.columns = headers
    body = body.dropna(how='all')

    def find_col(*needles):
        for h in headers:
            hl = h.lower()
            if any(n in hl for n in needles): return h
        return None
    col_date = find_col('value date', 'date')
    col_desc = find_col('description', 'particulars', 'narration')
    col_party = find_col('party')
    col_details = find_col('details', 'remark')
    col_drcr = find_col('cr/dr', 'dr/cr')
    col_amount = find_col('amount', 'transaction amount')

    for _, row in body.iterrows():
        raw_date = str(row.get(col_date, '') if col_date else '').strip()
        if not raw_date or raw_date == 'nan': continue
        date_str = None
        for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
            try: date_str = _dt.strptime(raw_date.split()[0], fmt).strftime('%Y-%m-%d'); break
            except: continue
        if not date_str: continue
        try: amt = float(str(row.get(col_amount, 0)).replace(',', '') or 0)
        except: amt = 0
        drcr = str(row.get(col_drcr, '')).strip().upper() if col_drcr else ''
        if drcr.startswith('DR'): amt = -abs(amt)
        elif drcr.startswith('CR'): amt = abs(amt)
        if abs(amt) < 0.01: continue
        desc = str(row.get(col_desc, '') if col_desc else '').strip()
        party_guess = str(row.get(col_party, '') if col_party else '').strip()
        details = str(row.get(col_details, '') if col_details else '').strip()
        full_desc = (desc + (' | ' + details if details else '')).strip()
        import re as _re
        mref = _re.search(r'\b([A-Z0-9]{8,})\b', desc)
        ref = mref.group(1) if mref else ''
        transactions.append({
            "date": date_str, "description": full_desc, "reference": ref,
            "amount": amt, "party_name": party_guess, "transaction_type": "Other",
        })
parse_time = time.time() - t0
print(f"  Pandas parse took {parse_time*1000:.0f}ms")
print(f"  Extracted {len(transactions)} transactions")
if transactions:
    print(f"  Sample transaction:")
    print("  | " + json.dumps(transactions[0], indent=2, ensure_ascii=False).replace("\n", "\n  | "))
    print(f"  Last transaction:")
    print("  | " + json.dumps(transactions[-1], indent=2, ensure_ascii=False).replace("\n", "\n  | "))

if not transactions:
    print("ABORT: no transactions parsed"); sys.exit(1)

print("\n" + "=" * 80)
print("STEP 3 — RUN AI RECONCILER (RAG + Gemini reasoning)")
print("=" * 80)

from utils.reconciler import ai_reconcile_statement

def progress_cb(p):
    phase = p.get("phase", "?")
    if phase == "starting":
        print(f"  [progress] starting, total={p['total']}")
    elif phase == "phase1":
        if p.get("done") % 10 == 0 or p.get("done") == p.get("total"):
            print(f"  [progress] phase1 (deterministic match)  {p['done']}/{p['total']}")
    elif phase == "phase2":
        if p.get("done") % 5 == 0 or p.get("done") == p.get("total"):
            print(f"  [progress] phase2 (vector retrieval)  {p['done']}/{p['total']}")
    elif phase == "gemini_start":
        print(f"  [progress] gemini_start  needs_ai={p['needs_ai']}")
    elif phase == "gemini_progress":
        print(f"  [progress] gemini_progress  done={p['done']}/{p['needs_ai']}")
    elif phase == "done":
        print(f"  [progress] DONE")

t0 = time.time()
reconciled = ai_reconcile_statement(transactions, COMPANY_NAME, company_id=COMPANY_ID, progress_cb=progress_cb, file_hint=os.path.basename(BANK_FILE))
recon_time = time.time() - t0
print(f"\n  Total reconciliation time: {recon_time:.1f}s")

print("\n" + "=" * 80)
print("STEP 4 — RESULTS SUMMARY")
print("=" * 80)
auto_matched = sum(1 for r in reconciled if r['status'] == 'auto_matched')
auto_filled  = sum(1 for r in reconciled if r['status'] == 'auto_filled')
unmatched    = sum(1 for r in reconciled if r['status'] == 'unmatched')
print(f"  Total: {len(reconciled)}")
print(f"  ✅ Matched existing voucher: {auto_matched}")
print(f"  🤖 AI Filled (party + head suggested): {auto_filled}")
print(f"  ⚠️  Needs review:                       {unmatched}")

print("\n" + "=" * 80)
print("STEP 5 — SAMPLE OUTPUTS (first 5 reconciled rows)")
print("=" * 80)
for i, r in enumerate(reconciled[:5]):
    tx = r['bank_transaction']
    print(f"\n--- Row {i+1} ---")
    print(f"  Bank line:      {tx.get('date')} | {tx.get('description', '')[:60]} | ₹{tx.get('amount')}")
    print(f"  Reference:      {tx.get('reference', '-')}")
    print(f"  Status:         {r['status']}  (confidence {round(r['confidence']*100)}%)")
    print(f"  → Party:        {r['suggested_party']}")
    print(f"  → Head:         {r['suggested_expense_head']}")
    print(f"  → Bank ledger:  {r['suggested_bank_ledger']}")
    print(f"  → Voucher type: {r['voucher_type']}")
    print(f"  → Rationale:    {r['rationale']}")
    print(f"  Candidate parties from vector search:  {r.get('candidate_parties', [])[:3]}")
    print(f"  Candidate heads from vector search:    {r.get('candidate_heads', [])[:3]}")

print("\n" + "=" * 80)
print("STEP 6 — HOW THE RAG ENGINE WORKED")
print("=" * 80)
print(f"""
  Phase 1 (deterministic match):
    For each bank line, scanned tally_vouchers for amount-exact +
    date-close (±7d if ref/desc hit, ±3d unconditional) + reference or
    ledger-name overlap match. Matched lines never need AI.

  Phase 2 (vector retrieval — RAG):
    For each unmatched line, embedded the narration with Gemini's
    embedding-2 model (3072-dim vector), then ran cosine-distance search
    across our knowledge_base table on three slices:
      - tally_master_party     (74 embeddings)  → top-5 candidate parties
      - tally_master_ledger    (147 embeddings) → top-12 ledgers, then
        filtered to revenue (for credits) or expense (for debits) using
        parent-group heuristic → top-8 candidate heads
      - tally_master_narration (1000 embeddings) → top-3 similar past
        transactions for additional context

  Phase 3 (Gemini reasoning — batched):
    Skipped Gemini entirely for high-confidence (>70%) vector matches.
    For the remaining lines, batched 25 at a time into ONE Gemini call.
    Prompt includes per-line candidates + full party/revenue/expense
    fallback lists from tally_ledgers. Gemini returns a JSON array with
    chosen party + head + confidence + rationale for each line.

  Phase 4 (output normalization):
    Bank ledger defaulted to first 'Bank Accounts' ledger.
    Voucher type auto-decided: Receipt if amount>0, Payment if <0.
    Status set to 'auto_filled' if confidence >= 0.6, else 'unmatched'.

  Learning loop (on confirm):
    Once you click 'Post All Vouchers' in the UI, each row's
    {{narration → party + head}} mapping is embedded again and stored in
    knowledge_base with type='bank_reconciliation'. Next time a similar
    bank narration appears, semantic_search_tally will pick it up
    immediately and confidence will be near 100%.
""")
