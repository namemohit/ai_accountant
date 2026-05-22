"""
Seed "Sample Co" — Universal demo company for YantrAI Accounting Agent.
Every user gets this company so they can explore all features with realistic data.

Run: python seed_sample_co.py
"""

import db
import uuid
import json
from datetime import datetime

COMPANY = "Sample Co"

def uid():
    return str(uuid.uuid4())

def seed():
    conn = db.get_conn()
    cursor = conn.cursor()

    print(f"🏗️  Seeding '{COMPANY}' demo data...")

    # ────────────────────────────────────────────────
    # 1. PARTIES (Party Master) — 10 parties
    # ────────────────────────────────────────────────
    parties = [
        (uid(), COMPANY, "Sunrise Electronics Pvt Ltd", "27AABCS1234M1Z5", "42 MG Road, Andheri West, Mumbai 400053", "HDFC Bank", "50100012345678", "HDFC0001234", "AABCS1234M", "sales@sunrise.co.in", "+919876543210"),
        (uid(), COMPANY, "Greenfield Organics", "29AABCG5678N1Z8", "15 Koramangala, Bangalore 560034", "ICICI Bank", "123405000678", "ICIC0005678", "AABCG5678N", "info@greenfield.in", "+919123456789"),
        (uid(), COMPANY, "Metro Office Supplies", "07AABCM9012P1Z2", "B-12 Nehru Place, New Delhi 110019", "", "", "", "AABCM9012P", "orders@metrooffice.com", "+911234567890"),
        (uid(), COMPANY, "Priya Textiles & Garments", "33AABCP3456Q1Z6", "78 T Nagar, Chennai 600017", "SBI", "30012345678", "SBIN0001234", "AABCP3456Q", "priya@priyatextiles.in", "+914412345678"),
        (uid(), COMPANY, "Sharma Transport Services", "09AABCS7890R1Z4", "22 Civil Lines, Jaipur 302006", "Axis Bank", "91701234567890", "UTIB0001234", "AABCS7890R", "dispatch@sharmatransport.in", "+919414123456"),
        (uid(), COMPANY, "CloudNine Software Labs", "29AABCC2345S1Z1", "201 HSR Layout, Bangalore 560102", "Kotak Mahindra Bank", "1234000056789", "KKBK0001234", "AABCC2345S", "billing@cloudnine.io", "+919845012345"),
        (uid(), COMPANY, "Raj Hardware & Tools", "24AABCR6789T1Z3", "45 MG Marg, Kolkata 700073", "Bank of Baroda", "12340100056789", "BARB0KOLKAT", "AABCR6789T", "raj@rajhardware.com", "+913324567890"),
        (uid(), COMPANY, "National Packers & Movers", "27AABCN0123U1Z7", "12 Sector 18, Navi Mumbai 400706", "", "", "", "AABCN0123U", "contact@nationalpnm.com", "+912227654321"),
        (uid(), COMPANY, "HDFC Bank - Current A/c", "", "", "HDFC Bank", "50100098765432", "HDFC0001234", "", "", ""),
        (uid(), COMPANY, "SBI - Savings A/c", "", "", "SBI", "30098765432100", "SBIN0005678", "", "", ""),
    ]

    for p in parties:
        try:
            cursor.execute("""
            INSERT INTO parties (id, company_name, name, gstin, address, bank_name, account_number, ifsc_code, pan, email, phone)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (company_name, name) DO UPDATE SET
                gstin=EXCLUDED.gstin, address=EXCLUDED.address, bank_name=EXCLUDED.bank_name,
                account_number=EXCLUDED.account_number, ifsc_code=EXCLUDED.ifsc_code, pan=EXCLUDED.pan,
                email=EXCLUDED.email, phone=EXCLUDED.phone
            """, p)
        except Exception as e:
            print(f"  Party error ({p[2]}): {e}")

    print(f"  ✅ {len(parties)} parties seeded")

    # ────────────────────────────────────────────────
    # 2. INVOICES / VOUCHERS — 20 entries
    # ────────────────────────────────────────────────
    # Clear existing Sample Co invoices first
    cursor.execute("DELETE FROM items WHERE invoice_id IN (SELECT id FROM invoices WHERE company_name = %s)", (COMPANY,))
    cursor.execute("DELETE FROM invoices WHERE company_name = %s", (COMPANY,))

    invoices = [
        # Sales Invoices (5)
        {"id": uid(), "inv": "INV-2026-001", "date": "2026-04-02", "party": "Greenfield Organics", "amount": 47200.00, "discount": 0, "gst": 7200.00, "cat": "Sales", "status": "synced", "type": "Sales",
         "billing": "Sample Co", "billing_gstin": "27AABCS0001M1Z1", "billed_gstin": "29AABCG5678N1Z8"},
        {"id": uid(), "inv": "INV-2026-002", "date": "2026-04-08", "party": "Metro Office Supplies", "amount": 18500.00, "discount": 500, "gst": 2745.00, "cat": "Sales", "status": "synced", "type": "Sales",
         "billing": "Sample Co", "billing_gstin": "27AABCS0001M1Z1", "billed_gstin": "07AABCM9012P1Z2"},
        {"id": uid(), "inv": "INV-2026-003", "date": "2026-04-15", "party": "Raj Hardware & Tools", "amount": 65300.00, "discount": 1300, "gst": 9756.00, "cat": "Sales", "status": "pending", "type": "Sales",
         "billing": "Sample Co", "billing_gstin": "27AABCS0001M1Z1", "billed_gstin": "24AABCR6789T1Z3"},
        {"id": uid(), "inv": "INV-2026-004", "date": "2026-05-01", "party": "CloudNine Software Labs", "amount": 125000.00, "discount": 5000, "gst": 18000.00, "cat": "Sales", "status": "synced", "type": "Sales",
         "billing": "Sample Co", "billing_gstin": "27AABCS0001M1Z1", "billed_gstin": "29AABCC2345S1Z1"},
        {"id": uid(), "inv": "INV-2026-005", "date": "2026-05-10", "party": "National Packers & Movers", "amount": 8400.00, "discount": 0, "gst": 1281.36, "cat": "Sales", "status": "pending", "type": "Sales",
         "billing": "Sample Co", "billing_gstin": "27AABCS0001M1Z1", "billed_gstin": "27AABCN0123U1Z7"},

        # Purchase Invoices (5)
        {"id": uid(), "inv": "PUR-2026-001", "date": "2026-04-05", "party": "Sunrise Electronics Pvt Ltd", "amount": 92400.00, "discount": 2400, "gst": 13716.00, "cat": "Purchase", "status": "synced", "type": "Purchase",
         "billing": "Sunrise Electronics Pvt Ltd", "billing_gstin": "27AABCS1234M1Z5", "billed_gstin": "27AABCS0001M1Z1"},
        {"id": uid(), "inv": "PUR-2026-002", "date": "2026-04-12", "party": "Priya Textiles & Garments", "amount": 34500.00, "discount": 0, "gst": 5261.02, "cat": "Purchase", "status": "synced", "type": "Purchase",
         "billing": "Priya Textiles & Garments", "billing_gstin": "33AABCP3456Q1Z6", "billed_gstin": "27AABCS0001M1Z1"},
        {"id": uid(), "inv": "PUR-2026-003", "date": "2026-04-22", "party": "Metro Office Supplies", "amount": 6750.00, "discount": 250, "gst": 990.00, "cat": "Purchase", "status": "pending", "type": "Purchase",
         "billing": "Metro Office Supplies", "billing_gstin": "07AABCM9012P1Z2", "billed_gstin": "27AABCS0001M1Z1"},
        {"id": uid(), "inv": "PUR-2026-004", "date": "2026-05-03", "party": "Sharma Transport Services", "amount": 15800.00, "discount": 0, "gst": 2410.17, "cat": "Purchase", "status": "synced", "type": "Purchase",
         "billing": "Sharma Transport Services", "billing_gstin": "09AABCS7890R1Z4", "billed_gstin": "27AABCS0001M1Z1"},
        {"id": uid(), "inv": "PUR-2026-005", "date": "2026-05-15", "party": "Sunrise Electronics Pvt Ltd", "amount": 41200.00, "discount": 1200, "gst": 6096.00, "cat": "Purchase", "status": "pending", "type": "Purchase",
         "billing": "Sunrise Electronics Pvt Ltd", "billing_gstin": "27AABCS1234M1Z5", "billed_gstin": "27AABCS0001M1Z1"},

        # Payments (3)
        {"id": uid(), "inv": "PAY-2026-001", "date": "2026-04-10", "party": "Sunrise Electronics Pvt Ltd", "amount": 92400.00, "discount": 0, "gst": 0, "cat": "Payment", "status": "synced", "type": "Payment",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},
        {"id": uid(), "inv": "PAY-2026-002", "date": "2026-04-25", "party": "Priya Textiles & Garments", "amount": 34500.00, "discount": 0, "gst": 0, "cat": "Payment", "status": "synced", "type": "Payment",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},
        {"id": uid(), "inv": "PAY-2026-003", "date": "2026-05-08", "party": "Sharma Transport Services", "amount": 15800.00, "discount": 0, "gst": 0, "cat": "Payment", "status": "pending", "type": "Payment",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},

        # Receipts (3)
        {"id": uid(), "inv": "RCT-2026-001", "date": "2026-04-12", "party": "Greenfield Organics", "amount": 47200.00, "discount": 0, "gst": 0, "cat": "Receipt", "status": "synced", "type": "Receipt",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},
        {"id": uid(), "inv": "RCT-2026-002", "date": "2026-04-20", "party": "Metro Office Supplies", "amount": 18500.00, "discount": 0, "gst": 0, "cat": "Receipt", "status": "synced", "type": "Receipt",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},
        {"id": uid(), "inv": "RCT-2026-003", "date": "2026-05-12", "party": "CloudNine Software Labs", "amount": 125000.00, "discount": 0, "gst": 0, "cat": "Receipt", "status": "pending", "type": "Receipt",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},

        # Journal Entry (2)
        {"id": uid(), "inv": "JRN-2026-001", "date": "2026-04-30", "party": "Depreciation - Office Equipment", "amount": 4500.00, "discount": 0, "gst": 0, "cat": "Journal", "status": "synced", "type": "Journal",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},
        {"id": uid(), "inv": "JRN-2026-002", "date": "2026-05-15", "party": "Salary Provision - May 2026", "amount": 180000.00, "discount": 0, "gst": 0, "cat": "Journal", "status": "synced", "type": "Journal",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},

        # Contra (1) — Bank to Cash Transfer
        {"id": uid(), "inv": "CTR-2026-001", "date": "2026-05-05", "party": "HDFC Bank - Current A/c", "amount": 50000.00, "discount": 0, "gst": 0, "cat": "Contra", "status": "synced", "type": "Contra",
         "billing": "Sample Co", "billing_gstin": "", "billed_gstin": ""},

        # Duplicate Invoice (for duplicate manager testing)
        {"id": uid(), "inv": "INV-2026-001", "date": "2026-04-02", "party": "Greenfield Organics", "amount": 47200.00, "discount": 0, "gst": 7200.00, "cat": "Sales", "status": "pending", "type": "Sales",
         "billing": "Sample Co", "billing_gstin": "27AABCS0001M1Z1", "billed_gstin": "29AABCG5678N1Z8"},
    ]

    for inv in invoices:
        inv_id = inv["id"]
        cursor.execute("""
        INSERT INTO invoices (id, invoice_number, date, party_name, total_amount, discount_amount, gst_amount,
            category, status, company_name, billing_party_name, billing_party_gstin, billed_to_party_gstin)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (inv_id, inv["inv"], inv["date"], inv["party"], inv["amount"], inv["discount"], inv["gst"],
              inv["cat"], inv["status"], COMPANY, inv["billing"], inv["billing_gstin"], inv["billed_gstin"]))

    print(f"  ✅ {len(invoices)} vouchers/invoices seeded")

    # ────────────────────────────────────────────────
    # 3. LINE ITEMS for Sales/Purchase invoices
    # ────────────────────────────────────────────────
    # Add items to first 4 invoices (Sales & Purchase)
    items_data = [
        # INV-2026-001 items (Sales to Greenfield)
        (invoices[0]["id"], "Organic LED Display Panel 55\"", 2, 15000.00, 30000.00, 9.0, 9.0, 0, "8528"),
        (invoices[0]["id"], "Wall Mount Bracket", 2, 1200.00, 2400.00, 9.0, 9.0, 0, "7616"),
        (invoices[0]["id"], "HDMI Cable 2m", 4, 350.00, 1400.00, 9.0, 9.0, 0, "8544"),

        # INV-2026-002 items (Sales to Metro Office)
        (invoices[1]["id"], "HP LaserJet Toner Cartridge", 5, 2800.00, 14000.00, 9.0, 9.0, 500, "8443"),
        (invoices[1]["id"], "A4 Copier Paper (500 sheets)", 10, 280.00, 2800.00, 6.0, 6.0, 0, "4802"),

        # PUR-2026-001 items (Purchase from Sunrise)
        (invoices[5]["id"], "Arduino Mega Board", 20, 2200.00, 44000.00, 9.0, 9.0, 0, "8542"),
        (invoices[5]["id"], "Raspberry Pi 5 - 8GB", 15, 3000.00, 45000.00, 9.0, 9.0, 2400, "8471"),
        (invoices[5]["id"], "Freight Charges", 1, 3400.00, 3400.00, 9.0, 9.0, 0, "9965"),

        # PUR-2026-002 items (Purchase from Priya Textiles)
        (invoices[6]["id"], "Cotton Fabric Roll 100m", 5, 4500.00, 22500.00, 6.0, 6.0, 0, "5208"),
        (invoices[6]["id"], "Polyester Blend 50m", 4, 3000.00, 12000.00, 6.0, 6.0, 0, "5407"),
    ]

    for it in items_data:
        cursor.execute("""
        INSERT INTO items (id, invoice_id, description, quantity, rate, amount, cgst_rate, sgst_rate, discount, hsn_sac)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (uid(), it[0], it[1], it[2], it[3], it[4], it[5], it[6], it[7], it[8]))

    print(f"  ✅ {len(items_data)} line items seeded")

    # ────────────────────────────────────────────────
    # 4. TALLY VOUCHERS (for Bank Register & Reconciliation)
    # ────────────────────────────────────────────────
    cursor.execute("DELETE FROM tally_vouchers WHERE company_name = %s", (COMPANY,))

    tally_vouchers = [
        (uid(), "2026-04-02", "TV-001", "Greenfield Organics", 47200.00, "Sales", "", COMPANY, False),
        (uid(), "2026-04-05", "TV-002", "Sunrise Electronics Pvt Ltd", 92400.00, "Purchase", "", COMPANY, False),
        (uid(), "2026-04-08", "TV-003", "Metro Office Supplies", 18500.00, "Sales", "", COMPANY, False),
        (uid(), "2026-04-10", "TV-004", "Sunrise Electronics Pvt Ltd", 92400.00, "Payment", "CHQ224455", COMPANY, True),
        (uid(), "2026-04-12", "TV-005", "Greenfield Organics", 47200.00, "Receipt", "NEFT778899", COMPANY, True),
        (uid(), "2026-04-12", "TV-006", "Priya Textiles & Garments", 34500.00, "Purchase", "", COMPANY, False),
        (uid(), "2026-04-15", "TV-007", "Raj Hardware & Tools", 65300.00, "Sales", "", COMPANY, False),
        (uid(), "2026-04-20", "TV-008", "Metro Office Supplies", 18500.00, "Receipt", "UPI112233", COMPANY, True),
        (uid(), "2026-04-22", "TV-009", "Metro Office Supplies", 6750.00, "Purchase", "", COMPANY, False),
        (uid(), "2026-04-25", "TV-010", "Priya Textiles & Garments", 34500.00, "Payment", "CHQ334455", COMPANY, True),
        (uid(), "2026-04-30", "TV-011", "Depreciation - Office Equipment", 4500.00, "Journal", "", COMPANY, False),
        (uid(), "2026-05-01", "TV-012", "CloudNine Software Labs", 125000.00, "Sales", "", COMPANY, False),
        (uid(), "2026-05-03", "TV-013", "Sharma Transport Services", 15800.00, "Purchase", "", COMPANY, False),
        (uid(), "2026-05-05", "TV-014", "HDFC Bank - Current A/c", 50000.00, "Contra", "FT556677", COMPANY, True),
        (uid(), "2026-05-08", "TV-015", "Sharma Transport Services", 15800.00, "Payment", "NEFT889900", COMPANY, False),
        (uid(), "2026-05-10", "TV-016", "National Packers & Movers", 8400.00, "Sales", "", COMPANY, False),
        (uid(), "2026-05-12", "TV-017", "CloudNine Software Labs", 125000.00, "Receipt", "NEFT990011", COMPANY, False),
        (uid(), "2026-05-15", "TV-018", "Salary Provision - May 2026", 180000.00, "Journal", "", COMPANY, False),
        (uid(), "2026-05-15", "TV-019", "Sunrise Electronics Pvt Ltd", 41200.00, "Purchase", "", COMPANY, False),
    ]

    for tv in tally_vouchers:
        cursor.execute("""
        INSERT INTO tally_vouchers (id, date, voucher_number, ledger_name, amount, voucher_type, instrument_number, company_name, reconciled)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, tv)

    print(f"  ✅ {len(tally_vouchers)} tally vouchers seeded")

    # ────────────────────────────────────────────────
    # 5. LEDGER MAPPINGS (Knowledge Base for Chart of Accounts)
    # ────────────────────────────────────────────────
    cursor.execute("DELETE FROM knowledge_base WHERE type = 'ledger' AND data->>'company_name' = %s", (COMPANY,))

    ledger_mappings = [
        {"ledger": "Sales Account", "group": "Sales Accounts", "company_name": COMPANY},
        {"ledger": "Purchase Account", "group": "Purchase Accounts", "company_name": COMPANY},
        {"ledger": "CGST Input", "group": "Duties & Taxes", "company_name": COMPANY},
        {"ledger": "SGST Input", "group": "Duties & Taxes", "company_name": COMPANY},
        {"ledger": "CGST Output", "group": "Duties & Taxes", "company_name": COMPANY},
        {"ledger": "SGST Output", "group": "Duties & Taxes", "company_name": COMPANY},
        {"ledger": "IGST Input", "group": "Duties & Taxes", "company_name": COMPANY},
        {"ledger": "IGST Output", "group": "Duties & Taxes", "company_name": COMPANY},
        {"ledger": "HDFC Bank - Current A/c", "group": "Bank Accounts", "company_name": COMPANY},
        {"ledger": "SBI - Savings A/c", "group": "Bank Accounts", "company_name": COMPANY},
        {"ledger": "Cash in Hand", "group": "Cash-in-Hand", "company_name": COMPANY},
        {"ledger": "Greenfield Organics", "group": "Sundry Debtors", "company_name": COMPANY},
        {"ledger": "Metro Office Supplies", "group": "Sundry Debtors", "company_name": COMPANY},
        {"ledger": "Raj Hardware & Tools", "group": "Sundry Debtors", "company_name": COMPANY},
        {"ledger": "CloudNine Software Labs", "group": "Sundry Debtors", "company_name": COMPANY},
        {"ledger": "National Packers & Movers", "group": "Sundry Debtors", "company_name": COMPANY},
        {"ledger": "Sunrise Electronics Pvt Ltd", "group": "Sundry Creditors", "company_name": COMPANY},
        {"ledger": "Priya Textiles & Garments", "group": "Sundry Creditors", "company_name": COMPANY},
        {"ledger": "Sharma Transport Services", "group": "Sundry Creditors", "company_name": COMPANY},
        {"ledger": "Salary Expense", "group": "Indirect Expenses", "company_name": COMPANY},
        {"ledger": "Rent Expense", "group": "Indirect Expenses", "company_name": COMPANY},
        {"ledger": "Office Equipment", "group": "Fixed Assets", "company_name": COMPANY},
        {"ledger": "Depreciation", "group": "Indirect Expenses", "company_name": COMPANY},
        {"ledger": "Freight Charges", "group": "Direct Expenses", "company_name": COMPANY},
    ]

    for lm in ledger_mappings:
        cursor.execute("""
        INSERT INTO knowledge_base (type, data) VALUES (%s, %s)
        """, ("ledger", json.dumps(lm)))

    print(f"  ✅ {len(ledger_mappings)} ledger mappings seeded")

    # ────────────────────────────────────────────────
    # 6. CLEAN OLD DEMO DATA (tasks + chats, respecting FK order)
    # ────────────────────────────────────────────────
    cursor.execute("DELETE FROM tasks WHERE company_name = %s", (COMPANY,))
    cursor.execute("DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE company_name = %s)", (COMPANY,))
    cursor.execute("DELETE FROM chat_sessions WHERE company_name = %s", (COMPANY,))

    # ────────────────────────────────────────────────
    # 7. CHAT SESSION with sample conversation
    # ────────────────────────────────────────────────

    demo_session_id = uid()
    cursor.execute("""
    INSERT INTO chat_sessions (id, title, company_name) VALUES (%s, %s, %s)
    """, (demo_session_id, "Sample Invoice Upload", COMPANY))

    chat_msgs = [
        (uid(), demo_session_id, "user", "I've uploaded an invoice from Sunrise Electronics for some Arduino boards", "text", None),
        (uid(), demo_session_id, "assistant",
         "I've analyzed the invoice from **Sunrise Electronics Pvt Ltd**. Here's what I found:\n\n"
         "- **Invoice #**: PUR-2026-001\n- **Date**: 05 Apr 2026\n- **Party**: Sunrise Electronics Pvt Ltd\n"
         "- **Total Amount**: ₹92,400.00\n- **GST**: ₹13,716.00 (CGST 9% + SGST 9%)\n\n"
         "This is a **Purchase** invoice with 3 line items. You can review and push to Tally below.",
         "text", None),
        (uid(), demo_session_id, "user", "Show me my total sales this month", "text", None),
        (uid(), demo_session_id, "assistant",
         "Here's your **sales summary for May 2026** (Sample Co):\n\n"
         "| Metric | Value |\n|--------|-------|\n"
         "| Total Sales | ₹1,33,400 |\n| Invoices Raised | 2 |\n"
         "| GST Collected | ₹19,281.36 |\n| Pending Sync | 1 invoice |\n\n"
         "Your biggest sale this month was to **CloudNine Software Labs** for ₹1,25,000.",
         "text", None),
    ]

    for cm in chat_msgs:
        cursor.execute("""
        INSERT INTO chat_messages (id, session_id, role, content, ui_type, ui_data)
        VALUES (%s,%s,%s,%s,%s,%s)
        """, cm)

    print(f"  ✅ Demo chat session seeded with {len(chat_msgs)} messages")

    # ────────────────────────────────────────────────
    # 8. TASKS (Service Requests at different lifecycle stages)
    # ────────────────────────────────────────────────
    tasks = [
        (uid(), demo_session_id, COMPANY, "sadmin",
         "[Automation] [Normal]\n\nSet up automated GST return filing\n\nConfigure automated GSTR-1 and GSTR-3B filing for Sample Co. Pull data from Tally and file returns monthly.\n\n---\nOriginal user message: Can you automate our GST filing every month?",
         "Completed"),
        (uid(), demo_session_id, COMPANY, "sadmin",
         "[Integration] [Normal]\n\nConnect bank feed to HDFC Current Account\n\nSet up automated bank statement import from HDFC Bank current account for daily transaction reconciliation.\n\n---\nOriginal user message: Please connect our HDFC bank account for auto-reconciliation",
         "In Progress"),
        (uid(), demo_session_id, COMPANY, "sadmin",
         "[Custom Report] [Urgent]\n\nBuild monthly MIS dashboard\n\nCreate a custom MIS report showing Sales vs Targets, Outstanding receivables ageing, and Cash flow forecast for the next quarter.\n\n---\nOriginal user message: I need a monthly MIS dashboard with receivables ageing and cash flow",
         "Requested"),
    ]

    for t in tasks:
        cursor.execute("""
        INSERT INTO tasks (id, session_id, company_name, assigned_to, description, status)
        VALUES (%s,%s,%s,%s,%s,%s)
        """, t)

    print(f"  ✅ {len(tasks)} sample tasks seeded")

    # ────────────────────────────────────────────────
    # 9. (DISABLED) Mass-add "Sample Co" to ALL users
    # ────────────────────────────────────────────────
    # PRE-SPRINT-7 BEHAVIOR (REMOVED):
    # We used to iterate every accounting_users row and insert "Sample Co" into
    # their companies array. This made Sample Co a SHARED NAMESPACE — any user
    # would see every other user's seeded vouchers / chats / bank rows. That's
    # a multi-tenant leak.
    #
    # NOW: each user gets their OWN per-user demo via /api/login, named
    # "Sample Co — <username>". This seed script seeds the shared "Sample Co"
    # company data itself (kept for super_admin / platform demos) but does NOT
    # attach it to user accounts.
    print(f"  ⏭️  Skipped mass-attach (per-user demo handled by /api/login now)")
    updated_count = 0

    # ────────────────────────────────────────────────
    conn.commit()
    cursor.close()
    conn.close()

    print(f"\n🎉 '{COMPANY}' seeding complete! All users now have access.")
    print(f"   Switch to '{COMPANY}' in the company dropdown to explore.")


if __name__ == "__main__":
    seed()
