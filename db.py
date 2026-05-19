import psycopg2
from psycopg2.extras import RealDictCursor
import uuid
from datetime import datetime
import os
import json

# Supabase Connection String (Pooler - IPv4 Compatible)
DB_URL = os.getenv("DB_URL", "postgresql://postgres.vxnflumpectzqdamjqsc:yantr_ai_labs@aws-1-ap-south-1.pooler.supabase.com:5432/postgres")

def get_conn():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_conn()
    cursor = conn.cursor()
    
    # Enable Vector Extension
    try:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    except Exception as ve:
        print(f"Warning: Could not enable vector extension: {ve}")
        
    # Invoices Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS invoices (
        id UUID PRIMARY KEY,
        invoice_number TEXT,
        date TEXT,
        party_name TEXT,
        total_amount REAL,
        discount_amount REAL,
        gst_amount REAL,
        category TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    try:
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS discount_amount REAL")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS company_name TEXT")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS file_url TEXT")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS billing_party_name TEXT")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS billing_party_gstin TEXT")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS billed_to_party_gstin TEXT")
    except:
        pass
    
    # Items Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS items (
        id UUID PRIMARY KEY,
        invoice_id UUID REFERENCES invoices(id),
        description TEXT,
        quantity REAL,
        rate REAL,
        amount REAL
    )
    """)
    
    try:
        cursor.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS cgst_rate REAL")
        cursor.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS sgst_rate REAL")
        cursor.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS discount REAL")
        cursor.execute("ALTER TABLE items ADD COLUMN IF NOT EXISTS hsn_sac TEXT")
    except:
        pass

    # Parties (Party Master) Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS parties (
        id UUID PRIMARY KEY,
        company_name TEXT,
        name TEXT,
        gstin TEXT,
        address TEXT,
        bank_name TEXT,
        account_number TEXT,
        ifsc_code TEXT,
        pan TEXT,
        email TEXT,
        phone TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(company_name, name)
    )
    """)
    
    # Knowledge Base Table (For learning)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS knowledge_base (
        id SERIAL PRIMARY KEY,
        type TEXT, -- 'ledger' or 'correction'
        data JSONB,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    try:
        cursor.execute("ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS embedding vector(3072);")
    except Exception as ae:
        print(f"Warning: Could not add embedding column: {ae}")
    
    # Chat Sessions Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id UUID PRIMARY KEY,
        title TEXT DEFAULT 'New Chat',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Chat Messages Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_messages (
        id UUID PRIMARY KEY,
        session_id UUID REFERENCES chat_sessions(id),
        role TEXT, -- 'user' or 'assistant'
        content TEXT,
        ui_type TEXT DEFAULT 'text', -- 'text', 'table', 'card', 'chart', 'list'
        ui_data JSONB,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Users Table (Custom Authentication)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accounting_users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'admin',
        name TEXT,
        email TEXT,
        phone TEXT,
        company_name TEXT,
        companies JSONB,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """ )

    # Tally Vouchers Table (Simulating Tally entries for Reconciliation)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tally_vouchers (
        id UUID PRIMARY KEY,
        date DATE,
        voucher_number TEXT,
        ledger_name TEXT,
        amount REAL,
        voucher_type TEXT,
        instrument_number TEXT,
        company_name TEXT,
        reconciled BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Deep-ingest fields for tally_vouchers — each ALTER in its own savepoint
    # so a single failure (e.g. unique index conflict) doesn't roll back the rest.
    deep_alters = [
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS narration TEXT",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS ledger_entries JSONB",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS reference_no TEXT",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS place_of_supply TEXT",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS party_gstin TEXT",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'INR'",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS cost_centres JSONB",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS bill_refs JSONB",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS taxable_value REAL",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS cgst_amount REAL",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS sgst_amount REAL",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS igst_amount REAL",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS tally_master_id TEXT",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS raw_xml TEXT",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "CREATE INDEX IF NOT EXISTS idx_tally_voucher_date ON tally_vouchers(company_name, date)",
        "CREATE INDEX IF NOT EXISTS idx_tally_voucher_type ON tally_vouchers(company_name, voucher_type)",
        "CREATE INDEX IF NOT EXISTS idx_tally_voucher_master_id ON tally_vouchers(company_name, tally_master_id) WHERE tally_master_id IS NOT NULL",
    ]
    for stmt in deep_alters:
        try:
            cursor.execute("SAVEPOINT sp")
            cursor.execute(stmt)
            cursor.execute("RELEASE SAVEPOINT sp")
        except Exception as e:
            cursor.execute("ROLLBACK TO SAVEPOINT sp")
            print(f"Migration warning ({stmt[:60]}…): {e}")

    # ── Full Tally master tables ──────────────────────────────────
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tally_ledgers (
        id UUID PRIMARY KEY,
        company_name TEXT NOT NULL,
        tally_master_id TEXT,           -- Tally's GUID
        name TEXT NOT NULL,
        parent_group TEXT,              -- immediate parent group
        group_path TEXT,                -- full hierarchy: "Liabilities > Duties & Taxes > GST"
        opening_balance REAL,
        closing_balance REAL,
        is_revenue BOOLEAN,
        is_deemedpositive BOOLEAN,
        gstin TEXT,
        pan TEXT,
        address TEXT,
        bank_name TEXT,
        account_number TEXT,
        ifsc_code TEXT,
        gst_registration_type TEXT,
        tds_applicable BOOLEAN,
        ledger_type TEXT,               -- bank, party, expense, income, etc.
        place_of_supply TEXT,
        raw_data JSONB,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (company_name, name)
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tally_ledgers_company ON tally_ledgers(company_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tally_ledgers_group ON tally_ledgers(company_name, parent_group)")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tally_stock_items (
        id UUID PRIMARY KEY,
        company_name TEXT NOT NULL,
        tally_master_id TEXT,
        name TEXT NOT NULL,
        parent_group TEXT,
        unit TEXT,
        hsn_code TEXT,
        gst_rate REAL,
        opening_qty REAL,
        opening_value REAL,
        closing_qty REAL,
        closing_value REAL,
        standard_rate REAL,
        godown_breakup JSONB,
        raw_data JSONB,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (company_name, name)
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tally_stock_company ON tally_stock_items(company_name)")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tally_groups (
        id UUID PRIMARY KEY,
        company_name TEXT NOT NULL,
        name TEXT NOT NULL,
        parent TEXT,
        is_revenue BOOLEAN,
        is_deemedpositive BOOLEAN,
        raw_data JSONB,
        UNIQUE (company_name, name)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tally_cost_centres (
        id UUID PRIMARY KEY,
        company_name TEXT NOT NULL,
        name TEXT NOT NULL,
        parent TEXT,
        category TEXT,
        UNIQUE (company_name, name)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tally_voucher_types (
        id UUID PRIMARY KEY,
        company_name TEXT NOT NULL,
        name TEXT NOT NULL,
        parent TEXT,                    -- standard voucher type (Sales, Purchase, etc.)
        is_active BOOLEAN DEFAULT TRUE,
        UNIQUE (company_name, name)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tally_sync_log (
        id UUID PRIMARY KEY,
        company_name TEXT NOT NULL,
        sync_type TEXT,                 -- 'baseline' | 'incremental' | 'voucher' | 'ledger'
        records_in INTEGER DEFAULT 0,
        records_upserted INTEGER DEFAULT 0,
        status TEXT,                    -- 'success' | 'partial' | 'failed'
        error_message TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    )
    """)

    # Tasks Table (Outcomes Marketplace)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id UUID PRIMARY KEY,
        session_id UUID REFERENCES chat_sessions(id),
        company_name TEXT,
        assigned_to TEXT DEFAULT 'sadmin',
        description TEXT,
        status TEXT DEFAULT 'Requested',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # ═══════════════════════════════════════════════════════════════
    # UNIVERSAL RECONCILIATION ENGINE — 5 tables that handle any industry
    # ═══════════════════════════════════════════════════════════════

    # Templates — industry/business configs (pure JSON, no business-specific code)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recon_templates (
        id UUID PRIMARY KEY,
        company_name TEXT,                       -- NULL = public template
        name TEXT NOT NULL,
        industry TEXT,                           -- 'Hotels', 'E-commerce', 'Restaurants', etc.
        description TEXT,
        is_public BOOLEAN DEFAULT FALSE,
        master_schema JSONB,                     -- canonical fields for master file
        source_schema JSONB,                     -- canonical fields for external sources
        supported_sources JSONB,                 -- list of platform names ['MakeMyTrip', 'OYO', ...]
        matching_rules JSONB,                    -- cascade of match strategies
        variance_formulas JSONB,                 -- formulas like "actual.commission - (actual.gross * config.rate)"
        default_config JSONB,                    -- default commission rates, tax rates per platform
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Sessions — each reconciliation run (e.g., "May 2026 OTA Reconciliation")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recon_sessions (
        id UUID PRIMARY KEY,
        company_name TEXT NOT NULL,
        template_id UUID REFERENCES recon_templates(id),
        name TEXT,
        status TEXT DEFAULT 'active',            -- active | completed | archived
        config JSONB,                            -- runtime overrides (commission rates, etc.)
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Sources — every uploaded file (master + external) belongs to a session
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recon_sources (
        id UUID PRIMARY KEY,
        session_id UUID REFERENCES recon_sessions(id) ON DELETE CASCADE,
        source_type TEXT NOT NULL,               -- 'master' | 'external'
        source_name TEXT NOT NULL,               -- 'Internal PMS', 'MakeMyTrip', 'OYO', etc.
        file_name TEXT,
        record_count INTEGER DEFAULT 0,
        column_mapping JSONB,                    -- AI/template-detected column mapping
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Records — universal: stores parsed rows from ANY source (hotels, restaurants, e-com — all in one table)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recon_records (
        id UUID PRIMARY KEY,
        session_id UUID REFERENCES recon_sessions(id) ON DELETE CASCADE,
        source_id UUID REFERENCES recon_sources(id) ON DELETE CASCADE,
        matching_key TEXT,                       -- normalized primary key (e.g., booking_id, order_id)
        canonical_data JSONB,                    -- normalized fields per template schema
        raw_data JSONB,                          -- original row, untouched
        status TEXT DEFAULT 'unmatched',         -- unmatched | matched | disputed
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_recon_records_session ON recon_records(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_recon_records_key ON recon_records(matching_key)")

    # Matches — pairs of master/external records with computed variances
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS recon_matches (
        id UUID PRIMARY KEY,
        session_id UUID REFERENCES recon_sessions(id) ON DELETE CASCADE,
        master_record_id UUID REFERENCES recon_records(id) ON DELETE CASCADE,
        external_record_id UUID REFERENCES recon_records(id) ON DELETE CASCADE,
        external_source_name TEXT,               -- denormalized for fast filtering
        match_type TEXT,                         -- exact_id | fuzzy | amount_date | manual
        match_score REAL DEFAULT 1.0,
        variances JSONB,                         -- computed per template formulas
        status TEXT DEFAULT 'pending',           -- pending | confirmed | disputed
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_recon_matches_session ON recon_matches(session_id)")
    
    # Run Column Alters to update existing database states
    try:
        cursor.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS company_name TEXT;")
        cursor.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS pan TEXT;")
    except:
        pass
        
    try:
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS company_name TEXT;")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS file_url TEXT;")
        # Link all legacy test invoices to sadmin's platform scope
        # so that Acme Corp (admin) starts with a completely fresh slate
        cursor.execute("""
        UPDATE invoices 
        SET company_name = 'YantrAI Platform Owner' 
        WHERE company_name IS NULL;
        """)
    except Exception as e:
        print(f"Migration warning: {e}")

    try:
        cursor.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS company_name TEXT;")
        # Link all legacy chat sessions to sadmin's platform scope so admin is clean
        cursor.execute("""
        UPDATE chat_sessions 
        SET company_name = 'YantrAI Platform Owner' 
        WHERE company_name IS NULL;
        """)
        
        cursor.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS companies JSONB;")
        cursor.execute("""
        UPDATE accounting_users 
        SET companies = jsonb_build_array(company_name) 
        WHERE companies IS NULL AND company_name IS NOT NULL;
        """)
    except Exception as e:
        print(f"Migration warning: {e}")
    
    # Seed default sadmin (Super Admin) if not exists
    cursor.execute("SELECT id FROM accounting_users WHERE username = 'sadmin'")
    if not cursor.fetchone():
        cursor.execute("""
        INSERT INTO accounting_users (username, password, role, name, email, phone, company_name, companies)
        VALUES ('sadmin', 'sadmin', 'super_admin', 'Super Admin', 'sadmin@yantrai.com', '+919999999999', 'YantrAI Platform Owner', '["YantrAI Platform Owner"]')
        """)
    else:
        cursor.execute("UPDATE accounting_users SET company_name = 'YantrAI Platform Owner', companies = '[\"YantrAI Platform Owner\"]' WHERE username = 'sadmin'")
        
    # Seed default admin (First Admin) if not exists
    cursor.execute("SELECT id FROM accounting_users WHERE username = 'admin'")
    if not cursor.fetchone():
        cursor.execute("""
        INSERT INTO accounting_users (username, password, role, name, email, phone, company_name, companies)
        VALUES ('admin', 'admin', 'admin', 'First Admin', 'admin@yantrai.com', '+918888888888', 'Acme Corp', '["Acme Corp"]')
        """)
    else:
        cursor.execute("UPDATE accounting_users SET company_name = 'Acme Corp', companies = '[\"Acme Corp\"]' WHERE username = 'admin' AND companies IS NULL")
        
    # Seed default tally_vouchers if empty
    cursor.execute("SELECT COUNT(*) FROM tally_vouchers")
    count = cursor.fetchone()[0]
    if count == 0:
        vouchers = [
            (str(uuid.uuid4()), '2026-05-01', 'RCV-001', 'LUXEDECO VENTURES PRIVATE LIMITED', 8320.0, 'Receipt', 'CHQ12345', 'Acme Corp', False),
            (str(uuid.uuid4()), '2026-05-06', 'RCV-002', 'Dwyane Clark', 12000.0, 'Receipt', 'NFT9988', 'Acme Corp', False),
            (str(uuid.uuid4()), '2026-05-10', 'RCV-003', 'Reka Labs', 25000.0, 'Receipt', 'NFT7766', 'Acme Corp', False),
            (str(uuid.uuid4()), '2026-05-01', 'RCV-S01', 'Global Tech Ltd', 45000.0, 'Receipt', 'CHQ8899', 'YantrAI Platform Owner', False)
        ]
        for v in vouchers:
            cursor.execute("""
            INSERT INTO tally_vouchers (id, date, voucher_number, ledger_name, amount, voucher_type, instrument_number, company_name, reconciled)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, v)
     
    conn.commit()
    cursor.close()
    conn.close()

def get_user_by_username(username: str):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM accounting_users WHERE username = %s", (username,))
        user = cursor.fetchone()
        return user
    except Exception as e:
        print(f"Error fetching user by username: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def add_company_to_user(username: str, new_company_name: str):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT companies FROM accounting_users WHERE username = %s", (username,))
        row = cursor.fetchone()
        if row:
            companies = row[0] if row[0] else []
            if isinstance(companies, str):
                import json
                companies = json.loads(companies)
            if new_company_name not in companies:
                companies.append(new_company_name)
                import json
                cursor.execute("""
                    UPDATE accounting_users 
                    SET companies = %s, company_name = %s 
                    WHERE username = %s
                """, (json.dumps(companies), new_company_name, username))
                conn.commit()
                return True
        return False
    except Exception as e:
        print(f"Error adding company: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

def update_user_active_company(username: str, new_company_name: str, pan: str = None):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT companies FROM accounting_users WHERE username = %s", (username,))
        row = cursor.fetchone()
        if row:
            companies = row[0] if row[0] else []
            if isinstance(companies, str):
                import json
                companies = json.loads(companies)
            if new_company_name not in companies:
                companies.append(new_company_name)
            import json
            cursor.execute("""
                UPDATE accounting_users 
                SET companies = %s, company_name = %s, pan = COALESCE(%s, pan)
                WHERE username = %s
            """, (json.dumps(companies), new_company_name, pan, username))
            conn.commit()
            return True
        return False
    except Exception as e:
        print(f"Error updating user active company: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

def create_user(username: str, password: str, role: str = "admin", name: str = None, email: str = None, phone: str = None, company_name: str = "Acme Corp"):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        import json
        companies_json = json.dumps([company_name])
        cursor.execute("""
            INSERT INTO accounting_users (username, password, role, name, email, phone, company_name, companies)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (username, password, role, name or username, email or f"{username}@yantrai.com", phone or "+919999999999", company_name, companies_json))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error creating user: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

# ---- Chat Functions ----

def create_chat_session(title="New Chat", company_name=None):
    conn = get_conn()
    cursor = conn.cursor()
    session_id = str(uuid.uuid4())
    cursor.execute("""
    INSERT INTO chat_sessions (id, title, company_name) VALUES (%s, %s, %s)
    """, (session_id, title, company_name))
    conn.commit()
    cursor.close()
    conn.close()
    return session_id

def get_chat_sessions(company_name=None):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    if company_name:
        cursor.execute("SELECT * FROM chat_sessions WHERE company_name = %s ORDER BY updated_at DESC", (company_name,))
    else:
        cursor.execute("SELECT * FROM chat_sessions ORDER BY updated_at DESC")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows

def save_chat_message(session_id, role, content, ui_type="text", ui_data=None):
    conn = get_conn()
    cursor = conn.cursor()
    msg_id = str(uuid.uuid4())
    cursor.execute("""
    INSERT INTO chat_messages (id, session_id, role, content, ui_type, ui_data)
    VALUES (%s, %s, %s, %s, %s, %s)
    """, (msg_id, session_id, role, content, ui_type, json.dumps(ui_data) if ui_data else None))
    # Update session timestamp
    cursor.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (session_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return msg_id

def update_chat_message_ui_data(message_id, ui_data):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
        UPDATE chat_messages 
        SET ui_data = %s 
        WHERE id = %s
        """, (json.dumps(ui_data) if ui_data else None, message_id))
        conn.commit()
    except Exception as e:
        print(f"Error updating chat message UI data: {e}")
    finally:
        cursor.close()
        conn.close()

def get_chat_message_by_id(message_id):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM chat_messages WHERE id = %s", (message_id,))
        return cursor.fetchone()
    except Exception as e:
        print(f"Error fetching chat message by ID: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def update_chat_title(session_id, title):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE chat_sessions SET title = %s WHERE id = %s", (title, session_id))
    conn.commit()
    cursor.close()
    conn.close()

def get_chat_messages(session_id):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM chat_messages WHERE session_id = %s ORDER BY created_at ASC", (session_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows

def save_invoice(data):
    conn = get_conn()
    cursor = conn.cursor()
    
    company_name = data.get('company_name')
    invoice_number = data.get('invoice_number')
    
    # Check if invoice already exists
    cursor.execute("SELECT id FROM invoices WHERE invoice_number = %s AND company_name = %s", (invoice_number, company_name))
    existing = cursor.fetchone()
    
    if existing:
        inv_id = existing[0]
        # Delete existing items first
        cursor.execute("DELETE FROM items WHERE invoice_id = %s", (inv_id,))
        # Update invoice
        cursor.execute("""
        UPDATE invoices 
        SET date = %s, party_name = %s, total_amount = %s, discount_amount = %s, gst_amount = %s, 
            category = %s, file_url = %s, billing_party_name = %s, billing_party_gstin = %s, billed_to_party_gstin = %s,
            created_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """, (data.get('date'), data.get('party_name'), data.get('total_amount'), data.get('discount_amount', 0), data.get('gst_amount', 0),
              data.get('category'), data.get('file_url'), data.get('billing_party_name'), data.get('billing_party_gstin'), data.get('billed_to_party_gstin'),
              inv_id))
    else:
        inv_id = str(uuid.uuid4())
        # Insert new invoice
        cursor.execute("""
        INSERT INTO invoices (id, invoice_number, date, party_name, total_amount, discount_amount, gst_amount, category, company_name, file_url, billing_party_name, billing_party_gstin, billed_to_party_gstin)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (inv_id, invoice_number, data.get('date'), data.get('party_name'), 
              data.get('total_amount'), data.get('discount_amount', 0), data.get('gst_amount', 0), data.get('category'), company_name, data.get('file_url'),
              data.get('billing_party_name'), data.get('billing_party_gstin'), data.get('billed_to_party_gstin')))
    
    # Save Items
    if 'items' in data:
        for item in data['items']:
            item_id = str(uuid.uuid4())
            cursor.execute("""
            INSERT INTO items (id, invoice_id, description, quantity, rate, amount, cgst_rate, sgst_rate, discount, hsn_sac)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (item_id, inv_id, item.get('description'), item.get('quantity'), item.get('rate'), item.get('amount'),
                  item.get('cgst_rate'), item.get('sgst_rate'), item.get('discount'), item.get('hsn_sac')))
            
    conn.commit()
    cursor.close()
    conn.close()
    return inv_id

def get_history(company_name=None):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    if company_name:
        cursor.execute("SELECT * FROM invoices WHERE company_name = %s ORDER BY created_at DESC", (company_name,))
    else:
        cursor.execute("SELECT * FROM invoices ORDER BY created_at DESC")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows

def save_correction(field, original, corrected, party_name=None, embedding=None, company_name="Acme Corp"):
    conn = get_conn()
    cursor = conn.cursor()
    data = {
        "field": field,
        "original": original,
        "corrected": corrected,
        "party_name": party_name,
        "company_name": company_name
    }
    
    if embedding:
        embedding_str = f"[{','.join(map(str, embedding))}]"
        cursor.execute("""
        INSERT INTO knowledge_base (type, data, embedding) VALUES (%s, %s, %s)
        """, ('correction', json.dumps(data), embedding_str))
    else:
        cursor.execute("""
        INSERT INTO knowledge_base (type, data) VALUES (%s, %s)
        """, ('correction', json.dumps(data)))
        
    conn.commit()
    cursor.close()
    conn.close()

def get_corrections(company_name="Acme Corp"):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT data FROM knowledge_base WHERE type = %s AND data->>'company_name' = %s", ('correction', company_name))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [r['data'] for r in rows]

def get_relevant_corrections(query_embedding, company_name="Acme Corp", limit=5):
    if not query_embedding:
        return []
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    embedding_str = f"[{','.join(map(str, query_embedding))}]"
    
    cursor.execute("""
    SELECT data FROM knowledge_base 
    WHERE type = 'correction' AND embedding IS NOT NULL AND data->>'company_name' = %s
    ORDER BY embedding <=> %s::vector 
    LIMIT %s
    """, (company_name, embedding_str, limit))
    
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [r['data'] for r in rows]

def save_tally_vouchers(company_name, vouchers):
    """
    UPSERT vouchers by (company_name, tally_master_id || voucher_number).
    Critical fix: previously did DELETE-then-INSERT which wiped all history every sync.
    Now incremental-safe — existing vouchers are updated, new ones appended.
    """
    if not vouchers:
        return {"upserted": 0, "skipped": 0}
    conn = get_conn()
    cursor = conn.cursor()
    upserted = 0
    skipped = 0
    try:
        for v in vouchers:
            date_raw = str(v.get("date", ""))
            if len(date_raw) == 8 and date_raw.isdigit():
                v_date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}"
            elif date_raw and "-" in date_raw:
                v_date = date_raw[:10]
            else:
                v_date = None
            v_num = str(v.get("number", "") or v.get("voucher_number", "") or "").strip()
            party = str(v.get("party", "") or v.get("party_ledger", "") or "")
            v_type = str(v.get("type", "") or v.get("voucher_type", ""))
            amount = float(v.get("amount", 0.0) or 0.0)
            narration = v.get("narration") or ""
            ledger_entries = v.get("ledger_entries") or []  # list of {ledger, amount, is_debit}
            reference_no = v.get("reference_no") or v.get("reference", "")
            place_of_supply = v.get("place_of_supply", "")
            party_gstin = v.get("party_gstin", "")
            currency = v.get("currency", "INR")
            cost_centres = v.get("cost_centres") or []
            bill_refs = v.get("bill_refs") or []
            taxable_value = float(v.get("taxable_value", 0.0) or 0.0)
            cgst_amount = float(v.get("cgst_amount", 0.0) or 0.0)
            sgst_amount = float(v.get("sgst_amount", 0.0) or 0.0)
            igst_amount = float(v.get("igst_amount", 0.0) or 0.0)
            tally_master_id = v.get("tally_master_id") or v.get("guid") or None
            raw_xml = v.get("raw_xml") or None
            instrument_number = v.get("instrument_number", "")

            # If we have no identifier at all, skip
            if not v_num and not tally_master_id:
                skipped += 1
                continue

            # Application-side upsert: check existence by (company, tally_master_id) preferred,
            # else (company, voucher_number). This avoids needing a hard unique index since
            # legacy data may contain duplicates.
            existing_id = None
            if tally_master_id:
                cursor.execute("""
                    SELECT id FROM tally_vouchers
                     WHERE company_name = %s AND tally_master_id = %s LIMIT 1
                """, (company_name, tally_master_id))
            else:
                cursor.execute("""
                    SELECT id FROM tally_vouchers
                     WHERE company_name = %s AND voucher_number = %s
                       AND (tally_master_id IS NULL OR tally_master_id = '') LIMIT 1
                """, (company_name, v_num))
            row = cursor.fetchone()
            if row:
                existing_id = row[0]

            if existing_id:
                cursor.execute("""
                    UPDATE tally_vouchers SET
                        date=%s, voucher_number=%s, ledger_name=%s, amount=%s,
                        voucher_type=%s, instrument_number=%s,
                        narration=%s, ledger_entries=%s, reference_no=%s,
                        place_of_supply=%s, party_gstin=%s, currency=%s,
                        cost_centres=%s, bill_refs=%s,
                        taxable_value=%s, cgst_amount=%s, sgst_amount=%s, igst_amount=%s,
                        tally_master_id=COALESCE(%s, tally_master_id),
                        raw_xml=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s
                """, (
                    v_date, v_num, party, amount, v_type, instrument_number,
                    narration, json.dumps(ledger_entries), reference_no,
                    place_of_supply, party_gstin, currency,
                    json.dumps(cost_centres), json.dumps(bill_refs),
                    taxable_value, cgst_amount, sgst_amount, igst_amount,
                    tally_master_id, raw_xml, existing_id
                ))
            else:
                cursor.execute("""
                    INSERT INTO tally_vouchers
                        (id, date, voucher_number, ledger_name, amount, voucher_type,
                         instrument_number, company_name, reconciled,
                         narration, ledger_entries, reference_no, place_of_supply,
                         party_gstin, currency, cost_centres, bill_refs,
                         taxable_value, cgst_amount, sgst_amount, igst_amount,
                         tally_master_id, raw_xml, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE,
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                """, (
                    str(uuid.uuid4()), v_date, v_num, party, amount, v_type,
                    instrument_number, company_name,
                    narration, json.dumps(ledger_entries), reference_no, place_of_supply,
                    party_gstin, currency, json.dumps(cost_centres), json.dumps(bill_refs),
                    taxable_value, cgst_amount, sgst_amount, igst_amount,
                    tally_master_id, raw_xml
                ))
            upserted += 1
        conn.commit()
    except Exception as e:
        print(f"Error saving tally vouchers: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
    return {"upserted": upserted, "skipped": skipped}


def save_tally_ledgers(company_name, ledgers):
    """Upsert full ledger master from Tally."""
    if not ledgers:
        return 0
    conn = get_conn()
    cursor = conn.cursor()
    count = 0
    try:
        for L in ledgers:
            if isinstance(L, str):
                L = {"name": L}
            name = (L.get("name") or "").strip()
            if not name:
                continue
            cursor.execute("""
                INSERT INTO tally_ledgers
                    (id, company_name, tally_master_id, name, parent_group, group_path,
                     opening_balance, closing_balance, is_revenue, is_deemedpositive,
                     gstin, pan, address, bank_name, account_number, ifsc_code,
                     gst_registration_type, tds_applicable, ledger_type, place_of_supply,
                     raw_data, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                ON CONFLICT (company_name, name)
                DO UPDATE SET
                    tally_master_id = EXCLUDED.tally_master_id,
                    parent_group = EXCLUDED.parent_group,
                    group_path = EXCLUDED.group_path,
                    opening_balance = EXCLUDED.opening_balance,
                    closing_balance = EXCLUDED.closing_balance,
                    gstin = EXCLUDED.gstin,
                    pan = EXCLUDED.pan,
                    address = EXCLUDED.address,
                    bank_name = EXCLUDED.bank_name,
                    account_number = EXCLUDED.account_number,
                    ifsc_code = EXCLUDED.ifsc_code,
                    gst_registration_type = EXCLUDED.gst_registration_type,
                    tds_applicable = EXCLUDED.tds_applicable,
                    ledger_type = EXCLUDED.ledger_type,
                    place_of_supply = EXCLUDED.place_of_supply,
                    raw_data = EXCLUDED.raw_data,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                str(uuid.uuid4()), company_name,
                L.get("tally_master_id") or L.get("guid"),
                name,
                L.get("parent_group") or L.get("parent") or L.get("group"),
                L.get("group_path"),
                _to_float(L.get("opening_balance")),
                _to_float(L.get("closing_balance")),
                _to_bool(L.get("is_revenue")),
                _to_bool(L.get("is_deemedpositive")),
                L.get("gstin"),
                L.get("pan"),
                L.get("address"),
                L.get("bank_name"),
                L.get("account_number"),
                L.get("ifsc_code"),
                L.get("gst_registration_type"),
                _to_bool(L.get("tds_applicable")),
                L.get("ledger_type"),
                L.get("place_of_supply"),
                json.dumps(L),
            ))
            count += 1
        conn.commit()
    except Exception as e:
        print(f"Error saving tally ledgers: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
    return count


def save_tally_stock_items(company_name, items):
    if not items:
        return 0
    conn = get_conn()
    cursor = conn.cursor()
    count = 0
    try:
        for s in items:
            if isinstance(s, str):
                s = {"name": s}
            name = (s.get("name") or "").strip()
            if not name:
                continue
            cursor.execute("""
                INSERT INTO tally_stock_items
                    (id, company_name, tally_master_id, name, parent_group, unit, hsn_code,
                     gst_rate, opening_qty, opening_value, closing_qty, closing_value,
                     standard_rate, godown_breakup, raw_data, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                ON CONFLICT (company_name, name)
                DO UPDATE SET
                    parent_group = EXCLUDED.parent_group,
                    unit = EXCLUDED.unit,
                    hsn_code = EXCLUDED.hsn_code,
                    gst_rate = EXCLUDED.gst_rate,
                    closing_qty = EXCLUDED.closing_qty,
                    closing_value = EXCLUDED.closing_value,
                    standard_rate = EXCLUDED.standard_rate,
                    godown_breakup = EXCLUDED.godown_breakup,
                    raw_data = EXCLUDED.raw_data,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                str(uuid.uuid4()), company_name,
                s.get("tally_master_id") or s.get("guid"),
                name,
                s.get("parent_group") or s.get("parent"),
                s.get("unit"),
                s.get("hsn_code") or s.get("hsn"),
                _to_float(s.get("gst_rate")),
                _to_float(s.get("opening_qty")),
                _to_float(s.get("opening_value")),
                _to_float(s.get("closing_qty")),
                _to_float(s.get("closing_value")),
                _to_float(s.get("standard_rate")),
                json.dumps(s.get("godown_breakup") or []),
                json.dumps(s),
            ))
            count += 1
        conn.commit()
    except Exception as e:
        print(f"Error saving stock items: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
    return count


def save_tally_groups(company_name, groups):
    if not groups:
        return 0
    conn = get_conn()
    cursor = conn.cursor()
    count = 0
    try:
        for g in groups:
            if isinstance(g, str):
                g = {"name": g}
            name = (g.get("name") or "").strip()
            if not name:
                continue
            cursor.execute("""
                INSERT INTO tally_groups (id, company_name, name, parent, is_revenue, is_deemedpositive, raw_data)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (company_name, name) DO UPDATE SET
                    parent = EXCLUDED.parent,
                    is_revenue = EXCLUDED.is_revenue,
                    is_deemedpositive = EXCLUDED.is_deemedpositive,
                    raw_data = EXCLUDED.raw_data
            """, (str(uuid.uuid4()), company_name, name, g.get("parent"),
                  _to_bool(g.get("is_revenue")), _to_bool(g.get("is_deemedpositive")),
                  json.dumps(g)))
            count += 1
        conn.commit()
    except Exception as e:
        print(f"Error saving groups: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
    return count


def log_tally_sync(company_name, sync_type, records_in, records_upserted, status, error_message=None):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO tally_sync_log
                (id, company_name, sync_type, records_in, records_upserted, status, error_message, completed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
        """, (str(uuid.uuid4()), company_name, sync_type, records_in,
              records_upserted, status, error_message))
        conn.commit()
    except Exception as e:
        print(f"Error logging sync: {e}")
    finally:
        cursor.close()
        conn.close()


def get_tally_ledgers(company_name):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM tally_ledgers WHERE company_name = %s ORDER BY name", (company_name,))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def get_tally_stock_items(company_name):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM tally_stock_items WHERE company_name = %s ORDER BY name", (company_name,))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("₹", "").strip())
    except Exception:
        return None


def _to_bool(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "true", "1", "y")

def get_all_tally_vouchers(company_name):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM tally_vouchers WHERE company_name = %s ORDER BY date ASC", (company_name,))
        rows = cursor.fetchall()
        return rows
    except Exception as e:
        print(f"Error fetching all tally vouchers: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

def get_accounting_summary(company_name=None, user_msg=""):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        msg_lower = str(user_msg).lower()
        target_company = company_name or "Acme Corp"
        
        cursor.execute("SELECT DISTINCT company_name FROM tally_vouchers UNION SELECT DISTINCT company_name FROM parties UNION SELECT DISTINCT company_name FROM invoices")
        known_companies = [r['company_name'] for r in cursor.fetchall() if r['company_name']]
        
        for kc in known_companies:
            if kc.lower() in msg_lower:
                target_company = kc
                break
        if "indulge" in msg_lower and not any("indulge" in kc.lower() for kc in known_companies):
            target_company = "Indulge"
        elif "indulge" in msg_lower:
            for kc in known_companies:
                if "indulge" in kc.lower():
                    target_company = kc
                    break
            
        param_like = f"%{target_company}%"
        
        cursor.execute("""
            SELECT data->>'original' as name, data->>'corrected' as grp 
            FROM knowledge_base 
            WHERE type='correction' AND data->>'field'='ledger_group_mapping'
            AND (data->>'company_name' ILIKE %s OR %s ILIKE ('%%' || (data->>'company_name') || '%%') OR data->>'company_name' IS NULL)
        """, (param_like, target_company))
        ledgers = cursor.fetchall()
        ledger_sample = [f"{l['name']} ({l['grp']})" for l in ledgers[:30]]
        
        cursor.execute("""
            SELECT date, voucher_number, ledger_name, voucher_type, amount,
                   narration, ledger_entries, party_gstin, place_of_supply,
                   reference_no, taxable_value, cgst_amount, sgst_amount, igst_amount,
                   instrument_number
            FROM tally_vouchers
            WHERE company_name ILIKE %s OR %s ILIKE ('%%' || company_name || '%%')
            ORDER BY date DESC
        """, (param_like, target_company))
        vouchers = cursor.fetchall()
        voucher_sample = vouchers[:50]  # bumped to give AI more context now that rows are richer

        # Pull full ledger master so AI can see GST setup, bank details, ledger types
        cursor.execute("""
            SELECT name, parent_group, group_path, closing_balance, gstin, pan,
                   ledger_type, gst_registration_type, place_of_supply
            FROM tally_ledgers
            WHERE company_name ILIKE %s OR %s ILIKE ('%%' || company_name || '%%')
            ORDER BY name
        """, (param_like, target_company))
        full_ledgers = cursor.fetchall()

        # Stock items with HSN — critical for GST classification
        cursor.execute("""
            SELECT name, hsn_code, gst_rate, unit, closing_qty, closing_value
            FROM tally_stock_items
            WHERE company_name ILIKE %s OR %s ILIKE ('%%' || company_name || '%%')
            ORDER BY name
        """, (param_like, target_company))
        stock_items = cursor.fetchall()

        # Auto-learned party → ledger patterns (from past vouchers)
        cursor.execute("""
            SELECT ledger_name AS party, voucher_type, COUNT(*) AS cnt
            FROM tally_vouchers
            WHERE (company_name ILIKE %s OR %s ILIKE ('%%' || company_name || '%%'))
              AND ledger_name IS NOT NULL AND ledger_name <> ''
            GROUP BY ledger_name, voucher_type
            ORDER BY cnt DESC
            LIMIT 30
        """, (param_like, target_company))
        party_patterns = cursor.fetchall()
        
        cursor.execute("""
            SELECT name 
            FROM parties 
            WHERE company_name ILIKE %s OR %s ILIKE ('%%' || company_name || '%%') 
            ORDER BY name ASC
        """, (param_like, target_company))
        parties = cursor.fetchall()
        party_sample = [p['name'] for p in parties[:30]]
        
        cursor.execute("""
            SELECT * 
            FROM invoices 
            WHERE company_name ILIKE %s OR %s ILIKE ('%%' || company_name || '%%') 
            ORDER BY created_at DESC LIMIT 20
        """, (param_like, target_company))
        recent_invoices = cursor.fetchall()
        
        summary = {
            "target_company": target_company,
            "active_ui_company": company_name,
            "tally_ledgers_ingested_count": len(full_ledgers) or len(ledgers),
            "tally_ledgers_sample": ledger_sample,
            "tally_ledger_master": [dict(r) for r in full_ledgers[:60]],
            "tally_vouchers_ingested_count": len(vouchers),
            "tally_vouchers_sample": voucher_sample,
            "tally_stock_master": [dict(r) for r in stock_items[:40]],
            "auto_learned_party_patterns": [dict(r) for r in party_patterns],
            "party_master_count": len(parties),
            "party_master_sample": party_sample,
            "recent_uploaded_invoices_count": len(recent_invoices),
            "recent_uploaded_invoices": recent_invoices
        }
        return f"Accounting Data Summary for company '{target_company}' (Active UI Company: '{company_name}'):\n" + json.dumps(summary, default=str)
    except Exception as e:
        print(f"Error getting accounting summary: {e}")
        return f"Accounting Data Summary for company '{company_name}': No recent data available."
    finally:
        cursor.close()
        conn.close()

def get_unreconciled_tally_vouchers(company_name):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM tally_vouchers WHERE company_name = %s AND reconciled = FALSE ORDER BY date ASC", (company_name,))
        rows = cursor.fetchall()
        return rows
    except Exception as e:
        print(f"Error fetching tally vouchers: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

def mark_tally_voucher_reconciled(voucher_id):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE tally_vouchers SET reconciled = TRUE WHERE id = %s", (voucher_id,))
        conn.commit()
    except Exception as e:
        print(f"Error updating tally voucher reconciled status: {e}")
    finally:
        cursor.close()
        conn.close()

def mark_invoice_synced(invoice_id):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE invoices SET status = 'synced' WHERE id = %s", (invoice_id,))
        conn.commit()
    except Exception as e:
        print(f"Error marking invoice synced: {e}")
    finally:
        cursor.close()
        conn.close()

# ---- Task Functions ----

def create_task(session_id, company_name, description, assigned_to='sadmin'):
    conn = get_conn()
    cursor = conn.cursor()
    task_id = str(uuid.uuid4())
    try:
        cursor.execute("""
        INSERT INTO tasks (id, session_id, company_name, assigned_to, description, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        """, (task_id, session_id, company_name, assigned_to, description, 'Requested'))
        conn.commit()
    except Exception as e:
        print(f"Error creating task: {e}")
    finally:
        cursor.close()
        conn.close()
    return task_id

def get_tasks(company_name=None, role='admin'):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if role == 'super_admin':
            # Super admin sees all tasks
            cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC")
        else:
            # Regular admin sees only their own company's tasks
            cursor.execute("SELECT * FROM tasks WHERE company_name = %s ORDER BY created_at DESC", (company_name,))
        rows = cursor.fetchall()
        return rows
    except Exception as e:
        print(f"Error fetching tasks: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

def update_task_status(task_id, new_status):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
        UPDATE tasks 
        SET status = %s, updated_at = CURRENT_TIMESTAMP 
        WHERE id = %s
        """, (new_status, task_id))
        conn.commit()
    except Exception as e:
        print(f"Error updating task status: {e}")
    finally:
        cursor.close()
        conn.close()

def save_or_update_party(company_name, name, gstin=None, address=None, bank_name=None, account_number=None, ifsc_code=None, pan=None, email=None, phone=None):
    if not name:
        return
    conn = get_conn()
    cursor = conn.cursor()
    try:
        # Check if party exists
        cursor.execute("SELECT id FROM parties WHERE company_name = %s AND name = %s", (company_name, name))
        row = cursor.fetchone()
        if row:
            party_id = row[0]
            updates = []
            params = []
            if gstin is not None:
                updates.append("gstin = %s")
                params.append(gstin)
            if address is not None:
                updates.append("address = %s")
                params.append(address)
            if bank_name is not None:
                updates.append("bank_name = %s")
                params.append(bank_name)
            if account_number is not None:
                updates.append("account_number = %s")
                params.append(account_number)
            if ifsc_code is not None:
                updates.append("ifsc_code = %s")
                params.append(ifsc_code)
            if pan is not None:
                updates.append("pan = %s")
                params.append(pan)
            if email is not None:
                updates.append("email = %s")
                params.append(email)
            if phone is not None:
                updates.append("phone = %s")
                params.append(phone)
            
            if updates:
                params.append(party_id)
                cursor.execute(f"UPDATE parties SET {', '.join(updates)} WHERE id = %s", tuple(params))
        else:
            party_id = str(uuid.uuid4())
            cursor.execute("""
            INSERT INTO parties (id, company_name, name, gstin, address, bank_name, account_number, ifsc_code, pan, email, phone)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (party_id, company_name, name, gstin or '', address or '', bank_name or '', account_number or '', ifsc_code or '', pan or '', email or '', phone or ''))
        conn.commit()
    except Exception as e:
        print(f"Error saving party: {e}")
    finally:
        cursor.close()
        conn.close()

def get_parties(company_name="Acme Corp"):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM parties WHERE company_name = %s ORDER BY name ASC", (company_name,))
        return cursor.fetchall()
    except Exception as e:
        print(f"Error fetching parties: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

def delete_invoice(invoice_id):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        # Delete related items first
        cursor.execute("DELETE FROM items WHERE invoice_id = %s", (invoice_id,))
        cursor.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error deleting invoice: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

def delete_party(party_id):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM parties WHERE id = %s", (party_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error deleting party: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

# ═══════════════════════════════════════════════════════════════════════════
# UNIVERSAL RECONCILIATION ENGINE — CRUD
# ═══════════════════════════════════════════════════════════════════════════

# ── Templates ──────────────────────────────────────────────────────────────

def create_recon_template(name, industry, master_schema, source_schema,
                           supported_sources, matching_rules, variance_formulas,
                           default_config=None, description=None,
                           company_name=None, is_public=False):
    """Create a new reconciliation template. company_name=None + is_public=True ⇒ system template."""
    conn = get_conn()
    cursor = conn.cursor()
    tid = str(uuid.uuid4())
    try:
        cursor.execute("""
            INSERT INTO recon_templates
              (id, company_name, name, industry, description, is_public,
               master_schema, source_schema, supported_sources,
               matching_rules, variance_formulas, default_config)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (tid, company_name, name, industry, description, is_public,
              json.dumps(master_schema), json.dumps(source_schema),
              json.dumps(supported_sources), json.dumps(matching_rules),
              json.dumps(variance_formulas), json.dumps(default_config or {})))
        conn.commit()
        return tid
    finally:
        cursor.close()
        conn.close()

def get_recon_templates(company_name=None):
    """Return public templates + this company's private templates."""
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT * FROM recon_templates
            WHERE is_public = TRUE OR company_name = %s
            ORDER BY industry, name
        """, (company_name,))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

def get_recon_template(template_id):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM recon_templates WHERE id = %s", (template_id,))
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

def upsert_recon_template_by_name(name, **kwargs):
    """Used by seed routine to create-or-update built-in templates."""
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT id FROM recon_templates WHERE name = %s AND is_public = TRUE", (name,))
        row = cursor.fetchone()
        if row:
            cursor.execute("""
                UPDATE recon_templates
                   SET industry=%s, description=%s,
                       master_schema=%s, source_schema=%s,
                       supported_sources=%s, matching_rules=%s,
                       variance_formulas=%s, default_config=%s,
                       updated_at=CURRENT_TIMESTAMP
                 WHERE id=%s
            """, (kwargs.get('industry'), kwargs.get('description'),
                  json.dumps(kwargs.get('master_schema', {})),
                  json.dumps(kwargs.get('source_schema', {})),
                  json.dumps(kwargs.get('supported_sources', [])),
                  json.dumps(kwargs.get('matching_rules', [])),
                  json.dumps(kwargs.get('variance_formulas', [])),
                  json.dumps(kwargs.get('default_config', {})),
                  row['id']))
            conn.commit()
            return row['id']
        else:
            return create_recon_template(name=name, is_public=True, **kwargs)
    finally:
        cursor.close()
        conn.close()

# ── Sessions ──────────────────────────────────────────────────────────────

def create_recon_session(company_name, template_id, name, config=None):
    conn = get_conn()
    cursor = conn.cursor()
    sid = str(uuid.uuid4())
    try:
        cursor.execute("""
            INSERT INTO recon_sessions (id, company_name, template_id, name, config)
            VALUES (%s,%s,%s,%s,%s)
        """, (sid, company_name, template_id, name, json.dumps(config or {})))
        conn.commit()
        return sid
    finally:
        cursor.close()
        conn.close()

def get_recon_session(session_id):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM recon_sessions WHERE id = %s", (session_id,))
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

def get_recon_sessions(company_name):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT s.*, t.name as template_name, t.industry
              FROM recon_sessions s
              LEFT JOIN recon_templates t ON s.template_id = t.id
             WHERE s.company_name = %s
             ORDER BY s.created_at DESC
        """, (company_name,))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

def update_recon_session_config(session_id, config):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE recon_sessions SET config=%s, updated_at=CURRENT_TIMESTAMP
             WHERE id=%s
        """, (json.dumps(config), session_id))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

# ── Sources ──────────────────────────────────────────────────────────────

def create_recon_source(session_id, source_type, source_name, file_name,
                        record_count, column_mapping):
    conn = get_conn()
    cursor = conn.cursor()
    src_id = str(uuid.uuid4())
    try:
        cursor.execute("""
            INSERT INTO recon_sources
              (id, session_id, source_type, source_name, file_name,
               record_count, column_mapping)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (src_id, session_id, source_type, source_name, file_name,
              record_count, json.dumps(column_mapping or {})))
        conn.commit()
        return src_id
    finally:
        cursor.close()
        conn.close()

def get_recon_sources(session_id):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT * FROM recon_sources WHERE session_id=%s ORDER BY created_at
        """, (session_id,))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

def get_recon_master_source(session_id):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT * FROM recon_sources
             WHERE session_id=%s AND source_type='master' LIMIT 1
        """, (session_id,))
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

# ── Records ──────────────────────────────────────────────────────────────

def bulk_insert_recon_records(session_id, source_id, records):
    """records is a list of dicts with keys: matching_key, canonical_data, raw_data."""
    if not records:
        return 0
    conn = get_conn()
    cursor = conn.cursor()
    try:
        values = []
        for r in records:
            values.append((
                str(uuid.uuid4()), session_id, source_id,
                r.get('matching_key', ''),
                json.dumps(r.get('canonical_data', {})),
                json.dumps(r.get('raw_data', {}))
            ))
        cursor.executemany("""
            INSERT INTO recon_records
              (id, session_id, source_id, matching_key, canonical_data, raw_data)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, values)
        conn.commit()
        return len(values)
    finally:
        cursor.close()
        conn.close()

def get_recon_records(session_id, source_id=None, status=None):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        q = "SELECT * FROM recon_records WHERE session_id=%s"
        params = [session_id]
        if source_id:
            q += " AND source_id=%s"; params.append(source_id)
        if status:
            q += " AND status=%s"; params.append(status)
        cursor.execute(q, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

def update_recon_record_status(record_id, status):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE recon_records SET status=%s WHERE id=%s", (status, record_id))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

# ── Matches ──────────────────────────────────────────────────────────────

def bulk_insert_recon_matches(session_id, matches):
    """matches: list of dicts with master_record_id, external_record_id,
    external_source_name, match_type, match_score, variances."""
    if not matches:
        return 0
    conn = get_conn()
    cursor = conn.cursor()
    try:
        values = []
        for m in matches:
            values.append((
                str(uuid.uuid4()), session_id,
                m.get('master_record_id'),
                m.get('external_record_id'),
                m.get('external_source_name'),
                m.get('match_type', 'exact_id'),
                m.get('match_score', 1.0),
                json.dumps(m.get('variances', {})),
            ))
        cursor.executemany("""
            INSERT INTO recon_matches
              (id, session_id, master_record_id, external_record_id,
               external_source_name, match_type, match_score, variances)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, values)
        conn.commit()
        return len(values)
    finally:
        cursor.close()
        conn.close()

def get_recon_matches(session_id, source_name=None, status=None):
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        q = """
        SELECT m.*,
               mr.canonical_data AS master_data,
               mr.matching_key AS master_key,
               er.canonical_data AS external_data,
               er.matching_key AS external_key
          FROM recon_matches m
          LEFT JOIN recon_records mr ON m.master_record_id = mr.id
          LEFT JOIN recon_records er ON m.external_record_id = er.id
         WHERE m.session_id=%s
        """
        params = [session_id]
        if source_name:
            q += " AND m.external_source_name=%s"; params.append(source_name)
        if status:
            q += " AND m.status=%s"; params.append(status)
        q += " ORDER BY m.created_at DESC"
        cursor.execute(q, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

def update_recon_match_status(match_id, status, notes=None):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE recon_matches SET status=%s, notes=%s WHERE id=%s",
                       (status, notes, match_id))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

def get_recon_session_summary(session_id):
    """Aggregated stats for the session dashboard."""
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Per-source record counts
        cursor.execute("""
            SELECT source_type, source_name, record_count
              FROM recon_sources WHERE session_id=%s
        """, (session_id,))
        sources = cursor.fetchall()

        # Match counts grouped by external source
        cursor.execute("""
            SELECT external_source_name, COUNT(*) AS matched,
                   AVG(match_score) AS avg_score
              FROM recon_matches
             WHERE session_id=%s
             GROUP BY external_source_name
        """, (session_id,))
        match_stats = cursor.fetchall()

        # Total unmatched records (excluding master records that ARE matched)
        cursor.execute("""
            SELECT COUNT(*) AS unmatched_count
              FROM recon_records r
             WHERE r.session_id=%s
               AND r.id NOT IN (
                 SELECT master_record_id FROM recon_matches WHERE session_id=%s
                 UNION
                 SELECT external_record_id FROM recon_matches WHERE session_id=%s
               )
        """, (session_id, session_id, session_id))
        unmatched = cursor.fetchone()

        return {
            'sources': sources,
            'match_stats': match_stats,
            'unmatched_count': unmatched['unmatched_count'] if unmatched else 0
        }
    finally:
        cursor.close()
        conn.close()

def seed_builtin_recon_templates():
    """Load all JSON files from templates/ and upsert them as public templates."""
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
    if not os.path.isdir(templates_dir):
        return 0
    count = 0
    for fname in os.listdir(templates_dir):
        if not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(templates_dir, fname), 'r', encoding='utf-8') as f:
                tpl = json.load(f)
            upsert_recon_template_by_name(
                name=tpl['name'],
                industry=tpl.get('industry'),
                description=tpl.get('description'),
                master_schema=tpl.get('master_schema', {}),
                source_schema=tpl.get('source_schema', {}),
                supported_sources=tpl.get('supported_sources', []),
                matching_rules=tpl.get('matching_rules', []),
                variance_formulas=tpl.get('variance_formulas', []),
                default_config=tpl.get('default_config', {}),
            )
            count += 1
        except Exception as e:
            print(f"  ⚠️ Failed to seed template {fname}: {e}")
    return count

# Initialize on start
try:
    init_db()
    seeded = seed_builtin_recon_templates()
    print(f"Cloud Database Initialized Successfully. ({seeded} recon templates loaded)")
except Exception as e:
    print(f"Cloud DB Error: {e}")
