import sys
import os
import uuid
import json

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from utils.reconciler import reconcile_statement, get_reconciliation_embedding

def run_tests():
    print("==================================================")
    print("🧪 STARTING RECONCILIATION VALIDATION TESTS 🧪")
    print("==================================================")

    # 1. Check tally_vouchers table creation and seed data
    print("\n[TEST 1] Checking Tally Vouchers Database table...")
    conn = db.get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM tally_vouchers WHERE company_name = 'Acme Corp'")
    acme_count = cursor.fetchone()[0]
    print(f"✓ Acme Corp has {acme_count} seeded tally vouchers.")
    assert acme_count >= 3, "Seed data failed to initialize!"
    
    # 2. Test Stage 1: Deterministic Match
    print("\n[TEST 2] Testing Stage 1: Deterministic Matching...")
    mock_transactions = [
        {
            "date": "2026-05-01",
            "description": "CHQ DEP LUXEDECO VENTURES",
            "reference": "CHQ12345",
            "amount": 8320.0
        }
    ]
    results = reconcile_statement(mock_transactions, "Acme Corp")
    print(f"✓ Reconciled result: {json.dumps(results, indent=2)}")
    
    assert len(results) == 1, "Failed to reconcile mock transaction."
    assert results[0]["status"] == "auto_matched", "Deterministic match failed to trigger!"
    assert results[0]["suggested_ledger"] == "LUXEDECO VENTURES PRIVATE LIMITED", "Mapped incorrect ledger name!"
    print("✓ Deterministic Stage matched PERFECTLY on date, reference, and amount!")

    # 3. Test Stage 2: RAG / Semantic match fallback
    print("\n[TEST 3] Testing Stage 2: Semantic Past-Learnings RAG Matcher...")
    
    # Let's seed a past reconciliation learning correction in the database first
    test_narration = "HDFC INTR CHG MONTHLY"
    target_ledger = "Bank Charges A/c"
    
    print(f"Training RAG model: Mapping '{test_narration}' to '{target_ledger}'...")
    emb = get_reconciliation_embedding(f"reconcile ledger mapping for bank narration {test_narration}")
    db.save_correction(
        field="ledger_mapping",
        original=test_narration,
        corrected=target_ledger,
        party_name=test_narration,
        embedding=emb
    )
    
    # Try semantic match with similar narration
    query_txn = [
        {
            "date": "2026-05-15",
            "description": "HDFC MONTHLY BANK CHARGES DEBIT",
            "reference": "TXN8899",
            "amount": -250.0
        }
    ]
    
    semantic_results = reconcile_statement(query_txn, "Acme Corp")
    print(f"✓ Semantic RAG result: {json.dumps(semantic_results, indent=2)}")
    
    assert len(semantic_results) == 1, "Failed to parse semantic txn."
    assert semantic_results[0]["status"] == "auto_filled", "RAG semantic auto-fill failed to trigger!"
    assert semantic_results[0]["suggested_ledger"] == "Bank Charges A/c", "Semantic mapping failed to predict correct ledger!"
    
    print("\n==================================================")
    print("🎉 ALL RECONCILIATION INTEGRITY TESTS PASSED! 🎉")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
