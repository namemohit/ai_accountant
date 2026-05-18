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
    
    # Run Column Alters to update existing database states
    try:
        cursor.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS company_name TEXT;")
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

def save_correction(field, original, corrected, party_name=None, embedding=None):
    conn = get_conn()
    cursor = conn.cursor()
    data = {
        "field": field,
        "original": original,
        "corrected": corrected,
        "party_name": party_name
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

def get_corrections():
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT data FROM knowledge_base WHERE type = %s", ('correction',))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [r['data'] for r in rows]

def get_relevant_corrections(query_embedding, limit=5):
    if not query_embedding:
        return []
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    embedding_str = f"[{','.join(map(str, query_embedding))}]"
    
    cursor.execute("""
    SELECT data FROM knowledge_base 
    WHERE type = 'correction' AND embedding IS NOT NULL
    ORDER BY embedding <=> %s::vector 
    LIMIT %s
    """, (embedding_str, limit))
    
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [r['data'] for r in rows]

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

# Initialize on start
try:
    init_db()
    print("Cloud Database Initialized Successfully.")
except Exception as e:
    print(f"Cloud DB Error: {e}")
