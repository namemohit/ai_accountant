import os
from dotenv import load_dotenv
load_dotenv()
import db
import json
from datetime import datetime
from google import generativeai as genai

# Setup key
if os.getenv("GEMINI_API_KEY"):
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def get_reconciliation_embedding(text):
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-2",
            content=text,
            task_type="retrieval_document"
        )
        return result['embedding']
    except Exception as e:
        print(f"Error generating reconciler embedding: {e}")
        return None

def reconcile_statement(transactions, company_name):
    """
    reconcile_statement takes a list of bank transactions:
    [
      {"date": "2026-05-01", "description": "LUXEDECO VENTURES DEP", "reference": "CHQ12345", "amount": 8320.0},
      ...
    ]
    and reconciles them against tally_vouchers and past learnings.
    """
    tally_vouchers = db.get_unreconciled_tally_vouchers(company_name)
    reconciled_list = []

    for tx in transactions:
        tx_date = None
        if tx.get("date"):
            try:
                tx_date = datetime.strptime(tx.get("date"), "%Y-%m-%d").date()
            except:
                try:
                    tx_date = datetime.strptime(tx.get("date"), "%d/%m/%Y").date()
                except:
                    pass
                    
        tx_amount = float(tx.get("amount", 0))
        tx_ref = str(tx.get("reference", "")).strip().lower()
        tx_desc = str(tx.get("description", "")).strip().lower()

        match_found = False
        best_match = None

        # Phase 1: Try to match deterministic tally_vouchers
        for v in tally_vouchers:
            if v.get("reconciled"):
                continue
            
            # Check absolute amount matching
            v_amount = float(v.get("amount", 0))
            if abs(v_amount - abs(tx_amount)) > 0.01:
                continue
            
            # Match factors:
            # 1. Date closeness
            v_date = None
            if v.get("date"):
                if isinstance(v.get("date"), str):
                    try:
                        v_date = datetime.strptime(v.get("date"), "%Y-%m-%d").date()
                    except:
                        pass
                elif hasattr(v.get("date"), "date"):
                    v_date = v.get("date").date()
                else:
                    v_date = v.get("date")
                    
            date_diff = abs((tx_date - v_date).days) if tx_date and v_date else 999
            
            # 2. Ref no matching
            v_inst = str(v.get("instrument_number", "")).strip().lower()
            ref_match = (tx_ref and v_inst and (tx_ref in v_inst or v_inst in tx_ref))
            
            # 3. Description matching
            v_ledger = str(v.get("ledger_name", "")).strip().lower()
            desc_match = (v_ledger in tx_desc or tx_desc in v_ledger)

            # Match criteria
            if (date_diff <= 7 and (ref_match or desc_match)) or (date_diff <= 3):
                best_match = v
                match_found = True
                break

        if match_found and best_match:
            tally_vouchers.remove(best_match)
            reconciled_list.append({
                "bank_transaction": tx,
                "status": "auto_matched",
                "suggested_ledger": best_match.get("ledger_name"),
                "tally_voucher_id": best_match.get("id"),
                "voucher_number": best_match.get("voucher_number"),
                "score": 1.0
            })
            continue

        # Phase 2: RAG / Semantic Matching on Past Corrections
        query_text = f"reconcile ledger mapping for bank narration {tx.get('description')} reference {tx.get('reference')}"
        emb = get_reconciliation_embedding(query_text)
        
        suggested_ledger = "Suspense A/c"
        status = "unmatched"
        
        if emb:
            relevant = db.get_relevant_corrections(emb, limit=3)
            if relevant:
                ledger_votes = {}
                for r in relevant:
                    r_dict = r if isinstance(r, dict) else json.loads(r)
                    l_name = r_dict.get("corrected") or r_dict.get("party_name")
                    if l_name:
                        ledger_votes[l_name] = ledger_votes.get(l_name, 0) + 1
                
                if ledger_votes:
                    suggested_ledger = max(ledger_votes, key=ledger_votes.get)
                    status = "auto_filled"

        reconciled_list.append({
            "bank_transaction": tx,
            "status": status,
            "suggested_ledger": suggested_ledger,
            "tally_voucher_id": None,
            "voucher_number": None,
            "score": 0.5 if status == "auto_filled" else 0.0
        })

    return reconciled_list
