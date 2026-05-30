import psycopg2
from psycopg2.extras import RealDictCursor
import uuid
from datetime import datetime
import os
import json
import re

# Sprint 40 — fail-fast on missing env vars instead of falling back to literals in
# committed source. Pulls .env into the process if it isn't already loaded (so this
# module works for scripts that don't go through server.py).
from dotenv import load_dotenv
load_dotenv()


def _require_env(name: str) -> str:
    """Return the value of env var `name`, or raise a clear error if it's not set.
    Use this in place of os.getenv(name, '<literal>') — fallbacks bake live
    credentials into committed code, which means git history leaks them forever
    even after you delete the line."""
    v = os.getenv(name)
    if not v:
        raise RuntimeError(
            f"Required env var {name!r} is not set. "
            f"Add it to .env (local) or the Cloud Run service env (prod). "
            f"See .env.example at the repo root for the full list."
        )
    return v


# Supabase Connection String (Pooler — IPv4 compatible). Read from env only; no
# in-code fallback — the password belongs in .env / Cloud Run env config.
DB_URL = _require_env("DB_URL")

def get_conn():
    return psycopg2.connect(DB_URL)

# ── Sprint 83 — connection pool for hot paths (chat/tasks). A fresh cloud-Postgres
# connection per call was the main latency source. pget()/pput() reuse warm
# connections; only the converted hot functions use them (rest keep get_conn()). ──
import psycopg2.pool as _pgpool
_POOL = None
def _pool():
    global _POOL
    if _POOL is None:
        try:
            _POOL = _pgpool.ThreadedConnectionPool(1, 16, DB_URL)
        except Exception as e:
            print(f"[pool init] {e}")
            _POOL = False   # fall back to direct connections
    return _POOL
import time as _time
_PREPING_AFTER_IDLE = 20.0   # seconds; only validate connections idle longer than this

def pget():
    p = _pool()
    if not p:
        return psycopg2.connect(DB_URL)
    # Cloud Postgres drops idle connections, but the pool doesn't know — a handed-out
    # dead connection raises "server closed the connection unexpectedly" on first use
    # (→ 500s after the app sits idle). Pre-ping ONLY connections that have been idle a
    # while (recently-used ones are almost certainly alive), so hot paths stay fast.
    for _ in range(4):
        conn = p.getconn()
        idle = _time.time() - getattr(conn, "_last_used", 0)
        if idle < _PREPING_AFTER_IDLE:
            return conn
        try:
            cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone(); cur.close()
            return conn
        except Exception:
            try: p.putconn(conn, close=True)   # discard the dead connection
            except Exception:
                try: conn.close()
                except Exception: pass
    # Pool unhealthy → fall back to a fresh direct connection so the request still works.
    return psycopg2.connect(DB_URL)
def pput(conn, bad=False):
    p = _pool()
    if not p:
        try: conn.close()
        except Exception: pass
        return
    try:
        try: conn._last_used = _time.time()   # stamp so pget can skip pre-ping when fresh
        except Exception: pass
        p.putconn(conn, close=bad)
    except Exception:
        try: conn.close()
        except Exception: pass

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
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS voucher_type TEXT")  # Sprint 35
        # GST breakdown — makes invoice totals self-verifying (total = taxable + cgst + sgst + igst)
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS taxable_value REAL")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cgst_amount REAL")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS sgst_amount REAL")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS igst_amount REAL")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP")  # soft archive
        # Speeds up the Vouchers list invoice SELECT (ORDER BY created_at DESC, scoped by company)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoices_company_created ON invoices(company_name, created_at DESC)")
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
        # Sprint 7 — audit trail (who created this voucher / TDS deduction)
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS created_by TEXT",
        # Edit-voucher: local edits flag a voucher dirty until it's re-pushed to Tally
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS needs_resync BOOLEAN DEFAULT FALSE",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS last_edited_at TIMESTAMP",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS last_edited_by TEXT",
        # Sticky origin + zero-dup: origin ('yantrai'|'tally'|NULL=tally); yantrai_uid links a
        # Tally sync-back back to the originating YantrAI invoice (invoices.id) via the round-trip marker.
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS origin TEXT",
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS yantrai_uid TEXT",
        # Soft archive (user-driven; SEPARATE from discarded_at which hides AI-duplicates).
        # Archived rows are hidden from the default list but kept on file + restorable.
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP",
        "ALTER TABLE tds_deductions ADD COLUMN IF NOT EXISTS created_by TEXT",
        "CREATE INDEX IF NOT EXISTS idx_tally_voucher_date ON tally_vouchers(company_name, date)",
        "CREATE INDEX IF NOT EXISTS idx_tally_voucher_type ON tally_vouchers(company_name, voucher_type)",
        "CREATE INDEX IF NOT EXISTS idx_tally_voucher_master_id ON tally_vouchers(company_name, tally_master_id) WHERE tally_master_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_tally_vouchers_yuid ON tally_vouchers(company_name, yantrai_uid) WHERE yantrai_uid IS NOT NULL",
    ]
    for stmt in deep_alters:
        try:
            cursor.execute("SAVEPOINT sp")
            cursor.execute(stmt)
            cursor.execute("RELEASE SAVEPOINT sp")
        except Exception as e:
            cursor.execute("ROLLBACK TO SAVEPOINT sp")
            print(f"Migration warning ({stmt[:60]}…): {e}")

    # One-time (idempotent) backfill: tag legacy Tally rows that share an EXACT voucher
    # number with a synced YantrAI invoice as origin='yantrai'. Number-exact only = safe
    # (no fuzzy party/amount guessing). Going forward the [YAI:<id>] marker handles this
    # precisely, and read-time dedup already hides matched twins. Only touches origin-NULL rows.
    try:
        cursor.execute("SAVEPOINT sp_origin_bf")
        cursor.execute("""
            UPDATE tally_vouchers tv SET origin = 'yantrai'
            WHERE tv.origin IS NULL AND COALESCE(tv.yantrai_uid, '') = ''
              AND tv.voucher_number IS NOT NULL AND tv.voucher_number <> ''
              AND EXISTS (
                SELECT 1 FROM invoices i
                WHERE i.company_name = tv.company_name
                  AND COALESCE(i.status, '') = 'synced'
                  AND i.invoice_number IS NOT NULL AND i.invoice_number <> ''
                  AND LOWER(i.invoice_number) = LOWER(tv.voucher_number)
              )
        """)
        cursor.execute("RELEASE SAVEPOINT sp_origin_bf")
    except Exception as e:
        cursor.execute("ROLLBACK TO SAVEPOINT sp_origin_bf")
        print(f"origin backfill warning: {e}")

    # ── 360° Bank — statement upload metadata ──────────────────────
    bank_ddl = [
        """CREATE TABLE IF NOT EXISTS bank_statement_uploads (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            company_name TEXT,
            file_url TEXT NOT NULL,
            original_name TEXT NOT NULL,
            bank_ledger TEXT,
            period_from DATE,
            period_to DATE,
            line_count INTEGER,
            total_credit NUMERIC(15,2),
            total_debit NUMERIC(15,2),
            sha256 TEXT,
            uploaded_by TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bsup_company ON bank_statement_uploads(company_id, uploaded_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bsup_sha ON bank_statement_uploads(company_id, sha256)",

        # ── 360° Bank — canonical bank transactions ────────────────
        """CREATE TABLE IF NOT EXISTS bank_transactions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            company_name TEXT NOT NULL,
            source TEXT NOT NULL,
            source_record_id UUID,
            source_file_id UUID REFERENCES bank_statement_uploads(id) ON DELETE SET NULL,
            source_row_idx INTEGER,
            source_payload JSONB,
            date DATE NOT NULL,
            value_date DATE,
            description TEXT,
            reference TEXT,
            amount NUMERIC(15,2) NOT NULL,
            currency TEXT DEFAULT 'INR',
            bank_ledger TEXT,
            party TEXT,
            head TEXT,
            voucher_type TEXT,
            instrument_type TEXT,
            instrument_number TEXT,
            payment_favouring TEXT,
            status TEXT DEFAULT 'unmatched',
            confidence NUMERIC(4,3) DEFAULT 0,
            rationale TEXT,
            match_reason TEXT,
            linked_id UUID,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bank_tx_company_date ON bank_transactions(company_id, date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bank_tx_source ON bank_transactions(company_id, source)",
        "CREATE INDEX IF NOT EXISTS idx_bank_tx_status ON bank_transactions(company_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_bank_tx_amount ON bank_transactions(company_id, amount)",
        "CREATE INDEX IF NOT EXISTS idx_bank_tx_linked ON bank_transactions(linked_id) WHERE linked_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_tx_tally_source ON bank_transactions(company_id, source, source_record_id, bank_ledger) WHERE source='tally' AND source_record_id IS NOT NULL",
        # Sprint 11 — actor flags for "Reconciled By" column
        "ALTER TABLE bank_transactions ADD COLUMN IF NOT EXISTS ai_touched    BOOLEAN DEFAULT FALSE",
        "ALTER TABLE bank_transactions ADD COLUMN IF NOT EXISTS human_touched BOOLEAN DEFAULT FALSE",

        # ── Sync run log ───────────────────────────────────────────
        """CREATE TABLE IF NOT EXISTS bank_sync_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            company_name TEXT,
            run_type TEXT,           -- 'manual_sync' | 'tally_hook' | 'statement_upload' | 'relink'
            tally_inserted INTEGER DEFAULT 0,
            tally_skipped INTEGER DEFAULT 0,
            invoices_inserted INTEGER DEFAULT 0,
            statement_inserted INTEGER DEFAULT 0,
            statement_skipped INTEGER DEFAULT 0,
            linked_pairs INTEGER DEFAULT 0,
            triggered_by TEXT,       -- user_id or 'system'
            notes TEXT,
            ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        # ALTER for already-deployed tables that lack statement_skipped
        "ALTER TABLE bank_sync_runs ADD COLUMN IF NOT EXISTS statement_skipped INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_bank_sync_runs_company ON bank_sync_runs(company_id, ran_at DESC)",

        # ── Voucher drafts (multi-file upload + review pipeline) ──
        """CREATE TABLE IF NOT EXISTS voucher_drafts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            company_name TEXT NOT NULL,
            source_file_url TEXT,
            source_file_name TEXT,
            source_file_type TEXT,
            parsed_payload JSONB NOT NULL,
            reviewed_payload JSONB,
            voucher_type TEXT,
            status TEXT DEFAULT 'ready_for_review',
            ai_confidence NUMERIC(4,3),
            duplicate_of UUID,
            posted_voucher_id UUID,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_voucher_drafts_company_status ON voucher_drafts(company_id, status, created_at DESC)",

        # ── SPRINT 26 — AI Gap Scan: suggestions table + tombstone column ──
        """CREATE TABLE IF NOT EXISTS voucher_ai_suggestions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            voucher_id      UUID NOT NULL REFERENCES tally_vouchers(id) ON DELETE CASCADE,
            company_id      UUID,
            company_name    TEXT,
            gap_type        TEXT NOT NULL,
            field           TEXT,
            current_value   TEXT,
            suggested_value TEXT,
            confidence      NUMERIC(4,3) DEFAULT 0,
            source          TEXT,
            rationale       TEXT,
            payload         JSONB,
            status          TEXT DEFAULT 'pending',
            scan_run_id     UUID,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_vais_voucher ON voucher_ai_suggestions(voucher_id, gap_type)",
        "CREATE INDEX IF NOT EXISTS idx_vais_company_status ON voucher_ai_suggestions(company_id, status, gap_type)",
        # Tombstone column for soft-discard of duplicate vouchers (no hard DELETE).
        "ALTER TABLE tally_vouchers ADD COLUMN IF NOT EXISTS discarded_at TIMESTAMP",

        # ── SPRINT 27 — Master AI Gap Scan: same propose-only pattern for Party + Item ──
        """CREATE TABLE IF NOT EXISTS master_ai_suggestions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            master_type     TEXT NOT NULL,           -- 'party' | 'item'
            record_id       UUID NOT NULL,           -- tally_ledgers.id OR tally_stock_items.id
            company_id      UUID,
            company_name    TEXT,
            gap_type        TEXT NOT NULL,
            field           TEXT,
            current_value   TEXT,
            suggested_value TEXT,
            confidence      NUMERIC(4,3) DEFAULT 0,
            source          TEXT,
            rationale       TEXT,
            payload         JSONB,
            status          TEXT DEFAULT 'pending',
            scan_run_id     UUID,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mais_company_status ON master_ai_suggestions(company_name, master_type, status, gap_type)",
        "CREATE INDEX IF NOT EXISTS idx_mais_record       ON master_ai_suggestions(record_id, gap_type)",

        # ── SPRINT 28 — Tally outbox + bridge-agent heartbeat ──
        """CREATE TABLE IF NOT EXISTS tally_outbox (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            invoice_id      UUID,
            voucher_id      UUID,
            company_id      UUID,
            company_name    TEXT,
            payload         JSONB NOT NULL,
            state           TEXT DEFAULT 'pending',          -- pending | pushing | pushed | error | cancelled
            attempts        INTEGER DEFAULT 0,
            last_error      TEXT,
            tally_voucher_guid TEXT,
            enqueued_by     TEXT,
            enqueued_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pushed_at       TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tally_outbox_pending ON tally_outbox(company_name, state, enqueued_at)",
        "CREATE INDEX IF NOT EXISTS idx_tally_outbox_invoice ON tally_outbox(invoice_id)",
        """CREATE TABLE IF NOT EXISTS tally_bridge_heartbeat (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_name    TEXT NOT NULL UNIQUE,
            agent_version   TEXT,
            ip              TEXT,
            last_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        # Sprint 33 — Persistent log of vouchers the user must manually delete
        # inside Tally Prime (e.g. wrongly-synced rows removed from YantrAI).
        # Survives voucher deletion so the audit trail + cleanup checklist stays.
        """CREATE TABLE IF NOT EXISTS tally_cleanup_log (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_name    TEXT NOT NULL,
            voucher_number  TEXT,
            voucher_type    TEXT,
            party           TEXT,
            amount          NUMERIC,
            voucher_date    DATE,
            reason          TEXT,
            status          TEXT DEFAULT 'pending',   -- pending | done
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            done_at         TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tally_cleanup_company ON tally_cleanup_log(company_name, status, created_at)",
        # Reuse this table for "things to do in Tally Prime": delete a voucher (default)
        # OR create a missing GST/system ledger. kind discriminates; ledger_name holds the ledger.
        "ALTER TABLE tally_cleanup_log ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'delete_voucher'",
        "ALTER TABLE tally_cleanup_log ADD COLUMN IF NOT EXISTS ledger_name TEXT",

        # ── SPRINT 4 — GSTR Reconciliation ──────────────────────────
        """CREATE TABLE IF NOT EXISTS gstr_filings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            company_name TEXT,
            period TEXT,
            return_type TEXT,
            source_file_url TEXT,
            source_file_name TEXT,
            sha256 TEXT,
            payload JSONB,
            match_summary JSONB,
            filed_at TIMESTAMP,
            status TEXT DEFAULT 'draft',
            uploaded_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gstr_filings_company_period ON gstr_filings(company_id, period, return_type)",

        """CREATE TABLE IF NOT EXISTS gstr_reco_lines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            filing_id UUID REFERENCES gstr_filings(id) ON DELETE CASCADE,
            company_id UUID,
            portal_row JSONB,
            matched_voucher_id UUID,
            match_status TEXT,
            match_diff JSONB,
            itc_eligible BOOLEAN,
            rationale TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gstr_reco_lines_filing ON gstr_reco_lines(filing_id, match_status)",

        # ── SPRINT 5 — Filing deadlines + audit wizard notes ────────
        """CREATE TABLE IF NOT EXISTS filing_deadlines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            filing_type TEXT NOT NULL,
            period TEXT,
            due_date DATE NOT NULL,
            description TEXT,
            fy TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_filing_deadlines_company_due ON filing_deadlines(company_id, due_date)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_filing_deadlines ON filing_deadlines(COALESCE(company_id::text, ''), filing_type, COALESCE(period, ''))",

        """CREATE TABLE IF NOT EXISTS audit_wizard_notes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            fy TEXT,
            check_id TEXT NOT NULL,
            user_status TEXT,
            note TEXT,
            updated_by TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_wizard ON audit_wizard_notes(COALESCE(company_id::text,''), fy, check_id)",

        # ── SPRINT 6 — TDS Filing + 26AS ────────────────────────────
        """CREATE TABLE IF NOT EXISTS tds_sections (
            code TEXT PRIMARY KEY,
            description TEXT,
            rate_individual NUMERIC(5,2),
            rate_company NUMERIC(5,2),
            threshold NUMERIC(12,2),
            annual_threshold NUMERIC(12,2),
            is_active BOOLEAN DEFAULT TRUE
        )""",

        """CREATE TABLE IF NOT EXISTS tds_deductions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            company_name TEXT,
            voucher_id UUID,
            party_name TEXT,
            party_pan TEXT,
            party_aadhaar_linked BOOLEAN,
            section TEXT,
            gross_amount NUMERIC(12,2),
            tds_amount NUMERIC(12,2),
            rate_applied NUMERIC(5,2),
            deduction_date DATE,
            challan_number TEXT,
            challan_date DATE,
            bsr_code TEXT,
            deposited BOOLEAN DEFAULT FALSE,
            quarter TEXT,
            fy TEXT,
            return_filed BOOLEAN DEFAULT FALSE,
            return_filing_id UUID,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tds_deductions_company_q ON tds_deductions(company_id, fy, quarter)",

        """CREATE TABLE IF NOT EXISTS tds_returns (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            company_name TEXT,
            return_form TEXT,
            quarter TEXT,
            fy TEXT,
            total_tds NUMERIC(12,2),
            total_deductees INTEGER,
            filed_at TIMESTAMP,
            status TEXT DEFAULT 'draft',
            acknowledgement_no TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",

        """CREATE TABLE IF NOT EXISTS form_26as_imports (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id UUID,
            company_name TEXT,
            fy TEXT,
            source_file_url TEXT,
            source_file_name TEXT,
            sha256 TEXT,
            total_tds_credit NUMERIC(12,2),
            parsed_payload JSONB,
            match_summary JSONB,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    for stmt in bank_ddl:
        try:
            cursor.execute("SAVEPOINT bsp")
            cursor.execute(stmt)
            cursor.execute("RELEASE SAVEPOINT bsp")
        except Exception as e:
            cursor.execute("ROLLBACK TO SAVEPOINT bsp")
            print(f"Bank DDL warning ({stmt[:60]}…): {e}")

    # Seed TDS sections (idempotent — INSERT … ON CONFLICT DO NOTHING)
    tds_seed = [
        ('192',  'Salary', 0.0, 0.0, 0, 0),
        ('194A', 'Interest other than securities', 10.0, 10.0, 5000, 50000),
        ('194C', 'Payment to contractors', 1.0, 2.0, 30000, 100000),
        ('194H', 'Commission or brokerage', 5.0, 5.0, 15000, 0),
        ('194I', 'Rent (land/building)', 10.0, 10.0, 240000, 0),
        ('194J', 'Professional / technical services', 10.0, 10.0, 30000, 0),
        ('194Q', 'Purchase of goods', 0.1, 0.1, 5000000, 0),
        ('195',  'Payments to non-residents', 0.0, 0.0, 0, 0),
    ]
    for code, desc, ri, rc, thr, athr in tds_seed:
        try:
            cursor.execute("SAVEPOINT tdss")
            cursor.execute("""INSERT INTO tds_sections (code, description, rate_individual, rate_company, threshold, annual_threshold)
                              VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (code) DO NOTHING""",
                           (code, desc, ri, rc, thr, athr))
            cursor.execute("RELEASE SAVEPOINT tdss")
        except Exception as e:
            cursor.execute("ROLLBACK TO SAVEPOINT tdss")
            print(f"TDS seed warning ({code}): {e}")

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

    cursor.execute("ALTER TABLE tally_ledgers ADD COLUMN IF NOT EXISTS display_name TEXT")
    cursor.execute("ALTER TABLE tally_stock_items ADD COLUMN IF NOT EXISTS display_name TEXT")

    # Sprint 40 — fully incremental sync. AlterId watermark per row + Tally GUID for
    # dedup/upsert + discarded_at for soft-delete (parallel to tally_vouchers).
    for _m in ("tally_ledgers", "tally_groups", "tally_stock_items"):
        cursor.execute(f"ALTER TABLE {_m} ADD COLUMN IF NOT EXISTS alter_id BIGINT DEFAULT 0")
        cursor.execute(f"ALTER TABLE {_m} ADD COLUMN IF NOT EXISTS tally_master_guid TEXT")
        cursor.execute(f"ALTER TABLE {_m} ADD COLUMN IF NOT EXISTS discarded_at TIMESTAMP")
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_m}_company_live ON {_m}(company_name) WHERE discarded_at IS NULL")

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

    # First-hand, verbatim Tally XML per record (vouchers/ledgers/groups/stock items).
    # The structured tally_* tables are a curated/parsed view; this keeps the ORIGINAL
    # so future training can re-parse fields we don't extract today. Plain text (Tally
    # XML is small — a ~3k-voucher company is only a few MB). Upsert by dedupe_key.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tally_raw (
        id UUID PRIMARY KEY,
        company_name TEXT NOT NULL,
        company_id UUID,
        entity_type TEXT NOT NULL,      -- 'voucher' | 'ledger' | 'group' | 'stock_item'
        dedupe_key TEXT NOT NULL,       -- Tally GUID, else voucher number / name
        tally_guid TEXT,
        alter_id BIGINT,                -- Tally change counter (version), when available
        raw_xml TEXT,                   -- verbatim Tally XML element
        captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (company_name, entity_type, dedupe_key)
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tally_raw_company ON tally_raw(company_name, entity_type)")

    # Windows Agent durable login — long-lived device token (valid until revoked).
    # Lets the agent auto-resume on boot (PC reboot + server restart) without re-entering
    # a password. Only the device_token is persisted; passwords are never stored.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS agent_devices (
        device_token TEXT PRIMARY KEY,
        user_id UUID,
        username TEXT,
        company_id UUID,
        company_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TIMESTAMP,
        revoked BOOLEAN DEFAULT FALSE
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_devices_user ON agent_devices(user_id)")

    # Persistent agent SESSION store (Sprint 35). Previously sessions lived in an
    # in-memory dict on the server, so every deploy/restart wiped them → all agents
    # silently 401'd (red dot + sync stalls) until they re-authed. Persisting here
    # lets a fresh process validate existing tokens, so deploys no longer log agents out.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS agent_sessions (
        token TEXT PRIMARY KEY,
        user_id UUID,
        username TEXT,
        name TEXT,
        is_super_admin BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP NOT NULL
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_expires ON agent_sessions(expires_at)")

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
    # Sprint 57 — "Chat with YantrAI": trackable task code + structured Problem Doc.
    for _col, _type in (("task_code", "TEXT"), ("title", "TEXT"), ("category", "TEXT"),
                        ("priority", "TEXT"), ("created_by", "TEXT"),
                        ("source", "TEXT"), ("pd", "JSONB")):
        try:
            cursor.execute(f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {_col} {_type}")
        except Exception as _e:
            print(f"[tasks col {_col}] {_e}")
    try:
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_task_code ON tasks(task_code)")
    except Exception as _e:
        print(f"[tasks task_code idx] {_e}")

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

    # ========================================================================
    # MULTI-TENANT SCHEMA (Phase A) — added 2026-05
    # New tables: users, organizations, companies, memberships, tenant_audit_log
    # Old tables get a company_id UUID column (kept alongside company_name).
    # Each CREATE wrapped in its own savepoint so one failure doesn't poison the batch.
    # ========================================================================
    mt_ddls = [
        ("users", """
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT,
            email TEXT,
            phone TEXT,
            pan TEXT,
            is_super_admin BOOLEAN DEFAULT FALSE,
            default_membership_id UUID,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );"""),
        # If 'users' already existed with old schema, add missing columns
        ("users_add_name",  "ALTER TABLE users ADD COLUMN IF NOT EXISTS name TEXT;"),
        ("users_add_email", "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;"),
        ("users_add_phone", "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;"),
        ("users_add_pan",   "ALTER TABLE users ADD COLUMN IF NOT EXISTS pan TEXT;"),
        ("users_add_is_super", "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN DEFAULT FALSE;"),
        ("users_add_default_mem", "ALTER TABLE users ADD COLUMN IF NOT EXISTS default_membership_id UUID;"),
        ("organizations", """
        CREATE TABLE IF NOT EXISTS organizations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('firm', 'company')),
            gstin TEXT,
            address TEXT,
            state_code TEXT,
            plan TEXT DEFAULT 'free',
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            archived_at TIMESTAMP
        );"""),
        ("companies", """
        CREATE TABLE IF NOT EXISTS companies (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            gstin TEXT,
            state_code TEXT,
            fiscal_year_start DATE DEFAULT '2026-04-01',
            currency TEXT DEFAULT 'INR',
            is_primary BOOLEAN DEFAULT FALSE,
            client_owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            archived_at TIMESTAMP,
            UNIQUE(org_id, name)
        );"""),
        ("memberships", """
        CREATE TABLE IF NOT EXISTS memberships (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'accountant', 'junior', 'viewer')),
            scope_company_ids JSONB,
            invited_by UUID REFERENCES users(id) ON DELETE SET NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, org_id)
        );"""),
        ("tenant_audit_log", """
        CREATE TABLE IF NOT EXISTS tenant_audit_log (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            entity_type TEXT,
            entity_id TEXT,
            company_id UUID,
            org_id UUID,
            payload JSONB,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );"""),
    ]
    for tname, ddl in mt_ddls:
        try:
            cursor.execute(f"SAVEPOINT sp_mt_{tname};")
            cursor.execute(ddl)
            cursor.execute(f"RELEASE SAVEPOINT sp_mt_{tname};")
        except Exception as e:
            print(f"Multi-tenant DDL warning ({tname}): {e}")
            try: cursor.execute(f"ROLLBACK TO SAVEPOINT sp_mt_{tname};")
            except: pass

    # Indexes
    mt_indexes = [
        ("idx_memberships_user", "memberships(user_id)"),
        ("idx_memberships_org", "memberships(org_id)"),
        ("idx_companies_org", "companies(org_id)"),
        ("idx_tenant_audit_user", "tenant_audit_log(user_id)"),
        ("idx_tenant_audit_company", "tenant_audit_log(company_id)"),
    ]
    for idx_name, target in mt_indexes:
        try:
            cursor.execute(f"SAVEPOINT sp_idx_{idx_name};")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {target};")
            cursor.execute(f"RELEASE SAVEPOINT sp_idx_{idx_name};")
        except Exception as e:
            print(f"Index warning ({idx_name}): {e}")
            try: cursor.execute(f"ROLLBACK TO SAVEPOINT sp_idx_{idx_name};")
            except: pass

    # Add company_id UUID column to every tenant-scoped table (idempotent, per-table savepoint)
    tenant_tables = [
        'invoices', 'parties', 'tally_vouchers', 'tally_ledgers',
        'tally_stock_items', 'tally_groups', 'tally_cost_centres',
        'tally_voucher_types', 'tally_sync_log', 'tasks',
        'recon_templates', 'recon_sessions', 'chat_sessions'
    ]
    for tbl in tenant_tables:
        try:
            cursor.execute("SAVEPOINT sp_add_cid;")
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS company_id UUID;")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{tbl}_company_id ON {tbl}(company_id);")
            cursor.execute("RELEASE SAVEPOINT sp_add_cid;")
        except Exception as e:
            print(f"company_id add warning ({tbl}): {e}")
            cursor.execute("ROLLBACK TO SAVEPOINT sp_add_cid;")

    # Mark sensitive ledgers (auto-flag by name pattern)
    try:
        cursor.execute("ALTER TABLE tally_ledgers ADD COLUMN IF NOT EXISTS is_sensitive BOOLEAN DEFAULT FALSE;")
    except Exception as e:
        print(f"is_sensitive add warning: {e}")

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

# ── Windows Agent durable device tokens ───────────────────────────────────────
def create_agent_device(user_id, username, company_id=None, company_name=None):
    """Mint a long-lived device token (valid until revoked) and persist it.
    Returns the new device_token string, or None on failure."""
    import secrets as _secrets
    token = "dv_" + _secrets.token_hex(32)
    conn = pget()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO agent_devices (device_token, user_id, username, company_id, company_name, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """, (token, user_id, username, company_id, company_name))
        conn.commit()
        return token
    except Exception as e:
        print(f"create_agent_device error: {e}")
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        cur.close(); pput(conn)

def get_agent_device(token):
    """Return the device row (dict) only if it exists and is not revoked, else None."""
    if not token:
        return None
    conn = pget()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT device_token, user_id, username, company_id, company_name,
                   created_at, last_seen_at, revoked
            FROM agent_devices
            WHERE device_token = %s AND revoked = FALSE
        """, (token,))
        return cur.fetchone()
    except Exception as e:
        print(f"get_agent_device error: {e}")
        return None
    finally:
        cur.close(); pput(conn)

def touch_agent_device(token):
    """Update last_seen_at on a device token (best-effort)."""
    if not token:
        return
    conn = pget()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE agent_devices SET last_seen_at = CURRENT_TIMESTAMP WHERE device_token = %s", (token,))
        conn.commit()
    except Exception as e:
        print(f"touch_agent_device error: {e}")
        try: conn.rollback()
        except Exception: pass
    finally:
        cur.close(); pput(conn)

def bind_agent_device(token, company_id=None, company_name=None):
    """Attach the chosen company to a device token (called after the company picker).
    Returns True if a non-revoked row was updated."""
    if not token:
        return False
    conn = pget()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE agent_devices
            SET company_id = %s, company_name = %s, last_seen_at = CURRENT_TIMESTAMP
            WHERE device_token = %s AND revoked = FALSE
        """, (company_id, company_name, token))
        ok = cur.rowcount > 0
        conn.commit()
        return ok
    except Exception as e:
        print(f"bind_agent_device error: {e}")
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        cur.close(); pput(conn)

def revoke_agent_device(token):
    """Revoke a device token so future /api/agent/resume calls fail. Returns True if updated."""
    if not token:
        return False
    conn = pget()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE agent_devices SET revoked = TRUE WHERE device_token = %s", (token,))
        ok = cur.rowcount > 0
        conn.commit()
        return ok
    except Exception as e:
        print(f"revoke_agent_device error: {e}")
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        cur.close(); pput(conn)

# ── Persistent agent sessions (survive server restarts/deploys) ──────────────
def db_create_agent_session(token, user_id, username=None, name=None,
                            is_super_admin=False, ttl_seconds=86400):
    """Persist (or refresh) an agent session token. Mirrors the old in-memory entry
    {user_id, username, name, is_super_admin, expires_at} but in the DB so deploys
    don't wipe it."""
    if not token:
        return False
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO agent_sessions (token, user_id, username, name, is_super_admin, expires_at)
            VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))
            ON CONFLICT (token) DO UPDATE
              SET user_id = EXCLUDED.user_id, username = EXCLUDED.username,
                  name = EXCLUDED.name, is_super_admin = EXCLUDED.is_super_admin,
                  expires_at = EXCLUDED.expires_at
        """, (token, user_id, username, name, bool(is_super_admin), int(ttl_seconds)))
        conn.commit()
        return True
    except Exception as e:
        print(f"db_create_agent_session error: {e}")
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        cur.close(); pput(conn)

def db_validate_agent_session(token, ttl_seconds=86400):
    """Return {user_id, username, name, expires_at} if the token is live, else None.
    Sliding-window: atomically refreshes expires_at on every successful access (so an
    agent that heartbeats regularly never expires). Returns None for missing/expired."""
    if not token:
        return None
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            UPDATE agent_sessions
               SET expires_at = now() + make_interval(secs => %s)
             WHERE token = %s AND expires_at >= now()
            RETURNING user_id, username, name, is_super_admin, expires_at
        """, (int(ttl_seconds), token))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return None
        if row.get("user_id") is not None:
            row["user_id"] = str(row["user_id"])
        return dict(row)
    except Exception as e:
        print(f"db_validate_agent_session error: {e}")
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        cur.close(); pput(conn)

def db_revoke_other_agent_sessions(user_id, keep_token):
    """Enforce ONE latest session per user: delete this user's other session rows,
    keeping only `keep_token`. Called right after minting a fresh session so a stale
    token can never linger and win a race. Returns the number of rows removed."""
    if not user_id or not keep_token:
        return 0
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM agent_sessions WHERE user_id = %s AND token <> %s",
                    (user_id, keep_token))
        n = cur.rowcount
        conn.commit()
        return n
    except Exception as e:
        print(f"db_revoke_other_agent_sessions error: {e}")
        try: conn.rollback()
        except Exception: pass
        return 0
    finally:
        cur.close(); pput(conn)

def db_revoke_agent_session(token):
    """Delete a session token (e.g. on sign-out). Returns True if a row was removed."""
    if not token:
        return False
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM agent_sessions WHERE token = %s", (token,))
        ok = cur.rowcount > 0
        conn.commit()
        return ok
    except Exception as e:
        print(f"db_revoke_agent_session error: {e}")
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        cur.close(); pput(conn)

def get_user_by_username(username: str):
    conn = pget()
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
        try: conn.rollback()      # release read snapshot before returning to pool
        except Exception: pass
        pput(conn)


_COMPANY_FILES_READY = False

def _ensure_company_files():
    """Sprint 55 — 'Upload anything': a company-scoped file library. Sprint 86 — also
    holds workspace-level UNALLOCATED files (shared in from other apps): company_name
    NULL + allocated=false + org_id scope, until the user sorts them into a company."""
    global _COMPANY_FILES_READY
    if _COMPANY_FILES_READY:
        return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS company_files (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                company_name TEXT NOT NULL,
                file_url TEXT NOT NULL,
                original_name TEXT,
                file_type TEXT,
                size_bytes BIGINT,
                uploaded_by TEXT,
                archived_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_company_files_co ON company_files(company_name, created_at DESC)")
        # Unallocated-inbox columns (additive; existing rows default to allocated=true).
        try: cur.execute("ALTER TABLE company_files ALTER COLUMN company_name DROP NOT NULL")
        except Exception: pass
        cur.execute("ALTER TABLE company_files ADD COLUMN IF NOT EXISTS allocated BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE company_files ADD COLUMN IF NOT EXISTS org_id UUID")
        cur.execute("ALTER TABLE company_files ADD COLUMN IF NOT EXISTS suggested_company TEXT")
        cur.execute("ALTER TABLE company_files ADD COLUMN IF NOT EXISTS suggest_status TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_company_files_unalloc ON company_files(org_id, allocated)")
        conn.commit(); cur.close(); conn.close()
        _COMPANY_FILES_READY = True
    except Exception as e:
        print(f"[_ensure_company_files] {e}")


def save_unallocated_file(org_id, file_url, original_name=None, file_type=None,
                          size_bytes=None, uploaded_by=None, suggest_status="none"):
    """Store a shared-in file in the workspace inbox (no company yet)."""
    _ensure_company_files()
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""INSERT INTO company_files
                         (company_name, file_url, original_name, file_type, size_bytes,
                          uploaded_by, org_id, allocated, suggest_status)
                       VALUES (NULL,%s,%s,%s,%s,%s,%s,FALSE,%s)
                       RETURNING id, file_url, original_name, file_type, size_bytes, created_at""",
                    (file_url, original_name, file_type, size_bytes, uploaded_by, str(org_id), suggest_status))
        row = cur.fetchone(); conn.commit(); return row
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[save_unallocated_file] {e}"); return None
    finally:
        cur.close(); pput(conn)


def list_unallocated_files(org_id):
    """Workspace inbox — unallocated files for an org (shown across all its companies)."""
    _ensure_company_files()
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT id, file_url, original_name, file_type, size_bytes, uploaded_by,
                              created_at, suggested_company, suggest_status
                       FROM company_files
                       WHERE org_id=%s AND allocated=FALSE AND archived_at IS NULL
                       ORDER BY created_at DESC""", (str(org_id),))
        return cur.fetchall()
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


def allocate_file(file_id, org_id, company_name):
    """Assign an inbox file to a company (must belong to the same org)."""
    _ensure_company_files()
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM companies WHERE org_id=%s AND name=%s AND archived_at IS NULL",
                    (str(org_id), company_name))
        if not cur.fetchone():
            return {"ok": False, "error": "That company isn't in this workspace."}
        cur.execute("""UPDATE company_files SET company_name=%s, allocated=TRUE
                       WHERE id=%s AND org_id=%s AND allocated=FALSE""",
                    (company_name, str(file_id), str(org_id)))
        ok = cur.rowcount > 0; conn.commit()
        return {"ok": ok} if ok else {"ok": False, "error": "File not found in the inbox."}
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[allocate_file] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); pput(conn)


def set_file_suggestion(file_id, suggested_company, status="done"):
    """Record the AI's best-guess company for an inbox file (background pass)."""
    _ensure_company_files()
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("UPDATE company_files SET suggested_company=%s, suggest_status=%s WHERE id=%s",
                    (suggested_company, status, str(file_id)))
        conn.commit(); return True
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[set_file_suggestion] {e}"); return False
    finally:
        cur.close(); pput(conn)


def save_company_file(company_name, file_url, original_name=None, file_type=None,
                      size_bytes=None, uploaded_by=None):
    _ensure_company_files()
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""INSERT INTO company_files
                         (company_name, file_url, original_name, file_type, size_bytes, uploaded_by)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       RETURNING id, file_url, original_name, file_type, size_bytes, created_at""",
                    (company_name, file_url, original_name, file_type, size_bytes, uploaded_by))
        row = cur.fetchone(); conn.commit(); return row
    finally:
        cur.close(); conn.close()


def list_company_files(company_name):
    _ensure_company_files()
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT id, file_url, original_name, file_type, size_bytes, uploaded_by, created_at
                       FROM company_files
                       WHERE company_name=%s AND archived_at IS NULL
                       ORDER BY created_at DESC""", (company_name,))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def archive_company_file(file_id, company_name):
    _ensure_company_files()
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE company_files SET archived_at=CURRENT_TIMESTAMP WHERE id=%s AND company_name=%s",
                    (file_id, company_name))
        ok = cur.rowcount > 0; conn.commit(); return ok
    except Exception as e:
        conn.rollback(); print(f"[archive_company_file] {e}"); return False
    finally:
        cur.close(); conn.close()


# ── Lead Generation — a generation run (batch) + its real-business leads. The
# status/action_taken columns are the seams for a future end-to-end CRM. ──
LEAD_STATUSES = ("new", "contacted", "qualified", "won", "lost")

def _ensure_leads_schema():
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_batches (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id TEXT,
                company_name TEXT,
                context_text TEXT,
                search_params JSONB DEFAULT '{}',
                source TEXT,
                lead_count INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                batch_id UUID REFERENCES lead_batches(id) ON DELETE CASCADE,
                user_id TEXT,
                company_name TEXT,
                source TEXT,
                name TEXT,
                business_name TEXT,
                category TEXT,
                address TEXT,
                city TEXT,
                phone TEXT,
                website TEXT,
                email TEXT,
                rating REAL,
                score INT,
                why_fit TEXT,
                status TEXT DEFAULT 'new',
                action_taken JSONB DEFAULT '{}',
                raw_json JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_user ON leads(user_id, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads(batch_id)")
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[_ensure_leads_schema] {e}")


def insert_lead_batch(user_id, company_name, context_text, search_params, source):
    _ensure_leads_schema()
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO lead_batches (user_id, company_name, context_text, search_params, source)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (user_id, company_name, context_text, json.dumps(search_params or {}), source))
        bid = cur.fetchone()[0]; conn.commit(); return str(bid)
    finally:
        cur.close(); conn.close()


def insert_leads(batch_id, user_id, company_name, rows):
    """Bulk-insert normalized+scored lead dicts. Returns the persisted rows."""
    _ensure_leads_schema()
    if not rows:
        return []
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        out = []
        for r in rows:
            cur.execute("""INSERT INTO leads
                             (batch_id, user_id, company_name, source, name, business_name,
                              category, address, city, phone, website, email, rating,
                              score, why_fit, raw_json)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           RETURNING id, source, name, business_name, category, address, city,
                                     phone, website, email, rating, score, why_fit, status,
                                     action_taken, created_at""",
                        (batch_id, user_id, company_name, r.get("source"), r.get("name"),
                         r.get("business_name"), r.get("category"), r.get("address"),
                         r.get("city"), r.get("phone"), r.get("website"), r.get("email"),
                         r.get("rating"), r.get("score"), r.get("why_fit"),
                         json.dumps(r.get("raw_json")) if r.get("raw_json") is not None else None))
            out.append(cur.fetchone())
        cur.execute("UPDATE lead_batches SET lead_count=%s WHERE id=%s", (len(out), batch_id))
        conn.commit(); return out
    except Exception as e:
        conn.rollback(); print(f"[insert_leads] {e}"); return []
    finally:
        cur.close(); conn.close()


def list_leads(user_id, batch_id=None, limit=500):
    _ensure_leads_schema()
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if batch_id:
            cur.execute("""SELECT id, source, name, business_name, category, address, city,
                                  phone, website, email, rating, score, why_fit, status,
                                  action_taken, created_at
                           FROM leads WHERE user_id=%s AND batch_id=%s
                           ORDER BY score DESC NULLS LAST, created_at DESC LIMIT %s""",
                        (user_id, batch_id, limit))
        else:
            cur.execute("""SELECT id, source, name, business_name, category, address, city,
                                  phone, website, email, rating, score, why_fit, status,
                                  action_taken, created_at
                           FROM leads WHERE user_id=%s
                           ORDER BY created_at DESC LIMIT %s""",
                        (user_id, limit))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def list_lead_batches(user_id, limit=50):
    _ensure_leads_schema()
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT id, context_text, source, lead_count, created_at
                       FROM lead_batches WHERE user_id=%s
                       ORDER BY created_at DESC LIMIT %s""", (user_id, limit))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def update_lead_status(lead_id, user_id, status=None, action=None):
    """Update a lead's CRM status and/or merge an action marker into action_taken."""
    _ensure_leads_schema()
    if status and status not in LEAD_STATUSES:
        return None
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""UPDATE leads
                       SET status = COALESCE(%s, status),
                           action_taken = CASE WHEN %s::jsonb IS NULL THEN action_taken
                                               ELSE COALESCE(action_taken,'{}'::jsonb) || %s::jsonb END
                       WHERE id=%s AND user_id=%s
                       RETURNING id, status, action_taken""",
                    (status,
                     json.dumps(action) if action is not None else None,
                     json.dumps(action) if action is not None else None,
                     lead_id, user_id))
        row = cur.fetchone(); conn.commit(); return row
    except Exception as e:
        conn.rollback(); print(f"[update_lead_status] {e}"); return None
    finally:
        cur.close(); conn.close()


def rename_company_file(file_id, company_name, new_name):
    """Sprint 55b — rename a file's display name (original_name); the stored
    file on disk is untouched, so existing links keep working."""
    _ensure_company_files()
    new_name = (new_name or "").strip()
    if not new_name:
        return False
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE company_files SET original_name=%s WHERE id=%s AND company_name=%s AND archived_at IS NULL",
                    (new_name, file_id, company_name))
        ok = cur.rowcount > 0; conn.commit(); return ok
    except Exception as e:
        conn.rollback(); print(f"[rename_company_file] {e}"); return False
    finally:
        cur.close(); conn.close()


def get_user_by_auth_uid(auth_uid):
    """Sprint 51 — map a Supabase auth user (auth.users.id) to our accounting_users row."""
    if not auth_uid:
        return None
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM accounting_users WHERE auth_uid = %s LIMIT 1", (str(auth_uid),))
        return cursor.fetchone()
    except Exception as e:
        print(f"[get_user_by_auth_uid] {e}")
        return None
    finally:
        cursor.close(); conn.close()


def set_email_verified(username, verified=True):
    """Mark a user's email as verified (self-service, from Settings)."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE")
        cur.execute("UPDATE accounting_users SET email_verified=%s WHERE username=%s", (bool(verified), username))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback(); print(f"[set_email_verified] {e}"); return False
    finally:
        cur.close(); conn.close()


def link_auth_uid(username, auth_uid):
    """Store the Supabase auth user id on both identity tables for `username`."""
    _ensure_billing_schema()
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE accounting_users SET auth_uid=%s WHERE username=%s", (str(auth_uid), username))
        cur.execute("UPDATE users SET auth_uid=%s WHERE username=%s", (str(auth_uid), username))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback(); print(f"[link_auth_uid] {e}"); return False
    finally:
        cur.close(); conn.close()

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

def list_all_users():
    """Sprint 24 — super_admin view: list every accounting_users row with derived
    company-access count + activity stats. Companies JSONB is parsed."""
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT id, username, role, name, email, phone, company_name, companies, created_at
            FROM accounting_users
            ORDER BY role DESC, username
        """)
        rows = cursor.fetchall()
        out = []
        for r in rows:
            companies = r.get("companies")
            if isinstance(companies, str):
                try: companies = json.loads(companies)
                except: companies = []
            companies = companies or []
            out.append({
                "id": r["id"], "username": r["username"], "role": r["role"],
                "name": r.get("name"), "email": r.get("email"), "phone": r.get("phone"),
                "company_name": r.get("company_name"),
                "companies": companies,
                "companies_count": len(companies),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
            })
        return out
    finally:
        cursor.close(); conn.close()


def list_all_companies_with_usage():
    """Sprint 24 — super_admin view: every distinct company name appearing in
    accounting_users.companies JSONB, plus row counts in tally_vouchers and
    bank_transactions so the admin can see how 'real' each company is."""
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT DISTINCT TRIM(comp.value::text, '"') AS name
            FROM accounting_users u,
                 LATERAL jsonb_array_elements(COALESCE(u.companies::jsonb, '[]'::jsonb)) AS comp
        """)
        names = sorted({r["name"] for r in cursor.fetchall() if r["name"]})
        # Per-company stats — single query
        cursor.execute("""
            SELECT company_name AS name,
                   (SELECT COUNT(*) FROM tally_vouchers tv      WHERE tv.company_name = c.company_name) AS voucher_count,
                   (SELECT COUNT(*) FROM bank_transactions bt   WHERE bt.company_name = c.company_name) AS bank_tx_count
            FROM (SELECT DISTINCT company_name FROM tally_vouchers
                  UNION SELECT DISTINCT company_name FROM bank_transactions) c
        """)
        stats = {r["name"]: r for r in cursor.fetchall() if r["name"]}
        # Per-company user-access count
        cursor.execute("""
            SELECT TRIM(comp.value::text, '"') AS name, COUNT(*) AS access_count
            FROM accounting_users u,
                 LATERAL jsonb_array_elements(COALESCE(u.companies::jsonb, '[]'::jsonb)) AS comp
            GROUP BY 1
        """)
        access = {r["name"]: r["access_count"] for r in cursor.fetchall() if r["name"]}
        out = []
        for n in names:
            s = stats.get(n, {})
            out.append({
                "name": n,
                "voucher_count": int(s.get("voucher_count") or 0),
                "bank_tx_count": int(s.get("bank_tx_count") or 0),
                "user_access_count": int(access.get(n, 0)),
            })
        # Sort: most-used first
        out.sort(key=lambda c: (c["voucher_count"] + c["bank_tx_count"]), reverse=True)
        return out
    finally:
        cursor.close(); conn.close()


def delete_user_by_username(username: str):
    """Sprint 24 — super_admin destructive op. Cascade: chat_sessions retain
    user_username text (no FK), so a deletion leaves orphan history rows we
    can scrub separately. Returns dict {deleted: bool, message: str}."""
    if not username:
        return {"deleted": False, "message": "Username required."}
    if username == "sadmin":
        return {"deleted": False, "message": "Refusing to delete sadmin (super_admin protection)."}
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM accounting_users WHERE username = %s", (username,))
        n = cursor.rowcount
        conn.commit()
        return {"deleted": bool(n), "rows_affected": n,
                "message": f"User '{username}' deleted." if n else f"No user '{username}' found."}
    except Exception as e:
        return {"deleted": False, "message": str(e)}
    finally:
        cursor.close(); conn.close()


def update_user_role(username: str, new_role: str):
    """Sprint 24 — super_admin. Only allows roles in {'admin','super_admin'}."""
    if new_role not in ("admin", "super_admin"):
        return {"ok": False, "message": f"Invalid role '{new_role}'."}
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE accounting_users SET role = %s WHERE username = %s",
                       (new_role, username))
        conn.commit()
        return {"ok": cursor.rowcount > 0, "rows_affected": cursor.rowcount}
    finally:
        cursor.close(); conn.close()


def remove_company_from_user(username: str, company_name: str):
    """Sprint 24 — super_admin. Pop a company from one user's JSONB list.
    Does NOT delete the company's actual data (vouchers/bank); just revokes
    that user's access."""
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT companies FROM accounting_users WHERE username = %s", (username,))
        row = cursor.fetchone()
        if not row: return {"ok": False, "message": "User not found."}
        comps = row[0]
        if isinstance(comps, str):
            try: comps = json.loads(comps)
            except: comps = []
        comps = [c for c in (comps or []) if c != company_name]
        cursor.execute("UPDATE accounting_users SET companies = %s WHERE username = %s",
                       (json.dumps(comps), username))
        conn.commit()
        return {"ok": True, "remaining": comps}
    finally:
        cursor.close(); conn.close()


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


def _ensure_onboarding_columns():
    """Idempotent — add user_type + users_id link columns to accounting_users."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS user_type TEXT")
        cur.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS users_id UUID")
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[_ensure_onboarding_columns] {e}")


# ============================================================
# Sprint 47 — Token wallet + metered AI billing (per-workspace/org)
# ============================================================
SIGNUP_FREE_TOKENS = 50000   # free grant for a new workspace

def _ensure_billing_schema():
    """Idempotent — org token_balance + usage/ledger/purchases tables."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS token_balance BIGINT DEFAULT 0")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_usage_log (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                org_id UUID, user_id UUID, company_name TEXT,
                action TEXT, model TEXT,
                prompt_tokens INT, output_tokens INT, total_tokens INT,
                tokens_charged BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_org ON ai_usage_log(org_id, created_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS token_ledger (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                org_id UUID NOT NULL,
                delta BIGINT NOT NULL,
                balance_after BIGINT,
                reason TEXT,            -- ai_usage | recharge | grant | admin_adjust
                ref_id TEXT, note TEXT, created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_token_ledger_org ON token_ledger(org_id, created_at DESC)")
        # Sprint 77 — login events (reliable daily-active-by-login for the Data Analyst).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_logins (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                username TEXT, user_id UUID, company_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_logins_day ON user_logins(created_at)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS token_purchases (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                org_id UUID NOT NULL,
                amount_inr NUMERIC(12,2), tokens BIGINT,
                status TEXT DEFAULT 'pending',   -- pending | paid | cancelled
                provider TEXT, provider_ref TEXT,
                created_by TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP
            );""")
        # Sprint 48 — agentic store: agent catalog + per-org installs + per-agent metering.
        # NB: table is `store_agents` (a pre-existing `agents` table holds Tally
        # desktop device-auth and must not be touched).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS store_agents (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL, tagline TEXT, description TEXT,
                icon TEXT, category TEXT,
                status TEXT DEFAULT 'published',     -- published | coming_soon | draft
                publisher TEXT DEFAULT 'first-party',
                token_policy JSONB DEFAULT '{}'::jsonb,
                manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
                sort_order INT DEFAULT 100,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS org_agent_installs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                org_id UUID NOT NULL,
                agent_slug TEXT NOT NULL REFERENCES store_agents(slug),
                installed_by_user_id UUID,
                installed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                enabled BOOLEAN DEFAULT TRUE,
                settings JSONB DEFAULT '{}'::jsonb,
                UNIQUE(org_id, agent_slug)
            );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_oai_org ON org_agent_installs(org_id)")
        cur.execute("ALTER TABLE ai_usage_log ADD COLUMN IF NOT EXISTS agent_slug TEXT")
        # Sprint 50 — developer portal: ownership + per-app auth on store_agents.
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS owner_org_id  UUID")
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS owner_user_id UUID")
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS visibility    TEXT DEFAULT 'public'")
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS client_id     TEXT")
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS signing_key   TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_store_agents_owner ON store_agents(owner_org_id)")
        # App-store: lightweight review gate for public listing of developer apps.
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS review_status TEXT DEFAULT 'none'")
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS review_note   TEXT")
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS reviewed_by   UUID")
        cur.execute("ALTER TABLE store_agents ADD COLUMN IF NOT EXISTS reviewed_at   TIMESTAMP")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_review_log (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_slug TEXT NOT NULL,
                action TEXT NOT NULL,                 -- requested | approved | rejected
                note TEXT,
                actor_user_id UUID,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        # Per-voucher sync events — the DOWNLOAD side of the Vouchers "Event Logs"
        # (upload side already comes from tally_outbox). direction: download|upload.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS voucher_sync_events (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                company_name TEXT,
                voucher_number TEXT,
                tally_master_id TEXT,
                direction TEXT,                       -- download | upload
                action TEXT,                          -- created | updated | pushed | failed
                party TEXT,
                amount NUMERIC,
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vse_company ON voucher_sync_events(company_name, created_at DESC)")
        # Sprint 51 — Supabase Auth: link our identity rows to the auth user (auth.users.id).
        cur.execute("ALTER TABLE users            ADD COLUMN IF NOT EXISTS auth_uid UUID")
        cur.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS auth_uid UUID")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_auth_uid      ON users(auth_uid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_acct_users_auth_uid ON accounting_users(auth_uid)")
        # Sprint — self-service email verification (from Settings). FALSE until the
        # user clicks the magic-link we email them.
        cur.execute("ALTER TABLE accounting_users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE")
        # Sprint 53 — Razorpay: store the gateway order id to match verify/webhook.
        cur.execute("ALTER TABLE token_purchases ADD COLUMN IF NOT EXISTS provider_order_id TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_token_purch_order ON token_purchases(provider_order_id)")
        # Sprint 54 — per-model pricing weight (credits charged per 1,000 tokens) so
        # different models normalise to the same credit currency at a target margin.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS model_pricing (
                model TEXT PRIMARY KEY,
                label TEXT,
                cost_per_1k NUMERIC,          -- our real provider cost, ₹ / 1k tokens (informational)
                markup NUMERIC DEFAULT 3,     -- target gross multiple (informational)
                weight NUMERIC NOT NULL,      -- CREDITS charged per 1,000 tokens (the live knob)
                active BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        # Seed Gemini Flash: cost ~₹0.03/1k × ~3.3 markup → 100 credits/1k (≈₹0.10/1k to user).
        cur.execute("""INSERT INTO model_pricing (model, label, cost_per_1k, markup, weight)
                       VALUES ('gemini-flash-latest','Gemini Flash',0.03,3.3,100)
                       ON CONFLICT (model) DO NOTHING""")
        conn.commit(); cur.close(); conn.close()
        _seed_agents()
    except Exception as e:
        print(f"[_ensure_billing_schema] {e}")


# ---------------------------------------------------------------------------
# Sprint 48 — Agentic store: catalog, installs, per-agent metering
# ---------------------------------------------------------------------------

# The first-party "AI Accountant" agent. Its manifest enumerates the existing
# views (grouped). Platform-level views (training/Connectors, store, users,
# whatsapp, settings) are agent-agnostic and always available — NOT listed here.
AI_ACCOUNTANT_MANIFEST = {
    "version": 1,
    "system_prompt_ref": "kb_default",
    "api_prefixes": ["/chat", "/api/bank", "/api/gstr", "/api/tds", "/api/audit",
                     "/api/recon", "/api/masters", "/api/vouchers"],
    "required_connectors": ["tally"],
    "nav_groups": [
        {"label": "Core", "items": [
            {"view": "aiacc-home", "label": "Apps", "icon": "🗂️"},
            {"view": "chat", "label": "Chat", "icon": "💬"},
            {"view": "vouchers", "label": "Vouchers", "icon": "📑"},
            {"view": "schema", "label": "Masters", "icon": "📋"},
        ]},
        {"label": "Reconciliation", "items": [
            {"view": "bank", "label": "Bank Reco", "icon": "🏦"},
            {"view": "gstr-reco", "label": "GSTR Reco", "icon": "📊", "role_gate": "super_admin"},
            {"view": "recon", "label": "Reconciliation Studio", "icon": "🔄", "role_gate": "super_admin"},
        ]},
        {"label": "Filing & Compliance", "items": [
            {"view": "gstr", "label": "GST Filing", "icon": "🧾", "role_gate": "super_admin"},
            {"view": "tds", "label": "TDS Filing", "icon": "🧑‍💼", "role_gate": "super_admin"},
            {"view": "itr", "label": "ITR Filing", "icon": "📋", "role_gate": "super_admin"},
            {"view": "audit", "label": "Audit & Compliance", "icon": "🛡️", "role_gate": "super_admin"},
            {"view": "reports", "label": "Financial Reports", "icon": "📈", "role_gate": "super_admin"},
            {"view": "tasks", "label": "YantrAI Tasks", "icon": "🎯", "role_gate": "super_admin"},
        ]},
    ],
}

AGENT_SEED = [
    {"slug": "ai-accountant", "name": "Tally Agent",
     "tagline": "Books, GST, bank reco & filing on Tally — by chat.",
     "description": "Your full accounting back-office: voucher entry, bank reconciliation, "
                    "GST/TDS/ITR filing, masters and audit — all powered by AI and synced to Tally.",
     "icon": "🧮", "category": "accounting", "status": "published", "publisher": "first-party",
     "token_policy": {"chargeable": True}, "manifest": AI_ACCOUNTANT_MANIFEST, "sort_order": 10},
    {"slug": "lead-gen", "name": "Lead Gen",
     "tagline": "Describe your ideal customer — get real, scored leads.",
     "description": "Tell it who you want to reach and it finds real businesses (name, "
                    "phone, website), scores each for fit, and lets you export to CSV or "
                    "track status. Works for any field; data comes from real sources.",
     "icon": "🎯", "category": "sales", "status": "published", "publisher": "first-party",
     "token_policy": {"chargeable": True},
     "manifest": {"version": 1, "agent_kind": "inapp",
                  "api_prefixes": ["/api/leads"],
                  "nav_groups": [{"label": "Lead Gen", "items": [
                      {"view": "leadgen", "label": "Lead Gen", "icon": "🎯"}]}]},
     "sort_order": 15},
    {"slug": "payroll", "name": "Payroll",
     "tagline": "Salaries, PF/ESI & payslips — coming soon.",
     "description": "Run payroll: salary structures, statutory deductions (PF/ESI/PT), "
                    "payslip generation and compliance. Launching soon.",
     "icon": "💼", "category": "hr", "status": "coming_soon", "publisher": "first-party",
     "token_policy": {"chargeable": True}, "manifest": {"version": 1, "nav_groups": []},
     "sort_order": 20},
    # Sprint 81 — Remote Demo (Sprint-49 marketplace demo agent) removed.
    # Sprint 50 — Developer Portal: an in-app first-party agent any user opens to
    # register + run their own remote agentic apps (self-integration).
    {"slug": "developer-portal", "name": "Developer Portal",
     "tagline": "Build & plug in your own agent.",
     "description": "Register your own remote app, get credentials, and run it in your "
                    "workspace — the same way first-party agents work.",
     "icon": "🛠️", "category": "developer", "status": "published", "publisher": "first-party",
     "token_policy": {"chargeable": False},
     "manifest": {"version": 1, "agent_kind": "inapp",
                  "nav_groups": [{"label": "Developer", "items": [
                      {"view": "dev-portal", "label": "My Apps", "icon": "🛠️"}]}]},
     "sort_order": 40},
    # Sprint 78 — Demo Alpha / Demo Beta (Sprint-65 dummy pager apps) removed.
    # Network — AnyDesk-style workspace relationships (CA ↔ owner, owner ↔ accountant, etc.)
    {"slug": "network", "name": "Network",
     "tagline": "Connect with your CA, clients & team — grow your network.",
     "description": "Link your workspace with your CA, accountant, auditor, staff or clients "
                    "using a persistent Workspace ID and a simple request → approve handshake. "
                    "The relationship type sets the access level automatically.",
     "icon": "🔗", "category": "admin", "status": "published", "publisher": "first-party",
     "token_policy": {"chargeable": False},
     "manifest": {"version": 1, "agent_kind": "inapp",
                  "api_prefixes": ["/api/network"],
                  "nav_groups": [{"label": "Network", "items": [
                      {"view": "network-home", "label": "Connect", "icon": "🔗"},
                      {"view": "network-list", "label": "My Network", "icon": "👥"},
                      {"view": "network-requests", "label": "Requests", "icon": "📨"}]}]},
     "sort_order": 20},
]


def _seed_agents():
    """Idempotent upsert of the first-party agent catalog."""
    import json as _json
    try:
        conn = get_conn(); cur = conn.cursor()
        for a in AGENT_SEED:
            cur.execute("""
                INSERT INTO store_agents (slug, name, tagline, description, icon, category,
                                    status, publisher, token_policy, manifest, sort_order)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (slug) DO UPDATE SET
                    name=EXCLUDED.name, tagline=EXCLUDED.tagline, description=EXCLUDED.description,
                    icon=EXCLUDED.icon, category=EXCLUDED.category, status=EXCLUDED.status,
                    publisher=EXCLUDED.publisher, token_policy=EXCLUDED.token_policy,
                    manifest=EXCLUDED.manifest, sort_order=EXCLUDED.sort_order
            """, (a["slug"], a["name"], a["tagline"], a["description"], a["icon"], a["category"],
                  a["status"], a["publisher"], _json.dumps(a["token_policy"]),
                  _json.dumps(a["manifest"]), a["sort_order"]))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[_seed_agents] {e}")
    _ensure_core_installs()


def _ensure_core_installs():
    """Network is core to the platform — make sure every existing org has it installed."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO org_agent_installs (org_id, agent_slug, enabled)
            SELECT o.id, 'network', TRUE FROM organizations o
            ON CONFLICT (org_id, agent_slug) DO NOTHING
        """)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[_ensure_core_installs] {e}")


def list_catalog(org_id=None, include_all=False):
    """Full agent catalog. If org_id given, each row carries an `installed` flag.
    include_all (super agent / super_admin): return EVERY agent — all visibility,
    all owners, and archived too — so the platform operator sees the whole estate."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if include_all:
            # Super-agent god-view: no visibility/owner/archived filter at all.
            cur.execute("""
                SELECT a.*, (i.org_id IS NOT NULL AND i.enabled) AS installed
                FROM store_agents a
                LEFT JOIN org_agent_installs i ON i.agent_slug = a.slug AND i.org_id = %s
                ORDER BY a.sort_order, a.name
            """, (org_id,))
        elif org_id:
            # public catalog + this org's own private apps; never archived.
            cur.execute("""
                SELECT a.*, (i.org_id IS NOT NULL AND i.enabled) AS installed
                FROM store_agents a
                LEFT JOIN org_agent_installs i ON i.agent_slug = a.slug AND i.org_id = %s
                WHERE COALESCE(a.status,'') <> 'archived'
                  AND (COALESCE(a.visibility,'public') = 'public' OR a.owner_org_id = %s)
                ORDER BY a.sort_order, a.name
            """, (org_id, org_id))
        else:
            cur.execute("""SELECT a.*, FALSE AS installed FROM store_agents a
                           WHERE COALESCE(a.status,'') <> 'archived'
                             AND COALESCE(a.visibility,'public') = 'public'
                           ORDER BY a.sort_order, a.name""")
        rows = cur.fetchall()
        return rows
    finally:
        cur.close(); conn.close()


def record_login(username, user_id=None, company_name=None):
    """Sprint 77 — best-effort login event for daily-active-by-login analytics."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO user_logins (username, user_id, company_name) VALUES (%s,%s,%s)",
                    (username, user_id, company_name))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[record_login] {e}", flush=True)


def platform_analytics(from_date=None, to_date=None):
    """Sprint 76 — super-agent platform analytics across ALL workspaces/users for a
    date range. Returns KPIs, by-user / by-agent / by-model / by-action breakdowns,
    a daily trend, the token economy, and a per-workspace rollup."""
    import datetime as _dt
    today = _dt.date.today()
    to_date = to_date or today.isoformat()
    from_date = from_date or (today - _dt.timedelta(days=29)).isoformat()
    lo, hi = from_date + ' 00:00:00', to_date + ' 23:59:59.999'
    rng = (lo, hi)
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        out = {'from': from_date, 'to': to_date}

        # ---- KPIs (ai_usage_log) ----
        cur.execute("""SELECT COUNT(*) calls, COUNT(DISTINCT user_id) active_users,
            COALESCE(SUM(total_tokens),0) tokens, COALESCE(SUM(tokens_charged),0) billed,
            COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(output_tokens),0) o
          FROM ai_usage_log WHERE created_at BETWEEN %s AND %s""", rng)
        k = cur.fetchone()
        cur.execute("SELECT COUNT(*) c FROM chat_sessions WHERE created_at BETWEEN %s AND %s", rng); chats = cur.fetchone()['c']
        cur.execute("SELECT COUNT(*) c FROM chat_messages WHERE created_at BETWEEN %s AND %s", rng); msgs = cur.fetchone()['c']
        cur.execute("SELECT COUNT(*) c FROM tasks WHERE created_at BETWEEN %s AND %s", rng); tasks = cur.fetchone()['c']
        cur.execute("SELECT COUNT(*) c FROM organizations WHERE created_at BETWEEN %s AND %s", rng); new_ws = cur.fetchone()['c']
        cur.execute("SELECT COUNT(*) c FROM accounting_users WHERE created_at BETWEEN %s AND %s", rng); new_users = cur.fetchone()['c']
        cur.execute("""SELECT COALESCE(SUM(tokens),0) tk, COALESCE(SUM(amount_inr),0) inr
            FROM token_purchases WHERE status='paid' AND COALESCE(paid_at, created_at) BETWEEN %s AND %s""", rng)
        pur = cur.fetchone()
        cur.execute("SELECT reason, COALESCE(SUM(delta),0) d FROM token_ledger WHERE created_at BETWEEN %s AND %s GROUP BY reason", rng)
        led = {r['reason']: int(r['d']) for r in cur.fetchall()}
        # Sprint 77 — companies added (split signup-primary vs added-later) + active companies + logins.
        cur.execute("""SELECT COUNT(*) total,
            COALESCE(SUM(CASE WHEN COALESCE(is_primary,false)=false THEN 1 ELSE 0 END),0) later
            FROM companies WHERE created_at BETWEEN %s AND %s""", rng)
        cmp_k = cur.fetchone()
        cur.execute("SELECT COUNT(DISTINCT company_name) c FROM ai_usage_log WHERE created_at BETWEEN %s AND %s", rng)
        active_co = cur.fetchone()['c']
        cur.execute("SELECT COUNT(DISTINCT username) c FROM user_logins WHERE created_at BETWEEN %s AND %s", rng)
        logins_k = cur.fetchone()['c']
        out['kpis'] = {
            'active_users': int(k['active_users'] or 0), 'active_companies': int(active_co or 0),
            'logins': int(logins_k or 0), 'ai_calls': int(k['calls'] or 0),
            'tokens_consumed': int(k['tokens'] or 0), 'tokens_billed': int(k['billed'] or 0),
            'prompt_tokens': int(k['p'] or 0), 'output_tokens': int(k['o'] or 0),
            'chats': int(chats or 0), 'messages': int(msgs or 0), 'tasks': int(tasks or 0),
            'new_workspaces': int(new_ws or 0), 'new_users': int(new_users or 0),     # signups (= workspaces, 1:1)
            'companies_added': int(cmp_k['total'] or 0), 'companies_added_later': int(cmp_k['later'] or 0),
            'tokens_purchased': int(pur['tk'] or 0), 'revenue_inr': float(pur['inr'] or 0),
            'tokens_granted': int(led.get('grant', 0) + led.get('admin_adjust', 0)),
        }

        # ---- by user ----
        cur.execute("""SELECT user_id, COUNT(*) calls, COALESCE(SUM(total_tokens),0) tokens,
              COALESCE(SUM(tokens_charged),0) billed, MAX(created_at) last_active
            FROM ai_usage_log WHERE created_at BETWEEN %s AND %s GROUP BY user_id""", rng)
        usage_rows = cur.fetchall()
        cur.execute("SELECT users_id, username, name, role, company_name FROM accounting_users WHERE users_id IS NOT NULL")
        ident = {str(r['users_id']): r for r in cur.fetchall()}
        cur.execute("""SELECT user_username un, COUNT(*) c FROM chat_sessions
            WHERE created_at BETWEEN %s AND %s AND user_username IS NOT NULL GROUP BY user_username""", rng)
        chats_by = {r['un']: int(r['c']) for r in cur.fetchall()}
        cur.execute("""SELECT s.user_username un, COUNT(*) c FROM chat_messages m
            JOIN chat_sessions s ON s.id=m.session_id
            WHERE m.created_at BETWEEN %s AND %s AND s.user_username IS NOT NULL GROUP BY s.user_username""", rng)
        msgs_by = {r['un']: int(r['c']) for r in cur.fetchall()}
        by_user = []
        for r in usage_rows:
            uid = str(r['user_id']) if r['user_id'] else None
            idn = ident.get(uid) if uid else None
            uname = (idn['username'] if idn else None) or ('system' if not uid else uid[:8])
            by_user.append({
                'username': uname, 'name': (idn['name'] if idn else '') or '',
                'role': (idn['role'] if idn else '') or '', 'workspace': (idn['company_name'] if idn else '') or '',
                'calls': int(r['calls']), 'tokens': int(r['tokens']), 'billed': int(r['billed']),
                'last_active': r['last_active'].isoformat() if r['last_active'] else None,
                'chats': chats_by.get(uname, 0), 'messages': msgs_by.get(uname, 0),
            })
        by_user.sort(key=lambda x: x['tokens'], reverse=True)
        out['by_user'] = by_user

        # ---- by agent ----
        cur.execute("""SELECT COALESCE(agent_slug,'ai-accountant') slug, COUNT(*) calls,
              COALESCE(SUM(total_tokens),0) tokens, COUNT(DISTINCT user_id) users
            FROM ai_usage_log WHERE created_at BETWEEN %s AND %s GROUP BY 1 ORDER BY tokens DESC""", rng)
        agents = cur.fetchall()
        cur.execute("SELECT slug, name FROM store_agents"); anames = {r['slug']: r['name'] for r in cur.fetchall()}
        cur.execute("SELECT agent_slug, COUNT(*) c FROM org_agent_installs WHERE enabled GROUP BY 1")
        installs = {r['agent_slug']: int(r['c']) for r in cur.fetchall()}
        out['by_agent'] = [{'slug': a['slug'], 'name': anames.get(a['slug'], a['slug']),
            'calls': int(a['calls']), 'tokens': int(a['tokens']), 'users': int(a['users']),
            'installs': installs.get(a['slug'], 0)} for a in agents]

        # ---- by model ----
        cur.execute("""SELECT COALESCE(model,'?') model, COUNT(*) calls, COALESCE(SUM(prompt_tokens),0) p,
              COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(total_tokens),0) t, COALESCE(SUM(tokens_charged),0) billed
            FROM ai_usage_log WHERE created_at BETWEEN %s AND %s GROUP BY 1 ORDER BY t DESC""", rng)
        out['by_model'] = [{'model': m['model'], 'calls': int(m['calls']), 'prompt': int(m['p']),
            'output': int(m['o']), 'total': int(m['t']), 'billed': int(m['billed'])} for m in cur.fetchall()]

        # ---- by action ----
        cur.execute("""SELECT COALESCE(action,'?') action, COUNT(*) calls, COALESCE(SUM(total_tokens),0) t
            FROM ai_usage_log WHERE created_at BETWEEN %s AND %s GROUP BY 1 ORDER BY t DESC""", rng)
        out['by_action'] = [{'action': a['action'], 'calls': int(a['calls']), 'tokens': int(a['t'])} for a in cur.fetchall()]

        # ---- daily series: acquisition (signups + companies) AND engagement (active + logins) ----
        cur.execute("""SELECT to_char(created_at::date,'YYYY-MM-DD') d, COUNT(*) calls,
              COALESCE(SUM(total_tokens),0) tokens, COUNT(DISTINCT user_id) users,
              COUNT(DISTINCT company_name) companies
            FROM ai_usage_log WHERE created_at BETWEEN %s AND %s GROUP BY 1""", rng)
        d_use = {r['d']: r for r in cur.fetchall()}
        cur.execute("""SELECT to_char(created_at::date,'YYYY-MM-DD') d, COUNT(*) c
            FROM accounting_users WHERE created_at BETWEEN %s AND %s GROUP BY 1""", rng)
        d_sign = {r['d']: int(r['c']) for r in cur.fetchall()}
        cur.execute("""SELECT to_char(created_at::date,'YYYY-MM-DD') d, COUNT(*) total,
              COALESCE(SUM(CASE WHEN COALESCE(is_primary,false)=false THEN 1 ELSE 0 END),0) later
            FROM companies WHERE created_at BETWEEN %s AND %s GROUP BY 1""", rng)
        d_comp = {r['d']: r for r in cur.fetchall()}
        cur.execute("""SELECT to_char(created_at::date,'YYYY-MM-DD') d, COUNT(DISTINCT username) c
            FROM user_logins WHERE created_at BETWEEN %s AND %s GROUP BY 1""", rng)
        d_login = {r['d']: int(r['c']) for r in cur.fetchall()}
        daily = []
        cd = _dt.date.fromisoformat(from_date); ed = _dt.date.fromisoformat(to_date)
        while cd <= ed:
            ds = cd.isoformat(); u = d_use.get(ds); cm = d_comp.get(ds)
            daily.append({'day': ds,
                'signups': d_sign.get(ds, 0),
                'new_companies': int(cm['total']) if cm else 0,
                'companies_later': int(cm['later']) if cm else 0,
                'active_users': int(u['users']) if u else 0,
                'active_companies': int(u['companies']) if u else 0,
                'logins': d_login.get(ds, 0),
                'calls': int(u['calls']) if u else 0,
                'tokens': int(u['tokens']) if u else 0})
            cd += _dt.timedelta(days=1)
        out['daily'] = daily

        # ---- token economy ----
        cur.execute("SELECT COALESCE(SUM(token_balance),0) bal FROM organizations WHERE archived_at IS NULL")
        total_balance = int(cur.fetchone()['bal'] or 0)
        out['token_economy'] = {
            'granted': int(led.get('grant', 0) + led.get('admin_adjust', 0)),
            'purchased': int(led.get('recharge', 0)),
            'consumed': -int(led.get('ai_usage', 0)),   # ai_usage deltas are negative
            'total_balance': total_balance,
        }

        # ---- workspaces ----
        cur.execute("SELECT COUNT(*) c FROM organizations WHERE archived_at IS NULL"); ws_total = int(cur.fetchone()['c'] or 0)
        cur.execute("""SELECT o.name, o.token_balance,
              COALESCE(SUM(u.total_tokens),0) tokens, COUNT(u.id) calls
            FROM organizations o
            LEFT JOIN ai_usage_log u ON u.org_id=o.id AND u.created_at BETWEEN %s AND %s
            WHERE o.archived_at IS NULL
            GROUP BY o.id, o.name, o.token_balance ORDER BY tokens DESC""", rng)
        wrows = cur.fetchall()
        ws_active = sum(1 for r in wrows if int(r['tokens'] or 0) > 0 or int(r['calls'] or 0) > 0)
        out['workspaces'] = {'total': ws_total, 'active': ws_active,
            'list': [{'name': r['name'], 'balance': int(r['token_balance'] or 0),
                      'tokens': int(r['tokens'] or 0), 'calls': int(r['calls'] or 0)} for r in wrows[:50]]}
        return out
    finally:
        cur.close(); conn.close()


def org_has_install_history(org_id):
    """True if the org has EVER had an install row (enabled or disabled). Lets the
    API distinguish 'pre-backfill, never installed' from 'deliberately removed all'."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM org_agent_installs WHERE org_id=%s LIMIT 1", (org_id,))
        return cur.fetchone() is not None
    finally:
        cur.close(); conn.close()


def list_installed_agents(org_id):
    """Enabled agents installed for an org, with manifests."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT a.slug, a.name, a.icon, a.category, a.status, a.manifest, a.token_policy
            FROM org_agent_installs i
            JOIN store_agents a ON a.slug = i.agent_slug
            WHERE i.org_id = %s AND i.enabled = TRUE
            ORDER BY a.sort_order, a.name
        """, (org_id,))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def install_agent(org_id, slug, by_user_id=None):
    """Install (or re-enable) an agent for an org. Idempotent; never deletes."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO org_agent_installs (org_id, agent_slug, installed_by_user_id, enabled)
            VALUES (%s,%s,%s,TRUE)
            ON CONFLICT (org_id, agent_slug)
            DO UPDATE SET enabled=TRUE, installed_by_user_id=COALESCE(org_agent_installs.installed_by_user_id, EXCLUDED.installed_by_user_id)
        """, (org_id, slug, by_user_id))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback(); print(f"[install_agent] {e}"); return False
    finally:
        cur.close(); conn.close()


def uninstall_agent(org_id, slug):
    """Soft-uninstall — disable, never delete (preserves history + settings)."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE org_agent_installs SET enabled=FALSE WHERE org_id=%s AND agent_slug=%s",
                    (org_id, slug))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback(); print(f"[uninstall_agent] {e}"); return False
    finally:
        cur.close(); conn.close()


def usage_by_agent(org_id, since=None):
    """Per-agent token usage breakdown for a workspace (legacy NULL rows fold under
    'ai-accountant')."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if since:
            cur.execute("""
                SELECT COALESCE(agent_slug,'ai-accountant') AS slug,
                       SUM(tokens_charged) AS tokens, COUNT(*) AS calls
                FROM ai_usage_log WHERE org_id=%s AND created_at>=%s
                GROUP BY 1 ORDER BY tokens DESC NULLS LAST
            """, (org_id, since))
        else:
            cur.execute("""
                SELECT COALESCE(agent_slug,'ai-accountant') AS slug,
                       SUM(tokens_charged) AS tokens, COUNT(*) AS calls
                FROM ai_usage_log WHERE org_id=%s
                GROUP BY 1 ORDER BY tokens DESC NULLS LAST
            """, (org_id,))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


# ---------------------------------------------------------------------------
# Sprint 50 — Developer Portal: a dev app is a store_agents row owned by an org
# (visibility='private'), with per-app client_id + signing_key for scoped SSO.
# ---------------------------------------------------------------------------
def create_app(org_id, user_id, name, remote_url, *, slug=None, tagline=None,
               description=None, icon="🧩", category="custom",
               publisher="developer", visibility="private", token_policy=None,
               sort_order=500):
    """Register (or idempotently re-publish) a remote agent in the store.

    One code path for both developer apps (random slug, private, developer) and
    first-party apps (explicit slug, public, first-party). On a slug conflict it
    updates metadata but PRESERVES client_id/signing_key (rotating them would
    break live sessions + metering)."""
    import secrets as _secrets, json as _json
    name = (name or "").strip() or "My App"
    remote_url = (remote_url or "").strip()
    if not remote_url:
        return {"ok": False, "error": "A remote URL is required."}
    slug = slug or ("app-" + _secrets.token_hex(6))
    view = "remote-" + slug
    conn = get_conn(); cur = conn.cursor()
    try:
        # Reuse existing credentials if the row exists; never rotate them here.
        cur.execute("SELECT client_id, signing_key FROM store_agents WHERE slug=%s", (slug,))
        row = cur.fetchone()
        client_id = (row[0] if row and row[0] else "cid_" + _secrets.token_hex(8))
        signing_key = (row[1] if row and row[1] else "sk_" + _secrets.token_hex(24))
        manifest = {"version": 1, "agent_kind": "remote", "remote_url": remote_url,
                    "client_id": client_id,
                    "nav_groups": [{"label": name, "items": [
                        {"view": view, "label": name, "icon": icon}]}]}
        cur.execute("""
            INSERT INTO store_agents (slug, name, tagline, description, icon, category,
                status, publisher, token_policy, manifest, sort_order,
                owner_org_id, owner_user_id, visibility, client_id, signing_key)
            VALUES (%s,%s,%s,%s,%s,%s,'published',%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (slug) DO UPDATE SET
                name=EXCLUDED.name, tagline=EXCLUDED.tagline, description=EXCLUDED.description,
                icon=EXCLUDED.icon, category=EXCLUDED.category, status=EXCLUDED.status,
                publisher=EXCLUDED.publisher, token_policy=EXCLUDED.token_policy,
                manifest=EXCLUDED.manifest, sort_order=EXCLUDED.sort_order,
                visibility=EXCLUDED.visibility,
                owner_org_id=COALESCE(store_agents.owner_org_id, EXCLUDED.owner_org_id),
                owner_user_id=COALESCE(store_agents.owner_user_id, EXCLUDED.owner_user_id),
                client_id=COALESCE(store_agents.client_id, EXCLUDED.client_id),
                signing_key=COALESCE(store_agents.signing_key, EXCLUDED.signing_key)
        """, (slug, name, tagline, description, icon, category, publisher,
              _json.dumps(token_policy or {}), _json.dumps(manifest), sort_order,
              org_id, user_id, visibility, client_id, signing_key))
        conn.commit()
        return {"ok": True, "slug": slug, "client_id": client_id,
                "client_secret": signing_key, "remote_url": remote_url, "name": name}
    except Exception as e:
        conn.rollback(); print(f"[create_app] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def create_dev_app(org_id, user_id, name, remote_url, tagline=None, description=None,
                   icon="🧩", category="custom"):
    """Portal default: a private, developer-published, non-chargeable remote app."""
    return create_app(org_id, user_id, name, remote_url, tagline=tagline,
                      description=description, icon=icon, category=category,
                      publisher="developer", visibility="private",
                      token_policy={}, sort_order=500)


def list_dev_apps(org_id):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT slug, name, tagline, description, icon, category, status,
                              visibility, client_id, manifest, token_policy,
                              review_status, created_at
                       FROM store_agents
                       WHERE owner_org_id=%s AND COALESCE(status,'')<>'archived'
                       ORDER BY created_at DESC""", (org_id,))
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def update_dev_app(slug, org_id, name=None, remote_url=None, tagline=None,
                   description=None, icon=None, category=None):
    import json as _json
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT manifest, name, icon FROM store_agents WHERE slug=%s AND owner_org_id=%s",
                    (slug, org_id))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "App not found."}
        manifest = row["manifest"] or {}
        if remote_url: manifest["remote_url"] = remote_url.strip()
        new_name = (name or row["name"]); new_icon = (icon or row["icon"])
        # keep the single nav item label/icon in sync
        try:
            manifest["nav_groups"][0]["items"][0]["label"] = new_name
            manifest["nav_groups"][0]["items"][0]["icon"] = new_icon
        except Exception:
            pass
        cur.execute("""UPDATE store_agents SET
                          name=COALESCE(%s,name), tagline=COALESCE(%s,tagline),
                          description=COALESCE(%s,description), icon=COALESCE(%s,icon),
                          category=COALESCE(%s,category), manifest=%s
                       WHERE slug=%s AND owner_org_id=%s""",
                    (name, tagline, description, icon, category, _json.dumps(manifest),
                     slug, org_id))
        conn.commit()
        return {"ok": cur.rowcount > 0}
    except Exception as e:
        conn.rollback(); print(f"[update_dev_app] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def archive_dev_app(slug, org_id):
    """Soft delete — mark archived + uninstall everywhere. Never hard-deletes."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE store_agents SET status='archived' WHERE slug=%s AND owner_org_id=%s",
                    (slug, org_id))
        ok = cur.rowcount > 0
        if ok:
            cur.execute("UPDATE org_agent_installs SET enabled=FALSE WHERE agent_slug=%s", (slug,))
        conn.commit()
        return {"ok": ok}
    except Exception as e:
        conn.rollback(); print(f"[archive_dev_app] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def rotate_dev_app_key(slug, org_id):
    import secrets as _secrets
    new_key = "sk_" + _secrets.token_hex(24)
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE store_agents SET signing_key=%s WHERE slug=%s AND owner_org_id=%s",
                    (new_key, slug, org_id))
        ok = cur.rowcount > 0
        conn.commit()
        return {"ok": ok, "client_secret": new_key if ok else None}
    except Exception as e:
        conn.rollback(); print(f"[rotate_dev_app_key] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def get_app_signing_key(kid):
    """Look up an app's signing key by client_id or slug (for SSO verification)."""
    if not kid:
        return None
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT signing_key FROM store_agents WHERE client_id=%s OR slug=%s LIMIT 1",
                    (kid, kid))
        r = cur.fetchone()
        return r[0] if r and r[0] else None
    finally:
        cur.close(); conn.close()


def get_agent_auth(slug):
    """(client_id, signing_key) for an agent by slug — works for private apps too."""
    if not slug:
        return None
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT client_id, signing_key FROM store_agents WHERE slug=%s LIMIT 1", (slug,))
        r = cur.fetchone()
        if r and r[0] and r[1]:
            return {"client_id": r[0], "signing_key": r[1]}
        return None
    finally:
        cur.close(); conn.close()


def slug_for_client_id(client_id):
    """Reverse of get_agent_auth: the agent slug for a given client_id (SSO `kid`)."""
    if not client_id:
        return None
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT slug FROM store_agents WHERE client_id=%s LIMIT 1", (client_id,))
        r = cur.fetchone()
        return r[0] if r else None
    finally:
        cur.close(); conn.close()


def get_dev_app(slug, org_id):
    """A single owned app row (or None) — for ownership checks + portal detail."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT slug, name, tagline, description, icon, category, status,
                              visibility, publisher, token_policy, client_id, manifest,
                              review_status, review_note, created_at
                       FROM store_agents WHERE slug=%s AND owner_org_id=%s""",
                    (slug, org_id))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def get_token_policy(slug):
    """The billing policy {"chargeable":bool,"credits_per_action":int} for an agent."""
    if not slug:
        return {}
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT token_policy FROM store_agents WHERE slug=%s LIMIT 1", (slug,))
        r = cur.fetchone()
        return (r[0] or {}) if r else {}
    finally:
        cur.close(); conn.close()


def set_app_billing(slug, org_id, chargeable=None, credits_per_action=None):
    """Update an owned app's token_policy (merges; only the fields you pass)."""
    import json as _json
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT token_policy FROM store_agents WHERE slug=%s AND owner_org_id=%s",
                    (slug, org_id))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "App not found."}
        tp = dict(row["token_policy"] or {})
        if chargeable is not None:
            tp["chargeable"] = bool(chargeable)
        if credits_per_action is not None:
            tp["credits_per_action"] = int(credits_per_action)
        cur.execute("UPDATE store_agents SET token_policy=%s::jsonb WHERE slug=%s AND owner_org_id=%s",
                    (_json.dumps(tp), slug, org_id))
        conn.commit()
        return {"ok": True, "token_policy": tp}
    except Exception as e:
        conn.rollback(); print(f"[set_app_billing] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def dev_app_usage(slug, org_id):
    """Usage/earnings for a dev-owned app across EVERY workspace that used it.
    Returns None if the app isn't owned by org_id (authorization)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT 1 FROM store_agents WHERE slug=%s AND owner_org_id=%s", (slug, org_id))
        if not cur.fetchone():
            return None
        cur.execute("""SELECT COALESCE(SUM(tokens_charged),0) AS tokens, COUNT(*) AS calls,
                              COUNT(DISTINCT org_id) AS workspaces
                       FROM ai_usage_log WHERE agent_slug=%s""", (slug,))
        total = cur.fetchone()
        cur.execute("""SELECT DATE(created_at) AS day,
                              COALESCE(SUM(tokens_charged),0) AS tokens, COUNT(*) AS calls
                       FROM ai_usage_log
                       WHERE agent_slug=%s AND created_at >= NOW() - INTERVAL '30 days'
                       GROUP BY 1 ORDER BY 1 DESC""", (slug,))
        daily = cur.fetchall()
        return {"total": total, "daily": daily}
    finally:
        cur.close(); conn.close()


def request_publish(slug, org_id, user_id=None):
    """Developer asks for their (owned, private) app to be listed publicly."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE store_agents SET review_status='requested'
                       WHERE slug=%s AND owner_org_id=%s AND COALESCE(status,'')<>'archived'""",
                    (slug, org_id))
        ok = cur.rowcount > 0
        if ok:
            cur.execute("""INSERT INTO agent_review_log (agent_slug, action, actor_user_id)
                           VALUES (%s,'requested',%s)""", (slug, user_id))
        conn.commit()
        return {"ok": ok, "error": None if ok else "App not found."}
    except Exception as e:
        conn.rollback(); print(f"[request_publish] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def publish_queue():
    """Apps awaiting review (super_admin view)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT slug, name, tagline, icon, category, publisher, visibility,
                              owner_org_id, manifest, review_status, created_at
                       FROM store_agents
                       WHERE review_status='requested' AND COALESCE(status,'')<>'archived'
                       ORDER BY created_at ASC""")
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def decide_publish(slug, approve, reviewer_user_id=None, note=None):
    """Approve (→ public + approved) or reject (→ rejected, stays private) an app."""
    conn = get_conn(); cur = conn.cursor()
    try:
        if approve:
            cur.execute("""UPDATE store_agents
                           SET visibility='public', review_status='approved',
                               review_note=%s, reviewed_by=%s, reviewed_at=CURRENT_TIMESTAMP
                           WHERE slug=%s""", (note, reviewer_user_id, slug))
        else:
            cur.execute("""UPDATE store_agents
                           SET review_status='rejected',
                               review_note=%s, reviewed_by=%s, reviewed_at=CURRENT_TIMESTAMP
                           WHERE slug=%s""", (note, reviewer_user_id, slug))
        ok = cur.rowcount > 0
        if ok:
            cur.execute("""INSERT INTO agent_review_log (agent_slug, action, note, actor_user_id)
                           VALUES (%s,%s,%s,%s)""",
                        (slug, "approved" if approve else "rejected", note, reviewer_user_id))
        conn.commit()
        return {"ok": ok, "error": None if ok else "App not found."}
    except Exception as e:
        conn.rollback(); print(f"[decide_publish] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def org_id_for_username(username):
    """Resolve the caller's home/owned org id from a username (for dev-app ownership)."""
    u = get_user_by_username(username) if username else None
    uid = u.get("users_id") if u else None
    if not uid:
        return None, None
    mems = get_user_memberships(uid) or []
    m = next((x for x in mems if x.get("role") in ("owner", "manager")), None) or (mems[0] if mems else None)
    return (m["org_id"] if m else None), uid


def org_balance(org_id):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(token_balance,0) FROM organizations WHERE id=%s", (org_id,))
        r = cur.fetchone()
        return int(r[0]) if r else 0
    finally:
        cur.close(); conn.close()


def credit_tokens(org_id, n, reason="recharge", ref_id=None, note=None, created_by=None):
    """Add tokens to a workspace; writes a ledger row. Returns new balance."""
    _ensure_billing_schema()
    n = int(n)
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE organizations SET token_balance = COALESCE(token_balance,0) + %s WHERE id=%s RETURNING token_balance",
                    (n, org_id))
        row = cur.fetchone()
        bal = int(row[0]) if row else None
        cur.execute("""INSERT INTO token_ledger (org_id, delta, balance_after, reason, ref_id, note, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""", (org_id, n, bal, reason, ref_id, note, created_by))
        conn.commit()
        return bal
    except Exception as e:
        conn.rollback(); print(f"[credit_tokens] {e}"); return None
    finally:
        cur.close(); conn.close()


def debit_tokens(org_id, n, action=None, model=None, user_id=None, company_name=None,
                 prompt_tokens=None, output_tokens=None, total_tokens=None,
                 agent_slug="ai-accountant"):
    """Deduct n tokens for an AI action; logs usage + ledger. Best-effort
    (allows balance to go slightly negative on the charging call — we pre-check
    before the call). Returns new balance."""
    _ensure_billing_schema()
    n = int(max(0, n))
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE organizations SET token_balance = COALESCE(token_balance,0) - %s WHERE id=%s RETURNING token_balance",
                    (n, org_id))
        row = cur.fetchone()
        bal = int(row[0]) if row else None
        cur.execute("""INSERT INTO ai_usage_log (org_id, user_id, company_name, action, model,
                          prompt_tokens, output_tokens, total_tokens, tokens_charged, agent_slug)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (org_id, user_id, company_name, action, model,
                     prompt_tokens, output_tokens, total_tokens, n, agent_slug))
        cur.execute("""INSERT INTO token_ledger (org_id, delta, balance_after, reason, note, created_by)
                       VALUES (%s,%s,%s,'ai_usage',%s,%s)""",
                    (org_id, -n, bal, action, user_id and str(user_id)))
        conn.commit()
        return bal
    except Exception as e:
        conn.rollback(); print(f"[debit_tokens] {e}"); return None
    finally:
        cur.close(); conn.close()


def org_id_for_company(company_name, caller_users_id=None):
    """Resolve which workspace (org) owns `company_name` for billing. Prefer an org
    the caller is a member of; else any org that has this company; else the caller's
    own owned org."""
    if not company_name:
        return None
    conn = pget(); cur = conn.cursor()
    try:
        if caller_users_id:
            cur.execute("""
                SELECT c.org_id FROM companies c
                JOIN memberships m ON m.org_id = c.org_id
                WHERE c.name=%s AND m.user_id=%s AND c.archived_at IS NULL
                ORDER BY c.is_primary DESC LIMIT 1
            """, (company_name, caller_users_id))
            r = cur.fetchone()
            if r: return r[0]
        cur.execute("SELECT org_id FROM companies WHERE name=%s AND archived_at IS NULL ORDER BY is_primary DESC LIMIT 1",
                    (company_name,))
        r = cur.fetchone()
        if r: return r[0]
        if caller_users_id:
            cur.execute("""SELECT org_id FROM memberships WHERE user_id=%s AND role IN ('owner','manager')
                           ORDER BY joined_at ASC LIMIT 1""", (caller_users_id,))
            r = cur.fetchone()
            if r: return r[0]
        return None
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


def recent_ledger(org_id, limit=20):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""SELECT delta, balance_after, reason, note, created_at
                   FROM token_ledger WHERE org_id=%s ORDER BY created_at DESC LIMIT %s""",
                (org_id, limit))
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows


def create_purchase(org_id, amount_inr, tokens, created_by=None, provider=None,
                    provider_order_id=None):
    _ensure_billing_schema()
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""INSERT INTO token_purchases (org_id, amount_inr, tokens, created_by, provider, provider_order_id)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, status""",
                (org_id, amount_inr, tokens, created_by, provider, provider_order_id))
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return row


def mark_purchase_paid(purchase_id, provider_ref=None):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""UPDATE token_purchases SET status='paid', paid_at=CURRENT_TIMESTAMP,
                   provider_ref=COALESCE(%s, provider_ref) WHERE id=%s AND status<>'paid'
                   RETURNING org_id, tokens""", (provider_ref, purchase_id))
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return row


def get_model_weight(model):
    """Sprint 54 — credits charged per 1,000 tokens for a model (None if not set)."""
    if not model:
        return None
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT weight FROM model_pricing WHERE model=%s AND active", (model,))
        r = cur.fetchone()
        return float(r[0]) if r and r[0] is not None else None
    except Exception as e:
        print(f"[get_model_weight] {e}"); return None
    finally:
        cur.close(); conn.close()


def mark_purchase_order(purchase_id, order_id):
    """Sprint 53 — attach the gateway order id to a purchase (for verify/webhook match)."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE token_purchases SET provider_order_id=%s WHERE id=%s", (order_id, purchase_id))
        conn.commit()
    except Exception as e:
        conn.rollback(); print(f"[mark_purchase_order] {e}")
    finally:
        cur.close(); conn.close()


def mark_purchase_paid_by_order(order_id, provider_ref=None):
    """Sprint 53 — atomically mark a Razorpay-order purchase paid (idempotent: returns
    org_id+tokens only on the transition to paid, so credit happens exactly once)."""
    if not order_id:
        return None
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""UPDATE token_purchases SET status='paid', paid_at=CURRENT_TIMESTAMP,
                   provider_ref=COALESCE(%s, provider_ref)
                   WHERE provider_order_id=%s AND status<>'paid'
                   RETURNING id, org_id, tokens""", (provider_ref, order_id))
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return row


def onboard_user(username, password, name=None, email=None, phone=None,
                 user_type=None, org_name=None, company_name=None,
                 gstin=None, state_code=None):
    """Sprint 46 — DEMOCRATIZED uniform self-onboarding. Everyone gets their own
    workspace (org) + first company + owner membership. No 'type' is required;
    relationships/roles are formed later via handshake codes. The legacy
    accounting_users row (login + company_name access) is kept in sync so the
    company_name-keyed data layer is untouched.

    Returns {ok, error?, org_id, company_name, companies, role, users_id}.
    """
    _ensure_onboarding_columns()
    _ensure_billing_schema()
    import json as _json
    username = (username or "").strip()
    if not username or not password:
        return {"ok": False, "error": "Username and password are required."}
    if get_user_by_username(username):
        return {"ok": False, "error": "That username is already taken."}

    # Every workspace is an org that can hold many companies + members.
    org_type = "firm" if user_type == "firm" else "company"
    primary_company = (company_name or "").strip() or None
    org_label = (org_name or "").strip() or primary_company or f"{username}'s workspace"
    # The app always needs a company_name to scope by → fall back to the org label.
    legacy_company = primary_company or org_label

    role = "admin"  # coarse app gate (unchanged); membership role is the real role
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # 1) Phase-B identity row
        cur.execute("""
            INSERT INTO users (username, password, name, email, phone)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (username, password, name or username,
              email or f"{username}@yantrai.com", phone or ""))
        users_id = cur.fetchone()["id"]

        # 2) Organization (owner = this user)
        cur.execute("""
            INSERT INTO organizations (name, type, gstin, plan, created_by_user_id, token_balance)
            VALUES (%s, %s, %s, 'free', %s, %s) RETURNING id
        """, (org_label, org_type, gstin, users_id, SIGNUP_FREE_TOKENS))
        org_id = cur.fetchone()["id"]

        # 3) Primary company (business/client only; firm adds clients later in-app)
        companies_list = []
        if primary_company:
            cur.execute("""
                INSERT INTO companies (org_id, name, gstin, state_code, is_primary)
                VALUES (%s, %s, %s, %s, TRUE)
                ON CONFLICT (org_id, name) DO UPDATE SET is_primary = TRUE
                RETURNING id
            """, (org_id, primary_company, gstin, state_code))
            companies_list = [primary_company]

        # 4) Owner membership
        cur.execute("""
            INSERT INTO memberships (user_id, org_id, role)
            VALUES (%s, %s, 'owner')
            ON CONFLICT (user_id, org_id) DO UPDATE SET role = 'owner'
            RETURNING id
        """, (users_id, org_id))
        membership_id = cur.fetchone()["id"]
        cur.execute("UPDATE users SET default_membership_id = %s WHERE id = %s", (membership_id, users_id))

        # 5) Legacy accounting_users row — login + company_name access projection
        cur.execute("""
            INSERT INTO accounting_users
              (username, password, role, name, email, phone, company_name, companies, user_type, users_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (username, password, role, name or username,
              email or f"{username}@yantrai.com", phone or "+919999999999",
              legacy_company, _json.dumps(companies_list or [legacy_company]),
              user_type, users_id))

        conn.commit()
        # Sprint 48 — every new workspace starts with Agent #1 (AI Accountant) installed.
        try:
            install_agent(org_id, "ai-accountant", users_id)
        except Exception as _e:
            print(f"[onboard_user] install Agent#1: {_e}")
        # Network is core to the platform — auto-install for every new workspace.
        try:
            install_agent(org_id, "network", users_id)
        except Exception as _e:
            print(f"[onboard_user] install Network: {_e}")
        return {"ok": True, "user_type": user_type, "org_id": str(org_id),
                "org_type": org_type, "company_name": legacy_company,
                "companies": companies_list or [legacy_company], "role": role,
                "users_id": str(users_id)}
    except Exception as e:
        conn.rollback()
        print(f"[onboard_user] {e}")
        return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def get_user_by_email(email):
    """Sprint 52 — find an account by email (for Google sign-in linking)."""
    if not email:
        return None
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM accounting_users WHERE LOWER(email)=LOWER(%s) LIMIT 1", (email,))
        return cur.fetchone()
    except Exception as e:
        print(f"[get_user_by_email] {e}"); return None
    finally:
        cur.close(); conn.close()


def onboard_google_user(auth_uid, email, name=None):
    """Sprint 52 — provision a workspace for a first-time Google user, then link the
    Supabase auth uid. Reuses onboard_user. Returns the accounting_users row or None."""
    import secrets as _secrets, re as _re
    email = (email or "").strip().lower()
    base = _re.sub(r'[^a-z0-9]', '', (email.split('@')[0] if email else '') or 'user') or 'user'
    username = base
    n = 1
    while get_user_by_username(username):
        n += 1; username = f"{base}{n}"
    display = (name or base).strip()
    res = onboard_user(username=username, password="goog_" + _secrets.token_hex(16),
                       name=display, email=email or None,
                       company_name=f"{display}'s workspace")
    if not res.get("ok"):
        print(f"[onboard_google_user] onboard failed: {res.get('error')}")
        return None
    link_auth_uid(username, auth_uid)
    return get_user_by_username(username)


# ---- Chat Functions ----

_CHAT_COLS_READY = False
def _ensure_chat_user_column():
    """Idempotent ALTER — runs ONCE per process (Sprint 83: was running a fresh
    connection + ALTERs on every chat call, a big latency source)."""
    global _CHAT_COLS_READY
    if _CHAT_COLS_READY:
        return
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS user_username TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_username, company_name)")
        # Sprint 82 — distinguish Tally chats ('chat') from Create-task sessions ('yantrai_task').
        cursor.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'chat'")
        # Sprint 84 — public shareable per-chat link (read-only).
        cursor.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS share_token TEXT")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_sessions_share ON chat_sessions(share_token)")
        # Speed up the sidebar session-list query (per-session message counts).
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id)")
        conn.commit()
        cursor.close()
        conn.close()
        _CHAT_COLS_READY = True
    except Exception as e:
        print(f"[_ensure_chat_user_column] {e}")


def create_chat_session(title="New Chat", company_name=None, user_username=None, kind='chat'):
    _ensure_chat_user_column()
    conn = pget()
    try:
        cursor = conn.cursor()
        session_id = str(uuid.uuid4())
        cursor.execute("""
        INSERT INTO chat_sessions (id, title, company_name, user_username, kind) VALUES (%s, %s, %s, %s, %s)
        """, (session_id, title, company_name, user_username, kind))
        conn.commit(); cursor.close()
        return session_id
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        pput(conn)


def get_chat_sessions(company_name=None, user_username=None, limit=100):
    """List chat sessions scoped to (company, user). Both filters apply when given.
    If user_username is None → super_admin / all (use cautiously).
    `limit` caps the number of most-recent sessions returned (sidebar is a recents list)."""
    _ensure_chat_user_column()
    conn = pget()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        # Filter+order+limit the sessions first, then a per-session message count
        # backed by idx_chat_messages_session — avoids aggregating the whole
        # chat_messages table on every call.
        # Sprint 82 — keep Create-task sessions out of the Tally recent-chats list.
        where = ["COALESCE(s.kind,'chat') <> 'yantrai_task'"]
        params = []
        if company_name:
            where.append("s.company_name = %s")
            params.append(company_name)
        if user_username:
            # Show sessions owned by this user only. Legacy sessions tagged
            # '__legacy__' (Sprint 7 backfill) are hidden from regular users
            # — they remain visible to super_admin who passes user_username=None.
            where.append("s.user_username = %s")
            params.append(user_username)
        query = """
            SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count
            FROM chat_sessions s
        """
        query += " WHERE " + " AND ".join(where)
        query += " ORDER BY s.updated_at DESC LIMIT %s"
        params.append(int(limit))
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        pput(conn)


def list_task_sessions(company_name=None, user_username=None):
    """Sprint 82 — Create-task sessions for the sidebar: each task = one chat session,
    with the linked task's status (NULL ⇒ 'Draft') for the progress badge."""
    _ensure_chat_user_column()
    conn = pget()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        where = ["COALESCE(s.kind,'chat') = 'yantrai_task'"]
        params = []
        if company_name: where.append("s.company_name = %s"); params.append(company_name)
        if user_username: where.append("s.user_username = %s"); params.append(user_username)
        cur.execute(f"""
            SELECT s.id, s.title, s.updated_at,
                   t.status, t.task_code,
                   COALESCE(m.c, 0) AS message_count
            FROM chat_sessions s
            LEFT JOIN LATERAL (
                SELECT status, task_code FROM tasks WHERE session_id = s.id
                ORDER BY created_at DESC LIMIT 1
            ) t ON TRUE
            LEFT JOIN (SELECT session_id, COUNT(*) c FROM chat_messages GROUP BY session_id) m
                   ON m.session_id = s.id
            WHERE {' AND '.join(where)}
            ORDER BY s.updated_at DESC
        """, tuple(params))
        return cur.fetchall()
    finally:
        cur.close(); pput(conn)


def get_chat_sessions_multi(company_names, user_username=None, limit=100):
    """Get chat sessions for multiple companies at once, scoped to a user.
    `limit` caps the number of most-recent sessions returned."""
    _ensure_chat_user_column()
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    # Per-session count backed by idx_chat_messages_session over the limited set,
    # instead of aggregating the whole chat_messages table.
    base = """
        SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count
        FROM chat_sessions s
    """
    where = []
    params = []
    if company_names:
        placeholders = ','.join(['%s'] * len(company_names))
        where.append(f"s.company_name IN ({placeholders})")
        params.extend(company_names)
    if user_username:
        where.append("s.user_username = %s")
        params.append(user_username)
    if where:
        base += " WHERE " + " AND ".join(where)
    base += " ORDER BY s.updated_at DESC LIMIT %s"
    params.append(int(limit))
    cursor.execute(base, tuple(params))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def get_chat_session_owner(session_id):
    """Return the user_username + company_name of a session, used for /chat/messages auth."""
    _ensure_chat_user_column()
    conn = pget()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT user_username, company_name FROM chat_sessions WHERE id = %s", (session_id,))
        row = cursor.fetchone()
        cursor.close()
        return row
    finally:
        pput(conn)

def save_chat_message(session_id, role, content, ui_type="text", ui_data=None):
    conn = pget()
    try:
        cursor = conn.cursor()
        msg_id = str(uuid.uuid4())
        cursor.execute("""
        INSERT INTO chat_messages (id, session_id, role, content, ui_type, ui_data)
        VALUES (%s, %s, %s, %s, %s, %s)
        """, (msg_id, session_id, role, content, ui_type, json.dumps(ui_data) if ui_data else None))
        # Update session timestamp
        cursor.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (session_id,))
        conn.commit(); cursor.close()
        return msg_id
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        pput(conn)

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
    conn = pget()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE chat_sessions SET title = %s WHERE id = %s", (title, session_id))
        conn.commit(); cursor.close()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        pput(conn)

def get_chat_messages(session_id):
    conn = pget()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM chat_messages WHERE session_id = %s ORDER BY created_at ASC", (session_id,))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        pput(conn)


# ─── Sprint 84 — public shareable chat links ──────────────────────────────
def get_or_create_share_token(session_id):
    """Return the session's public share token, minting one on first request."""
    _ensure_chat_user_column()
    conn = pget()
    try:
        cur = conn.cursor()
        cur.execute("SELECT share_token FROM chat_sessions WHERE id = %s", (session_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return None
        if row[0]:
            cur.close()
            return row[0]
        import secrets
        token = secrets.token_urlsafe(12)
        cur.execute("UPDATE chat_sessions SET share_token = %s WHERE id = %s", (token, session_id))
        conn.commit(); cur.close()
        return token
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        pput(conn)


def revoke_share_token(session_id):
    conn = pget()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE chat_sessions SET share_token = NULL WHERE id = %s", (session_id,))
        conn.commit(); cur.close()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        pput(conn)


def get_shared_transcript(token):
    """Public read-only transcript for a share token. Returns
    {title, kind, messages:[...]} or None if the token is unknown/revoked."""
    if not token:
        return None
    _ensure_chat_user_column()
    conn = pget()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, title, kind FROM chat_sessions WHERE share_token = %s", (token,))
        sess = cur.fetchone()
        if not sess:
            cur.close()
            return None
        cur.execute("""SELECT role, content, ui_type, ui_data, created_at
                       FROM chat_messages WHERE session_id = %s ORDER BY created_at ASC""",
                    (sess["id"],))
        msgs = cur.fetchall()
        cur.close()
        return {"title": sess.get("title") or "Shared chat",
                "kind": sess.get("kind") or "chat", "messages": msgs}
    finally:
        pput(conn)

def save_invoice(data):
    conn = get_conn()
    cursor = conn.cursor()

    company_name = data.get('company_name')
    invoice_number = data.get('invoice_number')

    # ── Total integrity guard ─────────────────────────────────────────────────
    # The headline total must be the GROSS (taxable + all GST). A client that sums
    # only line totals drops IGST for inter-state invoices (e.g. 249730 instead of
    # 262217). When a breakdown is present and the supplied total is short, trust
    # the computed gross. (₹1 tolerance absorbs rounding.)
    def _f(k):
        try: return float(data.get(k) or 0)
        except (TypeError, ValueError): return 0.0
    taxable = _f('taxable_value'); cgst = _f('cgst_amount')
    sgst = _f('sgst_amount');      igst = _f('igst_amount')
    gross = round(taxable + cgst + sgst + igst, 2)
    total = _f('total_amount')
    if gross > 0 and total + 1.0 < gross:
        data = {**data, 'total_amount': gross, 'gst_amount': round(cgst + sgst + igst, 2)}

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
            voucher_type = %s, taxable_value = %s, cgst_amount = %s, sgst_amount = %s, igst_amount = %s,
            created_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """, (data.get('date'), data.get('party_name'), data.get('total_amount'), data.get('discount_amount', 0), data.get('gst_amount', 0),
              data.get('category'), data.get('file_url'), data.get('billing_party_name'), data.get('billing_party_gstin'), data.get('billed_to_party_gstin'),
              data.get('voucher_type') or data.get('category'),
              taxable, cgst, sgst, igst,
              inv_id))
    else:
        inv_id = str(uuid.uuid4())
        # Insert new invoice
        cursor.execute("""
        INSERT INTO invoices (id, invoice_number, date, party_name, total_amount, discount_amount, gst_amount, category, company_name, file_url, billing_party_name, billing_party_gstin, billed_to_party_gstin, voucher_type, taxable_value, cgst_amount, sgst_amount, igst_amount)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (inv_id, invoice_number, data.get('date'), data.get('party_name'),
              data.get('total_amount'), data.get('discount_amount', 0), data.get('gst_amount', 0), data.get('category'), company_name, data.get('file_url'),
              data.get('billing_party_name'), data.get('billing_party_gstin'), data.get('billed_to_party_gstin'),
              data.get('voucher_type') or data.get('category'),
              taxable, cgst, sgst, igst))
    
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


def get_all_vouchers(company_name=None, company_id=None, voucher_type=None, limit=500, offset=0):
    """Return merged vouchers from tally_vouchers + invoices, sorted by date desc.

    Each row is normalized to a common shape for the frontend:
      id, date, voucher_number, voucher_type, party_name, amount,
      narration, source ('tally'|'invoice'), ledger_entries, ...
    """
    conn = pget()   # warm pooled connection (was a fresh get_conn per call — slow)
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    # Sprint 44 — make sure deleted_from_tally_at exists on both tables before
    # the SELECTs reference it. Idempotent + flag-cached after the first call.
    _ensure_tally_delete_column(cursor)
    results = []

    # --- Tally vouchers ---
    tally_where = ["1=1"]
    tally_params = []
    if company_id:
        tally_where.append("company_id = %s")
        tally_params.append(company_id)
    elif company_name:
        tally_where.append("company_name = %s")
        tally_params.append(company_name)
    if voucher_type and voucher_type.lower() != 'all':
        tally_where.append("LOWER(voucher_type) = LOWER(%s)")
        tally_params.append(voucher_type)

    cursor.execute(f"""
        SELECT id, date, voucher_number, voucher_type, ledger_name AS party_name,
               amount, narration, ledger_entries::text AS ledger_entries,
               reference_no, place_of_supply, currency,
               cost_centres::text AS cost_centres, bill_refs::text AS bill_refs,
               taxable_value, cgst_amount, sgst_amount, igst_amount,
               tally_master_id, instrument_number, reconciled,
               COALESCE(needs_resync, FALSE) AS needs_resync, last_edited_at,
               'tally' AS source, updated_at, created_at,
               COALESCE(origin, 'tally') AS origin, yantrai_uid,
               (archived_at IS NOT NULL) AS archived,
               deleted_from_tally_at
        FROM tally_vouchers
        WHERE {' AND '.join(tally_where)}
        ORDER BY date DESC
        LIMIT %s OFFSET %s
    """, tally_params + [limit, offset])
    tally_rows = cursor.fetchall()
    for r in tally_rows:
        # Parse JSON text fields back
        try:
            r['ledger_entries'] = json.loads(r['ledger_entries']) if r['ledger_entries'] else []
        except Exception:
            r['ledger_entries'] = []
        try:
            r['cost_centres'] = json.loads(r['cost_centres']) if r['cost_centres'] else []
        except Exception:
            r['cost_centres'] = []
        try:
            r['bill_refs'] = json.loads(r['bill_refs']) if r['bill_refs'] else []
        except Exception:
            r['bill_refs'] = []
        # Normalize date to string
        if r.get('date'):
            r['date'] = str(r['date'])
        r['created_at'] = str(r['created_at']) if r.get('created_at') else None
        # Sprint 44 — stringify the delete-from-tally timestamp for the UI.
        if r.get('deleted_from_tally_at'):
            r['deleted_from_tally_at'] = str(r['deleted_from_tally_at'])
        results.append(r)

    # --- Invoice-created vouchers (from invoices table) ---
    # Sprint 33 — build two parallel where-lists: bare (for COUNT) and i.-qualified
    # (for the JOIN query) so we don't do brittle string replacement.
    inv_where = ["1=1"]
    inv_qualified_where = ["1=1"]
    inv_params = []
    if company_name:
        inv_where.append("company_name = %s")
        inv_qualified_where.append("i.company_name = %s")
        inv_params.append(company_name)
    if voucher_type and voucher_type.lower() != 'all':
        inv_where.append("LOWER(category) = LOWER(%s)")
        inv_qualified_where.append("LOWER(i.category) = LOWER(%s)")
        inv_params.append(voucher_type)

    # Sprint 33 — also surface the invoice's own status, and let a pushed
    # tally_outbox row authoritatively mark it 'synced' (the bridge agent
    # ack'd it into Tally). This keeps the Vouchers list status column honest
    # vs the end-to-end Tally sync state shown in Event Logs.
    cursor.execute(f"""
        SELECT i.id, i.created_at AS date, i.invoice_number AS voucher_number,
               COALESCE(i.voucher_type, i.category) AS voucher_type, i.party_name,
               i.total_amount AS amount, '' AS narration,
               '[]' AS ledger_entries, '' AS reference_no,
               '' AS place_of_supply, 'INR' AS currency,
               '[]' AS cost_centres, '[]' AS bill_refs,
               COALESCE(i.taxable_value,0) AS taxable_value, COALESCE(i.cgst_amount,0) AS cgst_amount,
               COALESCE(i.sgst_amount,0) AS sgst_amount, COALESCE(i.igst_amount,0) AS igst_amount,
               NULL AS tally_master_id, '' AS instrument_number, FALSE AS reconciled,
               FALSE AS needs_resync, NULL AS last_edited_at,
               'invoice' AS source, i.created_at AS updated_at, i.created_at AS created_at,
               'yantrai' AS origin, NULL AS yantrai_uid,
               (i.archived_at IS NOT NULL) AS archived,
               i.deleted_from_tally_at,
               i.file_url,
               CASE
                 WHEN ob.pushed_state THEN 'synced'
                 ELSE COALESCE(i.status, 'pending')
               END AS status,
               ob.tally_voucher_guid,
               COALESCE(ob.pushed_state, FALSE) AS was_pushed
        FROM invoices i
        LEFT JOIN LATERAL (
            SELECT BOOL_OR(o.state = 'pushed') AS pushed_state,
                   MAX(o.tally_voucher_guid) AS tally_voucher_guid
            FROM tally_outbox o
            WHERE o.company_name = i.company_name
              AND o.payload->>'invoice_number' = i.invoice_number
        ) ob ON TRUE
        WHERE {' AND '.join(inv_qualified_where)}
        ORDER BY i.created_at DESC
        LIMIT %s OFFSET %s
    """, inv_params + [limit, offset])
    inv_rows = cursor.fetchall()
    for r in inv_rows:
        if r.get('date'):
            r['date'] = str(r['date'])
        r['created_at'] = str(r['created_at']) if r.get('created_at') else None
        # Sprint 44 — stringify the delete-from-tally timestamp for the UI.
        if r.get('deleted_from_tally_at'):
            r['deleted_from_tally_at'] = str(r['deleted_from_tally_at'])
        r['ledger_entries'] = []
        r['cost_centres'] = []
        r['bill_refs'] = []
        results.append(r)

    # Sprint 33/34 — Dedupe ONLY a PDF/invoice row against its Tally twin.
    # IMPORTANT: never collapse tally-vs-tally — Tally voucher numbers repeat
    # across voucher types (Payment 1, Receipt 1, Sales 1, blanks…), so keying
    # on number alone would wrongly hide hundreds of distinct vouchers.
    # Rule: if an invoice-source row's voucher_number also exists as a
    # tally-source row, drop the invoice row (keep Tally) and carry its file_url.
    # Sticky-origin dedup: a voucher that exists in YantrAI (invoices) is YantrAI-origin,
    # period. When its Tally sync-back ALSO appears (tally_vouchers), collapse to ONE row and
    # KEEP the YantrAI invoice row (origin=YantrAI, with file_url). Match a Tally row to its
    # YantrAI twin in priority order:
    #   (1) round-trip marker  tally.yantrai_uid == invoice.id   (definitive)
    #   (2) shared voucher_number (only against pushed/synced invoices)
    #   (3) party + abs(amount) + voucher_type signature (only against pushed/synced invoices)
    # (2)/(3) are restricted to pushed/synced invoices because only those can have a Tally twin
    # — this avoids hiding a genuinely-distinct Tally voucher that merely looks similar.
    def _sig(r):
        party = (r.get('party_name') or '').strip().lower()
        try: amt = round(abs(float(r.get('amount') or 0)), 2)
        except Exception: amt = 0
        vt = (r.get('voucher_type') or '').strip().lower()
        return (party, amt, vt)

    inv_rows_local = [r for r in results if r.get('source') != 'tally']
    invoice_ids = {str(r.get('id')) for r in inv_rows_local if r.get('id')}
    pushed_inv = [r for r in inv_rows_local
                  if r.get('was_pushed') or r.get('status') == 'synced']
    pushed_nums = {(r.get('voucher_number') or '').strip().lower()
                   for r in pushed_inv if (r.get('voucher_number') or '').strip()}
    pushed_sigs = {_sig(r) for r in pushed_inv}

    deduped = []
    for r in results:
        if r.get('source') == 'tally':
            num = (r.get('voucher_number') or '').strip().lower()
            yuid = str(r.get('yantrai_uid')) if r.get('yantrai_uid') else None
            is_yantrai_twin = (
                (yuid and yuid in invoice_ids)            # (1) definitive marker
                or (num and num in pushed_nums)           # (2) number match (pushed only)
                or (_sig(r) in pushed_sigs)               # (3) signature match (pushed only)
            )
            if is_yantrai_twin:
                # Tally sync-back of a YantrAI voucher → hide it; the YantrAI invoice row
                # (origin=YantrAI, with its file_url) is the single canonical row.
                continue
        deduped.append(r)
    results = deduped

    # Sort merged by date descending
    results.sort(key=lambda r: r.get('date') or '', reverse=True)

    # Get total count for pagination (counts ALL rows incl. archived, matching the
    # "Status: any" view which shows every line; the Status column marks archived ones).
    cursor.execute(f"SELECT COUNT(*) FROM tally_vouchers WHERE {' AND '.join(tally_where)}", tally_params)
    tally_total = cursor.fetchone()['count']
    cursor.execute(f"SELECT COUNT(*) FROM invoices WHERE {' AND '.join(inv_where)}", inv_params)
    inv_total = cursor.fetchone()['count']

    cursor.close()
    pput(conn)   # return the pooled connection (was conn.close())

    # Sprint 33 — total reflects the deduped set (the raw sum would double-count
    # invoices that have a Tally twin).
    return {
        "vouchers": results[:limit],
        "total": len(results),
        "tally_count": tally_total,
        "invoice_count": inv_total,
    }

def save_correction(field, original, corrected, party_name=None, embedding=None, company_name="Acme Corp"):
    conn = get_conn()
    cursor = conn.cursor()
    data = {
        "field": field,
        "original": original,
        "corrected": corrected,
        "party_name": party_name,
        "company_name": company_name,
        # exact embedded text (RAG transparency) — what the correction memory matches on
        "content": f"For {party_name or 'any party'}: {field} should be '{corrected}' (not '{original}')."
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

def training_stats(company_name="Acme Corp"):
    """Real AI-training stats for a company: how many learned correction mappings
    exist and how many are vectorized (embedded). Confidence = vectorized ratio."""
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*), COUNT(embedding) FROM knowledge_base "
            "WHERE type = 'correction' AND data->>'company_name' = %s",
            (company_name,))
        total, vectorized = cursor.fetchone()
        total = int(total or 0); vectorized = int(vectorized or 0)
    finally:
        cursor.close(); conn.close()
    if total == 0:
        confidence = 0.0; status = "Untrained"
    else:
        confidence = round(vectorized * 100.0 / total, 1)
        status = "Vectorized" if vectorized == total else ("Training" if vectorized else "Pending")
    return {"total_mappings": total, "vectorized": vectorized,
            "confidence_score": confidence, "status": status}

# Tunable benchmarks — "fully learned" target per knowledge type. Per-type % =
# min(100, learned/target); overall training % = average of per-type %.
TRAINING_TARGETS = {
    "correction": 50,
    "tally_master_ledger": 100,
    "tally_master_party": 100,
    "tally_master_item": 100,
    "tally_master_narration": 200,
    "bank_reconciliation": 100,
}

def training_metrics(company_name="Acme Corp"):
    """Benchmark-based training %: each learning type scored against a target, rolled
    into an overall %. Returns {overall_pct, per_type:{type:{count,target,pct}}}."""
    bd = training_breakdown(company_name)
    per_type = {}
    pcts = []
    for t, target in TRAINING_TARGETS.items():
        cnt = int(bd.get(t, 0) or 0)
        pct = min(100, round(cnt * 100.0 / target)) if target else 0
        per_type[t] = {"count": cnt, "target": target, "pct": pct}
        pcts.append(pct)
    overall = round(sum(pcts) / len(pcts)) if pcts else 0
    return {"overall_pct": overall, "per_type": per_type}

def inference_accuracy(company_name="Acme Corp"):
    """Accuracy % = right ÷ total AI inferences, from real outcome signals:
    bank (matched ÷ ai_touched) + vouchers (extractions − corrections). Returns
    {accuracy_pct|None, right_inferences, total_inferences}."""
    conn = get_conn(); cur = conn.cursor()
    bank_ai = bank_matched = extractions = corrections = 0
    try:
        try:
            cur.execute("SELECT COUNT(*) FILTER (WHERE ai_touched), "
                        "COUNT(*) FILTER (WHERE ai_touched AND status='matched') "
                        "FROM bank_transactions WHERE company_name = %s", (company_name,))
            r = cur.fetchone() or (0, 0); bank_ai = int(r[0] or 0); bank_matched = int(r[1] or 0)
        except Exception as e:
            print(f"[inference_accuracy bank] {e}")
        try:
            cur.execute("SELECT COUNT(*) FROM invoices WHERE company_name = %s", (company_name,))
            extractions = int((cur.fetchone() or [0])[0] or 0)
        except Exception as e:
            print(f"[inference_accuracy inv] {e}")
        try:
            cur.execute("SELECT COUNT(*) FROM knowledge_base WHERE type='correction' "
                        "AND data->>'company_name' = %s", (company_name,))
            corrections = int((cur.fetchone() or [0])[0] or 0)
        except Exception as e:
            print(f"[inference_accuracy corr] {e}")
    finally:
        cur.close(); conn.close()
    # Voucher accuracy = (extractions − corrections)/extractions, but ONLY when corrections
    # represent per-invoice edits (corrections <= extractions). If corrections far exceed
    # invoices (bulk-trained masters, not edit feedback), the ratio is meaningless → omit
    # the voucher term rather than report a misleading 0%.
    if extractions > 0 and corrections <= extractions:
        v_total = extractions
        v_right = extractions - corrections
    else:
        v_total = 0
        v_right = 0
    right = bank_matched + v_right
    total = bank_ai + v_total
    pct = round(right * 100.0 / total, 1) if total else None
    return {"accuracy_pct": pct, "right_inferences": right, "total_inferences": total}

def training_totals(company_name="Acme Corp"):
    """Headline 'how trained' across ALL learning types (corrections + masters +
    bank-reco), not just corrections — matches the per-type breakdown the UI shows."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*), COUNT(embedding) FROM knowledge_base "
                    "WHERE data->>'company_name' = %s", (company_name,))
        total, vect = cur.fetchone()
        total = int(total or 0); vect = int(vect or 0)
    except Exception as e:
        print(f"[training_totals] {e}"); total = vect = 0
    finally:
        cur.close(); conn.close()
    pct = round(vect * 100.0 / total, 1) if total else 0.0
    status = "Untrained" if total == 0 else ("Vectorized" if vect == total else "Training")
    return {"total_mappings": total, "vectorized": vect, "confidence_score": pct, "status": status}

def training_breakdown(company_name="Acme Corp"):
    """Count of learned items per knowledge_base type for a company (corrections +
    synced Tally masters + bank-reco patterns). Powers the Training Progress breakdown."""
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT type, COUNT(*) FROM knowledge_base "
            "WHERE data->>'company_name' = %s GROUP BY type",
            (company_name,))
        rows = cursor.fetchall() or []
    except Exception as e:
        print(f"[training_breakdown] {e}"); rows = []
    finally:
        cursor.close(); conn.close()
    return {str(t): int(c or 0) for (t, c) in rows}

def recent_training(company_name="Acme Corp", limit=25):
    """Most-recent learning events for a company → a training-log timeline."""
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute(
            "SELECT type, data, created_at FROM knowledge_base "
            "WHERE data->>'company_name' = %s ORDER BY created_at DESC LIMIT %s",
            (company_name, int(limit)))
        rows = cursor.fetchall() or []
    except Exception as e:
        print(f"[recent_training] {e}"); rows = []
    finally:
        cursor.close(); conn.close()
    out = []
    for r in rows:
        d = r.get("data") or {}
        out.append({
            "type": r.get("type"),
            "field": d.get("field"), "original": d.get("original"),
            "corrected": d.get("corrected"), "party": d.get("party_name") or d.get("party") or d.get("name"),
            "created_at": (r.get("created_at").isoformat() if r.get("created_at") else None),
        })
    return out

def _reconstruct_kb_content(kb_type, d):
    """Human-readable text for a knowledge_base row that predates stored `content`."""
    d = d or {}
    if kb_type == 'tally_master_ledger':
        s = f"Ledger '{d.get('name','?')}'"
        if d.get('parent_group'): s += f" under group '{d['parent_group']}'"
        if d.get('gstin'): s += f" · GSTIN {d['gstin']}"
        return s
    if kb_type == 'tally_master_party':
        n = d.get('party') or d.get('name') or '?'
        s = f"Party '{n}'"
        if d.get('transaction_count'): s += f" · {d['transaction_count']} transactions"
        return s
    if kb_type == 'tally_master_item':
        s = f"Item '{d.get('name','?')}'"
        hsn = d.get('hsn') or d.get('hsn_code')
        if hsn: s += f" · HSN {hsn}"
        if d.get('gst_rate'): s += f" · GST {d['gst_rate']}%"
        return s
    if kb_type == 'tally_master_narration':
        if d.get('narration'): return f"Narration: {d['narration']}"
        return (f"Narration on voucher #{d.get('voucher_number','?')} for "
                f"{d.get('party','?')} ₹{d.get('amount','')} (older entry — text not stored)")
    if kb_type == 'correction':
        return f"{d.get('field','?')}: '{d.get('original','')}' → '{d.get('corrected','')}'"
    if kb_type == 'bank_reconciliation':
        return d.get('pattern') or d.get('narration') or d.get('description') or 'Bank-reco pattern'
    extra = {k: v for k, v in d.items() if k not in ('company_name', 'company_id', 'kb_key', 'content')}
    return json.dumps(extra)[:200]


def list_training_items(company_name, kb_type, limit=100, offset=0):
    """The actual learned entries of one type (content + whether vectorized)."""
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT data, (embedding IS NOT NULL) AS vectorized, created_at
                       FROM knowledge_base WHERE data->>'company_name'=%s AND type=%s
                       ORDER BY created_at DESC LIMIT %s OFFSET %s""",
                    (company_name, kb_type, int(limit), int(offset)))
        out = []
        for r in cur.fetchall():
            d = r["data"] or {}
            out.append({"content": d.get("content") or _reconstruct_kb_content(kb_type, d),
                        "vectorized": bool(r["vectorized"]),
                        "created_at": r["created_at"].isoformat() if r.get("created_at") else None})
        return out
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


def retrieve_training_matches(company_name, query_embedding, kb_type=None, k=8):
    """RAG preview: nearest learned items to a query embedding (with similarity score)."""
    if not query_embedding:
        return []
    emb = "[" + ",".join(map(str, query_embedding)) + "]"
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if kb_type:
            cur.execute("""SELECT type, data, 1 - (embedding <=> %s::vector) AS score
                           FROM knowledge_base WHERE data->>'company_name'=%s AND type=%s
                             AND embedding IS NOT NULL
                           ORDER BY embedding <=> %s::vector LIMIT %s""",
                        (emb, company_name, kb_type, emb, int(k)))
        else:
            cur.execute("""SELECT type, data, 1 - (embedding <=> %s::vector) AS score
                           FROM knowledge_base WHERE data->>'company_name'=%s AND embedding IS NOT NULL
                           ORDER BY embedding <=> %s::vector LIMIT %s""",
                        (emb, company_name, emb, int(k)))
        out = []
        for r in cur.fetchall():
            d = r["data"] or {}
            out.append({"type": r["type"], "score": round(float(r["score"] or 0), 3),
                        "content": d.get("content") or _reconstruct_kb_content(r["type"], d)})
        return out
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


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


def get_learned_party_names(company_name):
    """Distinct party names the company has taught (tally_master_party). Used by the
    reconciler for deterministic name-in-narration matching."""
    if not company_name:
        return []
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""SELECT DISTINCT data->>'party' FROM knowledge_base
                       WHERE type='tally_master_party' AND data->>'company_name' = %s
                         AND COALESCE(data->>'party','') <> ''""", (company_name,))
        return [r[0] for r in cur.fetchall() if r[0]]
    except Exception as e:
        print(f"[get_learned_party_names] {e}"); return []
    finally:
        cur.close(); conn.close()


def count_company_tally_embeddings(company_name):
    """How many learned tally_master_* embeddings exist for THIS company — used to
    show an accurate, workspace-scoped count in the reconciliation progress UI
    (replaces a hardcoded placeholder). Returns int."""
    if not company_name:
        return 0
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""SELECT COUNT(*) FROM knowledge_base
                       WHERE data->>'company_name' = %s
                         AND type LIKE 'tally_master_%%' AND embedding IS NOT NULL""",
                    (company_name,))
        r = cur.fetchone()
        return int(r[0]) if r else 0
    except Exception as e:
        print(f"[count_company_tally_embeddings] {e}"); return 0
    finally:
        cur.close(); conn.close()


def semantic_search_tally(query_embedding, company_name, kb_types, limit=5):
    """Search the new tally_master_* embeddings by cosine distance.

    kb_types: list like ['tally_master_ledger', 'tally_master_party', 'tally_master_narration']
    Returns list of {type, data, distance} sorted by distance ascending (best first).
    """
    if not query_embedding or not kb_types:
        return []
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    embedding_str = f"[{','.join(map(str, query_embedding))}]"
    placeholders = ','.join(['%s'] * len(kb_types))
    cursor.execute(f"""
        SELECT type, data, (embedding <=> %s::vector) AS distance
        FROM knowledge_base
        WHERE type IN ({placeholders})
          AND data->>'company_name' = %s
          AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """, [embedding_str] + list(kb_types) + [company_name, embedding_str, limit])
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [{"type": r["type"], "data": r["data"], "distance": float(r["distance"])} for r in rows]


def get_ledger_master_for_company(company_id=None, company_name=None):
    """Return all ledger names + their parent_group for the active company.
    Used to constrain AI suggestions to real ledgers (avoid hallucination).
    """
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    if company_id:
        cursor.execute("""
            SELECT name, display_name, parent_group, ledger_type, closing_balance, is_sensitive
            FROM tally_ledgers WHERE company_id = %s ORDER BY parent_group, name
        """, (company_id,))
    elif company_name:
        cursor.execute("""
            SELECT name, display_name, parent_group, ledger_type, closing_balance, is_sensitive
            FROM tally_ledgers WHERE company_name = %s ORDER BY parent_group, name
        """, (company_name,))
    else:
        cursor.execute("""
            SELECT name, display_name, parent_group, ledger_type, closing_balance, is_sensitive
            FROM tally_ledgers ORDER BY parent_group, name
        """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def resolve_gst_ledgers(company_name=None, company_id=None):
    """Map the customer's REAL GST tax ledgers (from their own Tally dump) per head,
    so a push never hard-codes a name like 'IGST Output' that the customer may not use.

    Why: JMK's live Tally rejects 'IGST Output' because their real, used IGST ledger is
    'IGST Tax' (closing ~₹13.3L); 'IGST Output' is a stale simulator stub (−35,000, zero
    real usage). We have the dump but never reconcile the *tax* legs before pushing.

    Strategy — among Duties & Taxes ledgers, bucket candidates per head (igst/cgst/sgst)
    and split Output vs Input ('input' in the name ⇒ input). Rank each bucket by:
      (1) USAGE — how often the ledger actually appears in tally_vouchers.ledger_entries
          (so stale/simulator stubs with zero usage lose to the real, used ledger), then
      (2) abs(closing_balance) desc as a tiebreak.

    Returns: {igst_out, cgst_out, sgst_out, igst_in, cgst_in, sgst_in} — each the real
    ledger name (str) or None when the customer has no such ledger at all.
    """
    out = {"igst_out": None, "cgst_out": None, "sgst_out": None,
           "igst_in": None, "cgst_in": None, "sgst_in": None}
    if not company_name and not company_id:
        return out
    conn = pget()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 1) candidate tax ledgers (Duties & Taxes group)
        if company_id:
            cur.execute("""
                SELECT name, COALESCE(closing_balance, 0) AS bal
                FROM tally_ledgers
                WHERE company_id = %s
                  AND (parent_group ILIKE %s OR group_path ILIKE %s)
            """, (company_id, '%dut%', '%dut%'))
        else:
            cur.execute("""
                SELECT name, COALESCE(closing_balance, 0) AS bal
                FROM tally_ledgers
                WHERE company_name = %s
                  AND (parent_group ILIKE %s OR group_path ILIKE %s)
            """, (company_name, '%dut%', '%dut%'))
        cands = cur.fetchall()
        # 2) usage map: how often each ledger name appears across voucher legs
        #    (CASE-guard the LATERAL so non-array ledger_entries can't raise)
        usage = {}
        try:
            if company_id:
                cur.execute("""
                    SELECT COALESCE(le->>'ledger_name', le->>'ledger') AS lname, COUNT(*) AS n
                    FROM tally_vouchers tv
                    CROSS JOIN LATERAL jsonb_array_elements(
                        CASE WHEN jsonb_typeof(tv.ledger_entries) = 'array'
                             THEN tv.ledger_entries ELSE '[]'::jsonb END) le
                    WHERE tv.company_id = %s
                    GROUP BY 1
                """, (company_id,))
            else:
                cur.execute("""
                    SELECT COALESCE(le->>'ledger_name', le->>'ledger') AS lname, COUNT(*) AS n
                    FROM tally_vouchers tv
                    CROSS JOIN LATERAL jsonb_array_elements(
                        CASE WHEN jsonb_typeof(tv.ledger_entries) = 'array'
                             THEN tv.ledger_entries ELSE '[]'::jsonb END) le
                    WHERE tv.company_name = %s
                    GROUP BY 1
                """, (company_name,))
            for r in cur.fetchall():
                if r["lname"]:
                    usage[r["lname"].strip().lower()] = int(r["n"] or 0)
        except Exception as e:
            print(f"[resolve_gst_ledgers] usage scan skipped: {e}")
        cur.close()
    finally:
        pput(conn)

    def _pick(head):
        """Return (best_output_name, best_input_name) for a tax head."""
        outs, ins = [], []
        for c in cands:
            nm = (c["name"] or "").strip()
            low = nm.lower()
            if head not in low:
                continue
            rank = (usage.get(low, 0), abs(float(c["bal"] or 0)))  # (usage, |balance|)
            (ins if "input" in low else outs).append((rank, nm))
        outs.sort(reverse=True); ins.sort(reverse=True)
        return (outs[0][1] if outs else None), (ins[0][1] if ins else None)

    for head in ("igst", "cgst", "sgst"):
        o, i = _pick(head)
        out[head + "_out"], out[head + "_in"] = o, i
    return out


# ═══════════════════════════════════════════════════════════════
# 360° Bank Transactions — ingestion + CRUD + health
# ═══════════════════════════════════════════════════════════════

def save_statement_upload(company_id, company_name, file_url, original_name,
                          bank_ledger=None, period_from=None, period_to=None,
                          line_count=0, total_credit=0, total_debit=0, sha256_hex=None,
                          uploaded_by=None):
    """Insert a row in bank_statement_uploads. Returns the new id."""
    conn = get_conn()
    cur = conn.cursor()
    new_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO bank_statement_uploads
            (id, company_id, company_name, file_url, original_name, bank_ledger,
             period_from, period_to, line_count, total_credit, total_debit,
             sha256, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (new_id, company_id, company_name, file_url, original_name, bank_ledger,
          period_from, period_to, line_count, total_credit, total_debit,
          sha256_hex, uploaded_by))
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def find_statement_upload_by_sha(company_id, sha256_hex):
    """Return the existing row if a file with this sha exists for the company, else None."""
    if not sha256_hex:
        return None
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, original_name, uploaded_at FROM bank_statement_uploads
        WHERE company_id = %s AND sha256 = %s
        ORDER BY uploaded_at DESC LIMIT 1
    """, (company_id, sha256_hex))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def _lookup_company_id_by_name(cur, company_name):
    """Last-resort lookup of company_id by name. Uses an open cursor so it
    participates in the caller's transaction."""
    if not company_name:
        return None
    try:
        cur.execute("SELECT id FROM companies WHERE name = %s LIMIT 1", (company_name,))
        row = cur.fetchone()
        if not row:
            return None
        # cursor may be tuple-based or RealDictCursor
        return str(row[0]) if not isinstance(row, dict) else str(row["id"])
    except Exception as e:
        print(f"[_lookup_company_id_by_name] {e}")
        return None


def save_bank_transactions(rows):
    """Bulk insert into bank_transactions. Each row is a dict with the canonical
    fields. Returns count inserted + skipped (already-existing) duplicates.

    Per-line dedup: before inserting a 'bank_statement' or 'invoice' row, check if
    a row already exists for the same (company_id, source, date, amount, reference)
    OR (company_id, source, date, amount, description-token-overlap). If yes, skip.
    Tally rows are deduped via the existing unique index.

    Sprint 10: if a row arrives with company_id=NULL but company_name set, we
    look it up and patch the row in-place so it never lands as orphaned.
    """
    if not rows:
        return {"inserted": 0, "skipped_existing": 0, "skipped_error": 0}
    conn = get_conn()
    cur = conn.cursor()
    inserted = 0
    skipped_existing = 0   # row already existed in DB
    skipped_error = 0       # DB error (other than dup)

    # Sprint 10 — last-resort company_id resolution per batch (cached)
    _name_to_id_cache = {}
    null_cid_fixed = 0
    null_cid_unresolved = 0
    for r in rows:
        if not r.get("company_id") and r.get("company_name"):
            cname = r["company_name"]
            if cname not in _name_to_id_cache:
                _name_to_id_cache[cname] = _lookup_company_id_by_name(cur, cname)
            resolved = _name_to_id_cache[cname]
            if resolved:
                r["company_id"] = resolved
                null_cid_fixed += 1
            else:
                null_cid_unresolved += 1
    if null_cid_fixed or null_cid_unresolved:
        print(f"[save_bank_transactions] last-resort company_id: fixed={null_cid_fixed} unresolved={null_cid_unresolved}", flush=True)

    for r in rows:
        # Pre-check for per-line existing duplicates (statement / invoice / manual)
        if r["source"] in ("bank_statement", "invoice", "manual"):
            try:
                cur.execute("""
                    SELECT 1 FROM bank_transactions
                    WHERE company_id = %s AND source = %s
                          AND date = %s AND amount = %s
                          AND (
                              (reference IS NOT NULL AND reference = %s)
                              OR
                              (description IS NOT NULL AND %s IS NOT NULL AND description = %s)
                          )
                    LIMIT 1
                """, (
                    r.get("company_id"), r["source"],
                    r["date"], r["amount"],
                    r.get("reference") or "", r.get("description"), r.get("description"),
                ))
                if cur.fetchone():
                    skipped_existing += 1
                    continue
            except Exception as dup_check_err:
                print(f"[save_bank_transactions dup-check] {dup_check_err}")
                # Fall through to insert and let DB-level constraint catch any clash

        try:
            cur.execute("SAVEPOINT btx")
            cur.execute("""
                INSERT INTO bank_transactions
                    (id, company_id, company_name, source, source_record_id,
                     source_file_id, source_row_idx, source_payload,
                     date, value_date, description, reference, amount, currency,
                     bank_ledger, party, head, voucher_type,
                     instrument_type, instrument_number, payment_favouring,
                     status, confidence, rationale, match_reason, linked_id, created_by,
                     ai_touched, human_touched)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s)
            """, (
                r.get("id") or str(uuid.uuid4()),
                r.get("company_id"), r["company_name"], r["source"],
                r.get("source_record_id"), r.get("source_file_id"),
                r.get("source_row_idx"),
                json.dumps(r.get("source_payload") or {}),
                r["date"], r.get("value_date"), r.get("description"),
                r.get("reference"), r["amount"], r.get("currency", "INR"),
                r.get("bank_ledger"), r.get("party"), r.get("head"),
                r.get("voucher_type"),
                r.get("instrument_type"), r.get("instrument_number"),
                r.get("payment_favouring"),
                r.get("status", "unmatched"), r.get("confidence", 0),
                r.get("rationale"), r.get("match_reason"),
                r.get("linked_id"), r.get("created_by"),
                bool(r.get("ai_touched", False)),
                bool(r.get("human_touched", False)),
            ))
            cur.execute("RELEASE SAVEPOINT btx")
            inserted += 1
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT btx")
            if "duplicate key" in str(e).lower():
                skipped_existing += 1
            else:
                skipped_error += 1
                print(f"[save_bank_transactions] {e}")
    conn.commit()
    cur.close()
    conn.close()
    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_error": skipped_error,
        # Keep legacy key for callers that read 'skipped'
        "skipped": skipped_existing + skipped_error,
    }


def ingest_bank_from_tally(company_id):
    """Walk tally_vouchers for the company and emit a bank_transactions row for
    every voucher leg that hits a Bank/Cash ledger. Idempotent."""
    if not company_id:
        return {"inserted": 0, "skipped": 0, "reason": "no company_id"}

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Find the set of bank/cash ledger names for this company
    cur.execute("""
        SELECT name, parent_group FROM tally_ledgers WHERE company_id = %s
    """, (company_id,))
    ledger_rows = cur.fetchall()
    bank_set = set()
    cash_set = set()
    ledgers_by_name = {}     # Sprint 17 — look up parent_group when deciding Head
    for L in ledger_rows:
        pg = (L["parent_group"] or "").lower()
        n = L["name"]
        ledgers_by_name[n] = {"parent_group": pg}
        if "bank account" in pg:
            bank_set.add(n)
        if "cash" in pg or n.lower() == "cash":
            cash_set.add(n)
    bank_or_cash = bank_set | cash_set
    if not bank_or_cash:
        cur.close(); conn.close()
        return {"inserted": 0, "skipped": 0, "reason": "no bank/cash ledgers"}

    # Pull all vouchers + check existing bank_transactions to avoid duplicates
    cur.execute("""
        SELECT id, date, voucher_number, ledger_name, amount, voucher_type,
               instrument_number, narration, ledger_entries, reference_no,
               company_name
        FROM tally_vouchers
        WHERE company_id = %s
    """, (company_id,))
    vouchers = cur.fetchall()

    cur.execute("""
        SELECT source_record_id, bank_ledger
        FROM bank_transactions
        WHERE company_id = %s AND source = 'tally'
    """, (company_id,))
    existing = {(str(r["source_record_id"]), r["bank_ledger"]) for r in cur.fetchall()}
    cur.close()
    conn.close()

    rows_to_insert = []
    for v in vouchers:
        entries = v.get("ledger_entries") or []
        if isinstance(entries, str):
            try: entries = json.loads(entries)
            except: entries = []
        if not entries:
            # Fallback: if no ledger_entries, but voucher's ledger_name is a bank
            if v["ledger_name"] in bank_or_cash:
                key = (str(v["id"]), v["ledger_name"])
                if key not in existing:
                    rows_to_insert.append({
                        "company_id": company_id, "company_name": v["company_name"],
                        "source": "tally", "source_record_id": str(v["id"]),
                        "source_payload": dict(v) if False else None,
                        "date": v["date"], "description": v.get("narration"),
                        "reference": v.get("reference_no") or v.get("voucher_number"),
                        "amount": v["amount"] or 0,
                        "bank_ledger": v["ledger_name"],
                        "party": None, "head": None,
                        "voucher_type": v.get("voucher_type"),
                        "instrument_type": None,
                        "instrument_number": v.get("instrument_number"),
                        "payment_favouring": None,
                        "status": "matched", "confidence": 1.0,
                        "rationale": "Imported from Tally voucher",
                        "match_reason": "tally_ground_truth",
                        "created_by": "tally_sync",
                        "human_touched": True, "ai_touched": False,
                    })
            continue

        # Find bank-leg entries and the "other side"
        bank_entries = [e for e in entries if (e.get("ledger_name") or e.get("ledger") or "") in bank_or_cash]
        other_entries = [e for e in entries if (e.get("ledger_name") or e.get("ledger") or "") not in bank_or_cash]

        for be in bank_entries:
            be_name = be.get("ledger_name") or be.get("ledger")
            be_amount_raw = float(be.get("amount") or 0)
            key = (str(v["id"]), be_name)
            if key in existing:
                continue

            # Sprint 18 — Normalize amount sign to BANK perspective.
            # Tally's per-ledger `amount` already encodes the sign per Tally's
            # convention (Cr=+, Dr=-). From the bank's perspective these are
            # OPPOSITE (cash IN = Dr bank = +ve in our model; cash OUT = Cr bank
            # = -ve in our model). So a simple NEGATE converts the convention.
            # Works correctly for:
            #   - Payment vouchers (Cr bank +ve → -ve cash out)
            #   - Receipt vouchers (Dr bank -ve → +ve cash in)
            #   - Contra-Receipt or Contra-Payment vouchers with TWO bank legs
            #     (each leg keeps its own opposite sign, so IDBI Cr +850000
            #      and ICICI Dr -850000 become -850000 and +850000 respectively)
            # If is_debit is explicitly set on the leg, prefer it (most reliable).
            is_debit = be.get("is_debit")
            vt = (v.get("voucher_type") or "").lower()
            if is_debit is True:
                be_amount = abs(be_amount_raw)
            elif is_debit is False:
                be_amount = -abs(be_amount_raw)
            elif be_amount_raw != 0:
                be_amount = -be_amount_raw
            else:
                # Last-resort fallback for vouchers with neither sign nor flag
                if vt == "payment":   be_amount = -abs(be_amount_raw)
                elif vt == "receipt": be_amount = abs(be_amount_raw)
                else: be_amount = be_amount_raw

            # Sprint 17 — Party = first non-bank ledger.
            # Head = first non-bank ledger that is NOT a Sundry party (i.e. a real
            # expense / revenue / asset account). Leave blank if the only non-bank
            # leg is the Sundry party (typical for Payment / Receipt vouchers).
            party = None
            head = None
            for oe in other_entries:
                oe_name = oe.get("ledger_name") or oe.get("ledger") or ""
                if not oe_name: continue
                if party is None:
                    party = oe_name
                oe_pg = (ledgers_by_name.get(oe_name, {}).get("parent_group") or "").lower()
                is_sundry = ("sundry" in oe_pg) or ("debtor" in oe_pg) or ("creditor" in oe_pg)
                if head is None and not is_sundry:
                    head = oe_name

            # Instrument from bank_allocations (if present)
            alloc = (be.get("bank_allocations") or [None])[0] if be.get("bank_allocations") else None
            inst_type = (alloc or {}).get("transaction_type") if alloc else None
            inst_num = (alloc or {}).get("instrument_number") or v.get("instrument_number") if alloc else v.get("instrument_number")
            favour = (alloc or {}).get("payment_favouring") if alloc else None
            value_date = None
            if alloc and alloc.get("bank_date"):
                bd = str(alloc["bank_date"])
                if len(bd) == 8 and bd.isdigit():
                    try:
                        from datetime import date as _d
                        value_date = _d(int(bd[:4]), int(bd[4:6]), int(bd[6:]))
                    except: pass

            rows_to_insert.append({
                "company_id": company_id, "company_name": v["company_name"],
                "source": "tally", "source_record_id": str(v["id"]),
                "source_payload": be,
                "date": v["date"], "value_date": value_date,
                "description": v.get("narration") or v.get("voucher_number"),
                "reference": v.get("reference_no") or v.get("voucher_number"),
                "amount": be_amount,
                "bank_ledger": be_name,
                "party": party, "head": head,
                "voucher_type": v.get("voucher_type"),
                "instrument_type": inst_type,
                "instrument_number": inst_num,
                "payment_favouring": favour,
                "status": "matched", "confidence": 1.0,
                "rationale": f"Imported from Tally voucher {v.get('voucher_number') or ''}",
                "match_reason": "tally_ground_truth",
                "created_by": "tally_sync",
                "human_touched": True, "ai_touched": False,
            })

    return save_bank_transactions(rows_to_insert)


def ingest_bank_from_invoices(company_id, company_name):
    """Emit a bank_transactions row for every paid invoice. Idempotent."""
    if not company_id or not company_name:
        return {"inserted": 0, "skipped": 0}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, invoice_number, date, party_name, total_amount,
               category, status, billing_party_name
        FROM invoices
        WHERE company_name = %s AND (status = 'paid' OR status = 'reconciled')
    """, (company_name,))
    invs = cur.fetchall()
    cur.execute("""
        SELECT source_record_id FROM bank_transactions
        WHERE company_id = %s AND source = 'invoice'
    """, (company_id,))
    existing = {str(r["source_record_id"]) for r in cur.fetchall()}
    cur.close()
    conn.close()

    rows = []
    for inv in invs:
        if str(inv["id"]) in existing:
            continue
        category = (inv.get("category") or "").lower()
        amount = float(inv.get("total_amount") or 0)
        # Sales invoice → inflow (positive). Purchase / vendor invoice → outflow (negative).
        signed_amount = abs(amount) if category == "sales" else -abs(amount)
        rows.append({
            "company_id": company_id, "company_name": company_name,
            "source": "invoice", "source_record_id": str(inv["id"]),
            "source_payload": dict(inv) if False else None,
            "date": inv.get("date"),
            "description": f"Invoice {inv.get('invoice_number')}",
            "reference": inv.get("invoice_number"),
            "amount": signed_amount,
            "party": inv.get("party_name") or inv.get("billing_party_name"),
            "voucher_type": "Receipt" if signed_amount > 0 else "Payment",
            "status": "ai_filled", "confidence": 0.9,
            "rationale": "Auto-imported from paid invoice",
            "match_reason": "invoice_paid",
            "created_by": "invoice_extract",
            "ai_touched": True, "human_touched": False,
        })
    return save_bank_transactions(rows)


def link_bank_transactions(company_id):
    """Cross-source linking: pair rows from different sources that represent
    the same real-world event. Sets linked_id and bumps status to 'matched'.

    Sprint 19 — Scored greedy linker:
      1. Enumerate ALL candidate pairs (cross-source, same ABS amount, ≤7d apart).
      2. Score each by evidence strength (UTR exact > UTR overlap > party+bank > bank+day).
      3. Sort by score DESCENDING. Greedy-pick: highest-scoring pair wins.
      4. Once a row is used, it can't be paired again.
    This avoids the previous "first-match wins" bug where a weak token coincidence
    burned a row that should have linked to a stronger candidate.
    """
    if not company_id:
        return {"linked_pairs": 0}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, source, date, amount, reference, party, bank_ledger,
               description, bank_ledger AS bnk
        FROM bank_transactions
        WHERE company_id = %s AND linked_id IS NULL
    """, (company_id,))
    rows = cur.fetchall()

    # Sprint 18 — Pre-extract UTR/UPI/cheque-style tokens from BOTH `reference`
    # AND `description`. Bank statements put the UTR in the description (e.g.
    # "INF/INFT/043999133351/JMK...") while Tally's `reference` is the voucher
    # number (e.g. "16"). Matching on these long numeric tokens is the strongest
    # signal real-world bank lines share.
    import re as _re
    def _ref_tokens(row):
        out = set()
        for fld in ("reference", "description"):
            text = (row.get(fld) or "")
            # Long alnum sequences (10+ chars): UTR (16 digits), IFT (12 digits),
            # UPI ref (12-22 chars), cheque-like (6-10 digits) — we use 10 as a
            # threshold to avoid false matches on small numbers.
            for m in _re.findall(r'[A-Za-z0-9]{10,}', text):
                out.add(m.upper())
        return out
    for r in rows:
        r["_tokens"] = _ref_tokens(r)

    linked = 0
    by_amount = {}
    for r in rows:
        key = round(abs(float(r["amount"])), 2)
        by_amount.setdefault(key, []).append(r)

    # Sprint 19 — score every candidate pair, then greedy-pick highest first.
    def _score_pair(a, b):
        """Returns (score, reason) — higher is stronger evidence. 0 means reject."""
        if a["source"] == b["source"]: return (0, "same-source")
        # Date proximity
        if a["date"] and b["date"]:
            d_diff = abs((a["date"] - b["date"]).days)
            if d_diff > 7: return (0, "date-too-far")
        else:
            d_diff = 7
        # Bank ledger guard — never link across different bank ledgers when both set
        bnk_a = (a.get("bank_ledger") or "").lower()
        bnk_b = (b.get("bank_ledger") or "").lower()
        if bnk_a and bnk_b and bnk_a != bnk_b:
            return (0, "bank-mismatch")
        # Tokens
        toks_a = a.get("_tokens") or set()
        toks_b = b.get("_tokens") or set()
        common = toks_a & toks_b
        # Reference exact / substring overlap
        ref_a = (a.get("reference") or "").lower()
        ref_b = (b.get("reference") or "").lower()
        ref_hit = bool(ref_a) and bool(ref_b) and (ref_a in ref_b or ref_b in ref_a)
        # Party tokens
        party_a = (a.get("party") or "").lower()
        party_b = (b.get("party") or "").lower()
        party_hit = bool(party_a) and bool(party_b) and any(
            t for t in party_a.split() if len(t) > 3 and t in party_b
        )
        # Score: longer common tokens > shorter; same-day > week-old; same-bank bonus
        score = 0
        if common:
            # Reward by longest common token; UTR-length (16) is much stronger than 10
            longest = max(len(t) for t in common)
            score += 100 + longest * 2 + len(common) * 5
        if ref_hit: score += 80
        if party_hit: score += 30
        if bnk_a and bnk_b and bnk_a == bnk_b: score += 20
        # Date penalty
        score -= d_diff * 3
        # Same-bank-day fallback (lowest tier — kicks in when nothing else fires)
        if score == 0 - d_diff * 3:
            if bnk_a and bnk_b and bnk_a == bnk_b and d_diff <= 2:
                score = 40 - d_diff * 3
        return (score, f"d_diff={d_diff} common={len(common)} ref_hit={ref_hit} party_hit={party_hit} samebank={bnk_a==bnk_b}")

    candidate_pairs = []  # list of (score, a_id, b_id)
    for amt, group in by_amount.items():
        if len(group) < 2: continue
        # All cross-source pairs within the group
        for i in range(len(group)):
            for j in range(i+1, len(group)):
                a, b = group[i], group[j]
                if a["source"] == b["source"]: continue
                score, _ = _score_pair(a, b)
                if score > 0:
                    candidate_pairs.append((score, str(a["id"]), str(b["id"])))

    # Sort highest-score first. Greedy pick.
    candidate_pairs.sort(key=lambda x: x[0], reverse=True)
    used_ids = set()
    pairs = []
    for score, a_id, b_id in candidate_pairs:
        if a_id in used_ids or b_id in used_ids: continue
        pairs.append((a_id, b_id))
        used_ids.add(a_id); used_ids.add(b_id)

    for a_id, b_id in pairs:
        try:
            cur.execute("""
                UPDATE bank_transactions SET linked_id = %s, status = 'matched',
                       updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (b_id, a_id))
            cur.execute("""
                UPDATE bank_transactions SET linked_id = %s, status = 'matched',
                       updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (a_id, b_id))
            linked += 1
        except Exception as e:
            print(f"[link_bank_transactions] {e}")
    conn.commit()
    cur.close()
    conn.close()
    return {"linked_pairs": linked}


def list_bank_transactions(company_id=None, company_name=None, source=None, status=None,
                            from_date=None, to_date=None, q=None, view="per_source",
                            sort="date_desc", limit=500, offset=0, tally_status=None,
                            bank_ledger=None, source_file_id=None):
    """Return a filtered list of bank_transactions for the active company."""
    where = []
    params = []
    # Sprint 10 — OR-clause fallback so any row that slipped in with
    # company_id=NULL but a matching company_name is still visible.
    if company_id and company_name:
        where.append("(bt.company_id = %s OR (bt.company_id IS NULL AND bt.company_name = %s))")
        params.extend([company_id, company_name])
    elif company_id:
        # P0 FIX: do NOT return company_id IS NULL rows here — that leaked every
        # orphan row to every tenant. Strict company_id scoping only.
        where.append("bt.company_id = %s")
        params.append(company_id)
    elif company_name:
        where.append("bt.company_name = %s"); params.append(company_name)
    # Vouchers section is now the single master of "exists in Tally". This view
    # reconciles ONLY bulk-uploaded bank statements, so it shows nothing but
    # statement lines — duplicate source='tally' rows (and invoices/manual) are
    # excluded here. Each line's Tally status is derived from whether it matched
    # a voucher in the master (status='matched'/linked_id), set by the reconciler.
    where.append("bt.source = 'bank_statement'")
    if source:
        where.append("bt.source = %s"); params.append(source)
    if status:
        where.append("bt.status = %s"); params.append(status)
    # Sprint 12 — derived "Tally Status" filter (presets over source/status/linked_id)
    TS_CLAUSES = {
        'needs_attention': "bt.status IN ('ai_filled','unmatched')",
        # "Done" = anything not needing triage. Covers source=tally, status=matched
        # (Phase 1 deterministic matches without persisted linked_id), status=posted,
        # and rows with linked_id set. Simpler invariant: not in the needs-attention set.
        'done':            "bt.status NOT IN ('ai_filled','unmatched')",
        'ai_ready':        "bt.status='ai_filled'",
        'needs_review':    "bt.status='unmatched'",
        'linked':          "bt.linked_id IS NOT NULL",
        'posted':          "bt.status='posted'",
    }
    if tally_status and tally_status in TS_CLAUSES:
        where.append(TS_CLAUSES[tally_status])
    # Sprint 14 — filter by bank / cash ledger
    if bank_ledger:
        where.append("bt.bank_ledger = %s"); params.append(bank_ledger)
    # Filter to a single uploaded statement (source_file_id → bank_statement_uploads).
    if source_file_id:
        where.append("bt.source_file_id = %s"); params.append(source_file_id)
    if from_date:
        where.append("bt.date >= %s"); params.append(from_date)
    if to_date:
        where.append("bt.date <= %s"); params.append(to_date)
    if q:
        where.append("(bt.description ILIKE %s OR bt.party ILIKE %s OR bt.reference ILIKE %s)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    # Note: the old "collapsed" view rolled paired Tally rows up against their
    # statement line. With only bank_statement rows shown now, there is no Tally
    # side to collapse — the `view` param is kept for API compatibility but inert.

    # Sort
    sort_map = {
        "date_desc":   "bt.date DESC, bt.created_at DESC",
        "date_asc":    "bt.date ASC, bt.created_at ASC",
        "amount_desc": "ABS(bt.amount) DESC, bt.date DESC",
        "amount_asc":  "ABS(bt.amount) ASC, bt.date DESC",
        "status":      "bt.status, bt.date DESC",
        "source":      "bt.source, bt.date DESC",
    }
    order_by = sort_map.get(sort, sort_map["date_desc"])

    where_sql = " AND ".join(where) if where else "TRUE"
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"""
        SELECT bt.*,
               bsu.original_name AS source_file_name,
               bsu.file_url AS source_file_url
        FROM bank_transactions bt
        LEFT JOIN bank_statement_uploads bsu ON bsu.id = bt.source_file_id
        WHERE {where_sql}
        ORDER BY {order_by}
        LIMIT %s OFFSET %s
    """, params + [limit, offset])
    rows = cur.fetchall()
    cur.execute(f"""
        SELECT COUNT(*) AS n FROM bank_transactions bt WHERE {where_sql}
    """, params)
    total = cur.fetchone()["n"]

    # 4-state Tally Status aggregate over the uploaded statement lines (all rows
    # here are source='bank_statement'). "In Tally" was dropped — a line that
    # matched a voucher in the master now rolls into Linked. Mutually exclusive,
    # evaluated in the same order the cell renders them:
    #   Posted    → status='posted'
    #   Linked    → linked_id IS NOT NULL  OR  status='matched'  (Phase-1 voucher-master match)
    #   AI Ready  → status='ai_filled'
    #   Needs Review → status='unmatched'
    cur.execute(f"""
        SELECT
          SUM(CASE WHEN bt.status='posted' THEN 1 ELSE 0 END) AS posted,
          SUM(CASE WHEN bt.status<>'posted'
                    AND (bt.linked_id IS NOT NULL OR bt.status='matched')
                   THEN 1 ELSE 0 END) AS linked,
          SUM(CASE WHEN bt.status='ai_filled' THEN 1 ELSE 0 END) AS ai_ready,
          SUM(CASE WHEN bt.status='unmatched' THEN 1 ELSE 0 END) AS needs_review
        FROM bank_transactions bt
        WHERE {where_sql}
    """, params)
    s = cur.fetchone() or {}
    stats = {
        "linked":       int(s.get("linked")       or 0),
        "ai_ready":     int(s.get("ai_ready")     or 0),
        "needs_review": int(s.get("needs_review") or 0),
        "posted":       int(s.get("posted")       or 0),
    }

    cur.close()
    conn.close()
    return {"rows": rows, "total": total, "stats": stats}


# ════════════════════════════════════════════════════════════════════
# SPRINT 26 — AI Gap Scan for tally_vouchers
# ════════════════════════════════════════════════════════════════════

# Recognised GSTIN shape: 2-digit state code + 10-char PAN + 1 entity + Z + checksum
_GSTIN_RE = None
def _gstin_regex():
    global _GSTIN_RE
    if _GSTIN_RE is None:
        import re as _re
        _GSTIN_RE = _re.compile(r'\b\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}Z[A-Z\d]{1}\b')
    return _GSTIN_RE


def _resolve_co(company_id, company_name):
    """Resolve company_id from company_name if only the latter was given."""
    if company_id:
        return company_id
    if not company_name:
        return None
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM companies WHERE name = %s LIMIT 1", (company_name,))
        r = cur.fetchone()
        return str(r[0]) if r else None
    finally:
        cur.close(); conn.close()


def _purge_pending_suggestions(company_name):
    """Idempotent: remove prior pending proposals so a re-scan doesn't double-write."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM voucher_ai_suggestions WHERE company_name = %s AND status = 'pending'",
                    (company_name,))
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        cur.close(); conn.close()


def _insert_suggestions(rows):
    """Bulk insert. Each row is a dict with keys voucher_id, company_id, company_name,
    gap_type, field, current_value, suggested_value, confidence, source, rationale,
    payload, scan_run_id."""
    if not rows:
        return 0
    conn = get_conn(); cur = conn.cursor()
    try:
        from psycopg2.extras import execute_values
        execute_values(cur, """
            INSERT INTO voucher_ai_suggestions
                (voucher_id, company_id, company_name, gap_type, field,
                 current_value, suggested_value, confidence, source,
                 rationale, payload, scan_run_id)
            VALUES %s
        """, [(
            r["voucher_id"], r.get("company_id"), r["company_name"], r["gap_type"],
            r.get("field"), r.get("current_value"), r.get("suggested_value"),
            r.get("confidence") or 0, r.get("source"), r.get("rationale"),
            json.dumps(r.get("payload") or {}) if r.get("payload") else None,
            r.get("scan_run_id"),
        ) for r in rows])
        conn.commit()
        return len(rows)
    finally:
        cur.close(); conn.close()


def _detect_missing_gstin(company_id, company_name, scan_run_id):
    """For Sales/Purchase vouchers missing party_gstin, propose a fill from
    (1) tally_ledgers party-name match, (2) peer-voucher consensus from same
    party that DO have GSTIN, (3) regex extraction from narration."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # 1) Master ledger GSTIN lookup map: ledger_name -> gstin
        cur.execute("""
            SELECT name, gstin FROM tally_ledgers
            WHERE company_name = %s AND gstin IS NOT NULL AND gstin != ''
        """, (company_name,))
        ledger_gstin = {r["name"]: r["gstin"] for r in cur.fetchall() if r["name"]}

        # 2) Peer-voucher consensus: most common GSTIN per party from other vouchers
        cur.execute("""
            SELECT ledger_name AS party, party_gstin, COUNT(*) AS n
            FROM tally_vouchers
            WHERE company_name = %s AND party_gstin IS NOT NULL AND party_gstin != ''
            GROUP BY ledger_name, party_gstin
            ORDER BY ledger_name, n DESC
        """, (company_name,))
        peer = {}
        for r in cur.fetchall():
            peer.setdefault(r["party"], r["party_gstin"])  # first wins (highest count)

        # 3) Pull missing-GSTIN vouchers (Sales / Purchase only)
        cur.execute("""
            SELECT id, ledger_name AS party, narration, voucher_number
            FROM tally_vouchers
            WHERE company_name = %s
              AND voucher_type IN ('Sales','Purchase')
              AND (party_gstin IS NULL OR party_gstin = '')
              AND (discarded_at IS NULL)
        """, (company_name,))
        gaps = cur.fetchall()

        rx = _gstin_regex()
        out = []
        for v in gaps:
            party = v.get("party") or ""
            narr  = v.get("narration") or ""
            sug = None; conf = 0.0; source = None; rationale = None
            # 1) ledger master
            if party and party in ledger_gstin:
                sug = ledger_gstin[party]; conf = 0.95; source = "ledger_master"
                rationale = f"GSTIN found on '{party}' in Ledger Master."
            # 2) peer voucher consensus
            elif party in peer:
                sug = peer[party]; conf = 0.90; source = "peer_voucher"
                rationale = f"Same party uses GSTIN {sug} on other vouchers."
            # 3) regex on narration
            else:
                m = rx.search(narr.upper()) if narr else None
                if m:
                    sug = m.group(0); conf = 0.75; source = "narration_regex"
                    rationale = f"GSTIN-shaped token found in narration."
            if sug:
                out.append({
                    "voucher_id": str(v["id"]), "company_id": company_id,
                    "company_name": company_name, "gap_type": "missing_gstin",
                    "field": "party_gstin", "current_value": None,
                    "suggested_value": sug, "confidence": conf,
                    "source": source, "rationale": rationale,
                    "scan_run_id": scan_run_id,
                })
        return out
    finally:
        cur.close(); conn.close()


def _detect_duplicates(company_id, company_name, scan_run_id):
    """For each (voucher_number, ledger_name, amount) group with COUNT>1, pick
    the keeper = the row with the most populated fields; propose discarding the rest."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Identify duplicate groups
        cur.execute("""
            SELECT voucher_number, ledger_name, amount,
                   ARRAY_AGG(id ORDER BY created_at) AS ids
            FROM tally_vouchers
            WHERE company_name = %s AND discarded_at IS NULL
              AND voucher_number IS NOT NULL AND voucher_number != ''
            GROUP BY voucher_number, ledger_name, amount
            HAVING COUNT(*) > 1
        """, (company_name,))
        groups = cur.fetchall()
        out = []
        for g in groups:
            ids = [str(x) for x in g["ids"]]
            # Score each candidate by populated-field count to pick a keeper
            cur.execute("""
                SELECT id, voucher_number, narration, party_gstin, reference_no,
                       place_of_supply, taxable_value, cgst_amount, sgst_amount, igst_amount
                FROM tally_vouchers WHERE id = ANY(%s)
            """, (ids,))
            scored = []
            for row in cur.fetchall():
                score = sum(1 for k in row.keys() if k != "id" and row[k] not in (None, '', 0))
                scored.append((score, str(row["id"])))
            scored.sort(reverse=True)
            keeper = scored[0][1]
            siblings = [vid for _, vid in scored[1:]]
            # One suggestion per duplicate sibling (the row that should be discarded)
            for sib in siblings:
                out.append({
                    "voucher_id": sib, "company_id": company_id,
                    "company_name": company_name, "gap_type": "duplicate",
                    "field": "discarded_at", "current_value": None,
                    "suggested_value": "now()",
                    "confidence": 0.88,
                    "source": "duplicate_pair",
                    "rationale": f"Duplicate of voucher {g['voucher_number']} (keeper: {keeper[:8]}…). Same number, party, amount.",
                    "payload": {"keeper_voucher_id": keeper, "sibling_voucher_ids": siblings, "group_size": len(ids)},
                    "scan_run_id": scan_run_id,
                })
        return out
    finally:
        cur.close(); conn.close()


def _detect_unbalanced(company_id, company_name, scan_run_id):
    """Vouchers whose ledger_entries SUM(debit) != SUM(credit) within ±0.01.
    Surface for review — no auto-fix value (user must edit)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, voucher_number, ledger_entries
            FROM tally_vouchers
            WHERE company_name = %s AND discarded_at IS NULL
              AND ledger_entries IS NOT NULL
              AND jsonb_array_length(COALESCE(ledger_entries::jsonb, '[]'::jsonb)) > 0
        """, (company_name,))
        out = []
        for v in cur.fetchall():
            entries = v["ledger_entries"]
            if isinstance(entries, str):
                try: entries = json.loads(entries)
                except: entries = []
            if not entries: continue
            dr = 0.0; cr = 0.0
            for e in entries:
                amt = float(e.get("amount") or 0)
                is_debit = e.get("is_debit")
                if is_debit is True:
                    dr += abs(amt)
                elif is_debit is False:
                    cr += abs(amt)
                elif amt < 0:
                    cr += abs(amt)
                else:
                    dr += amt
            diff = round(dr - cr, 2)
            if abs(diff) > 0.01:
                out.append({
                    "voucher_id": str(v["id"]), "company_id": company_id,
                    "company_name": company_name, "gap_type": "unbalanced",
                    "field": "ledger_entries", "current_value": f"Dr ₹{dr:.2f} / Cr ₹{cr:.2f}",
                    "suggested_value": None,
                    "confidence": 1.0,
                    "source": "computed",
                    "rationale": f"Voucher {v['voucher_number']}: Dr {dr:.2f}, Cr {cr:.2f}, diff {diff:+.2f}.",
                    "payload": {"dr_total": dr, "cr_total": cr, "diff": diff},
                    "scan_run_id": scan_run_id,
                })
        return out
    finally:
        cur.close(); conn.close()


def _detect_missing_head_and_narration(company_id, company_name, scan_run_id):
    """Vouchers missing narration; propose a 1-line summary from voucher_type + party + amount.
    'Missing head' for now is just flagged if no non-bank/non-cash counter-leg exists in
    ledger_entries — surfaces for human review (we don't auto-guess the head ledger)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, voucher_number, voucher_type, ledger_name AS party, amount, narration, ledger_entries
            FROM tally_vouchers
            WHERE company_name = %s AND discarded_at IS NULL
              AND (narration IS NULL OR narration = '')
            LIMIT 5000
        """, (company_name,))
        out = []
        for v in cur.fetchall():
            vt = v.get("voucher_type") or "Voucher"
            party = v.get("party") or ""
            amt = float(v.get("amount") or 0)
            sug = f"{vt} of ₹{abs(amt):,.2f}" + (f" to {party}" if party else "")
            out.append({
                "voucher_id": str(v["id"]), "company_id": company_id,
                "company_name": company_name, "gap_type": "missing_narration",
                "field": "narration", "current_value": None,
                "suggested_value": sug, "confidence": 0.60,
                "source": "template",
                "rationale": "Voucher had no narration; generated a 1-line summary from voucher_type + party + amount.",
                "scan_run_id": scan_run_id,
            })
        return out
    finally:
        cur.close(); conn.close()


def run_voucher_ai_scan(company_id, company_name, gap_types=None):
    """Dispatcher. Clears prior pending suggestions for the company and re-runs
    the requested detectors. Returns {run_id, totals: {gap_type → count}}."""
    if not company_name:
        return {"error": "company_name required"}
    company_id = _resolve_co(company_id, company_name)
    purged = _purge_pending_suggestions(company_name)
    run_id = str(uuid.uuid4())
    detectors = {
        "missing_gstin":     _detect_missing_gstin,
        "duplicate":         _detect_duplicates,
        "unbalanced":        _detect_unbalanced,
        "missing_narration": _detect_missing_head_and_narration,
    }
    if gap_types:
        detectors = {k: v for k, v in detectors.items() if k in gap_types}
    totals = {}
    all_rows = []
    for gt, fn in detectors.items():
        try:
            rows = fn(company_id, company_name, run_id)
            totals[gt] = len(rows)
            all_rows.extend(rows)
        except Exception as e:
            print(f"[ai_scan] detector {gt} failed: {e}", flush=True)
            totals[gt] = 0
    _insert_suggestions(all_rows)
    return {"run_id": run_id, "totals": totals, "purged_pending": purged,
            "company_id": company_id, "company_name": company_name}


def list_ai_suggestions(company_name, gap_type=None, status="pending", limit=10000):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        where = ["s.company_name = %s", "s.status = %s"]
        params = [company_name, status]
        if gap_type:
            where.append("s.gap_type = %s"); params.append(gap_type)
        cur.execute(f"""
            SELECT s.id, s.voucher_id, s.gap_type, s.field, s.current_value,
                   s.suggested_value, s.confidence, s.source, s.rationale,
                   s.payload, s.status, s.created_at,
                   v.voucher_number, v.voucher_type, v.ledger_name AS party,
                   v.amount, v.date AS voucher_date, v.party_gstin
            FROM voucher_ai_suggestions s
            LEFT JOIN tally_vouchers v ON v.id = s.voucher_id
            WHERE {' AND '.join(where)}
            ORDER BY s.confidence DESC, s.created_at DESC
            LIMIT %s
        """, params + [limit])
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def ai_suggestion_counts(company_name):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT gap_type, COUNT(*) FROM voucher_ai_suggestions
            WHERE company_name = %s AND status = 'pending'
            GROUP BY gap_type
        """, (company_name,))
        return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        cur.close(); conn.close()


def accept_ai_suggestion(suggestion_id):
    """Apply the suggested value to the parent tally_voucher and mark accepted."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, voucher_id, gap_type, field, suggested_value, payload, status
            FROM voucher_ai_suggestions WHERE id = %s
        """, (suggestion_id,))
        s = cur.fetchone()
        if not s:
            return {"ok": False, "message": "Suggestion not found."}
        if s["status"] != "pending":
            return {"ok": False, "message": f"Suggestion already {s['status']}."}
        gt = s["gap_type"]; field = s["field"]; sug = s["suggested_value"]
        if gt == "duplicate":
            # Soft-discard this voucher (the sibling, not the keeper).
            cur.execute("UPDATE tally_vouchers SET discarded_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (s["voucher_id"],))
        elif gt == "unbalanced":
            # No value to write — accept means "I've reviewed, please dismiss".
            pass
        elif gt == "missing_gstin" and field == "party_gstin" and sug:
            cur.execute("UPDATE tally_vouchers SET party_gstin = %s WHERE id = %s",
                        (sug, s["voucher_id"]))
        elif gt == "missing_narration" and field == "narration" and sug:
            cur.execute("UPDATE tally_vouchers SET narration = %s WHERE id = %s",
                        (sug, s["voucher_id"]))
        # Mark accepted
        cur.execute("UPDATE voucher_ai_suggestions SET status='accepted', updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (suggestion_id,))
        conn.commit()
        return {"ok": True}
    finally:
        cur.close(); conn.close()


def reject_ai_suggestion(suggestion_id):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE voucher_ai_suggestions SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status='pending'",
                    (suggestion_id,))
        conn.commit()
        return {"ok": cur.rowcount > 0}
    finally:
        cur.close(); conn.close()


def bulk_accept_ai_suggestions(company_name, gap_type=None, min_confidence=0.0):
    """Accept all pending suggestions matching the filter. Returns count applied."""
    rows = list_ai_suggestions(company_name, gap_type=gap_type, status="pending")
    applied = 0
    for r in rows:
        if (r.get("confidence") or 0) < min_confidence:
            continue
        res = accept_ai_suggestion(str(r["id"]))
        if res.get("ok"): applied += 1
    return {"applied": applied, "scanned": len(rows)}


# ════════════════════════════════════════════════════════════════════
# SPRINT 27 — Master AI Gap Scan (Party + Item)
# ════════════════════════════════════════════════════════════════════

# Indian state code → name (used to fill `place_of_supply` from GSTIN[0:2])
_STATE_CODES = {
    '01':'Jammu and Kashmir','02':'Himachal Pradesh','03':'Punjab','04':'Chandigarh',
    '05':'Uttarakhand','06':'Haryana','07':'Delhi','08':'Rajasthan','09':'Uttar Pradesh',
    '10':'Bihar','11':'Sikkim','12':'Arunachal Pradesh','13':'Nagaland','14':'Manipur',
    '15':'Mizoram','16':'Tripura','17':'Meghalaya','18':'Assam','19':'West Bengal',
    '20':'Jharkhand','21':'Odisha','22':'Chhattisgarh','23':'Madhya Pradesh','24':'Gujarat',
    '25':'Daman and Diu','26':'Dadra and Nagar Haveli','27':'Maharashtra','28':'Andhra Pradesh',
    '29':'Karnataka','30':'Goa','31':'Lakshadweep','32':'Kerala','33':'Tamil Nadu',
    '34':'Puducherry','35':'Andaman and Nicobar Islands','36':'Telangana','37':'Andhra Pradesh',
    '38':'Ladakh','97':'Other Territory','99':'Centre Jurisdiction',
}

# Tiny built-in HSN → GST rate table (top items; extend as needed).
_HSN_GST_RATES = {
    '7606': 18.0,   # Aluminium plates, sheets, strip
    '7607': 18.0,   # Aluminium foil
    '7308': 18.0,   # Iron / steel structures
    '7210': 18.0,   # Flat-rolled iron / steel
    '1006': 5.0,    # Rice
    '1101': 5.0,    # Wheat flour
    '2106': 18.0,   # Food preparations nesoi
    '8443': 18.0,   # Printing machinery
    '9018': 18.0,   # Medical instruments
    '4901': 0.0,    # Printed books
    '4820': 12.0,   # Registers / notebooks
    '3923': 18.0,   # Plastic articles for packaging
    '3926': 18.0,   # Other plastic articles
    '8517': 18.0,   # Telephones / smartphones
    '6403': 18.0,   # Footwear with leather uppers
    '6204': 12.0,   # Women's apparel
    '2202': 28.0,   # Aerated waters / sweetened drinks
    '0401': 0.0,    # Fresh milk
    '0813': 12.0,   # Dried fruits
    '9983': 18.0,   # Other professional services (SAC)
    '9954': 18.0,   # Construction services (SAC)
    '9985': 18.0,   # Support services (SAC)
    '9988': 5.0,    # Manufacturing services on physical inputs owned by others
    '9971': 18.0,   # Financial / related services
}


def _purge_pending_master_suggestions(company_name, master_type=None):
    conn = get_conn(); cur = conn.cursor()
    try:
        if master_type:
            cur.execute("""DELETE FROM master_ai_suggestions
                           WHERE company_name=%s AND master_type=%s AND status='pending'""",
                        (company_name, master_type))
        else:
            cur.execute("""DELETE FROM master_ai_suggestions
                           WHERE company_name=%s AND status='pending'""", (company_name,))
        n = cur.rowcount; conn.commit(); return n
    finally:
        cur.close(); conn.close()


def _insert_master_suggestions(rows):
    if not rows: return 0
    conn = get_conn(); cur = conn.cursor()
    try:
        from psycopg2.extras import execute_values
        execute_values(cur, """
            INSERT INTO master_ai_suggestions
                (master_type, record_id, company_id, company_name, gap_type, field,
                 current_value, suggested_value, confidence, source,
                 rationale, payload, scan_run_id)
            VALUES %s
        """, [(
            r["master_type"], r["record_id"], r.get("company_id"), r["company_name"],
            r["gap_type"], r.get("field"), r.get("current_value"),
            r.get("suggested_value"), r.get("confidence") or 0, r.get("source"),
            r.get("rationale"),
            json.dumps(r.get("payload") or {}) if r.get("payload") else None,
            r.get("scan_run_id"),
        ) for r in rows])
        conn.commit(); return len(rows)
    finally:
        cur.close(); conn.close()


def _detect_party_master_gaps(company_id, company_name, scan_run_id):
    """Detect gaps on party-like tally_ledgers rows (Sundry Debtors/Creditors)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Pull all party-type ledgers
        cur.execute("""
            SELECT id, name, gstin, pan, place_of_supply, gst_registration_type, address, parent_group
            FROM tally_ledgers
            WHERE company_name = %s
              AND (parent_group ILIKE '%%sundry%%' OR parent_group ILIKE '%%debtor%%' OR parent_group ILIKE '%%creditor%%')
        """, (company_name,))
        parties = cur.fetchall()
        # Peer-voucher consensus for missing GSTIN
        cur.execute("""
            SELECT ledger_name AS party, party_gstin, COUNT(*) AS n
            FROM tally_vouchers
            WHERE company_name = %s AND party_gstin IS NOT NULL AND party_gstin != ''
            GROUP BY ledger_name, party_gstin
            ORDER BY n DESC
        """, (company_name,))
        peer_gstin = {}
        for r in cur.fetchall():
            peer_gstin.setdefault(r["party"], r["party_gstin"])

        out = []
        for p in parties:
            pid = str(p["id"]); name = p.get("name") or ""
            gstin = (p.get("gstin") or "").strip().upper() or None
            pan = (p.get("pan") or "").strip().upper() or None
            pos = (p.get("place_of_supply") or "").strip() or None
            regt = (p.get("gst_registration_type") or "").strip() or None

            # 1) Missing GSTIN via peer consensus
            if not gstin and name in peer_gstin:
                cand = peer_gstin[name]
                out.append({
                    "master_type": "party", "record_id": pid,
                    "company_id": company_id, "company_name": company_name,
                    "gap_type": "missing_gstin", "field": "gstin",
                    "current_value": None, "suggested_value": cand,
                    "confidence": 0.90, "source": "peer_voucher",
                    "rationale": f"Other vouchers for '{name}' use GSTIN {cand}.",
                    "scan_run_id": scan_run_id,
                })
                gstin = cand  # downstream derivations use this

            # 2) Missing PAN — derive from GSTIN positions 3-12
            if not pan and gstin and len(gstin) >= 15:
                derived_pan = gstin[2:12]
                out.append({
                    "master_type": "party", "record_id": pid,
                    "company_id": company_id, "company_name": company_name,
                    "gap_type": "missing_pan", "field": "pan",
                    "current_value": None, "suggested_value": derived_pan,
                    "confidence": 0.99, "source": "derived_from_gstin",
                    "rationale": "PAN is embedded in GSTIN positions 3–12.",
                    "scan_run_id": scan_run_id,
                })

            # 3) Missing place_of_supply — derive from GSTIN state code
            if not pos and gstin and len(gstin) >= 2:
                code = gstin[0:2]
                state = _STATE_CODES.get(code)
                if state:
                    out.append({
                        "master_type": "party", "record_id": pid,
                        "company_id": company_id, "company_name": company_name,
                        "gap_type": "missing_pos", "field": "place_of_supply",
                        "current_value": None, "suggested_value": state,
                        "confidence": 0.99, "source": "derived_from_gstin",
                        "rationale": f"GSTIN state code '{code}' → {state}.",
                        "scan_run_id": scan_run_id,
                    })

            # 4) Missing gst_registration_type
            if not regt:
                if gstin:
                    sug = "Regular"; conf = 0.90
                    rationale = "Party has a GSTIN → Regular registration."
                else:
                    sug = "Unregistered"; conf = 0.60
                    rationale = "No GSTIN on record → likely Unregistered."
                out.append({
                    "master_type": "party", "record_id": pid,
                    "company_id": company_id, "company_name": company_name,
                    "gap_type": "missing_gst_registration_type", "field": "gst_registration_type",
                    "current_value": None, "suggested_value": sug,
                    "confidence": conf, "source": "derived_from_gstin",
                    "rationale": rationale,
                    "scan_run_id": scan_run_id,
                })

            # 5) Missing address (flag only)
            if not (p.get("address") or "").strip():
                out.append({
                    "master_type": "party", "record_id": pid,
                    "company_id": company_id, "company_name": company_name,
                    "gap_type": "missing_address", "field": "address",
                    "current_value": None, "suggested_value": None,
                    "confidence": 1.0, "source": "computed",
                    "rationale": "Address is blank — please fill.",
                    "scan_run_id": scan_run_id,
                })

            # Sprint 34 — 6) Party type B2B / B2C (informational, from GSTIN presence)
            ptype = "B2B (registered)" if gstin else "B2C (unregistered / consumer)"
            out.append({
                "master_type": "party", "record_id": pid,
                "company_id": company_id, "company_name": company_name,
                "gap_type": "party_type", "field": "party_type",
                "current_value": None, "suggested_value": ptype,
                "confidence": 0.95 if gstin else 0.70, "source": "derived_from_gstin",
                "rationale": ("Has GSTIN → treat as B2B for GST reporting."
                              if gstin else "No GSTIN → likely B2C (end consumer)."),
                "scan_run_id": scan_run_id,
            })

        # Sprint 34 — 7) Duplicate parties: same GSTIN OR near-identical name.
        import re as _re
        def _norm(s): return _re.sub(r'[^a-z0-9]', '', (s or '').lower())
        by_gstin = {}; by_name = {}
        for p in parties:
            g = (p.get("gstin") or "").strip().upper()
            n = _norm(p.get("name"))
            if g: by_gstin.setdefault(g, []).append(p)
            if n: by_name.setdefault(n, []).append(p)
        flagged = set()
        for group in list(by_gstin.values()) + list(by_name.values()):
            if len(group) > 1:
                keeper = group[0]
                for dup in group[1:]:
                    did = str(dup["id"])
                    if did in flagged: continue
                    flagged.add(did)
                    out.append({
                        "master_type": "party", "record_id": did,
                        "company_id": company_id, "company_name": company_name,
                        "gap_type": "duplicate_party", "field": "name",
                        "current_value": dup.get("name"),
                        "suggested_value": f"Possible duplicate of '{keeper.get('name')}'",
                        "confidence": 0.85, "source": "computed",
                        "rationale": ("Same GSTIN as another party." if (dup.get("gstin") or "").strip().upper()==(keeper.get("gstin") or "").strip().upper() and dup.get("gstin")
                                      else f"Name nearly identical to '{keeper.get('name')}'."),
                        "payload": {"keeper_id": str(keeper["id"]), "keeper_name": keeper.get("name")},
                        "scan_run_id": scan_run_id,
                    })
        return out
    finally:
        cur.close(); conn.close()


def _detect_item_master_gaps(company_id, company_name, scan_run_id):
    """Detect gaps on tally_stock_items rows. HSN inference via heuristic table;
    Gemini is a future upgrade — for now we use name-based keyword matches that
    work for the bulk of common items."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, name, hsn_code, gst_rate, unit, standard_rate,
                   opening_qty, opening_value, parent_group
            FROM tally_stock_items
            WHERE company_name = %s
        """, (company_name,))
        items = cur.fetchall()
        out = []
        # Simple keyword → HSN map for common Indian goods (extend as needed)
        keyword_hsn = [
            ('aluminum sheet', '7606'), ('aluminium sheet', '7606'),
            ('aluminium foil', '7607'), ('aluminum foil', '7607'),
            ('iron rod', '7214'), ('steel rod', '7214'),
            ('plastic bag', '3923'), ('packaging', '3923'),
            ('book', '4901'), ('register', '4820'), ('notebook', '4820'),
            ('rice', '1006'), ('wheat', '1101'), ('flour', '1101'),
            ('milk', '0401'), ('biscuit', '1905'),
            ('shoe', '6403'), ('footwear', '6403'),
            ('phone', '8517'), ('mobile', '8517'),
            ('soda', '2202'), ('drink', '2202'),
            ('professional', '9983'), ('consulting', '9983'),
            ('construction', '9954'), ('repair', '9985'),
        ]
        for it in items:
            iid = str(it["id"]); name = (it.get("name") or "").lower()
            hsn = (it.get("hsn_code") or "").strip() or None
            rate = it.get("gst_rate")
            unit = (it.get("unit") or "").strip() or None
            sr = it.get("standard_rate")

            # 1) Missing HSN via keyword heuristic
            new_hsn = None
            if not hsn:
                for kw, code in keyword_hsn:
                    if kw in name:
                        new_hsn = code; break
                if new_hsn:
                    out.append({
                        "master_type": "item", "record_id": iid,
                        "company_id": company_id, "company_name": company_name,
                        "gap_type": "missing_hsn_code", "field": "hsn_code",
                        "current_value": None, "suggested_value": new_hsn,
                        "confidence": 0.75, "source": "keyword_match",
                        "rationale": f"Item name contains a keyword matching HSN {new_hsn}.",
                        "scan_run_id": scan_run_id,
                    })

            # 2) Missing GST rate — derive from (existing OR new) HSN
            eff_hsn = hsn or new_hsn
            if (rate is None or rate == 0) and eff_hsn:
                # Try 4-digit prefix first
                cand = _HSN_GST_RATES.get(eff_hsn[:4]) if len(eff_hsn) >= 4 else None
                if cand is None and len(eff_hsn) >= 2:
                    # No fallback — only return if confident
                    pass
                if cand is not None:
                    out.append({
                        "master_type": "item", "record_id": iid,
                        "company_id": company_id, "company_name": company_name,
                        "gap_type": "missing_gst_rate", "field": "gst_rate",
                        "current_value": None, "suggested_value": str(cand),
                        "confidence": 0.95, "source": "hsn_rate_table",
                        "rationale": f"HSN {eff_hsn[:4]} maps to GST rate {cand}%.",
                        "scan_run_id": scan_run_id,
                    })

            # 3) Missing unit
            if not unit:
                u = None; conf = 0.5; src = "name_token"
                if 'sheet' in name: u = 'Sheets'
                elif 'kg' in name or 'kilo' in name: u = 'Kgs'
                elif 'ltr' in name or 'litre' in name or 'liter' in name: u = 'Ltrs'
                elif 'mtr' in name or 'meter' in name or 'metre' in name: u = 'Mtrs'
                elif 'pc' in name or 'piece' in name: u = 'Pcs'
                elif 'box' in name: u = 'Box'
                else: u = 'Nos'; conf = 0.4
                out.append({
                    "master_type": "item", "record_id": iid,
                    "company_id": company_id, "company_name": company_name,
                    "gap_type": "missing_unit", "field": "unit",
                    "current_value": None, "suggested_value": u,
                    "confidence": conf, "source": src,
                    "rationale": f"Inferred unit from item name.",
                    "scan_run_id": scan_run_id,
                })

            # 4) Missing standard_rate — compute from opening_value / opening_qty
            if (sr is None or sr == 0):
                ov = float(it.get("opening_value") or 0)
                oq = float(it.get("opening_qty") or 0)
                if oq > 0 and ov > 0:
                    val = round(ov / oq, 2)
                    out.append({
                        "master_type": "item", "record_id": iid,
                        "company_id": company_id, "company_name": company_name,
                        "gap_type": "missing_standard_rate", "field": "standard_rate",
                        "current_value": None, "suggested_value": str(val),
                        "confidence": 0.95, "source": "computed",
                        "rationale": f"Opening value {ov:.2f} ÷ opening qty {oq:.2f} = {val:.2f}.",
                        "scan_run_id": scan_run_id,
                    })

            # Sprint 34 — 5) Clean / normalized item name (Title Case, trim noise)
            raw_name = it.get("name") or ""
            cleaned = _re_clean_item_name(raw_name)
            if cleaned and cleaned != raw_name:
                out.append({
                    "master_type": "item", "record_id": iid,
                    "company_id": company_id, "company_name": company_name,
                    "gap_type": "clean_name", "field": "name",
                    "current_value": raw_name, "suggested_value": cleaned,
                    "confidence": 0.70, "source": "name_normalize",
                    "rationale": "Suggested a cleaner, properly-cased item name.",
                    "scan_run_id": scan_run_id,
                })

        # Sprint 34 — 6) Standard rate + source supplier mined from invoice line items
        try:
            cur.execute("""
                SELECT i.line_items, i.party_name, i.billing_party_name
                FROM invoices i
                WHERE i.company_name = %s AND i.line_items IS NOT NULL
            """, (company_name,))
            inv_rows = cur.fetchall()
        except Exception:
            inv_rows = []
        # Build description → {rates:[], sources:set()}
        learned = {}
        for ir in inv_rows:
            li = ir.get("line_items")
            if isinstance(li, str):
                try: li = json.loads(li)
                except: li = []
            if not isinstance(li, list): continue
            src = ir.get("billing_party_name") or ir.get("party_name")
            for ln in li:
                if not isinstance(ln, dict): continue
                desc = (ln.get("description") or ln.get("item") or "").strip().lower()
                if not desc: continue
                rate = ln.get("rate") or ln.get("price")
                entry = learned.setdefault(desc, {"rates": [], "sources": set()})
                try:
                    if rate is not None: entry["rates"].append(float(rate))
                except: pass
                if src: entry["sources"].add(src)
        for it in items:
            iid = str(it["id"]); nm = (it.get("name") or "").strip().lower()
            if nm in learned:
                e = learned[nm]
                if (it.get("standard_rate") in (None, 0)) and e["rates"]:
                    avg = round(sum(e["rates"]) / len(e["rates"]), 2)
                    out.append({
                        "master_type": "item", "record_id": iid,
                        "company_id": company_id, "company_name": company_name,
                        "gap_type": "invoice_price", "field": "standard_rate",
                        "current_value": None, "suggested_value": str(avg),
                        "confidence": 0.80, "source": "invoice_history",
                        "rationale": f"Avg rate across {len(e['rates'])} invoice line(s) = {avg:.2f}.",
                        "scan_run_id": scan_run_id,
                    })
                if e["sources"]:
                    out.append({
                        "master_type": "item", "record_id": iid,
                        "company_id": company_id, "company_name": company_name,
                        "gap_type": "source_suppliers", "field": "source",
                        "current_value": None,
                        "suggested_value": ", ".join(sorted(e["sources"])[:5]),
                        "confidence": 0.85, "source": "invoice_history",
                        "rationale": "Suppliers seen on invoices for this item.",
                        "scan_run_id": scan_run_id,
                    })

        # Sprint 34 — 7) Duplicate items (near-identical normalized names)
        seen_norm = {}
        for it in items:
            n = _re_norm_name(it.get("name"))
            if not n: continue
            seen_norm.setdefault(n, []).append(it)
        for grp in seen_norm.values():
            if len(grp) > 1:
                keeper = grp[0]
                for dup in grp[1:]:
                    out.append({
                        "master_type": "item", "record_id": str(dup["id"]),
                        "company_id": company_id, "company_name": company_name,
                        "gap_type": "duplicate_item", "field": "name",
                        "current_value": dup.get("name"),
                        "suggested_value": f"Possible duplicate of '{keeper.get('name')}'",
                        "confidence": 0.80, "source": "computed",
                        "rationale": f"Name nearly identical to '{keeper.get('name')}'.",
                        "payload": {"keeper_id": str(keeper["id"])},
                        "scan_run_id": scan_run_id,
                    })
        return out
    finally:
        cur.close(); conn.close()


def _re_norm_name(s):
    import re as _re
    return _re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _re_clean_item_name(s):
    """Light normalization: collapse whitespace, Title Case, fix common abbrevs."""
    import re as _re
    if not s: return s
    t = _re.sub(r'\s+', ' ', s.strip())
    # Don't touch names that already look clean (have mixed case + spaces)
    if t == s and any(c.isupper() for c in s) and any(c.islower() for c in s):
        return s
    # Title-case word by word, preserve all-caps tokens <= 3 chars (HSN-ish/units)
    words = []
    for w in t.split(' '):
        if len(w) <= 3 and w.isupper():
            words.append(w)
        else:
            words.append(w[:1].upper() + w[1:].lower())
    return ' '.join(words)


def run_master_ai_scan(company_id, company_name, master_types=None):
    """Dispatcher — clears prior pending suggestions then runs the requested
    detectors. Returns {run_id, totals: {gap_type → count}, by_master}."""
    if not company_name:
        return {"error": "company_name required"}
    company_id = _resolve_co(company_id, company_name)
    types = master_types or ['party', 'item']
    purged = _purge_pending_master_suggestions(company_name)
    run_id = str(uuid.uuid4())
    detectors = []
    if 'party' in types: detectors.append(('party', _detect_party_master_gaps))
    if 'item'  in types: detectors.append(('item',  _detect_item_master_gaps))
    totals = {}; by_master = {}; all_rows = []
    for label, fn in detectors:
        try:
            rows = fn(company_id, company_name, run_id)
            all_rows.extend(rows)
            by_master[label] = len(rows)
            for r in rows:
                gt = r["gap_type"]; totals[gt] = (totals.get(gt) or 0) + 1
        except Exception as e:
            print(f"[master_scan] detector {label} failed: {e}", flush=True)
            by_master[label] = 0
    _insert_master_suggestions(all_rows)
    return {"run_id": run_id, "totals": totals, "by_master": by_master,
            "purged_pending": purged, "company_id": company_id,
            "company_name": company_name}


def list_master_ai_suggestions(company_name, master_type=None, gap_type=None,
                                status="pending", limit=20000):
    """Return suggestions joined with the parent master row for display."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        where = ["s.company_name = %s"]
        params = [company_name]
        # Sprint 34 — '', None, or 'all' means no status filter (show everything)
        if status and status.lower() != "all":
            where.append("s.status = %s"); params.append(status)
        if master_type:
            where.append("s.master_type = %s"); params.append(master_type)
        if gap_type:
            where.append("s.gap_type = %s"); params.append(gap_type)
        cur.execute(f"""
            SELECT s.id, s.master_type, s.record_id, s.gap_type, s.field,
                   s.current_value, s.suggested_value, s.confidence, s.source,
                   s.rationale, s.payload, s.status, s.created_at,
                   CASE WHEN s.master_type='party' THEN l.name ELSE i.name END AS record_name,
                   CASE WHEN s.master_type='party' THEN l.parent_group ELSE i.parent_group END AS parent_group
            FROM master_ai_suggestions s
            LEFT JOIN tally_ledgers     l ON s.master_type='party' AND l.id = s.record_id
            LEFT JOIN tally_stock_items i ON s.master_type='item'  AND i.id = s.record_id
            WHERE {' AND '.join(where)}
            ORDER BY s.master_type, s.confidence DESC, s.created_at DESC
            LIMIT %s
        """, params + [limit])
        return cur.fetchall()
    finally:
        cur.close(); conn.close()


def master_ai_suggestion_counts(company_name):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT master_type, gap_type, COUNT(*) FROM master_ai_suggestions
            WHERE company_name = %s AND status = 'pending'
            GROUP BY master_type, gap_type
        """, (company_name,))
        out = {"party": {}, "item": {}}
        for r in cur.fetchall():
            out[r[0]][r[1]] = r[2]
        return out
    finally:
        cur.close(); conn.close()


# Field whitelist per master_type for safe UPDATEs on accept
_PARTY_UPDATE_FIELDS = {'gstin','pan','place_of_supply','gst_registration_type','address','display_name'}
_ITEM_UPDATE_FIELDS  = {'hsn_code','gst_rate','unit','standard_rate','parent_group','display_name'}

def accept_master_ai_suggestion(suggestion_id):
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT * FROM master_ai_suggestions WHERE id = %s""", (suggestion_id,))
        s = cur.fetchone()
        if not s: return {"ok": False, "message": "Suggestion not found."}
        if s["status"] != "pending": return {"ok": False, "message": f"Already {s['status']}."}
        mt = s["master_type"]; field = s["field"]; sug = s["suggested_value"]
        if s.get("gap_type") == "clean_name" and field == "name":
            field = "display_name"
        if mt == "party":
            if field not in _PARTY_UPDATE_FIELDS:
                return {"ok": False, "message": f"Field {field} not writable for party master."}
            if sug is None and field != 'address':
                # No suggested value (e.g., flag-only); accept just dismisses.
                pass
            else:
                cur.execute(f"UPDATE tally_ledgers SET {field} = %s WHERE id = %s",
                            (sug, s["record_id"]))
        elif mt == "item":
            if field not in _ITEM_UPDATE_FIELDS:
                return {"ok": False, "message": f"Field {field} not writable for item master."}
            if sug is not None:
                # Cast numeric where appropriate
                val = sug
                if field in ('gst_rate', 'standard_rate'):
                    try: val = float(sug)
                    except: pass
                cur.execute(f"UPDATE tally_stock_items SET {field} = %s WHERE id = %s",
                            (val, s["record_id"]))
        cur.execute("""UPDATE master_ai_suggestions SET status='accepted', updated_at=CURRENT_TIMESTAMP
                       WHERE id = %s""", (suggestion_id,))
        conn.commit()
        return {"ok": True}
    finally:
        cur.close(); conn.close()


def reject_master_ai_suggestion(suggestion_id):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE master_ai_suggestions SET status='rejected', updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s AND status='pending'""", (suggestion_id,))
        conn.commit()
        return {"ok": cur.rowcount > 0}
    finally:
        cur.close(); conn.close()


def bulk_accept_master_ai_suggestions(company_name, master_type=None, gap_type=None, min_confidence=0.0):
    rows = list_master_ai_suggestions(company_name, master_type=master_type, gap_type=gap_type, status="pending")
    applied = 0
    for r in rows:
        if (r.get("confidence") or 0) < min_confidence:
            continue
        res = accept_master_ai_suggestion(str(r["id"]))
        if res.get("ok"): applied += 1
    return {"applied": applied, "scanned": len(rows)}


# ════════════════════════════════════════════════════════════════════
# SPRINT 28 — Tally outbox: real two-way sync contract with bridge agent
# ════════════════════════════════════════════════════════════════════

_TALLY_OUTBOX_APPROVAL_READY = False


def _ensure_tally_outbox_approval(cur):
    """Sprint 43 — per-voucher ledger-create approval flow. Idempotent ALTERs.

    Adds two JSONB columns to tally_outbox:
      • needs_approval     — bundle of ledgers the agent proposes to create
                              (set by agent when state -> 'pending_approval').
      • approved_ledgers   — subset the user OKed (set by UI when state goes
                              back to 'pending'). Agent reads this on the
                              next poll and creates exactly these before
                              pushing the voucher.

    Also enables two new states for tally_outbox.state:
      • 'pending_approval' — waiting on the user to approve / reject.
      • 'rejected'         — user said no; voucher stays in YantrAI but
                              is NOT pushed to Tally.
    (state is a free-text TEXT column, so no enum migration needed.)
    """
    global _TALLY_OUTBOX_APPROVAL_READY
    if _TALLY_OUTBOX_APPROVAL_READY:
        return
    cur.execute("ALTER TABLE tally_outbox "
                "ADD COLUMN IF NOT EXISTS needs_approval JSONB")
    cur.execute("ALTER TABLE tally_outbox "
                "ADD COLUMN IF NOT EXISTS approved_ledgers JSONB")
    _TALLY_OUTBOX_APPROVAL_READY = True


def mark_outbox_needs_approval(outbox_id, bundle):
    """Agent calls this when it can't push because Tally is missing one or more
    ledgers. Flips the row to 'pending_approval' and stores the proposed
    create-bundle so the UI can render it for user opt-in."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _ensure_tally_outbox_approval(cur)
        cur.execute("""
            UPDATE tally_outbox
            SET state = 'pending_approval',
                needs_approval = %s::jsonb,
                approved_ledgers = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING id, state
        """, (json.dumps(bundle or []), outbox_id))
        row = cur.fetchone()
        conn.commit()
        return row
    finally:
        cur.close(); conn.close()


def approve_outbox_ledgers(outbox_id, approved_names):
    """UI calls this when the user clicks Approve. Flips the row back to
    'pending' and stores the approved subset so the agent's next poll
    can create exactly those, then push the voucher."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _ensure_tally_outbox_approval(cur)
        cur.execute("""
            UPDATE tally_outbox
            SET state = 'pending',
                approved_ledgers = %s::jsonb,
                updated_at = CURRENT_TIMESTAMP,
                attempts = 0          -- reset so the row is claimable again
            WHERE id = %s
              AND state = 'pending_approval'
            RETURNING id, state
        """, (json.dumps(list(approved_names or [])), outbox_id))
        row = cur.fetchone()
        conn.commit()
        return row
    finally:
        cur.close(); conn.close()


def reject_outbox_ledgers(outbox_id):
    """UI calls this when the user clicks Reject. The voucher stays in
    YantrAI books but won't be pushed to Tally."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _ensure_tally_outbox_approval(cur)
        cur.execute("""
            UPDATE tally_outbox
            SET state = 'rejected', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND state IN ('pending_approval', 'pending')
            RETURNING id, state
        """, (outbox_id,))
        row = cur.fetchone()
        conn.commit()
        return row
    finally:
        cur.close(); conn.close()


def get_outbox_approval_state(outbox_id):
    """Helper: read an outbox row's approval-related fields (used by the
    UI to render the banner)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _ensure_tally_outbox_approval(cur)
        cur.execute("""
            SELECT id, state, needs_approval, approved_ledgers, last_error
            FROM tally_outbox WHERE id = %s
        """, (outbox_id,))
        row = cur.fetchone()
        return row
    finally:
        cur.close(); conn.close()


def enqueue_tally_push(payload, invoice_id=None, voucher_id=None,
                       company_name=None, enqueued_by=None):
    """Append a row to tally_outbox in state='pending'. The bridge agent
    polls /api/tally/queue and processes pending rows."""
    if not payload:
        return None
    # Centralize GST ledger resolution at THE SINGLE chokepoint — every enqueue path
    # (chat-confirm via /push-to-tally, bulk /tally/sync-batch, resync) injects the
    # customer's REAL tax ledger (e.g. "IGST Tax" from their dump) so the agent never
    # falls back to the hardcoded "IGST Output" that doesn't exist in their Tally.
    # That mismatch was the root cause of the c0000005 Memory Access Violation — the
    # malformed Import XML crashed Tally mid-parse. Idempotent: skips if /push-to-tally
    # already injected the names. Best-effort: a resolve failure does NOT block enqueue.
    try:
        _vt_raw = (payload.get("voucher_type") or payload.get("category") or "Sales").strip()
        _vt = {"Sale": "Sales", "Sales": "Sales", "Purchase": "Purchase"}.get(
            _vt_raw.capitalize(), _vt_raw.capitalize())
        if _vt in ("Sales", "Purchase") and company_name:
            _cgst = float(payload.get("cgst_amount") or 0)
            _sgst = float(payload.get("sgst_amount") or 0)
            _igst = float(payload.get("igst_amount") or 0)
            if (_cgst > 0 or _sgst > 0 or _igst > 0) and not any(
                    payload.get(k) for k in ("igst_ledger", "cgst_ledger", "sgst_ledger")):
                _gl = resolve_gst_ledgers(company_name)
                _suffix = "in" if _vt == "Purchase" else "out"
                for _head, _amt in (("igst", _igst), ("cgst", _cgst), ("sgst", _sgst)):
                    if _amt > 0:
                        _name = _gl.get(f"{_head}_{_suffix}")
                        if _name:
                            payload[f"{_head}_ledger"] = _name
    except Exception as _ge:
        print(f"[enqueue_tally_push] gst-ledger resolve skipped: {_ge}")
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Sprint 43 — ensure approval columns exist before any insert/update.
        _ensure_tally_outbox_approval(cur)
        # Idempotency: if a non-terminal push for this invoice is already queued (pending or
        # pushing), UPDATE it in place instead of inserting a second one. This prevents a
        # duplicate Tally voucher when the user clicks Confirm & Sync again before the first
        # push completes. (After it has pushed + synced back, the /push-to-tally re-push guard
        # via tally_twin_exists() blocks re-enqueue entirely.)
        if invoice_id:
            cur.execute("""SELECT id FROM tally_outbox
                           WHERE company_name = %s AND invoice_id = %s
                             AND state IN ('pending','pushing','pending_approval')
                           ORDER BY enqueued_at DESC LIMIT 1""", (company_name, invoice_id))
            ex = cur.fetchone()
            if ex:
                cur.execute("""UPDATE tally_outbox
                               SET payload = %s::jsonb, updated_at = CURRENT_TIMESTAMP
                               WHERE id = %s RETURNING id, enqueued_at, state""",
                            (json.dumps(payload), ex["id"]))
                row = cur.fetchone(); conn.commit()
                return {"id": str(row["id"]), "state": row["state"],
                        "enqueued_at": row["enqueued_at"], "deduped": True}
        cur.execute("""
            INSERT INTO tally_outbox (invoice_id, voucher_id, company_name, payload, enqueued_by)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            RETURNING id, enqueued_at, state
        """, (invoice_id, voucher_id, company_name, json.dumps(payload), enqueued_by))
        row = cur.fetchone()
        conn.commit()
        return {"id": str(row["id"]), "state": row["state"], "enqueued_at": row["enqueued_at"]}
    finally:
        cur.close(); conn.close()


def claim_tally_outbox(company_name, limit=10, agent_id=None):
    """Bridge agent calls this to atomically grab the next N pending rows
    and flip them to state='pushing'. Returns the payloads.

    Sprint 43 — `approved_ledgers` is exposed alongside the payload so the
    agent knows on this poll whether the user has already approved a
    ledger-create bundle for this voucher. When set, the agent creates the
    listed ledgers (via _build_ledger_master_xml / _build_system_ledger_xml)
    before pushing the voucher in the same iteration."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _ensure_tally_outbox_approval(cur)
        cur.execute("""
            UPDATE tally_outbox
            SET state = 'pushing', attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id IN (
                SELECT id FROM tally_outbox
                WHERE company_name = %s
                  AND attempts < 5
                  -- P1 FIX: also reclaim rows stuck in 'pushing' (agent died/ack lost)
                  -- after a 5-min lease. Safe now that re-pushes are idempotent
                  -- (deterministic voucher numbers + Alter-by-master-id).
                  AND (state = 'pending'
                       OR (state = 'pushing'
                           AND updated_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes'))
                ORDER BY enqueued_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, invoice_id, voucher_id, payload, enqueued_at, attempts,
                      approved_ledgers
        """, (company_name, limit))
        rows = cur.fetchall()
        conn.commit()
        for r in rows:
            r["id"] = str(r["id"])
            if r.get("invoice_id"): r["invoice_id"] = str(r["invoice_id"])
            if r.get("voucher_id"): r["voucher_id"] = str(r["voucher_id"])
        return rows
    finally:
        cur.close(); conn.close()


def ack_tally_outbox(outbox_id, tally_voucher_guid=None):
    """Bridge agent confirms a successful push to Tally."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE tally_outbox
            SET state = 'pushed', tally_voucher_guid = %s,
                pushed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP,
                last_error = NULL
            WHERE id = %s
            RETURNING voucher_id
        """, (tally_voucher_guid, outbox_id))
        row = cur.fetchone()
        ok = row is not None
        # P1 FIX: on a CONFIRMED push, record Tally's GUID on the voucher so a later
        # edit ALTERs the same Tally voucher instead of creating a duplicate, and clear
        # needs_resync only now (not at enqueue time). COALESCE keeps any existing id.
        if row and row[0]:
            if tally_voucher_guid:
                cur.execute("""UPDATE tally_vouchers
                    SET tally_master_id = COALESCE(tally_master_id, %s),
                        needs_resync = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s""", (tally_voucher_guid, row[0]))
            else:
                cur.execute("""UPDATE tally_vouchers
                    SET needs_resync = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s""", (row[0],))
        conn.commit()
        return {"ok": ok}
    finally:
        cur.close(); conn.close()


def add_tally_cleanup(company_name, voucher_number, voucher_type=None, party=None,
                      amount=None, voucher_date=None, reason=None,
                      kind='delete_voucher', ledger_name=None):
    """Record a 'to do in Tally Prime' item: a voucher to delete (kind='delete_voucher',
    default) OR a missing GST/system ledger to create (kind='create_ledger', ledger_name set).
    For create_ledger, dedup on (company, ledger_name) while still pending."""
    conn = get_conn(); cur = conn.cursor()
    try:
        if kind == 'create_ledger' and ledger_name:
            cur.execute("""SELECT id FROM tally_cleanup_log
                           WHERE company_name=%s AND kind='create_ledger'
                             AND LOWER(ledger_name)=LOWER(%s) AND status='pending' LIMIT 1""",
                        (company_name, ledger_name))
            ex = cur.fetchone()
            if ex:
                return str(ex[0])
        cur.execute("""INSERT INTO tally_cleanup_log
            (company_name, voucher_number, voucher_type, party, amount, voucher_date, reason, kind, ledger_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (company_name, voucher_number, voucher_type, party, amount,
             voucher_date if voucher_date else None, reason, kind, ledger_name))
        rid = cur.fetchone()[0]; conn.commit()
        return str(rid)
    finally:
        cur.close(); conn.close()


def archive_vouchers(company_name, ids, archived_by=None):
    """Soft-archive vouchers (set archived_at) — NOTHING is deleted. Works for both
    tally_vouchers and invoices. When the archived voucher is present in Tally (a
    tally_vouchers row, or a synced invoice), also log a tally_cleanup_log entry so the
    user knows to void/remove it in Tally Prime. Returns {'archived': n}."""
    if not ids:
        return {"archived": 0}
    conn = get_conn(); cur = conn.cursor()
    n = 0
    _reason = "Archived in YantrAI — void/remove in Tally Prime"
    def _log(vnum, vtype, party, amt, vdate):
        try:
            cur.execute("""INSERT INTO tally_cleanup_log
                (company_name, voucher_number, voucher_type, party, amount, voucher_date, reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (company_name, vnum, vtype, party, amt, vdate or None, _reason))
        except Exception as ce:
            print(f"archive cleanup-log warn: {ce}")
    try:
        for vid in ids:
            cur.execute("""UPDATE tally_vouchers SET archived_at = CURRENT_TIMESTAMP
                           WHERE id = %s AND company_name = %s AND archived_at IS NULL
                           RETURNING voucher_number, voucher_type, ledger_name, amount, date""",
                        (vid, company_name))
            row = cur.fetchone()
            if row:
                n += 1
                _log(row[0], row[1], row[2], row[3], row[4])   # tally-present → cleanup checklist
                continue
            cur.execute("""UPDATE invoices SET archived_at = CURRENT_TIMESTAMP
                           WHERE id = %s AND company_name = %s AND archived_at IS NULL
                           RETURNING invoice_number, COALESCE(voucher_type, category),
                                     party_name, total_amount, date, status""",
                        (vid, company_name))
            inv = cur.fetchone()
            if inv:
                n += 1
                if (inv[5] or '') == 'synced':   # already in Tally → needs manual void there
                    _log(inv[0], inv[1], inv[2], inv[3], inv[4])
        conn.commit()
        return {"archived": n}
    except Exception as e:
        conn.rollback(); print(f"archive_vouchers error: {e}")
        return {"archived": n, "error": str(e)}
    finally:
        cur.close(); conn.close()


def unarchive_vouchers(company_name, ids):
    """Restore soft-archived vouchers (clear archived_at) in both tables. Returns {'restored': n}."""
    if not ids:
        return {"restored": 0}
    conn = get_conn(); cur = conn.cursor()
    n = 0
    try:
        for vid in ids:
            cur.execute("UPDATE tally_vouchers SET archived_at = NULL WHERE id=%s AND company_name=%s AND archived_at IS NOT NULL", (vid, company_name))
            n += cur.rowcount
            cur.execute("UPDATE invoices SET archived_at = NULL WHERE id=%s AND company_name=%s AND archived_at IS NOT NULL", (vid, company_name))
            n += cur.rowcount
        conn.commit()
        return {"restored": n}
    except Exception as e:
        conn.rollback(); print(f"unarchive_vouchers error: {e}")
        return {"restored": n, "error": str(e)}
    finally:
        cur.close(); conn.close()


# Sprint 44 — explicit "deleted from Tally" state. Distinct from archive
# (user-initiated YantrAI-only soft-delete with a manual-cleanup todo) and
# from hard-delete (drops the row entirely). When the agent successfully
# pushes a <VOUCHER ACTION="Delete"> and acks, ack_tally_outbox calls
# mark_voucher_deleted_in_tally — the YantrAI row becomes:
#   archived_at            = NOW()  (so it disappears from the active list)
#   deleted_from_tally_at  = NOW()  (so the UI can render a distinct
#                                    "Deleted (synced with Tally)" badge)
#   needs_resync           = FALSE  (defensive: nothing to resync now)
# The row is kept (no hard delete) so audit trails remain intact.
_TALLY_DELETE_READY = False


def _ensure_tally_delete_column(cur):
    global _TALLY_DELETE_READY
    if _TALLY_DELETE_READY:
        return
    cur.execute("ALTER TABLE tally_vouchers "
                "ADD COLUMN IF NOT EXISTS deleted_from_tally_at TIMESTAMP")
    cur.execute("ALTER TABLE invoices "
                "ADD COLUMN IF NOT EXISTS deleted_from_tally_at TIMESTAMP")
    # Persist the ALTERs immediately so a same-cursor SELECT later in this
    # function sees the column — otherwise it lives in an uncommitted
    # transaction that gets rolled back when the pooled connection is
    # returned, leaving the column missing for the very next request.
    try:
        cur.connection.commit()
    except Exception:
        pass
    _TALLY_DELETE_READY = True


def mark_voucher_deleted_in_tally(voucher_id, company_name=None):
    """Sprint 44 — flip a voucher to 'deleted in both systems' state. Idempotent;
    safe to call repeatedly. Operates on both tally_vouchers AND invoices
    (UI routes deletes through whichever table owns the row).

    Returns {'updated': n} where n is 0 or 1."""
    if not voucher_id:
        return {"updated": 0}
    conn = get_conn(); cur = conn.cursor()
    n = 0
    try:
        _ensure_tally_delete_column(cur)
        # tally_vouchers
        if company_name:
            cur.execute("""UPDATE tally_vouchers
                SET deleted_from_tally_at = CURRENT_TIMESTAMP,
                    archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP),
                    needs_resync = FALSE,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND company_name = %s""",
                (voucher_id, company_name))
        else:
            cur.execute("""UPDATE tally_vouchers
                SET deleted_from_tally_at = CURRENT_TIMESTAMP,
                    archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP),
                    needs_resync = FALSE,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s""", (voucher_id,))
        n += cur.rowcount
        # invoices (yantrai-uid path: chat-confirmed vouchers live here)
        if company_name:
            cur.execute("""UPDATE invoices
                SET deleted_from_tally_at = CURRENT_TIMESTAMP,
                    archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP)
                WHERE id = %s AND company_name = %s""",
                (voucher_id, company_name))
        else:
            cur.execute("""UPDATE invoices
                SET deleted_from_tally_at = CURRENT_TIMESTAMP,
                    archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP)
                WHERE id = %s""", (voucher_id,))
        n += cur.rowcount
        conn.commit()
        return {"updated": n}
    except Exception as e:
        conn.rollback(); print(f"mark_voucher_deleted_in_tally error: {e}")
        return {"updated": n, "error": str(e)}
    finally:
        cur.close(); conn.close()


def get_voucher_for_delete(voucher_id, company_name=None):
    """Sprint 44 — fetch a voucher's minimal delete-relevant fields from EITHER
    tally_vouchers OR invoices. Returns dict with tally_master_id, voucher_type,
    voucher_number, party, company_name, source ('tally'|'invoice'), id."""
    if not voucher_id:
        return None
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _ensure_tally_delete_column(cur)
        # tally_vouchers first
        if company_name:
            cur.execute("""SELECT id, voucher_number, voucher_type, ledger_name AS party,
                                  company_name, tally_master_id
                           FROM tally_vouchers WHERE id = %s AND company_name = %s""",
                        (voucher_id, company_name))
        else:
            cur.execute("""SELECT id, voucher_number, voucher_type, ledger_name AS party,
                                  company_name, tally_master_id
                           FROM tally_vouchers WHERE id = %s""", (voucher_id,))
        row = cur.fetchone()
        if row:
            d = dict(row); d["source"] = "tally"; d["id"] = str(d["id"])
            return d
        # invoices
        if company_name:
            cur.execute("""SELECT id, invoice_number AS voucher_number,
                                  COALESCE(voucher_type, category) AS voucher_type,
                                  party_name AS party, company_name,
                                  NULL::text AS tally_master_id
                           FROM invoices WHERE id = %s AND company_name = %s""",
                        (voucher_id, company_name))
        else:
            cur.execute("""SELECT id, invoice_number AS voucher_number,
                                  COALESCE(voucher_type, category) AS voucher_type,
                                  party_name AS party, company_name,
                                  NULL::text AS tally_master_id
                           FROM invoices WHERE id = %s""", (voucher_id,))
        row = cur.fetchone()
        if row:
            d = dict(row); d["source"] = "invoice"; d["id"] = str(d["id"])
            # Look up the tally_master_id from a previously-pushed outbox row.
            cur.execute("""SELECT tally_voucher_guid FROM tally_outbox
                           WHERE invoice_id = %s AND state = 'pushed'
                             AND tally_voucher_guid IS NOT NULL
                           ORDER BY pushed_at DESC LIMIT 1""", (voucher_id,))
            r2 = cur.fetchone()
            if r2 and r2.get("tally_voucher_guid"):
                d["tally_master_id"] = r2["tally_voucher_guid"]
            return d
        return None
    finally:
        cur.close(); conn.close()


def list_tally_cleanup(company_name, status=None, kind=None):
    """List manual-Tally-cleanup items for a company. Optionally filter by status
    ('pending'|'done') and kind ('delete_voucher'|'create_ledger')."""
    conn = get_conn()
    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        q = "SELECT * FROM tally_cleanup_log WHERE company_name=%s"
        p = [company_name]
        if status:
            q += " AND status=%s"; p.append(status)
        if kind:
            q += " AND COALESCE(kind,'delete_voucher')=%s"; p.append(kind)
        q += " ORDER BY created_at DESC"
        cur.execute(q, p)
        rows = []
        for r in cur.fetchall():
            r["id"] = str(r["id"])
            for k in ("created_at", "done_at", "voucher_date"):
                if r.get(k): r[k] = str(r[k])
            if r.get("amount") is not None: r["amount"] = float(r["amount"])
            rows.append(r)
        return rows
    finally:
        cur.close(); conn.close()


def mark_tally_cleanup_done(cleanup_id, done=True):
    """Mark a cleanup item done (user deleted it in Tally) or re-open it."""
    conn = get_conn(); cur = conn.cursor()
    try:
        if done:
            cur.execute("UPDATE tally_cleanup_log SET status='done', done_at=CURRENT_TIMESTAMP WHERE id=%s", (cleanup_id,))
        else:
            cur.execute("UPDATE tally_cleanup_log SET status='pending', done_at=NULL WHERE id=%s", (cleanup_id,))
        conn.commit()
        return {"ok": cur.rowcount > 0}
    finally:
        cur.close(); conn.close()


def fail_tally_outbox(outbox_id, error):
    """Bridge agent reports a failed push."""
    conn = get_conn(); cur = conn.cursor()
    try:
        # P1 FIX: retry transient failures (back to 'pending') up to the attempt cap,
        # then mark terminal 'error'. Previously every failure was terminal with no retry.
        cur.execute("""
            UPDATE tally_outbox
            SET state = CASE WHEN attempts < 5 THEN 'pending' ELSE 'error' END,
                last_error = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (str(error)[:2000], outbox_id))
        conn.commit()
        return {"ok": cur.rowcount > 0}
    finally:
        cur.close(); conn.close()


def upsert_tally_heartbeat(company_name, agent_version=None, ip=None):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO tally_bridge_heartbeat (company_name, agent_version, ip, last_seen)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (company_name) DO UPDATE
            SET agent_version = EXCLUDED.agent_version,
                ip = EXCLUDED.ip,
                last_seen = CURRENT_TIMESTAMP
        """, (company_name, agent_version, ip))
        conn.commit()
        return {"ok": True}
    finally:
        cur.close(); conn.close()


def tally_outbox_status_for_invoice(invoice_id):
    """For the UI to poll: returns the latest outbox state for one invoice.
    Sprint 43 — also returns needs_approval (the proposed ledger bundle) +
    approved_ledgers so the Vouchers row can render the approval banner."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _ensure_tally_outbox_approval(cur)
        cur.execute("""
            SELECT id, state, attempts, last_error, tally_voucher_guid,
                   enqueued_at, pushed_at, updated_at,
                   needs_approval, approved_ledgers
            FROM tally_outbox
            WHERE invoice_id = %s
            ORDER BY enqueued_at DESC LIMIT 1
        """, (invoice_id,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def tally_status_summary(company_name):
    """For the UI: agent online?, pending count, errors, last push."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT agent_version, ip, last_seen,
                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - last_seen)) AS seconds_since
            FROM tally_bridge_heartbeat
            WHERE company_name = %s
        """, (company_name,))
        hb = cur.fetchone() or {}
        cur.execute("""
            SELECT state, COUNT(*) AS n FROM tally_outbox
            WHERE company_name = %s
            GROUP BY state
        """, (company_name,))
        counts = {r["state"]: r["n"] for r in cur.fetchall()}
        cur.execute("""
            SELECT MAX(pushed_at) AS last_pushed_at FROM tally_outbox
            WHERE company_name = %s AND state = 'pushed'
        """, (company_name,))
        last_pushed = cur.fetchone()
        secs = hb.get("seconds_since")
        # Agent considered "online" if heartbeat within 60s
        agent_online = bool(secs is not None and secs < 60)
        return {
            "agent_online": agent_online,
            "agent_last_seen": str(hb["last_seen"]) if hb.get("last_seen") else None,
            "agent_version": hb.get("agent_version"),
            "seconds_since_seen": float(secs) if secs is not None else None,
            "pending": int(counts.get("pending", 0)),
            "pushing": int(counts.get("pushing", 0)),
            "pushed":  int(counts.get("pushed", 0)),
            "error":   int(counts.get("error", 0)),
            "last_pushed_at": str(last_pushed["last_pushed_at"]) if last_pushed and last_pushed.get("last_pushed_at") else None,
        }
    finally:
        cur.close(); conn.close()


def update_bank_transaction(tx_id, updates, user_id=None, company_id=None):
    """Inline-edit one bank_transactions row. updates is a dict of fields → values.
    Allowed fields: party, head, bank_ledger, voucher_type, status, confidence,
    rationale, ai_touched, human_touched.

    Sprint 11: if `user_id` is provided (= a human triggered this edit), we
    automatically set human_touched=TRUE so the "Reconciled By" badge can
    flip to 🤖+👤 AI+Human. We no longer overwrite `created_by` on edit —
    the original creator signal is preserved for audit."""
    allowed = {"party", "head", "bank_ledger", "voucher_type", "status",
               "confidence", "rationale", "ai_touched", "human_touched"}
    sets = []
    params = []
    for k, v in updates.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            params.append(v)
    if not sets:
        return None
    sets.append("updated_at = CURRENT_TIMESTAMP")
    if user_id:
        # Mark this row as human-touched (additive — keeps ai_touched intact)
        if "human_touched = %s" not in sets:
            sets.append("human_touched = %s")
            params.append(True)
    params.append(tx_id)
    # P0 FIX: scope the update to the caller's company so a guessed tx_id can't
    # mutate another tenant's row.
    scope = "WHERE id = %s"
    if company_id:
        scope += " AND company_id = %s"
        params.append(company_id)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"""
        UPDATE bank_transactions SET {', '.join(sets)}
        {scope} RETURNING *
    """, params)
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row


def get_bank_transaction(tx_id, company_id=None):
    """Fetch one bank_transactions row (company-scoped when company_id given)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if company_id:
            cur.execute("SELECT * FROM bank_transactions WHERE id = %s AND company_id = %s",
                        (tx_id, company_id))
        else:
            cur.execute("SELECT * FROM bank_transactions WHERE id = %s", (tx_id,))
        return cur.fetchone()
    finally:
        cur.close(); conn.close()


def get_voucher_id_by_number(company_name, voucher_number):
    """Resolve a tally_vouchers id by its voucher_number for a company (used to find
    the voucher a posted bank line created, so an edit can re-sync it)."""
    if not voucher_number:
        return None
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM tally_vouchers WHERE company_name = %s AND voucher_number = %s LIMIT 1",
                    (company_name, voucher_number))
        r = cur.fetchone()
        return r[0] if r else None
    finally:
        cur.close(); conn.close()


def get_rerunnable_bank_lines(company_id=None, company_name=None, upload_id=None):
    """Unreconciled, not-yet-human-edited bank-statement lines — the targets for an
    AI re-run. Excludes matched/posted lines and anything a human already touched
    (so manual fixes are preserved). Optionally scope to one statement upload."""
    where = ["bt.source = 'bank_statement'",
             "bt.status IN ('ai_filled','unmatched')",
             "COALESCE(bt.human_touched, FALSE) = FALSE"]
    params = []
    if company_id and company_name:
        where.append("(bt.company_id = %s OR (bt.company_id IS NULL AND bt.company_name = %s))")
        params += [company_id, company_name]
    elif company_id:
        where.append("bt.company_id = %s"); params.append(company_id)
    elif company_name:
        where.append("bt.company_name = %s"); params.append(company_name)
    if upload_id:
        where.append("bt.source_file_id = %s"); params.append(upload_id)
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(f"""SELECT id, date, description, reference, amount, source_payload, source_row_idx
                        FROM bank_transactions bt
                        WHERE {' AND '.join(where)}
                        ORDER BY bt.source_row_idx NULLS LAST, bt.date""", params)
        return cur.fetchall()
    finally:
        cur.close(); pput(conn)


def get_submittable_bank_lines(company_id=None, company_name=None):
    """Bank-statement lines ready to become NEW vouchers in the Voucher section:
    a party is assigned, the line is AI-ready/manually-set, and it isn't already in
    the books (not Linked, not Posted, not blank). These are what 'Submit to Vouchers'
    creates."""
    where = ["bt.source = 'bank_statement'",
             "bt.status = 'ai_filled'",
             "COALESCE(bt.party, '') <> ''",
             "bt.linked_id IS NULL"]
    params = []
    if company_id and company_name:
        where.append("(bt.company_id = %s OR (bt.company_id IS NULL AND bt.company_name = %s))")
        params += [company_id, company_name]
    elif company_id:
        where.append("bt.company_id = %s"); params.append(company_id)
    elif company_name:
        where.append("bt.company_name = %s"); params.append(company_name)
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(f"""SELECT id, date, description, reference, amount, party, head,
                               bank_ledger, voucher_type, human_touched
                        FROM bank_transactions bt
                        WHERE {' AND '.join(where)}
                        ORDER BY bt.source_row_idx NULLS LAST, bt.date""", params)
        return cur.fetchall()
    finally:
        cur.close(); pput(conn)


def bank_health_check(company_id, company_name):
    """Compute health metrics for the Bank tab Health Check card.

    Returns:
      {
        "closing_balance_match": [{bank_ledger, tally_balance, sum_bank_tx, diff, ok}, ...],
        "coverage": [{bank_ledger, months_covered: [...], months_missing: [...]}, ...],
        "duplicates": [{amount, date, reference, sources: [..], ids: [..]}, ...],
        "orphans_tally": <int>,   # tally_vouchers bank-legs not yet ingested
        "period_stats": [{month, total, matched, ai_filled, unmatched}, ...],
        "totals": {total, matched, ai_filled, unmatched}
      }
    """
    out = {
        "closing_balance_match": [],
        "coverage": [],
        "duplicates": [],
        "orphans_tally": 0,
        "period_stats": [],
        "totals": {},
    }
    if not company_id:
        return out
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Closing balance check per bank ledger
    cur.execute("""
        SELECT l.name AS bank_ledger, l.closing_balance AS tally_balance,
               COALESCE(SUM(bt.amount), 0) AS sum_bank_tx
        FROM tally_ledgers l
        LEFT JOIN bank_transactions bt
          ON bt.bank_ledger = l.name AND bt.company_id = l.company_id
              AND bt.source = 'tally'
        WHERE l.company_id = %s
          AND (l.parent_group ILIKE '%%bank account%%' OR l.parent_group ILIKE '%%cash%%')
        GROUP BY l.name, l.closing_balance
        ORDER BY l.name
    """, (company_id,))
    for r in cur.fetchall():
        tb = float(r["tally_balance"] or 0)
        sb = float(r["sum_bank_tx"] or 0)
        out["closing_balance_match"].append({
            "bank_ledger": r["bank_ledger"],
            "tally_balance": tb,
            "sum_bank_tx": sb,
            "diff": round(tb - sb, 2),
            "ok": abs(tb - sb) < 1.0,
        })

    # Coverage — for each bank_ledger inferred from statement uploads
    cur.execute("""
        SELECT bank_ledger,
               array_agg(DISTINCT to_char(period_from, 'YYYY-MM')) AS months,
               MIN(period_from) AS earliest, MAX(period_to) AS latest
        FROM bank_statement_uploads
        WHERE company_id = %s
        GROUP BY bank_ledger
    """, (company_id,))
    for r in cur.fetchall():
        out["coverage"].append({
            "bank_ledger": r["bank_ledger"],
            "months_covered": [m for m in (r["months"] or []) if m],
            "earliest": str(r["earliest"]) if r["earliest"] else None,
            "latest": str(r["latest"]) if r["latest"] else None,
        })

    # Duplicates: same amount+date+ref appearing in DIFFERENT sources but NOT linked
    cur.execute("""
        SELECT amount, date, reference,
               array_agg(DISTINCT source) AS sources,
               array_agg(id::text) AS ids,
               COUNT(*) AS cnt
        FROM bank_transactions
        WHERE company_id = %s AND linked_id IS NULL
              AND reference IS NOT NULL AND reference <> ''
        GROUP BY amount, date, reference
        HAVING COUNT(*) > 1 AND COUNT(DISTINCT source) > 1
        LIMIT 50
    """, (company_id,))
    for r in cur.fetchall():
        out["duplicates"].append({
            "amount": float(r["amount"]),
            "date": str(r["date"]),
            "reference": r["reference"],
            "sources": r["sources"],
            "ids": r["ids"],
            "count": r["cnt"],
        })

    # Orphans: tally bank-leg vouchers not yet in bank_transactions
    cur.execute("""
        SELECT COUNT(*) AS n FROM tally_vouchers v
        WHERE v.company_id = %s
          AND v.ledger_name IN (
              SELECT name FROM tally_ledgers
              WHERE company_id = %s AND parent_group ILIKE '%%bank account%%'
          )
          AND NOT EXISTS (
              SELECT 1 FROM bank_transactions bt
              WHERE bt.company_id = v.company_id AND bt.source = 'tally'
                    AND bt.source_record_id = v.id
          )
    """, (company_id, company_id))
    out["orphans_tally"] = cur.fetchone()["n"]

    # Period stats — last 6 months
    cur.execute("""
        SELECT to_char(date_trunc('month', date), 'YYYY-MM') AS month,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status = 'matched') AS matched,
               COUNT(*) FILTER (WHERE status = 'ai_filled') AS ai_filled,
               COUNT(*) FILTER (WHERE status = 'unmatched') AS unmatched,
               COUNT(*) FILTER (WHERE status = 'posted') AS posted
        FROM bank_transactions
        WHERE company_id = %s
              AND date >= (CURRENT_DATE - INTERVAL '6 months')
        GROUP BY 1 ORDER BY 1 DESC
    """, (company_id,))
    for r in cur.fetchall():
        out["period_stats"].append(dict(r))

    # Totals
    cur.execute("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status = 'matched') AS matched,
               COUNT(*) FILTER (WHERE status = 'ai_filled') AS ai_filled,
               COUNT(*) FILTER (WHERE status = 'unmatched') AS unmatched,
               COUNT(*) FILTER (WHERE status = 'posted') AS posted
        FROM bank_transactions WHERE company_id = %s
    """, (company_id,))
    out["totals"] = dict(cur.fetchone())

    cur.close()
    conn.close()
    return out


def list_statement_uploads(company_id, company_name=None):
    """Uploaded statements newest-first. Matches by company_id when known, but falls
    back to company_name so companies with a NULL company_id (e.g. unregistered/demo)
    still see their uploads."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if company_id and company_name:
        cur.execute("""SELECT * FROM bank_statement_uploads
                       WHERE company_id = %s OR (company_id IS NULL AND company_name = %s)
                       ORDER BY uploaded_at DESC""", (company_id, company_name))
    elif company_id:
        cur.execute("""SELECT * FROM bank_statement_uploads
                       WHERE company_id = %s ORDER BY uploaded_at DESC""", (company_id,))
    elif company_name:
        cur.execute("""SELECT * FROM bank_statement_uploads
                       WHERE company_name = %s ORDER BY uploaded_at DESC""", (company_name,))
    else:
        cur.close(); conn.close(); return []
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_statement_upload(upload_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM bank_statement_uploads WHERE id = %s", (upload_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def log_bank_sync_run(company_id, company_name, run_type, tally_res=None,
                     invoice_res=None, statement_res=None, link_res=None,
                     triggered_by=None, notes=None):
    """Record a single sync attempt for transparency / audit."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bank_sync_runs
                (company_id, company_name, run_type,
                 tally_inserted, tally_skipped,
                 invoices_inserted, statement_inserted, statement_skipped, linked_pairs,
                 triggered_by, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            company_id, company_name, run_type,
            (tally_res or {}).get("inserted", 0),
            (tally_res or {}).get("skipped", 0),
            (invoice_res or {}).get("inserted", 0),
            (statement_res or {}).get("inserted", 0),
            (statement_res or {}).get("skipped_existing", 0),
            (link_res or {}).get("linked_pairs", 0),
            triggered_by, notes,
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[log_bank_sync_run] {e}")


def list_bank_sync_runs(company_id, limit=20):
    if not company_id:
        return []
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT * FROM bank_sync_runs WHERE company_id = %s
        ORDER BY ran_at DESC LIMIT %s
    """, (company_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════
# Voucher Drafts — upload pipeline + review workflow
# ═══════════════════════════════════════════════════════════════

def save_voucher_draft(company_id, company_name, parsed_payload, source_file_url=None,
                        source_file_name=None, source_file_type=None, voucher_type=None,
                        ai_confidence=None, created_by=None):
    """Persist an AI-parsed invoice/voucher waiting for review."""
    conn = get_conn()
    cur = conn.cursor()
    new_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO voucher_drafts
            (id, company_id, company_name, source_file_url, source_file_name,
             source_file_type, parsed_payload, voucher_type, ai_confidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
    """, (new_id, company_id, company_name, source_file_url, source_file_name,
          source_file_type, json.dumps(parsed_payload), voucher_type,
          ai_confidence, created_by))
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def get_voucher_draft(draft_id, company_id=None):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # P0 FIX: scope by company so a guessed draft_id can't read another tenant's draft.
    if company_id:
        cur.execute("SELECT * FROM voucher_drafts WHERE id = %s AND company_id = %s",
                    (draft_id, company_id))
    else:
        cur.execute("SELECT * FROM voucher_drafts WHERE id = %s", (draft_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def list_voucher_drafts(company_id, status=None, limit=200):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if status:
        cur.execute("""
            SELECT id, source_file_name, source_file_type, voucher_type, status,
                   ai_confidence, parsed_payload, reviewed_payload,
                   created_at, updated_at
            FROM voucher_drafts
            WHERE company_id = %s AND status = %s
            ORDER BY created_at DESC LIMIT %s
        """, (company_id, status, limit))
    else:
        cur.execute("""
            SELECT id, source_file_name, source_file_type, voucher_type, status,
                   ai_confidence, parsed_payload, reviewed_payload,
                   created_at, updated_at
            FROM voucher_drafts
            WHERE company_id = %s
            ORDER BY created_at DESC LIMIT %s
        """, (company_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def update_voucher_draft(draft_id, reviewed_payload=None, voucher_type=None, status=None,
                         company_id=None):
    sets, params = [], []
    if reviewed_payload is not None:
        sets.append("reviewed_payload = %s::jsonb")
        params.append(json.dumps(reviewed_payload))
        sets.append("status = 'edited'")
    if voucher_type is not None:
        sets.append("voucher_type = %s")
        params.append(voucher_type)
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if not sets:
        return None
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(draft_id)
    scope = "WHERE id = %s"
    if company_id:
        scope += " AND company_id = %s"
        params.append(company_id)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"UPDATE voucher_drafts SET {', '.join(sets)} {scope} RETURNING *", params)
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row


def discard_voucher_draft(draft_id, company_id=None):
    return update_voucher_draft(draft_id, status='discarded', company_id=company_id)


def validate_voucher_for_post(voucher):
    """P0 FIX: gate before a voucher reaches Tally. Returns (ok, error_message).
    Checks double-entry balance (sum of debits == sum of credits) and GST math
    (taxable + cgst + sgst + igst == total). Lenient by ₹1 for rounding. Trusted
    Tally→YantrAI ingest does NOT call this — only human/AI-originated posts do."""
    def _f(x):
        try: return abs(float(x or 0))
        except (TypeError, ValueError): return 0.0
    entries = voucher.get("ledger_entries") or []
    if entries:
        dr = sum(_f(e.get("amount")) for e in entries if e.get("is_debit"))
        cr = sum(_f(e.get("amount")) for e in entries if not e.get("is_debit"))
        if abs(dr - cr) > 1.0:
            return False, f"Voucher is unbalanced — debit {dr:.2f} ≠ credit {cr:.2f}."
    total = _f(voucher.get("amount"))
    taxable = _f(voucher.get("taxable_value"))
    tax = _f(voucher.get("cgst_amount")) + _f(voucher.get("sgst_amount")) + _f(voucher.get("igst_amount"))
    if taxable and total and abs((taxable + tax) - total) > 1.0:
        return False, (f"GST math doesn't add up — taxable {taxable:.2f} + tax {tax:.2f} "
                       f"= {taxable + tax:.2f}, but total is {total:.2f}.")
    return True, None


def post_voucher_from_draft(draft_id, company_id=None):
    """Take a draft's reviewed_payload (or parsed_payload), insert into
    tally_vouchers, mark draft 'posted', return the voucher row."""
    draft = get_voucher_draft(draft_id, company_id=company_id)
    if not draft:
        return {"error": "draft not found"}
    # P0 FIX: never double-post the same draft.
    if draft.get("status") in ("posted", "discarded"):
        return {"error": f"draft already {draft.get('status')}"}
    payload = draft.get("reviewed_payload") or draft.get("parsed_payload") or {}
    # P1 FIX: don't post a draft the AI couldn't read — make the user fill it in
    # (editing sets reviewed_payload, which clears this flag).
    if payload.get("_parse_failed"):
        return {"error": "This document couldn't be read automatically — open the draft "
                         "and enter the details before posting."}
    voucher = {
        "date": payload.get("date") or "",
        "type": payload.get("voucher_type") or draft.get("voucher_type") or "Purchase",
        "voucher_type": payload.get("voucher_type") or draft.get("voucher_type") or "Purchase",
        "party": payload.get("party_name") or payload.get("party") or "",
        "number": payload.get("invoice_number") or payload.get("voucher_number") or "",
        "amount": float(payload.get("total_amount") or payload.get("amount") or 0),
        "narration": payload.get("narration") or payload.get("description") or "",
        "ledger_entries": payload.get("ledger_entries") or [],
        "reference_no": payload.get("reference_no") or payload.get("reference") or "",
        "instrument_number": payload.get("instrument_number") or "",
        "place_of_supply": payload.get("place_of_supply") or "",
        "party_gstin": payload.get("party_gstin") or "",
        "currency": payload.get("currency", "INR"),
        "taxable_value": float(payload.get("taxable_value") or 0),
        "cgst_amount": float(payload.get("cgst_amount") or 0),
        "sgst_amount": float(payload.get("sgst_amount") or 0),
        "igst_amount": float(payload.get("igst_amount") or 0),
        "tally_master_id": None,
    }
    ok, err = validate_voucher_for_post(voucher)
    if not ok:
        return {"error": err, "status": "validation_failed"}
    save_res = save_tally_vouchers(draft["company_name"], [voucher])
    if save_res.get("upserted"):
        # Backfill company_id
        if draft.get("company_id"):
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                UPDATE tally_vouchers SET company_id = %s
                WHERE company_name = %s AND company_id IS NULL
            """, (draft["company_id"], draft["company_name"]))
            conn.commit()
            cur.close()
            conn.close()
        update_voucher_draft(draft_id, status='posted', company_id=company_id)
        return {"status": "posted", "voucher": voucher, "upserted": save_res["upserted"]}
    return {"status": "error", "message": "save_tally_vouchers did not upsert"}


def check_voucher_duplicate(company_name, invoice_number=None, party=None, amount=None, date=None):
    """Return existing tally_voucher if it looks like the same."""
    if not company_name:
        return None
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    where = ["company_name = %s"]
    params = [company_name]
    if invoice_number:
        where.append("voucher_number = %s")
        params.append(invoice_number)
    if amount is not None:
        where.append("ABS(amount - %s) < 0.01")
        params.append(float(amount))
    if party:
        where.append("ledger_name ILIKE %s")
        params.append(f"%{party}%")
    if date:
        where.append("ABS(EXTRACT(EPOCH FROM (date - %s::date))) < 259200")  # ±3 days
        params.append(date)
    if len(where) < 3:
        cur.close()
        conn.close()
        return None
    cur.execute(f"""
        SELECT id, voucher_number, date, ledger_name, amount, voucher_type
        FROM tally_vouchers
        WHERE {' AND '.join(where)}
        LIMIT 1
    """, params)
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        for k in ("id", "date"):
            if row.get(k) is not None:
                row[k] = str(row[k])
        if row.get("amount") is not None:
            row["amount"] = float(row["amount"])
    return row


_VCOUNTER_READY = False


def next_voucher_number(company_name, voucher_type):
    """Suggest the next voucher number e.g. 'SAL-2026-042'.

    P0 FIX: was a COUNT(*)+1 keyed on the *calendar* year, which (a) raced — two
    concurrent requests minted the SAME number — and (b) used Jan-Dec, not India's
    Apr-Mar financial year. Now uses an atomic per-(company,type,FY) counter
    (INSERT ... ON CONFLICT DO UPDATE ... RETURNING) so concurrent callers always
    get distinct numbers, seeded once from any pre-existing rows for that FY.
    """
    global _VCOUNTER_READY
    if not company_name or not voucher_type:
        return None
    type_prefix = {
        "Sales": "SAL", "Purchase": "PUR", "Payment": "PAY",
        "Receipt": "REC", "Journal": "JNL", "Contra": "CON",
    }.get(voucher_type, voucher_type[:3].upper())
    from datetime import date as _d
    today = _d.today()
    fy_start = today.year if today.month >= 4 else today.year - 1   # India FY: Apr–Mar
    fy_from = _d(fy_start, 4, 1)
    fy_to = _d(fy_start + 1, 4, 1)
    conn = get_conn()
    cur = conn.cursor()
    try:
        if not _VCOUNTER_READY:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS voucher_counters (
                    company_name TEXT, voucher_type TEXT, fy_start INT,
                    counter INT NOT NULL DEFAULT 0,
                    PRIMARY KEY (company_name, voucher_type, fy_start)
                )""")
            _VCOUNTER_READY = True
        # Atomic: seed from existing rows on first use, then increment-and-return.
        cur.execute("""
            INSERT INTO voucher_counters (company_name, voucher_type, fy_start, counter)
            VALUES (%s, %s, %s,
                    (SELECT COUNT(*) + 1 FROM tally_vouchers
                       WHERE company_name = %s AND voucher_type = %s
                         AND date >= %s AND date < %s))
            ON CONFLICT (company_name, voucher_type, fy_start)
            DO UPDATE SET counter = voucher_counters.counter + 1
            RETURNING counter
        """, (company_name, voucher_type, fy_start,
              company_name, voucher_type, fy_from, fy_to))
        n = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        conn.rollback(); print(f"[next_voucher_number] {e}")
        n = None
    finally:
        cur.close(); conn.close()
    if n is None:
        return None
    return f"{type_prefix}-{fy_start}-{n:03d}"


def lookup_party_by_gstin(gstin, company_id=None, company_name=None):
    """Look up an existing ledger by GSTIN. Falls back to checking tally_vouchers
    party_gstin if no direct match in tally_ledgers."""
    if not gstin:
        return None
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # Try tally_ledgers first (most reliable)
    if company_id:
        cur.execute("""
            SELECT name, parent_group, NULL AS context
            FROM tally_ledgers
            WHERE company_id = %s AND gstin = %s LIMIT 1
        """, (company_id, gstin))
    else:
        cur.execute("""
            SELECT name, parent_group, NULL AS context
            FROM tally_ledgers
            WHERE company_name = %s AND gstin = %s LIMIT 1
        """, (company_name,) if company_name else (None,))
    row = cur.fetchone()
    if not row:
        # Fallback: tally_vouchers party_gstin
        if company_name:
            cur.execute("""
                SELECT ledger_name AS name, NULL AS parent_group, 'voucher' AS context
                FROM tally_vouchers
                WHERE company_name = %s AND party_gstin = %s
                LIMIT 1
            """, (company_name, gstin))
            row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def autocomplete_parties(company_id, q, limit=10):
    """Autocomplete query for party dropdown — sundry creditors/debtors matching q."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if company_id:
        cur.execute("""
            SELECT name, parent_group, gstin, closing_balance
            FROM tally_ledgers
            WHERE company_id = %s
                  AND (parent_group ILIKE '%%sundry%%' OR parent_group ILIKE '%%debtor%%' OR parent_group ILIKE '%%creditor%%')
                  AND name ILIKE %s
            ORDER BY name LIMIT %s
        """, (company_id, f"%{q or ''}%", limit))
    else:
        cur.execute("""
            SELECT name, parent_group, gstin, closing_balance
            FROM tally_ledgers
            WHERE (parent_group ILIKE '%%sundry%%' OR parent_group ILIKE '%%debtor%%' OR parent_group ILIKE '%%creditor%%')
                  AND name ILIKE %s
            ORDER BY name LIMIT %s
        """, (f"%{q or ''}%", limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════
# SPRINT 4 — GSTR Reconciliation helpers
# ═══════════════════════════════════════════════════════════════

def save_gstr_filing(company_id, company_name, period, return_type,
                     source_file_url=None, source_file_name=None, sha256_hex=None,
                     payload=None, uploaded_by=None):
    """Insert a GSTR filing record. Returns the new id."""
    conn = get_conn()
    cur = conn.cursor()
    new_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO gstr_filings
            (id, company_id, company_name, period, return_type,
             source_file_url, source_file_name, sha256, payload, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
    """, (new_id, company_id, company_name, period, return_type,
          source_file_url, source_file_name, sha256_hex,
          json.dumps(payload or {}), uploaded_by))
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def match_gstr_against_vouchers(filing_id, company_id, company_name, portal_rows):
    """For each portal row from a GSTR-2A/2B upload, find the best matching
    purchase voucher in tally_vouchers and insert a gstr_reco_lines row.
    portal_rows: list of dicts with keys (invoice_number, party_name, gstin,
                                          invoice_date, amount, taxable, cgst, sgst, igst)
    Returns counts: {matched, only_portal, only_books, mismatch}
    """
    if not company_name:
        return {"matched": 0, "only_portal": 0, "only_books": 0, "mismatch": 0}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # Pull candidate purchase vouchers
    cur.execute("""
        SELECT id, voucher_number, date, ledger_name, amount, party_gstin,
               taxable_value, cgst_amount, sgst_amount, igst_amount
        FROM tally_vouchers
        WHERE company_name = %s AND voucher_type = 'Purchase'
    """, (company_name,))
    voucher_rows = cur.fetchall()
    # Index by (lower(voucher_number), round(amount)) for fast match
    idx_by_num = {}
    idx_by_amount = {}
    for v in voucher_rows:
        vn = (v.get("voucher_number") or "").strip().lower()
        if vn:
            idx_by_num.setdefault(vn, []).append(v)
        amt = round(abs(float(v.get("amount") or 0)), 2)
        idx_by_amount.setdefault(amt, []).append(v)

    matched = 0
    only_portal = 0
    only_books = 0
    mismatch = 0
    matched_voucher_ids = set()

    for p in portal_rows:
        portal_num = (p.get("invoice_number") or "").strip().lower()
        portal_amt = round(abs(float(p.get("amount") or p.get("invoice_value") or 0)), 2)

        match = None
        if portal_num and portal_num in idx_by_num:
            for cand in idx_by_num[portal_num]:
                if abs(round(abs(float(cand["amount"] or 0)), 2) - portal_amt) < 0.01:
                    match = cand; break
        if not match and portal_amt in idx_by_amount:
            # amount match only — weaker
            cands = idx_by_amount[portal_amt]
            if len(cands) == 1:
                match = cands[0]

        if match:
            matched += 1
            matched_voucher_ids.add(str(match["id"]))
            # Detect mismatch on tax fields
            diff = {}
            for k in ("taxable_value", "cgst_amount", "sgst_amount", "igst_amount"):
                portal_v = float(p.get(k) or p.get(k.replace("_amount", "")) or 0)
                book_v = float(match.get(k) or 0)
                if abs(portal_v - book_v) > 1.0:
                    diff[k] = {"portal": portal_v, "books": book_v}
            status = "matched" if not diff else "mismatch"
            if diff: mismatch += 1
            cur.execute("""
                INSERT INTO gstr_reco_lines
                    (filing_id, company_id, portal_row, matched_voucher_id, match_status, match_diff, itc_eligible)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)
            """, (filing_id, company_id, json.dumps(p), str(match["id"]),
                  status, json.dumps(diff), True))
        else:
            only_portal += 1
            cur.execute("""
                INSERT INTO gstr_reco_lines
                    (filing_id, company_id, portal_row, match_status, itc_eligible)
                VALUES (%s, %s, %s::jsonb, %s, %s)
            """, (filing_id, company_id, json.dumps(p), "only_portal", False))

    # Vouchers in books not matched to any portal row
    for v in voucher_rows:
        if str(v["id"]) not in matched_voucher_ids:
            only_books += 1
            cur.execute("""
                INSERT INTO gstr_reco_lines
                    (filing_id, company_id, portal_row, matched_voucher_id, match_status, itc_eligible)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s)
            """, (filing_id, company_id,
                  json.dumps({"voucher_number": v["voucher_number"],
                              "party": v["ledger_name"],
                              "amount": float(v["amount"] or 0)}),
                  str(v["id"]), "only_books", None))

    # Update filing match_summary
    summary = {"matched": matched, "only_portal": only_portal,
               "only_books": only_books, "mismatch": mismatch}
    cur.execute("UPDATE gstr_filings SET match_summary = %s::jsonb, status = 'reconciled', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (json.dumps(summary), filing_id))
    conn.commit()
    cur.close()
    conn.close()
    return summary


def list_gstr_filings(company_id, return_type=None, period=None, limit=50):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    where = ["company_id = %s"]
    params = [company_id]
    if return_type:
        where.append("return_type = %s"); params.append(return_type)
    if period:
        where.append("period = %s"); params.append(period)
    cur.execute(f"""
        SELECT id, period, return_type, source_file_name, status,
               match_summary, created_at
        FROM gstr_filings WHERE {' AND '.join(where)}
        ORDER BY created_at DESC LIMIT %s
    """, params + [limit])
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def get_gstr_filing_lines(filing_id, status=None, limit=500):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    where = ["filing_id = %s"]
    params = [filing_id]
    if status:
        where.append("match_status = %s"); params.append(status)
    cur.execute(f"""
        SELECT id, portal_row, matched_voucher_id, match_status, match_diff,
               itc_eligible, rationale
        FROM gstr_reco_lines WHERE {' AND '.join(where)}
        ORDER BY match_status, id LIMIT %s
    """, params + [limit])
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def update_gstr_reco_line(line_id, updates):
    allowed = {"match_status", "itc_eligible", "rationale", "matched_voucher_id"}
    sets, params = [], []
    for k, v in updates.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            params.append(v)
    if not sets:
        return None
    params.append(line_id)
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"UPDATE gstr_reco_lines SET {', '.join(sets)} WHERE id = %s RETURNING *", params)
    row = cur.fetchone()
    conn.commit()
    cur.close(); conn.close()
    return row


def itc_comparison(company_id, company_name, from_period, to_period):
    """For each month in range, return claimed (from GSTR-3B) vs claimable (from GSTR-2B)."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    # Claimable from 2A/2B reco rows
    cur.execute("""
        SELECT period,
               COALESCE(SUM((portal_row->>'cgst_amount')::numeric), 0) AS cgst_claimable,
               COALESCE(SUM((portal_row->>'sgst_amount')::numeric), 0) AS sgst_claimable,
               COALESCE(SUM((portal_row->>'igst_amount')::numeric), 0) AS igst_claimable
        FROM gstr_filings f
        LEFT JOIN gstr_reco_lines l ON l.filing_id = f.id AND l.itc_eligible = TRUE
        WHERE f.company_id = %s AND f.return_type IN ('GSTR-2A','GSTR-2B')
              AND f.period BETWEEN %s AND %s
        GROUP BY period ORDER BY period
    """, (company_id, from_period, to_period))
    claimable = {r["period"]: r for r in cur.fetchall()}

    # Claimed from tally purchase vouchers
    cur.execute("""
        SELECT TO_CHAR(date, 'YYYY-MM') AS period,
               COALESCE(SUM(cgst_amount), 0) AS cgst_claimed,
               COALESCE(SUM(sgst_amount), 0) AS sgst_claimed,
               COALESCE(SUM(igst_amount), 0) AS igst_claimed
        FROM tally_vouchers
        WHERE company_name = %s AND voucher_type = 'Purchase'
              AND TO_CHAR(date, 'YYYY-MM') BETWEEN %s AND %s
        GROUP BY period ORDER BY period
    """, (company_name, from_period, to_period))
    claimed = {r["period"]: r for r in cur.fetchall()}
    cur.close(); conn.close()

    periods = sorted(set(list(claimable.keys()) + list(claimed.keys())))
    out = []
    for p in periods:
        c = claimable.get(p, {})
        b = claimed.get(p, {})
        out.append({
            "period": p,
            "cgst_claimable": float(c.get("cgst_claimable") or 0),
            "cgst_claimed":   float(b.get("cgst_claimed") or 0),
            "sgst_claimable": float(c.get("sgst_claimable") or 0),
            "sgst_claimed":   float(b.get("sgst_claimed") or 0),
            "igst_claimable": float(c.get("igst_claimable") or 0),
            "igst_claimed":   float(b.get("igst_claimed") or 0),
        })
    return out


def gstr1_vs_3b_variance(company_name, period):
    """Compare GSTR-1 outward supplies vs GSTR-3B reported sales for a period.
    For now both come from tally_vouchers Sales — they should agree."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT COALESCE(SUM(taxable_value), 0) AS taxable,
               COALESCE(SUM(cgst_amount), 0) AS cgst,
               COALESCE(SUM(sgst_amount), 0) AS sgst,
               COALESCE(SUM(igst_amount), 0) AS igst,
               COUNT(*) AS n
        FROM tally_vouchers
        WHERE company_name = %s AND voucher_type = 'Sales'
              AND TO_CHAR(date, 'YYYY-MM') = %s
    """, (company_name, period))
    r = cur.fetchone() or {}
    cur.close(); conn.close()
    return {
        "period": period,
        "gstr1": {k: float(r.get(k) or 0) for k in ("taxable","cgst","sgst","igst")} | {"count": r.get("n",0)},
        "gstr3b": {k: float(r.get(k) or 0) for k in ("taxable","cgst","sgst","igst")} | {"count": r.get("n",0)},
        "variance": {k: 0.0 for k in ("taxable","cgst","sgst","igst")},
        "note": "Both GSTR-1 and GSTR-3B currently derived from same tally_vouchers Sales. Variance appears when GSTR-3B JSON is filed separately (future).",
    }


def gstr9_aggregate(company_name, fy):
    """Aggregate 12 months of GSTR-1 + 3B data for annual GSTR-9.
    fy format: '2025-26' meaning Apr-2025 to Mar-2026."""
    try:
        start_year = int(fy.split("-")[0])
    except Exception:
        return {"error": "invalid fy"}
    periods = [f"{start_year}-{m:02d}" for m in range(4, 13)] + [f"{start_year+1}-{m:02d}" for m in range(1, 4)]
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT TO_CHAR(date, 'YYYY-MM') AS period,
               voucher_type,
               COALESCE(SUM(taxable_value), 0) AS taxable,
               COALESCE(SUM(cgst_amount), 0) AS cgst,
               COALESCE(SUM(sgst_amount), 0) AS sgst,
               COALESCE(SUM(igst_amount), 0) AS igst,
               COUNT(*) AS n
        FROM tally_vouchers
        WHERE company_name = %s
              AND TO_CHAR(date, 'YYYY-MM') = ANY(%s)
              AND voucher_type IN ('Sales', 'Purchase')
        GROUP BY period, voucher_type ORDER BY period, voucher_type
    """, (company_name, periods))
    rows = cur.fetchall()
    cur.close(); conn.close()
    sales_total = {"taxable":0,"cgst":0,"sgst":0,"igst":0,"count":0}
    purchase_total = {"taxable":0,"cgst":0,"sgst":0,"igst":0,"count":0}
    per_month = {}
    for r in rows:
        t = r["voucher_type"]
        for k in ("taxable","cgst","sgst","igst"):
            v = float(r.get(k) or 0)
            if t == "Sales": sales_total[k] += v
            elif t == "Purchase": purchase_total[k] += v
        if t == "Sales": sales_total["count"] += r.get("n",0)
        elif t == "Purchase": purchase_total["count"] += r.get("n",0)
        per_month.setdefault(r["period"], {})[t] = {k: float(r.get(k) or 0) for k in ("taxable","cgst","sgst","igst")}
    return {"fy": fy, "sales": sales_total, "purchase": purchase_total, "per_month": per_month}


def invoice_serial_gaps(company_name, voucher_type="Sales"):
    """Detect gaps in voucher number sequence for a type."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT voucher_number, date FROM tally_vouchers
        WHERE company_name = %s AND voucher_type = %s
              AND voucher_number IS NOT NULL AND voucher_number <> ''
        ORDER BY voucher_number
    """, (company_name, voucher_type))
    rows = cur.fetchall()
    cur.close(); conn.close()
    # Group by prefix, detect gaps in numeric suffix
    import re as _re
    groups = {}
    for r in rows:
        m = _re.match(r"^(.*?)(\d+)$", r["voucher_number"] or "")
        if not m: continue
        prefix, suffix = m.group(1), int(m.group(2))
        groups.setdefault(prefix, []).append(suffix)
    gaps = []
    for prefix, nums in groups.items():
        nums.sort()
        for i in range(1, len(nums)):
            if nums[i] - nums[i-1] > 1:
                for missing in range(nums[i-1]+1, nums[i]):
                    gaps.append({"prefix": prefix, "missing_number": missing,
                                  "missing_voucher_id": f"{prefix}{missing:03d}",
                                  "between": [f"{prefix}{nums[i-1]:03d}", f"{prefix}{nums[i]:03d}"]})
    return gaps


def hsn_summary(company_name, period):
    """Aggregate by HSN for the period."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT hsn_code,
               COUNT(*) AS items,
               COALESCE(SUM(closing_value), 0) AS total_value
        FROM tally_stock_items
        WHERE company_name = %s AND hsn_code IS NOT NULL AND hsn_code <> ''
        GROUP BY hsn_code ORDER BY total_value DESC
    """, (company_name,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"hsn_code": r["hsn_code"], "items": r["items"], "total_value": float(r["total_value"] or 0)} for r in rows]


# ═══════════════════════════════════════════════════════════════
# SPRINT 5 — Filing deadlines + audit checks
# ═══════════════════════════════════════════════════════════════

def seed_filing_deadlines_for_fy(fy, company_id=None):
    """Seed all due dates for a fiscal year. Idempotent via UNIQUE INDEX."""
    try:
        start_year = int(fy.split("-")[0])
    except Exception:
        return {"error": "invalid fy"}
    from datetime import date as _d
    deadlines = []
    for m in range(4, 16):
        year = start_year if m <= 12 else start_year + 1
        month = m if m <= 12 else m - 12
        period = f"{year}-{month:02d}"
        # GSTR-1: 11th of next month
        nxt_year = year + (1 if month == 12 else 0)
        nxt_month = 1 if month == 12 else month + 1
        deadlines.append(("GSTR-1", period, _d(nxt_year, nxt_month, 11), f"GSTR-1 for {period}"))
        # GSTR-3B: 20th of next month
        deadlines.append(("GSTR-3B", period, _d(nxt_year, nxt_month, 20), f"GSTR-3B for {period}"))
    # TDS quarterly: Q1 → 31-Jul-Y, Q2 → 31-Oct, Q3 → 31-Jan-(Y+1), Q4 → 31-May-(Y+1)
    deadlines.extend([
        ("TDS-24Q-26Q", "Q1", _d(start_year, 7, 31), "TDS quarterly return (Q1: Apr-Jun)"),
        ("TDS-24Q-26Q", "Q2", _d(start_year, 10, 31), "TDS quarterly return (Q2: Jul-Sep)"),
        ("TDS-24Q-26Q", "Q3", _d(start_year + 1, 1, 31), "TDS quarterly return (Q3: Oct-Dec)"),
        ("TDS-24Q-26Q", "Q4", _d(start_year + 1, 5, 31), "TDS quarterly return (Q4: Jan-Mar)"),
        # Advance tax quarterly
        ("ADVANCE-TAX", "Q1", _d(start_year, 6, 15), "Advance Tax — 15% by 15-Jun"),
        ("ADVANCE-TAX", "Q2", _d(start_year, 9, 15), "Advance Tax — cumulative 45% by 15-Sep"),
        ("ADVANCE-TAX", "Q3", _d(start_year, 12, 15), "Advance Tax — cumulative 75% by 15-Dec"),
        ("ADVANCE-TAX", "Q4", _d(start_year + 1, 3, 15), "Advance Tax — 100% by 15-Mar"),
        # GSTR-9 & ITR
        ("GSTR-9", fy, _d(start_year + 1, 12, 31), f"GSTR-9 Annual return for FY {fy}"),
        ("ITR-4", fy, _d(start_year + 1, 7, 31), f"ITR-4 for FY {fy}"),
    ])

    conn = get_conn()
    cur = conn.cursor()
    inserted = 0
    skipped = 0
    for ftype, period, due, desc in deadlines:
        try:
            cur.execute("SAVEPOINT fdsp")
            cur.execute("""
                INSERT INTO filing_deadlines (company_id, filing_type, period, due_date, description, fy)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (company_id, ftype, period, due, desc, fy))
            cur.execute("RELEASE SAVEPOINT fdsp")
            inserted += 1
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT fdsp")
            skipped += 1
    conn.commit()
    cur.close(); conn.close()
    return {"inserted": inserted, "skipped": skipped}


def list_filing_deadlines(company_id, from_date=None, to_date=None):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    where = ["(company_id = %s OR company_id IS NULL)"]
    params = [company_id]
    if from_date:
        where.append("due_date >= %s"); params.append(from_date)
    if to_date:
        where.append("due_date <= %s"); params.append(to_date)
    cur.execute(f"""
        SELECT id, filing_type, period, due_date, description, fy
        FROM filing_deadlines WHERE {' AND '.join(where)}
        ORDER BY due_date
    """, params)
    rows = cur.fetchall()
    cur.close(); conn.close()
    for r in rows:
        r["id"] = str(r["id"])
        r["due_date"] = str(r["due_date"])
    return rows


def run_audit_checks(company_id, company_name):
    """Run a subset of the 38 CA audit checks. Each returns
    {check_id, category, name, status, count, message}.
    v1 implements the most data-driven 12 checks; rest return 'pending' placeholders."""
    if not company_id or not company_name:
        return []
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    out = []

    # 1. Suspense aging
    try:
        cur.execute("""
            SELECT COUNT(*) AS n FROM tally_vouchers
            WHERE company_name = %s AND ledger_name ILIKE '%%suspense%%'
                  AND date < (CURRENT_DATE - INTERVAL '30 days')
        """, (company_name,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "suspense_aging", "category": "Reconciliation",
                    "name": "Suspense > 30 days", "status": "pass" if n == 0 else "warn",
                    "count": n, "message": f"{n} suspense entries older than 30 days" if n else "No stale suspense entries"})
    except Exception as e:
        out.append({"check_id": "suspense_aging", "category": "Reconciliation",
                    "name": "Suspense > 30 days", "status": "skip", "count": 0, "message": str(e)})

    # 2. Cash never negative
    try:
        cur.execute("""
            WITH daily AS (
                SELECT date,
                       SUM(CASE WHEN voucher_type = 'Receipt' AND ledger_name ILIKE '%%cash%%' THEN amount
                                WHEN voucher_type = 'Payment' AND ledger_name ILIKE '%%cash%%' THEN -amount
                                ELSE 0 END) AS net
                FROM tally_vouchers
                WHERE company_name = %s
                GROUP BY date
            ),
            cumul AS (
                SELECT date, SUM(net) OVER (ORDER BY date) AS running FROM daily
            )
            SELECT COUNT(*) AS n FROM cumul WHERE running < 0
        """, (company_name,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "cash_non_negative", "category": "Balance Sheet",
                    "name": "Cash never negative", "status": "pass" if n == 0 else "fail",
                    "count": n, "message": "Cash balance stays ≥ 0" if n == 0 else f"{n} days where cash went negative"})
    except Exception:
        pass

    # 3. Sales/Purchase missing GSTIN — handled by Vouchers ▸ AI Gaps (which also
    #    proposes fills), so it's NOT duplicated here in the Health Check.

    # 4. Invoice serial gaps (Sales)
    try:
        gaps = invoice_serial_gaps(company_name, "Sales")
        n = len(gaps)
        out.append({"check_id": "invoice_serial_gaps_sales", "category": "GST",
                    "name": "Sales invoice serial gaps", "status": "pass" if n == 0 else "warn",
                    "count": n, "message": f"{n} gaps in Sales voucher numbering" if n else "Sales serial numbering is continuous"})
    except Exception: pass

    # 5. Debtor with credit balance (advance received)
    try:
        cur.execute("""
            SELECT COUNT(*) AS n FROM tally_ledgers
            WHERE company_id = %s AND parent_group ILIKE '%%sundry debtor%%'
                  AND closing_balance < 0
        """, (company_id,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "debtor_credit_balance", "category": "Balance Sheet",
                    "name": "Debtor with credit balance", "status": "pass" if n == 0 else "warn",
                    "count": n, "message": f"{n} debtors with credit balance (advance received)" if n else "Clean"})
    except Exception: pass

    # 6. Creditor with debit balance (advance paid)
    try:
        cur.execute("""
            SELECT COUNT(*) AS n FROM tally_ledgers
            WHERE company_id = %s AND parent_group ILIKE '%%sundry creditor%%'
                  AND closing_balance > 0
        """, (company_id,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "creditor_debit_balance", "category": "Balance Sheet",
                    "name": "Creditor with debit balance", "status": "pass" if n == 0 else "warn",
                    "count": n, "message": f"{n} creditors with debit balance (advance paid)" if n else "Clean"})
    except Exception: pass

    # 7. Bank balance match (uses bank_transactions sum vs ledger closing)
    try:
        cur.execute("""
            SELECT l.name AS bank, l.closing_balance AS tally_bal,
                   COALESCE(SUM(bt.amount), 0) AS bt_sum
            FROM tally_ledgers l
            LEFT JOIN bank_transactions bt ON bt.bank_ledger = l.name AND bt.company_id = l.company_id AND bt.source='tally'
            WHERE l.company_id = %s AND l.parent_group ILIKE '%%bank account%%'
            GROUP BY l.name, l.closing_balance
        """, (company_id,))
        mismatches = [r for r in cur.fetchall() if abs(float(r["tally_bal"] or 0) - float(r["bt_sum"] or 0)) > 1.0]
        n = len(mismatches)
        out.append({"check_id": "bank_balance_match", "category": "Reconciliation",
                    "name": "Bank balance matches Tally", "status": "pass" if n == 0 else "fail",
                    "count": n, "message": f"{n} bank ledgers don't reconcile" if n else "All bank balances match"})
    except Exception: pass

    # 8. Loans without secured/unsecured classification
    try:
        cur.execute("""
            SELECT COUNT(*) AS n FROM tally_ledgers
            WHERE company_id = %s AND parent_group ILIKE '%%loan%%'
                  AND (parent_group NOT ILIKE '%%secured%%' AND parent_group NOT ILIKE '%%unsecured%%')
        """, (company_id,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "loans_classified", "category": "Balance Sheet",
                    "name": "Loans classified secured/unsecured", "status": "pass" if n == 0 else "warn",
                    "count": n, "message": f"{n} loan ledgers not classified" if n else "All loans classified"})
    except Exception: pass

    # 9. GSTR drafts pending
    try:
        cur.execute("SELECT COUNT(*) AS n FROM voucher_drafts WHERE company_id = %s AND status = 'ready_for_review'", (company_id,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "voucher_drafts_pending", "category": "Workflow",
                    "name": "Voucher drafts pending review", "status": "pass" if n == 0 else "warn",
                    "count": n, "message": f"{n} drafts awaiting review"})
    except Exception: pass

    # 10. Sensitive ledgers count
    try:
        cur.execute("SELECT COUNT(*) AS n FROM tally_ledgers WHERE company_id = %s AND is_sensitive = TRUE", (company_id,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "sensitive_ledgers", "category": "Audit",
                    "name": "Sensitive ledgers flagged", "status": "pass", "count": n,
                    "message": f"{n} sensitive ledgers under watch"})
    except Exception: pass

    # 11. GSTR filings status
    try:
        cur.execute("SELECT COUNT(*) AS n FROM gstr_filings WHERE company_id = %s", (company_id,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "gstr_filings_uploaded", "category": "GST",
                    "name": "GSTR filings uploaded", "status": "pass" if n > 0 else "warn",
                    "count": n, "message": f"{n} GSTR filings recorded" if n else "No GSTR filings uploaded yet"})
    except Exception: pass

    # 12. TDS deductions recorded
    try:
        cur.execute("SELECT COUNT(*) AS n FROM tds_deductions WHERE company_id = %s", (company_id,))
        n = (cur.fetchone() or {}).get("n", 0)
        out.append({"check_id": "tds_deductions_recorded", "category": "TDS",
                    "name": "TDS deductions recorded", "status": "pass" if n > 0 else "warn",
                    "count": n, "message": f"{n} TDS entries recorded" if n else "No TDS entries yet"})
    except Exception: pass

    # Placeholders for the remaining 26 checks
    placeholder_checks = [
        ("opening_balance", "Balance Sheet", "Opening balance carry-forward"),
        ("creditor_payments_traced", "Reconciliation", "Creditor payments via bank"),
        ("debtor_receipts_traced", "Reconciliation", "Debtor receipts via bank"),
        ("tds_rates_correct", "TDS", "TDS rates by section"),
        ("gstr1_completeness", "GST", "GSTR-1 sales completeness"),
        ("credit_notes_in_gstr1", "GST", "Credit notes in GSTR-1"),
        ("reverse_charge", "GST", "Reverse charge tagging"),
        ("gstr1_vs_3b", "GST", "GSTR-1 vs GSTR-3B variance"),
        ("2a_vs_2b", "GST", "GSTR-2A vs 2B mismatch"),
        ("gstr9_carry_forward", "GST", "GSTR-9 ITC carry-forward"),
        ("fixed_asset_depreciation", "Fixed Assets", "Depreciation calculation"),
        ("tds_payment_timely", "TDS", "TDS payment timeliness"),
        ("aadhaar_pan_linked", "TDS", "Aadhaar-PAN linked"),
        ("tds_on_gst", "TDS", "TDS on GST recorded"),
        ("turnover_consistency", "GST", "Turnover GST/3B/Books"),
        ("26as_match", "TDS", "Form 26AS reconciled"),
        ("gst_dashboard_vs_books", "GST", "GST dashboard vs books"),
        ("refund_split", "Income Tax", "Refund interest/principal split"),
        ("provision_for_tax", "Income Tax", "Provision for tax"),
        ("itc_comparison_sheet", "GST", "ITC comparison sheet"),
        ("gst_on_advance", "GST", "GST on advance received"),
        ("lut_for_exports", "GST", "LUT for exports"),
        ("ineligible_itc", "GST", "Ineligible ITC tracked"),
        ("audit_applicability", "Audit", "Audit applicability (5% cash)"),
        ("secured_loan_closing", "Balance Sheet", "Secured loan closing balance"),
        ("input_not_submitted", "GST", "Input not submitted by parties"),
    ]
    for cid, cat, name in placeholder_checks:
        out.append({"check_id": cid, "category": cat, "name": name,
                    "status": "pending", "count": 0,
                    "message": "Will be implemented in upcoming sprint"})

    cur.close(); conn.close()
    return out


# ═══════════════════════════════════════════════════════════════
# SPRINT 6 — TDS helpers
# ═══════════════════════════════════════════════════════════════

def list_tds_sections():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM tds_sections WHERE is_active = TRUE ORDER BY code")
    rows = cur.fetchall()
    cur.close(); conn.close()
    for r in rows:
        for k in ("rate_individual", "rate_company", "threshold", "annual_threshold"):
            if r.get(k) is not None: r[k] = float(r[k])
    return rows


def suggest_tds_for_voucher(voucher_payload, party_ledger=None):
    """Given a voucher payload, suggest applicable TDS section.
    voucher_payload: {voucher_type, party_name, total_amount, ...}
    party_ledger: dict from tally_ledgers if available (has parent_group)
    Returns: {section, rate, tds_amount, rationale} or None.
    """
    vtype = (voucher_payload.get("voucher_type") or "").lower()
    amount = float(voucher_payload.get("total_amount") or voucher_payload.get("amount") or 0)
    if vtype not in ("payment", "purchase") or amount <= 0:
        return None
    pg = (party_ledger or {}).get("parent_group", "") if party_ledger else ""
    pg_lower = pg.lower()
    # Heuristic mapping
    if "rent" in pg_lower and amount >= 240000:
        return {"section": "194I", "rate": 10.0, "tds_amount": round(amount * 0.10, 2),
                "rationale": "Rent payment ≥ ₹2,40,000 → 194I @ 10%"}
    if any(k in pg_lower for k in ("professional", "consultancy", "legal", "audit fees")) and amount >= 30000:
        return {"section": "194J", "rate": 10.0, "tds_amount": round(amount * 0.10, 2),
                "rationale": "Professional/technical service ≥ ₹30,000 → 194J @ 10%"}
    if "commission" in pg_lower and amount >= 15000:
        return {"section": "194H", "rate": 5.0, "tds_amount": round(amount * 0.05, 2),
                "rationale": "Commission/brokerage ≥ ₹15,000 → 194H @ 5%"}
    if any(k in pg_lower for k in ("contract", "transport", "freight")) and amount >= 30000:
        return {"section": "194C", "rate": 1.0, "tds_amount": round(amount * 0.01, 2),
                "rationale": "Contract payment ≥ ₹30,000 → 194C @ 1% (individual) / 2% (company)"}
    return None


def save_tds_deduction(company_id, company_name, voucher_id, party_name, section,
                       gross_amount, tds_amount, rate_applied, deduction_date,
                       fy, quarter, party_pan=None, created_by=None):
    conn = get_conn()
    cur = conn.cursor()
    new_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO tds_deductions
            (id, company_id, company_name, voucher_id, party_name, party_pan,
             section, gross_amount, tds_amount, rate_applied, deduction_date,
             fy, quarter, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (new_id, company_id, company_name, voucher_id, party_name, party_pan,
          section, gross_amount, tds_amount, rate_applied, deduction_date,
          fy, quarter, created_by))
    conn.commit()
    cur.close(); conn.close()
    return new_id


def list_tds_deductions(company_id, fy=None, quarter=None, section=None, limit=500):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    where = ["company_id = %s"]
    params = [company_id]
    if fy:
        where.append("fy = %s"); params.append(fy)
    if quarter:
        where.append("quarter = %s"); params.append(quarter)
    if section:
        where.append("section = %s"); params.append(section)
    cur.execute(f"""
        SELECT id, party_name, party_pan, section, gross_amount, tds_amount,
               rate_applied, deduction_date, challan_number, deposited,
               quarter, fy, return_filed
        FROM tds_deductions WHERE {' AND '.join(where)}
        ORDER BY deduction_date DESC LIMIT %s
    """, params + [limit])
    rows = cur.fetchall()
    cur.close(); conn.close()
    for r in rows:
        r["id"] = str(r["id"])
        for k in ("gross_amount", "tds_amount", "rate_applied"):
            if r.get(k) is not None: r[k] = float(r[k])
        if r.get("deduction_date"): r["deduction_date"] = str(r["deduction_date"])
    return rows


def tds_quarterly_summary(company_id, fy):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT quarter,
               COUNT(*) AS deductee_count,
               COALESCE(SUM(tds_amount), 0) AS total_tds,
               COUNT(*) FILTER (WHERE deposited = TRUE) AS deposited_count,
               COUNT(*) FILTER (WHERE return_filed = TRUE) AS filed_count
        FROM tds_deductions
        WHERE company_id = %s AND fy = %s
        GROUP BY quarter ORDER BY quarter
    """, (company_id, fy))
    rows = cur.fetchall()
    cur.close(); conn.close()
    for r in rows:
        if r.get("total_tds") is not None: r["total_tds"] = float(r["total_tds"])
    return rows


def tally_twin_exists(company_name, yantrai_uid):
    """True if a tally_vouchers row already carries this YantrAI uid — i.e. the voucher
    was already pushed to Tally and synced back. Used to avoid creating a duplicate in Tally."""
    if not yantrai_uid:
        return False
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("""SELECT 1 FROM tally_vouchers
                       WHERE company_name = %s AND yantrai_uid = %s LIMIT 1""",
                    (company_name, str(yantrai_uid)))
        return cur.fetchone() is not None
    except Exception as e:
        print(f"tally_twin_exists error: {e}")
        return False
    finally:
        cur.close(); pput(conn)


def save_tally_vouchers(company_name, vouchers, source=None):
    """
    UPSERT vouchers by (company_name, tally_master_id || voucher_number).
    Critical fix: previously did DELETE-then-INSERT which wiped all history every sync.
    Now incremental-safe — existing vouchers are updated, new ones appended.

    `source` (e.g. 'tally_pull'): when set, log a per-voucher DOWNLOAD event into
    voucher_sync_events (created/updated) so the Vouchers Event Log shows what came
    in from Tally. Local create paths leave source=None (they have their own logging).
    """
    if not vouchers:
        return {"upserted": 0, "skipped": 0, "created": 0, "updated": 0}
    conn = get_conn()
    cursor = conn.cursor()
    upserted = 0
    skipped = 0
    created = 0
    updated = 0
    sync_events = []   # (voucher_number, tally_master_id, action, party, amount)
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

            # Sticky-origin marker: a pushed YantrAI voucher carries [YAI:<invoices.id>] in its
            # narration. If present, this Tally row is the sync-back of that YantrAI voucher →
            # tag origin + link, and STRIP the marker so users never see it.
            origin = None
            yantrai_uid = None
            _yai = re.search(r'\[YAI:([0-9a-fA-F-]{8,})\]', narration or '')
            if not _yai and raw_xml:
                _yai = re.search(r'\[YAI:([0-9a-fA-F-]{8,})\]', raw_xml)
            if _yai:
                yantrai_uid = _yai.group(1)
                origin = 'yantrai'
                narration = re.sub(r'\s*\[YAI:[0-9a-fA-F-]{8,}\]', '', narration or '').strip()

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
                        origin=COALESCE(%s, origin), yantrai_uid=COALESCE(%s, yantrai_uid),
                        raw_xml=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s
                """, (
                    v_date, v_num, party, amount, v_type, instrument_number,
                    narration, json.dumps(ledger_entries), reference_no,
                    place_of_supply, party_gstin, currency,
                    json.dumps(cost_centres), json.dumps(bill_refs),
                    taxable_value, cgst_amount, sgst_amount, igst_amount,
                    tally_master_id, origin, yantrai_uid, raw_xml, existing_id
                ))
            else:
                cursor.execute("""
                    INSERT INTO tally_vouchers
                        (id, date, voucher_number, ledger_name, amount, voucher_type,
                         instrument_number, company_name, reconciled,
                         narration, ledger_entries, reference_no, place_of_supply,
                         party_gstin, currency, cost_centres, bill_refs,
                         taxable_value, cgst_amount, sgst_amount, igst_amount,
                         tally_master_id, origin, yantrai_uid, raw_xml, created_by, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE,
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                """, (
                    str(uuid.uuid4()), v_date, v_num, party, amount, v_type,
                    instrument_number, company_name,
                    narration, json.dumps(ledger_entries), reference_no, place_of_supply,
                    party_gstin, currency, json.dumps(cost_centres), json.dumps(bill_refs),
                    taxable_value, cgst_amount, sgst_amount, igst_amount,
                    tally_master_id, origin, yantrai_uid, raw_xml, v.get("created_by")
                ))
            upserted += 1
            if existing_id:
                updated += 1
            else:
                created += 1
            if source:
                sync_events.append((v_num, tally_master_id,
                                    "updated" if existing_id else "created", party, amount))
        conn.commit()
        # Per-voucher DOWNLOAD events (only on a real Tally pull). Best-effort.
        if source and sync_events:
            try:
                for vn, mid, act, pty, amt in sync_events:
                    cursor.execute("""
                        INSERT INTO voucher_sync_events
                            (company_name, voucher_number, tally_master_id, direction,
                             action, party, amount, detail)
                        VALUES (%s,%s,%s,'download',%s,%s,%s,%s)
                    """, (company_name, vn, mid, act, pty, amt, source))
                conn.commit()
            except Exception as _ee:
                print(f"[voucher_sync_events] {_ee}"); conn.rollback()
    except Exception as e:
        print(f"Error saving tally vouchers: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
    return {"upserted": upserted, "skipped": skipped, "created": created, "updated": updated}


_SYNC_STATE_READY = False


def _ensure_sync_state(cur):
    """Create tally_sync_state if missing, plus add Sprint-40 per-entity AlterId columns
    via additive ALTERs (idempotent)."""
    global _SYNC_STATE_READY
    if _SYNC_STATE_READY:
        return
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tally_sync_state (
            company_name TEXT PRIMARY KEY,
            last_voucher_alterid BIGINT DEFAULT 0,
            last_full_sync_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    # Sprint 40 — per-entity watermarks so masters can be incremental too.
    for _col in ("last_ledger_alterid", "last_group_alterid", "last_stock_alterid"):
        cur.execute(f"ALTER TABLE tally_sync_state ADD COLUMN IF NOT EXISTS {_col} BIGINT DEFAULT 0")
    _SYNC_STATE_READY = True


def get_sync_watermark(company_name):
    """Incremental-download watermark for a company (or None). Returns dict with
    per-entity AlterId watermarks + last_full_sync_at."""
    if not company_name:
        return None
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _ensure_sync_state(cur)
        cur.execute("""SELECT last_voucher_alterid, last_ledger_alterid,
                              last_group_alterid, last_stock_alterid, last_full_sync_at
                       FROM tally_sync_state WHERE company_name=%s""", (company_name,))
        r = cur.fetchone(); conn.commit(); return r
    except Exception as e:
        conn.rollback(); print(f"[get_sync_watermark] {e}"); return None
    finally:
        cur.close(); conn.close()


def set_sync_watermark(company_name, max_alter_id=0, max_ledger_alterid=0,
                       max_group_alterid=0, max_stock_alterid=0, full=False):
    """Advance the per-entity watermarks to the highest AlterId seen. `full=True`
    also stamps last_full_sync_at (used to schedule the periodic full reconcile for
    deletions). Each watermark is GREATEST()ed so we never go backwards."""
    if not company_name:
        return
    conn = get_conn(); cur = conn.cursor()
    try:
        _ensure_sync_state(cur)
        params = (company_name, int(max_alter_id or 0), int(max_ledger_alterid or 0),
                  int(max_group_alterid or 0), int(max_stock_alterid or 0))
        if full:
            cur.execute("""
                INSERT INTO tally_sync_state (company_name, last_voucher_alterid,
                    last_ledger_alterid, last_group_alterid, last_stock_alterid,
                    last_full_sync_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                ON CONFLICT (company_name) DO UPDATE SET
                    last_voucher_alterid=GREATEST(tally_sync_state.last_voucher_alterid, EXCLUDED.last_voucher_alterid),
                    last_ledger_alterid =GREATEST(tally_sync_state.last_ledger_alterid,  EXCLUDED.last_ledger_alterid),
                    last_group_alterid  =GREATEST(tally_sync_state.last_group_alterid,   EXCLUDED.last_group_alterid),
                    last_stock_alterid  =GREATEST(tally_sync_state.last_stock_alterid,   EXCLUDED.last_stock_alterid),
                    last_full_sync_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            """, params)
        else:
            cur.execute("""
                INSERT INTO tally_sync_state (company_name, last_voucher_alterid,
                    last_ledger_alterid, last_group_alterid, last_stock_alterid, updated_at)
                VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                ON CONFLICT (company_name) DO UPDATE SET
                    last_voucher_alterid=GREATEST(tally_sync_state.last_voucher_alterid, EXCLUDED.last_voucher_alterid),
                    last_ledger_alterid =GREATEST(tally_sync_state.last_ledger_alterid,  EXCLUDED.last_ledger_alterid),
                    last_group_alterid  =GREATEST(tally_sync_state.last_group_alterid,   EXCLUDED.last_group_alterid),
                    last_stock_alterid  =GREATEST(tally_sync_state.last_stock_alterid,   EXCLUDED.last_stock_alterid),
                    updated_at=CURRENT_TIMESTAMP
            """, params)
        conn.commit()
    except Exception as e:
        conn.rollback(); print(f"[set_sync_watermark] {e}")
    finally:
        cur.close(); conn.close()


def reconcile_tally_deletions(company_name, entity, live_guids):
    """Soft-delete master rows whose Tally GUID is NOT in `live_guids` — these were
    deleted in Tally since the last sync. **Only call this when the agent reported a
    COMPLETE full pull** (otherwise we'd delete rows the agent simply didn't fetch
    because of an incremental filter). entity is 'ledgers', 'groups', or 'stock_items'
    (maps to tally_ledgers/tally_groups/tally_stock_items). Returns the count
    soft-deleted. Existing discarded_at rows are NOT touched (preserve first-delete
    timestamp)."""
    if not company_name or entity not in ("ledgers", "groups", "stock_items"):
        return 0
    if live_guids is None:
        return 0
    table = "tally_" + entity if entity == "ledgers" else (
            "tally_" + entity if entity == "groups" else "tally_stock_items")
    conn = get_conn(); cur = conn.cursor()
    try:
        live = [g for g in live_guids if g]
        # Soft-delete: rows in YantrAI that have a GUID, are not already discarded,
        # and whose GUID isn't in the live set the agent just returned.
        if live:
            cur.execute(
                f"""UPDATE {table} SET discarded_at=CURRENT_TIMESTAMP
                    WHERE company_name=%s AND discarded_at IS NULL
                      AND tally_master_guid IS NOT NULL AND tally_master_guid <> ''
                      AND NOT (tally_master_guid = ANY(%s))""",
                (company_name, live))
        else:
            # Empty live list = the company is genuinely empty for that entity.
            cur.execute(
                f"""UPDATE {table} SET discarded_at=CURRENT_TIMESTAMP
                    WHERE company_name=%s AND discarded_at IS NULL
                      AND tally_master_guid IS NOT NULL AND tally_master_guid <> ''""",
                (company_name,))
        n = cur.rowcount
        conn.commit()
        if n:
            print(f"[reconcile_tally_deletions] {entity}: soft-deleted {n} row(s) for {company_name!r}", flush=True)
        return n
    except Exception as e:
        conn.rollback(); print(f"[reconcile_tally_deletions] {e}"); return 0
    finally:
        cur.close(); conn.close()


def list_voucher_sync_events(company_name, limit=50):
    """Recent per-voucher sync events (the download side of the Event Log)."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT voucher_number, tally_master_id, direction, action, party,
                   amount, detail, created_at
            FROM voucher_sync_events
            WHERE company_name = %s
            ORDER BY created_at DESC LIMIT %s
        """, (company_name, limit))
        rows = cur.fetchall()
        for r in rows:
            if r.get("amount") is not None: r["amount"] = float(r["amount"])
            if r.get("created_at"): r["created_at"] = str(r["created_at"])
        return rows
    except Exception as e:
        print(f"[list_voucher_sync_events] {e}"); return []
    finally:
        cur.close(); conn.close()


def get_tally_voucher(voucher_id):
    """Fetch a single tally_vouchers row by id, with JSON fields parsed."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, company_name, company_id, date, voucher_number, voucher_type,
                   ledger_name AS party_name, amount, narration,
                   ledger_entries::text AS ledger_entries, reference_no, instrument_number,
                   place_of_supply, party_gstin, currency,
                   taxable_value, cgst_amount, sgst_amount, igst_amount,
                   tally_master_id, COALESCE(needs_resync, FALSE) AS needs_resync,
                   last_edited_at, last_edited_by
            FROM tally_vouchers WHERE id = %s
        """, (voucher_id,))
        r = cur.fetchone()
        if not r:
            return None
        try:
            r["ledger_entries"] = json.loads(r["ledger_entries"]) if r["ledger_entries"] else []
        except Exception:
            r["ledger_entries"] = []
        if r.get("date"):
            r["date"] = str(r["date"])
        return r
    finally:
        cur.close(); conn.close()


# Fields a user may edit on a posted voucher via the edit modal.
_VOUCHER_EDIT_FIELDS = {
    "voucher_number": "voucher_number", "voucher_type": "voucher_type",
    "party_name": "ledger_name", "narration": "narration",
    "reference_no": "reference_no", "instrument_number": "instrument_number",
    "place_of_supply": "place_of_supply", "party_gstin": "party_gstin",
    "currency": "currency",
    "taxable_value": "taxable_value", "cgst_amount": "cgst_amount",
    "sgst_amount": "sgst_amount", "igst_amount": "igst_amount",
}


def update_tally_voucher(voucher_id, fields, edited_by=None):
    """Apply a local edit to a posted voucher. Updates editable columns and/or
    ledger_entries, recomputes amount from the legs, and flags needs_resync so
    the user can re-push to Tally. Does NOT touch Tally itself.
    Returns the updated row, or {"error": ...}."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id, tally_master_id FROM tally_vouchers WHERE id = %s", (voucher_id,))
        existing = cur.fetchone()
        if not existing:
            return {"error": "Voucher not found."}

        sets, params = [], []

        # Scalar fields
        for in_key, col in _VOUCHER_EDIT_FIELDS.items():
            if in_key in fields:
                val = fields[in_key]
                if in_key in ("taxable_value", "cgst_amount", "sgst_amount", "igst_amount"):
                    try: val = float(val or 0)
                    except Exception: val = 0.0
                sets.append(f"{col} = %s"); params.append(val)

        # Date (accept ISO or YYYYMMDD)
        if "date" in fields and fields["date"]:
            d = str(fields["date"])
            if len(d) == 8 and d.isdigit():
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            sets.append("date = %s"); params.append(d[:10])

        # Ledger entries — validate Dr == Cr, recompute amount
        if "ledger_entries" in fields and isinstance(fields["ledger_entries"], list):
            entries = []
            dr_total = cr_total = 0.0
            for e in fields["ledger_entries"]:
                nm = (e.get("ledger_name") or e.get("ledger") or "").strip()
                if not nm:
                    continue
                amt = float(e.get("amount") or 0)
                is_debit = bool(e.get("is_debit")) if "is_debit" in e else (amt >= 0)
                entries.append({"ledger_name": nm, "amount": amt, "is_debit": is_debit})
                if amt >= 0: dr_total += amt
                else: cr_total += -amt
            if entries and abs(dr_total - cr_total) > 0.01:
                return {"error": f"Dr ({dr_total:.2f}) ≠ Cr ({cr_total:.2f}). Entry not balanced."}
            sets.append("ledger_entries = %s"); params.append(json.dumps(entries))
            sets.append("amount = %s"); params.append(round(max(dr_total, cr_total), 2))

        if not sets:
            return {"error": "No editable fields supplied."}

        sets.append("needs_resync = TRUE")
        sets.append("last_edited_at = CURRENT_TIMESTAMP")
        sets.append("last_edited_by = %s"); params.append(edited_by)
        sets.append("updated_at = CURRENT_TIMESTAMP")

        params.append(voucher_id)
        cur.execute(f"UPDATE tally_vouchers SET {', '.join(sets)} WHERE id = %s", params)
        conn.commit()
        return {"ok": True}
    finally:
        cur.close(); conn.close()


def mark_voucher_resynced(voucher_id):
    """Clear the needs_resync flag once a resync push has been enqueued."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE tally_vouchers SET needs_resync = FALSE, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (voucher_id,))
        conn.commit()
    finally:
        cur.close(); conn.close()


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
            # Sprint 40 — also persist alter_id + tally_master_guid for incremental sync
            # and deletion-reconcile. Clear discarded_at on re-appear (un-soft-delete).
            cursor.execute("""
                INSERT INTO tally_ledgers
                    (id, company_name, tally_master_id, name, parent_group, group_path,
                     opening_balance, closing_balance, is_revenue, is_deemedpositive,
                     gstin, pan, address, bank_name, account_number, ifsc_code,
                     gst_registration_type, tds_applicable, ledger_type, place_of_supply,
                     raw_data, alter_id, tally_master_guid, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
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
                    alter_id = GREATEST(tally_ledgers.alter_id, EXCLUDED.alter_id),
                    tally_master_guid = COALESCE(EXCLUDED.tally_master_guid, tally_ledgers.tally_master_guid),
                    discarded_at = NULL,
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
                int(L.get("alter_id") or 0),
                L.get("guid") or L.get("tally_master_id"),
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


def add_party_ledger(company_name, name, group="Sundry Debtors", company_id=None):
    """Add a single party to the company's ledger master (the source the Bank-Reco
    party dropdown reads from). Idempotent on (company_name, name). `group` should be
    a Sundry Debtors/Creditors group so bank-ledger-options classifies it as a party.
    Returns {status, name, group}. The ledger is created in Tally when a voucher
    referencing it is posted; this only seeds the local master + dropdown."""
    name = (name or "").strip()
    if not name:
        return {"status": "error", "message": "name required"}
    group = (group or "Sundry Debtors").strip()
    conn = pget()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tally_ledgers (id, company_id, company_name, name, parent_group, group_path, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
            ON CONFLICT (company_name, name) DO UPDATE SET
                parent_group = COALESCE(tally_ledgers.parent_group, EXCLUDED.parent_group),
                company_id   = COALESCE(tally_ledgers.company_id, EXCLUDED.company_id),
                updated_at   = CURRENT_TIMESTAMP
        """, (str(uuid.uuid4()), company_id, company_name, name, group, group))
        conn.commit()
        cur.close()
        return {"status": "success", "name": name, "group": group}
    except Exception as e:
        conn.rollback()
        print(f"Error in add_party_ledger: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        pput(conn)


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
            # Sprint 40 — same incremental + soft-delete treatment as ledgers.
            cursor.execute("""
                INSERT INTO tally_stock_items
                    (id, company_name, tally_master_id, name, parent_group, unit, hsn_code,
                     gst_rate, opening_qty, opening_value, closing_qty, closing_value,
                     standard_rate, godown_breakup, raw_data, alter_id, tally_master_guid, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
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
                    alter_id = GREATEST(tally_stock_items.alter_id, EXCLUDED.alter_id),
                    tally_master_guid = COALESCE(EXCLUDED.tally_master_guid, tally_stock_items.tally_master_guid),
                    discarded_at = NULL,
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
                int(s.get("alter_id") or 0),
                s.get("guid") or s.get("tally_master_id"),
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
            # Sprint 40 — incremental + soft-delete columns.
            cursor.execute("""
                INSERT INTO tally_groups (id, company_name, name, parent, is_revenue, is_deemedpositive,
                                          raw_data, alter_id, tally_master_guid)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (company_name, name) DO UPDATE SET
                    parent = EXCLUDED.parent,
                    is_revenue = EXCLUDED.is_revenue,
                    is_deemedpositive = EXCLUDED.is_deemedpositive,
                    raw_data = EXCLUDED.raw_data,
                    alter_id = GREATEST(tally_groups.alter_id, EXCLUDED.alter_id),
                    tally_master_guid = COALESCE(EXCLUDED.tally_master_guid, tally_groups.tally_master_guid),
                    discarded_at = NULL
            """, (str(uuid.uuid4()), company_name, name, g.get("parent"),
                  _to_bool(g.get("is_revenue")), _to_bool(g.get("is_deemedpositive")),
                  json.dumps(g), int(g.get("alter_id") or 0),
                  g.get("guid") or g.get("tally_master_id")))
            count += 1
        conn.commit()
    except Exception as e:
        print(f"Error saving groups: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()
    return count


def save_tally_raw(company_name, records, entity_type, company_id=None):
    """Preserve the VERBATIM Tally XML per record into tally_raw (first-hand data for
    future training). Each record may carry a 'raw_xml' field (the full Tally element);
    records without it are skipped — so this is a harmless no-op for older agents that
    don't send raw. Upsert by dedupe_key (GUID, else voucher number / name). Returns count."""
    if not records:
        return 0
    rows = []
    for r in records:
        if not isinstance(r, dict):
            continue
        raw = r.get("raw_xml")
        if not raw:
            continue
        guid = r.get("guid") or r.get("tally_master_id")
        key = guid or r.get("number") or r.get("voucher_number") or r.get("name")
        if not key:
            continue
        try:
            alter = int(r.get("alterid") or r.get("alter_id") or 0) or None
        except (TypeError, ValueError):
            alter = None
        rows.append((str(uuid.uuid4()), company_name,
                     str(company_id) if company_id else None,
                     entity_type, str(key), guid, alter, raw))
    if not rows:
        return 0
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.executemany("""
            INSERT INTO tally_raw
                (id, company_name, company_id, entity_type, dedupe_key, tally_guid, alter_id, raw_xml, captured_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
            ON CONFLICT (company_name, entity_type, dedupe_key) DO UPDATE SET
                raw_xml = EXCLUDED.raw_xml,
                alter_id = EXCLUDED.alter_id,
                tally_guid = EXCLUDED.tally_guid,
                company_id = COALESCE(EXCLUDED.company_id, tally_raw.company_id),
                captured_at = CURRENT_TIMESTAMP
        """, rows)
        conn.commit()
        return len(rows)
    except Exception as e:
        print(f"[save_tally_raw] {e}")
        conn.rollback()
        return 0
    finally:
        cursor.close()
        conn.close()


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

def _next_task_code(cursor):
    """Human-friendly, trackable task code: YT-00001, YT-00002, …"""
    cursor.execute("SELECT COALESCE(MAX(CAST(SUBSTRING(task_code FROM 4) AS INTEGER)), 0) "
                   "FROM tasks WHERE task_code ~ '^YT-[0-9]+$'")
    n = (cursor.fetchone()[0] or 0) + 1
    return f"YT-{n:05d}"

def create_task(session_id, company_name, description, assigned_to='sadmin',
                title=None, category=None, priority=None, created_by=None,
                source=None, pd=None):
    conn = pget()
    cursor = conn.cursor()
    task_id = str(uuid.uuid4())
    task_code = None
    try:
        # Retry a couple of times in case two submissions race on the same code.
        for _ in range(3):
            task_code = _next_task_code(cursor)
            try:
                cursor.execute("""
                INSERT INTO tasks (id, session_id, company_name, assigned_to, description,
                                   status, task_code, title, category, priority, created_by,
                                   source, pd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (task_id, session_id, company_name, assigned_to, description, 'Requested',
                      task_code, title, category, priority, created_by, source,
                      json.dumps(pd) if pd is not None else None))
                conn.commit()
                break
            except Exception as ie:
                conn.rollback()
                if 'task_code' in str(ie).lower():
                    continue
                raise
    except Exception as e:
        print(f"Error creating task: {e}")
    finally:
        cursor.close()
        pput(conn)
    return {"task_id": task_id, "task_code": task_code}

def get_tasks_for_company(company_name):
    """Tasks raised by a given workspace — lets the requester track their own."""
    conn = pget()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM tasks WHERE company_name = %s ORDER BY created_at DESC",
                       (company_name,))
        return cursor.fetchall()
    except Exception as e:
        print(f"Error fetching company tasks: {e}")
        return []
    finally:
        cursor.close(); pput(conn)

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
    """Party Master directory = the UNION of every place a party for this company can
    live, de-duplicated case-insensitively by name, so a party touched anywhere in
    Bank Reco shows up here:
      1. `parties` table (rich, editable rows — these win on a name clash)
      2. `tally_ledgers` Sundry Debtor/Creditor ledgers (gives gstin/pan/address)
      3. distinct `bank_transactions.party` (parties assigned on a bank line — incl.
         AI-suggested ones that were never explicitly 'Added' as a ledger)
      4. `knowledge_base` tally_master_party (parties the AI learned)"""
    conn = get_conn()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("SELECT * FROM parties WHERE company_name = %s ORDER BY name ASC", (company_name,))
        rows = cursor.fetchall()
        seen = {(r.get("name") or "").strip().lower() for r in rows}

        def _add(nm, gstin=None, pan=None, address=None):
            nm = (nm or "").strip()
            if not nm or nm.lower() in seen:
                return
            seen.add(nm.lower())
            rows.append({
                # Unique synthetic id — the UI de-dupes rows by id, so these must NOT
                # collide (a shared null id would collapse them all into one row).
                "id": "virtual::" + nm, "company_name": company_name, "name": nm,
                "gstin": gstin, "address": address,
                "bank_name": None, "account_number": None, "ifsc_code": None,
                "pan": pan, "email": None, "phone": None,
                "created_at": None, "company_id": None,
            })

        # 2. Sundry Debtor/Creditor ledgers
        cursor.execute("""
            SELECT name, gstin, pan, address FROM tally_ledgers
            WHERE company_name = %s
              AND (LOWER(COALESCE(parent_group,'')) LIKE '%%sundry%%'
                   OR LOWER(COALESCE(parent_group,'')) LIKE '%%debtor%%'
                   OR LOWER(COALESCE(parent_group,'')) LIKE '%%creditor%%')
        """, (company_name,))
        for L in cursor.fetchall():
            _add(L.get("name"), L.get("gstin"), L.get("pan"), L.get("address"))

        # 3. Parties assigned on bank-reco lines (AI-suggested or manually set)
        try:
            cursor.execute("""SELECT DISTINCT party FROM bank_transactions
                              WHERE company_name = %s AND COALESCE(party,'') <> ''""",
                           (company_name,))
            for r in cursor.fetchall():
                _add(r.get("party"))
        except Exception:
            conn.rollback()

        # 4. Parties the AI learned (knowledge_base)
        try:
            cursor.execute("""SELECT DISTINCT data->>'party' AS p FROM knowledge_base
                              WHERE type='tally_master_party' AND data->>'company_name' = %s
                                AND COALESCE(data->>'party','') <> ''""",
                           (company_name,))
            for r in cursor.fetchall():
                _add(r.get("p"))
        except Exception:
            conn.rollback()

        rows.sort(key=lambda r: (r.get("name") or "").lower())
        return rows
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

# =============================================================================
# MULTI-TENANT HELPERS (Phase A)
# =============================================================================

ROLE_PERMISSIONS = {
    'super_admin':        ['*'],
    'firm_owner':         ['view', 'create', 'edit', 'delete', 'finalize', 'export', 'reconcile_draft', 'manage_users', 'manage_billing'],
    'firm_manager':       ['view', 'create', 'edit', 'delete', 'finalize', 'export', 'reconcile_draft'],
    'firm_accountant':    ['view', 'create', 'edit', 'finalize', 'export', 'reconcile_draft'],
    'firm_junior':        ['view', 'create', 'edit', 'reconcile_draft'],
    'company_owner':      ['view', 'create', 'edit', 'delete', 'finalize', 'export', 'reconcile_draft', 'manage_users', 'manage_billing'],
    'company_accountant': ['view', 'create', 'edit', 'finalize', 'export', 'reconcile_draft'],
    'company_viewer':     ['view'],
}

# Map short role (owner/manager/accountant/junior/viewer) + org type -> full role key for permission lookup
def _full_role_key(short_role, org_type):
    """Convert ('owner','firm') -> 'firm_owner'."""
    if short_role == 'super_admin':
        return 'super_admin'
    prefix = 'firm' if org_type == 'firm' else 'company'
    # company-side doesn't have 'manager' or 'junior' in our matrix; map gracefully
    if org_type == 'company' and short_role in ('manager', 'junior'):
        short_role = 'accountant'
    return f"{prefix}_{short_role}"


def user_memberships_basic(user_id):
    """Lightweight: (org_id, role) per membership in join order — no per-org company
    fetch (avoids the N+1 in get_user_memberships). Used by hot paths like Network."""
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT m.org_id, m.role
            FROM memberships m JOIN organizations o ON o.id = m.org_id
            WHERE m.user_id = %s AND o.archived_at IS NULL
            ORDER BY m.joined_at ASC
        """, (str(user_id),))
        return cur.fetchall()
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


def get_user_memberships(user_id):
    """Return all memberships for a user, including org + companies list per membership."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT m.id AS membership_id, m.role, m.scope_company_ids,
               o.id AS org_id, o.name AS org_name, o.type AS org_type, o.plan
        FROM memberships m
        JOIN organizations o ON m.org_id = o.id
        WHERE m.user_id = %s AND o.archived_at IS NULL
        ORDER BY m.joined_at ASC
    """, (user_id,))
    rows = cur.fetchall()
    # Attach companies per org
    for r in rows:
        cur.execute("""
            SELECT id, name, gstin, state_code, is_primary
            FROM companies WHERE org_id = %s AND archived_at IS NULL
            ORDER BY is_primary DESC, name ASC
        """, (r['org_id'],))
        r['companies'] = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def user_can(user_id, permission, company_id=None):
    """Check if a user has a specific permission, optionally scoped to a company."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Super admin shortcut
        cur.execute("SELECT is_super_admin FROM users WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if u and u['is_super_admin']:
            return True

        if company_id:
            # Find membership in the org that owns this company
            cur.execute("""
                SELECT m.role, m.scope_company_ids, o.type AS org_type
                FROM memberships m
                JOIN organizations o ON m.org_id = o.id
                JOIN companies c ON c.org_id = o.id
                WHERE m.user_id = %s AND c.id = %s
            """, (user_id, company_id))
            row = cur.fetchone()
            if not row:
                return False
            # Check scope_company_ids
            scope = row['scope_company_ids']
            if scope is not None and isinstance(scope, list) and len(scope) > 0:
                if str(company_id) not in [str(s) for s in scope]:
                    return False
            full_key = _full_role_key(row['role'], row['org_type'])
        else:
            # No company scope — just need ANY membership granting this perm
            cur.execute("""
                SELECT m.role, o.type AS org_type
                FROM memberships m JOIN organizations o ON m.org_id = o.id
                WHERE m.user_id = %s
            """, (user_id,))
            rows = cur.fetchall()
            for r in rows:
                full_key = _full_role_key(r['role'], r['org_type'])
                perms = ROLE_PERMISSIONS.get(full_key, [])
                if '*' in perms or permission in perms:
                    return True
            return False

        perms = ROLE_PERMISSIONS.get(full_key, [])
        return '*' in perms or permission in perms
    finally:
        cur.close()
        conn.close()


def list_org_companies(org_id):
    """Companies in one org (id + name) — used by the Network approve scope picker."""
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id, name FROM companies WHERE org_id=%s AND archived_at IS NULL ORDER BY name",
                    (str(org_id),))
        return [{"id": str(r["id"]), "name": r["name"]} for r in cur.fetchall()]
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


def list_org_companies_with_gstin(org_id):
    """Workspace companies (name + gstin) — for matching a shared doc to the user's
    OWN company (not party-master entries) in the Unallocated inbox suggestion."""
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT name, gstin FROM companies WHERE org_id=%s AND archived_at IS NULL ORDER BY name",
                    (str(org_id),))
        return cur.fetchall()
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


def get_companies_for_user(user_id):
    """Return all companies the user has any access to (across all memberships)."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT DISTINCT c.id, c.name, c.gstin, c.org_id, o.name AS org_name, o.type AS org_type
        FROM companies c
        JOIN memberships m ON m.org_id = c.org_id
        JOIN organizations o ON o.id = c.org_id
        WHERE m.user_id = %s AND c.archived_at IS NULL AND o.archived_at IS NULL
          AND (
            m.scope_company_ids IS NULL
            OR (jsonb_typeof(m.scope_company_ids) = 'array' AND m.scope_company_ids::text = '[]')
            OR (m.scope_company_ids @> to_jsonb(c.id::text))
          )
        ORDER BY o.name, c.name
    """, (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def classify_companies_for_user(user_id):
    """Tag every company the user can access for the switcher badges:
      'owned'    — they own the workspace or are the company's client owner ("added by you")
      'shared'   — access was granted to them by someone else ("shared with you")
      'archived' — soft-deleted (removed) company they could otherwise see
    Returns {'meta': {name: 'owned'|'shared'}, 'archived': [name, ...]}. A name that is
    active somewhere is never reported as archived; 'owned' wins over 'shared' on ties."""
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT c.name,
                   c.archived_at IS NOT NULL AS is_archived,
                   (m.role = 'owner' OR c.client_owner_user_id = %s) AS is_owned
            FROM companies c
            JOIN memberships m ON m.org_id = c.org_id
            JOIN organizations o ON o.id = c.org_id
            WHERE m.user_id = %s AND o.archived_at IS NULL
              AND (
                m.scope_company_ids IS NULL
                OR (jsonb_typeof(m.scope_company_ids) = 'array' AND m.scope_company_ids::text = '[]')
                OR (m.scope_company_ids @> to_jsonb(c.id::text))
              )
        """, (str(user_id), str(user_id)))
        meta = {}; archived = set()
        for r in cur.fetchall():
            name = r["name"]
            if r["is_archived"]:
                archived.add(name)
                continue
            if meta.get(name) != "owned":
                meta[name] = "owned" if r["is_owned"] else "shared"
        return {"meta": meta, "archived": [n for n in archived if n not in meta]}
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


def create_organization(name, org_type, owner_user_id, gstin=None, plan='free'):
    """Create a new organization (firm or company)."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        INSERT INTO organizations (name, type, gstin, plan, created_by_user_id)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
    """, (name, org_type, gstin, plan, owner_user_id))
    new_id = cur.fetchone()['id']
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def create_company(org_id, name, gstin=None, state_code=None, is_primary=False, client_owner_user_id=None):
    """Create a company under an organization."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        INSERT INTO companies (org_id, name, gstin, state_code, is_primary, client_owner_user_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (org_id, name) DO UPDATE SET gstin = COALESCE(EXCLUDED.gstin, companies.gstin)
        RETURNING id
    """, (org_id, name, gstin, state_code, is_primary, client_owner_user_id))
    new_id = cur.fetchone()['id']
    conn.commit()
    cur.close()
    conn.close()
    return new_id


# ── Manage Companies (workspace owner) ──────────────────────────────────────
def list_workspace_companies(org_id):
    """Active companies in a workspace (id, name, gstin, state, primary)."""
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT id, name, gstin, state_code, is_primary
                       FROM companies WHERE org_id=%s AND archived_at IS NULL
                       ORDER BY is_primary DESC, name""", (str(org_id),))
        return [{"id": str(r["id"]), "name": r["name"], "gstin": r["gstin"],
                 "state_code": r["state_code"], "is_primary": r["is_primary"]} for r in cur.fetchall()]
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)


def update_company_details(org_id, company_id, gstin=None, state_code=None):
    """Edit a company's GSTIN / state (NOT the name — name is a cross-table key)."""
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("""UPDATE companies SET gstin=%s, state_code=%s
                       WHERE id=%s AND org_id=%s AND archived_at IS NULL""",
                    (gstin, state_code, str(company_id), str(org_id)))
        ok = cur.rowcount > 0; conn.commit()
        return {"ok": ok} if ok else {"ok": False, "error": "Company not found in this workspace."}
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[update_company_details] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); pput(conn)


def archive_company(org_id, company_id):
    """Soft-delete a company (recoverable) + drop its name from the org members' legacy
    company lists so it leaves the switcher. Refuses to archive the last active company."""
    import json as _json
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT name FROM companies WHERE id=%s AND org_id=%s AND archived_at IS NULL",
                    (str(company_id), str(org_id)))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "Company not found in this workspace."}
        name = row["name"]
        cur.execute("SELECT COUNT(*) AS c FROM companies WHERE org_id=%s AND archived_at IS NULL", (str(org_id),))
        if (cur.fetchone()["c"] or 0) <= 1:
            return {"ok": False, "error": "You can't remove your only company."}
        cur.execute("UPDATE companies SET archived_at=CURRENT_TIMESTAMP WHERE id=%s AND org_id=%s",
                    (str(company_id), str(org_id)))
        # Drop the name from every org member's legacy company list (+ fix their active company).
        cur.execute("""SELECT au.username, au.companies, au.company_name
                       FROM accounting_users au
                       JOIN users u ON u.id = au.users_id
                       JOIN memberships m ON m.user_id = u.id
                       WHERE m.org_id=%s""", (str(org_id),))
        for r in cur.fetchall():
            comps = r["companies"]
            if isinstance(comps, str):
                try: comps = _json.loads(comps)
                except Exception: comps = []
            comps = comps or []
            if name in comps:
                comps = [c for c in comps if c != name]
                new_active = r["company_name"] if r["company_name"] != name else (comps[0] if comps else None)
                cur.execute("UPDATE accounting_users SET companies=%s, company_name=%s WHERE username=%s",
                            (_json.dumps(comps), new_active, r["username"]))
        conn.commit()
        return {"ok": True, "name": name}
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[archive_company] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); pput(conn)


def create_membership(user_id, org_id, role, scope_company_ids=None, invited_by=None):
    """Add a user to an org with a role."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    scope_json = json.dumps([str(s) for s in scope_company_ids]) if scope_company_ids else None
    cur.execute("""
        INSERT INTO memberships (user_id, org_id, role, scope_company_ids, invited_by)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (user_id, org_id) DO UPDATE SET role = EXCLUDED.role
        RETURNING id
    """, (user_id, org_id, role, scope_json, invited_by))
    new_id = cur.fetchone()['id']
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def get_company_gstin(company_name):
    """The workspace company's own GSTIN (for the 'is this workspace a party to the
    invoice?' check). Returns the normalized GSTIN string or None."""
    if not company_name:
        return None
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT gstin FROM companies WHERE name = %s AND archived_at IS NULL "
                    "ORDER BY is_primary DESC NULLS LAST LIMIT 1", (company_name,))
        row = cur.fetchone()
        g = (row[0] if row else None) or ""
        return g.strip().upper() or None
    except Exception as e:
        print(f"[get_company_gstin] {e}"); return None
    finally:
        cur.close(); conn.close()

def get_company_by_name_and_org(org_id, name):
    """Resolve a company by (org_id, name) -> company row, or None."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM companies WHERE org_id = %s AND name = %s", (org_id, name))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


# ============================================================
# Sprint 46 — AnyDesk-style handshake codes (share workspace access)
# ============================================================
def _ensure_connection_codes_table():
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS connection_codes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code TEXT UNIQUE NOT NULL,
                org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('manager','accountant','junior','viewer','owner')),
                scope_company_ids JSONB,
                created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                expires_at TIMESTAMP NOT NULL,
                used_at TIMESTAMP,
                used_by_user_id UUID,
                revoked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conn_codes_org ON connection_codes(org_id, created_at DESC)")
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[_ensure_connection_codes_table] {e}")


def _gen_handshake_code():
    """9-digit numeric, shown grouped as XXX-XXX-XXX (AnyDesk-style)."""
    import secrets
    return "".join(secrets.choice("0123456789") for _ in range(9))


def create_connection_code(org_id, role, scope_company_ids=None,
                           created_by_user_id=None, ttl_hours=24):
    """Issue a single-use, time-limited code granting `role` into `org_id`."""
    _ensure_connection_codes_table()
    if role not in ('manager', 'accountant', 'junior', 'viewer', 'owner'):
        return {"ok": False, "error": "Invalid role."}
    import json as _json
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        scope_json = _json.dumps([str(s) for s in scope_company_ids]) if scope_company_ids else None
        code = None
        for _ in range(6):  # retry on the rare unique collision
            cand = _gen_handshake_code()
            cur.execute("SELECT 1 FROM connection_codes WHERE code=%s", (cand,))
            if not cur.fetchone():
                code = cand
                break
        if not code:
            return {"ok": False, "error": "Could not allocate a code, try again."}
        cur.execute("""
            INSERT INTO connection_codes (code, org_id, role, scope_company_ids,
                                          created_by_user_id, expires_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, CURRENT_TIMESTAMP + (%s || ' hours')::interval)
            RETURNING code, expires_at
        """, (code, org_id, role, scope_json, created_by_user_id, str(int(ttl_hours))))
        row = cur.fetchone()
        conn.commit()
        return {"ok": True, "code": row["code"], "expires_at": str(row["expires_at"]),
                "role": role}
    except Exception as e:
        conn.rollback(); print(f"[create_connection_code] {e}")
        return {"ok": False, "error": str(e)}
    finally:
        cur.close(); conn.close()


def accept_connection_code(code, accepter_users_id, accepter_username):
    """Validate a code (exists, not revoked/used/expired), create the membership,
    and project the granted companies' NAMES into the accepter's
    accounting_users.companies so the company_name-keyed data layer grants access."""
    _ensure_connection_codes_table()
    import json as _json
    code = (code or "").replace("-", "").replace(" ", "").strip()
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT * FROM connection_codes WHERE code=%s""", (code,))
        c = cur.fetchone()
        if not c:
            return {"ok": False, "error": "Invalid code."}
        if c["revoked_at"]:
            return {"ok": False, "error": "This code was revoked."}
        if c["used_at"]:
            return {"ok": False, "error": "This code has already been used."}
        cur.execute("SELECT CURRENT_TIMESTAMP > %s AS expired", (c["expires_at"],))
        if cur.fetchone()["expired"]:
            return {"ok": False, "error": "This code has expired."}

        org_id = c["org_id"]
        # Don't let someone accept into a workspace they already belong to as owner.
        cur.execute("SELECT name FROM organizations WHERE id=%s", (org_id,))
        org = cur.fetchone()
        org_name = org["name"] if org else "workspace"

        scope = c["scope_company_ids"]
        if isinstance(scope, str):
            try: scope = _json.loads(scope)
            except Exception: scope = None
        if scope:
            cur.execute("SELECT name FROM companies WHERE org_id=%s AND id::text = ANY(%s)",
                        (org_id, [str(s) for s in scope]))
        else:
            cur.execute("SELECT name FROM companies WHERE org_id=%s AND archived_at IS NULL", (org_id,))
        company_names = [r["name"] for r in cur.fetchall()]

        # membership (issuer-chosen role)
        scope_json = _json.dumps([str(s) for s in scope]) if scope else None
        cur.execute("""
            INSERT INTO memberships (user_id, org_id, role, scope_company_ids, invited_by)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (user_id, org_id) DO UPDATE SET role = EXCLUDED.role,
                                                        scope_company_ids = EXCLUDED.scope_company_ids
        """, (accepter_users_id, org_id, c["role"], scope_json, c["created_by_user_id"]))

        # mark used
        cur.execute("""UPDATE connection_codes SET used_at=CURRENT_TIMESTAMP, used_by_user_id=%s
                       WHERE id=%s""", (accepter_users_id, c["id"]))
        conn.commit()
        cur.close(); conn.close()

        # project company access into legacy accounting_users.companies (separate conns)
        for cname in company_names:
            try: add_company_to_user(accepter_username, cname)
            except Exception as pe: print(f"[accept_connection_code project] {pe}")

        return {"ok": True, "org_name": org_name, "role": c["role"],
                "companies": company_names}
    except Exception as e:
        conn.rollback(); print(f"[accept_connection_code] {e}")
        try: cur.close(); conn.close()
        except Exception: pass
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────
# Network — AnyDesk-style workspace relationships (persistent ID + approve).
# Reuses memberships for the actual access grant; org_relationships tracks the
# typed relationship + request/approve lifecycle.
# ─────────────────────────────────────────────────────────────────────────
import random as _rnd

# Relationship type → access role the requester gets in the TARGET workspace.
NETWORK_REL_ROLES = {
    "ca":         "manager",      # I'm their CA / accounting firm
    "accountant": "accountant",
    "auditor":    "viewer",
    "staff":      "junior",
    "manager":    "manager",
    "other":      "viewer",       # custom — approver can override the role
}

_NETWORK_SCHEMA_READY = False

def _ensure_network_schema():
    # Run once per process. The DDL here (ALTER TABLE / CREATE TABLE) takes an
    # AccessExclusiveLock; doing it on every network call serialized requests and
    # could deadlock against concurrent readers. The schema is stable after first run.
    global _NETWORK_SCHEMA_READY
    if _NETWORK_SCHEMA_READY:
        return
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS connect_id TEXT UNIQUE")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS org_relationships (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                requester_org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                target_org_id    UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                relationship_type TEXT NOT NULL,
                granted_role     TEXT NOT NULL,
                grantee_user_id  UUID,                 -- requester's user who gains access
                scope_company_ids JSONB,               -- null = all target companies
                status TEXT NOT NULL DEFAULT 'pending', -- pending|active|declined|revoked
                requested_by UUID,
                approved_by  UUID,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orgrel_target ON org_relationships(target_org_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orgrel_req ON org_relationships(requester_org_id, status)")
        # Role + relationship are chosen by the ACCEPTOR at approval, not at request time,
        # so they must be nullable while the request is pending (idempotent, non-destructive).
        try: cur.execute("ALTER TABLE org_relationships ALTER COLUMN relationship_type DROP NOT NULL")
        except Exception: pass
        try: cur.execute("ALTER TABLE org_relationships ALTER COLUMN granted_role DROP NOT NULL")
        except Exception: pass
        conn.commit()
        _NETWORK_SCHEMA_READY = True
    except Exception as e:
        conn.rollback(); print(f"[_ensure_network_schema] {e}")
    finally:
        cur.close(); conn.close()

def _gen_connect_id():
    return f"YTR-{_rnd.randint(100,999)}-{_rnd.randint(100,999)}"

def get_or_create_connect_id(org_id):
    """Stable AnyDesk-style Workspace ID for an org (generated once)."""
    _ensure_network_schema()
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("SELECT connect_id FROM organizations WHERE id=%s", (org_id,))
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
        for _ in range(12):
            cid = _gen_connect_id()
            try:
                cur.execute("UPDATE organizations SET connect_id=%s WHERE id=%s AND connect_id IS NULL", (cid, org_id))
                if cur.rowcount:
                    conn.commit(); return cid
            except Exception:
                conn.rollback()
        cur.execute("SELECT connect_id FROM organizations WHERE id=%s", (org_id,))
        r2 = cur.fetchone(); return r2[0] if r2 else None
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)

def org_by_connect_id(code):
    _ensure_network_schema()
    code = (code or "").strip().upper()
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id, name FROM organizations WHERE UPPER(connect_id)=%s AND archived_at IS NULL", (code,))
        return cur.fetchone()
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)

def get_org_name(org_id):
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM organizations WHERE id=%s", (str(org_id),))
        r = cur.fetchone(); return r[0] if r else None
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)

def grant_membership(user_id, org_id, role, scope_company_ids=None, invited_by=None):
    """Create/update a membership (user → org, role + company scope) and project the
    granted companies' names into legacy accounting_users.companies. Reused by the
    connect-code accept flow and the Network approve flow."""
    import json as _json
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        scope = scope_company_ids
        if isinstance(scope, str):
            try: scope = _json.loads(scope)
            except Exception: scope = None
        if scope:
            cur.execute("SELECT name FROM companies WHERE org_id=%s AND id::text = ANY(%s)",
                        (org_id, [str(s) for s in scope]))
        else:
            cur.execute("SELECT name FROM companies WHERE org_id=%s AND archived_at IS NULL", (org_id,))
        company_names = [r["name"] for r in cur.fetchall()]
        scope_json = _json.dumps([str(s) for s in scope]) if scope else None
        cur.execute("""
            INSERT INTO memberships (user_id, org_id, role, scope_company_ids, invited_by)
            VALUES (%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (user_id, org_id) DO UPDATE SET role=EXCLUDED.role,
                                                        scope_company_ids=EXCLUDED.scope_company_ids
        """, (str(user_id), str(org_id), role, scope_json, str(invited_by) if invited_by else None))
        cur.execute("SELECT username FROM users WHERE id=%s", (str(user_id),))
        urow = cur.fetchone(); uname = urow["username"] if urow else None
        conn.commit(); cur.close(); pput(conn)
        if uname:
            for cname in company_names:
                try: add_company_to_user(uname, cname)
                except Exception as pe: print(f"[grant_membership project] {pe}")
        return {"ok": True, "companies": company_names}
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        try: cur.close()
        except Exception: pass
        pput(conn, bad=True)
        print(f"[grant_membership] {e}"); return {"ok": False, "error": str(e)}

def create_relationship_request(requester_org_id, target_org_id, requested_by):
    """Requester just asks to connect to the target's workspace. The ACCEPTOR assigns the
    role + chooses which companies to share at approval, so relationship_type/granted_role
    stay NULL until then."""
    _ensure_network_schema()
    if str(requester_org_id) == str(target_org_id):
        return {"ok": False, "error": "You can't connect a workspace to itself."}
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""SELECT id, status FROM org_relationships
                       WHERE requester_org_id=%s AND target_org_id=%s AND status IN ('pending','active')
                       LIMIT 1""", (str(requester_org_id), str(target_org_id)))
        ex = cur.fetchone()
        if ex:
            conn.rollback()
            return {"ok": False, "error": f"A {ex['status']} connection already exists with that workspace."}
        cur.execute("""INSERT INTO org_relationships
            (requester_org_id, target_org_id, grantee_user_id, requested_by, status)
            VALUES (%s,%s,%s,%s,'pending') RETURNING id""",
            (str(requester_org_id), str(target_org_id),
             str(requested_by) if requested_by else None, str(requested_by) if requested_by else None))
        rid = cur.fetchone()["id"]; conn.commit()
        return {"ok": True, "id": str(rid)}
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[create_relationship_request] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); pput(conn)

def list_relationship_requests(org_id):
    """Pending requests where this org is the TARGET (incoming, to approve) or the
    REQUESTER (outgoing, awaiting their approval)."""
    _ensure_network_schema()
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT r.*, ro.name AS requester_name, ro.connect_id AS requester_connect_id,
                         to2.name AS target_name, to2.connect_id AS target_connect_id,
                         rown.owner AS requester_owner, town.owner AS target_owner
            FROM org_relationships r
            JOIN organizations ro ON ro.id = r.requester_org_id
            JOIN organizations to2 ON to2.id = r.target_org_id
            LEFT JOIN LATERAL (
                SELECT COALESCE(au.name, au.username) AS owner
                FROM memberships m JOIN accounting_users au ON au.users_id = m.user_id
                WHERE m.org_id = ro.id AND m.role='owner' ORDER BY m.joined_at ASC LIMIT 1
            ) rown ON TRUE
            LEFT JOIN LATERAL (
                SELECT COALESCE(au.name, au.username) AS owner
                FROM memberships m JOIN accounting_users au ON au.users_id = m.user_id
                WHERE m.org_id = to2.id AND m.role='owner' ORDER BY m.joined_at ASC LIMIT 1
            ) town ON TRUE
            WHERE r.status='pending' AND (r.target_org_id=%s OR r.requester_org_id=%s)
            ORDER BY r.created_at DESC""", (str(org_id), str(org_id)))
        rows = cur.fetchall()
        inc = [_relrow(x) for x in rows if str(x["target_org_id"]) == str(org_id)]
        out = [_relrow(x) for x in rows if str(x["requester_org_id"]) == str(org_id)]
        return {"incoming": inc, "outgoing": out}
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)

def _relrow(x):
    return {"id": str(x["id"]), "relationship_type": x["relationship_type"],
            "granted_role": x["granted_role"], "status": x["status"],
            "requester_name": x.get("requester_name"), "requester_connect_id": x.get("requester_connect_id"),
            "requester_owner": x.get("requester_owner"),
            "target_name": x.get("target_name"), "target_connect_id": x.get("target_connect_id"),
            "target_owner": x.get("target_owner"),
            "created_at": x["created_at"].isoformat() if x.get("created_at") else None}

def approve_relationship(rel_id, relationship_type=None, scope_company_ids=None):
    """Acceptor approves → assigns the role (via relationship_type) + which companies to share,
    and grants the requester's user scoped access to the acceptor's workspace."""
    _ensure_network_schema()
    if not relationship_type:
        return {"ok": False, "error": "Pick their role before granting access."}
    grant_role = NETWORK_REL_ROLES.get(relationship_type, "viewer")
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM org_relationships WHERE id=%s", (str(rel_id),))
        r = cur.fetchone()
        if not r: conn.rollback(); cur.close(); pput(conn); return {"ok": False, "error": "Request not found."}
        if r["status"] != "pending": conn.rollback(); cur.close(); pput(conn); return {"ok": False, "error": f"Already {r['status']}."}
        conn.rollback(); cur.close(); pput(conn)
        g = grant_membership(r["grantee_user_id"], r["target_org_id"], grant_role,
                             scope_company_ids, invited_by=None)
        if not g.get("ok"): return {"ok": False, "error": g.get("error", "grant failed")}
        conn2 = pget(); cur2 = conn2.cursor()
        try:
            import json as _json
            sj = _json.dumps([str(s) for s in scope_company_ids]) if scope_company_ids else None
            cur2.execute("""UPDATE org_relationships SET status='active', relationship_type=%s,
                            granted_role=%s, scope_company_ids=%s::jsonb,
                            updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                         (relationship_type, grant_role, sj, str(rel_id)))
            conn2.commit()
        finally:
            cur2.close(); pput(conn2)
        return {"ok": True, "companies": g.get("companies", [])}
    except Exception as e:
        print(f"[approve_relationship] {e}"); return {"ok": False, "error": str(e)}

def decline_relationship(rel_id):
    _ensure_network_schema()
    conn = pget(); cur = conn.cursor()
    try:
        cur.execute("UPDATE org_relationships SET status='declined', updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status='pending'", (str(rel_id),))
        conn.commit(); return {"ok": True}
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[decline_relationship] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); pput(conn)

def list_connections(org_id):
    """Active relationships where this org is on either side."""
    _ensure_network_schema()
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT r.*, ro.name AS requester_name, ro.connect_id AS requester_connect_id,
                         to2.name AS target_name, to2.connect_id AS target_connect_id,
                         rown.owner AS requester_owner, town.owner AS target_owner
            FROM org_relationships r
            JOIN organizations ro ON ro.id=r.requester_org_id
            JOIN organizations to2 ON to2.id=r.target_org_id
            LEFT JOIN LATERAL (
                SELECT COALESCE(au.name, au.username) AS owner
                FROM memberships m JOIN accounting_users au ON au.users_id = m.user_id
                WHERE m.org_id = ro.id AND m.role='owner' ORDER BY m.joined_at ASC LIMIT 1
            ) rown ON TRUE
            LEFT JOIN LATERAL (
                SELECT COALESCE(au.name, au.username) AS owner
                FROM memberships m JOIN accounting_users au ON au.users_id = m.user_id
                WHERE m.org_id = to2.id AND m.role='owner' ORDER BY m.joined_at ASC LIMIT 1
            ) town ON TRUE
            WHERE r.status='active' AND (r.requester_org_id=%s OR r.target_org_id=%s)
            ORDER BY r.updated_at DESC""", (str(org_id), str(org_id)))
        out = []
        for x in cur.fetchall():
            mine_is_target = str(x["target_org_id"]) == str(org_id)
            out.append({"id": str(x["id"]), "relationship_type": x["relationship_type"],
                        "granted_role": x["granted_role"],
                        "other_owner": x["requester_owner"] if mine_is_target else x["target_owner"],
                        "other_name": x["requester_name"] if mine_is_target else x["target_name"],
                        "other_connect_id": x["requester_connect_id"] if mine_is_target else x["target_connect_id"],
                        "direction": "they access mine" if mine_is_target else "I access theirs"})
        return out
    finally:
        cur.close()
        try: conn.rollback()
        except Exception: pass
        pput(conn)

def revoke_relationship(rel_id):
    _ensure_network_schema()
    conn = pget(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT grantee_user_id, target_org_id FROM org_relationships WHERE id=%s", (str(rel_id),))
        r = cur.fetchone()
        if r and r["grantee_user_id"]:
            cur.execute("DELETE FROM memberships WHERE user_id=%s AND org_id=%s",
                        (str(r["grantee_user_id"]), str(r["target_org_id"])))
        cur.execute("UPDATE org_relationships SET status='revoked', updated_at=CURRENT_TIMESTAMP WHERE id=%s", (str(rel_id),))
        conn.commit(); return {"ok": True}
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        print(f"[revoke_relationship] {e}"); return {"ok": False, "error": str(e)}
    finally:
        cur.close(); pput(conn)


def list_connection_codes(org_id):
    """All codes issued for an org (active first)."""
    _ensure_connection_codes_table()
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, code, role, scope_company_ids, expires_at, used_at, used_by_user_id,
               revoked_at, created_at,
               (revoked_at IS NULL AND used_at IS NULL AND CURRENT_TIMESTAMP <= expires_at) AS active
        FROM connection_codes WHERE org_id=%s ORDER BY created_at DESC LIMIT 50
    """, (org_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows


def revoke_connection_code(code_id, org_id=None):
    _ensure_connection_codes_table()
    conn = get_conn(); cur = conn.cursor()
    try:
        if org_id:
            cur.execute("UPDATE connection_codes SET revoked_at=CURRENT_TIMESTAMP WHERE id=%s AND org_id=%s", (code_id, org_id))
        else:
            cur.execute("UPDATE connection_codes SET revoked_at=CURRENT_TIMESTAMP WHERE id=%s", (code_id,))
        conn.commit(); return cur.rowcount > 0
    finally:
        cur.close(); conn.close()


def list_org_members(org_id):
    """People in an org with their roles."""
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT m.id AS membership_id, m.role, m.scope_company_ids, m.joined_at,
               u.id AS user_id, u.username, u.name, u.email
        FROM memberships m JOIN users u ON m.user_id = u.id
        WHERE m.org_id = %s ORDER BY m.joined_at ASC
    """, (org_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows


def embed_confirmed_voucher(company_name, voucher, embed_fn, company_id=None):
    """Seed the workspace knowledge base from a CONFIRMED voucher so Training
    Progress grows as the user processes invoices. Embeds the party, narration,
    line items and counter ledger — idempotent by kb_key (re-confirming the same
    party/item won't double-count). embed_fn(text)->list[float] is supplied by the
    caller (server.get_embedding). Returns counts."""
    if not embed_fn or not voucher:
        return {"skipped": True}
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    counts = {"parties": 0, "narrations": 0, "txns": 0, "items": 0, "ledgers": 0, "skipped": 0}
    def _already(kb_type, kb_key):
        cur.execute("SELECT 1 FROM knowledge_base WHERE type=%s AND data->>'company_name'=%s AND data->>'kb_key'=%s LIMIT 1",
                    (kb_type, company_name, kb_key))
        return cur.fetchone() is not None
    def _insert(kb_type, kb_key, text, payload):
        if _already(kb_type, kb_key):
            counts["skipped"] += 1; return False
        try:
            emb = embed_fn(text)
        except Exception as e:
            print(f"[embed_confirmed_voucher] embed err {kb_type} {kb_key}: {e}"); return False
        if not emb:
            return False
        emb_str = "[" + ",".join(map(str, emb)) + "]"
        p = dict(payload); p["company_name"] = company_name
        p["company_id"] = str(company_id) if company_id else None; p["kb_key"] = kb_key
        p["content"] = text   # the exact text that was embedded (RAG transparency)
        cur.execute("INSERT INTO knowledge_base (type, data, embedding) VALUES (%s, %s::jsonb, %s)",
                    (kb_type, json.dumps(p), emb_str))
        return True
    try:
        party = (voucher.get("billing_party_name") or voucher.get("party_name") or "").strip()
        if party and _insert("tally_master_party", f"party::{party}",
                             f"Party '{party}' seen on a confirmed voucher.",
                             {"party": party, "gstin": voucher.get("billing_party_gstin")}):
            counts["parties"] += 1

        # Type-aware artefacts. A confirmed voucher gives us the head directly
        # (counter_ledger), so the txn row is high-signal even though the confirm
        # path doesn't write tally_vouchers.ledger_entries (that arrives on the next
        # Tally sync — get_party_default_head only learns the head from history then).
        canonical = canonicalize_voucher_type(voucher.get("voucher_type"))
        direction = _VTYPE_DIRECTION.get(canonical, "none")
        amt = voucher.get("total_amount") or voucher.get("amount") or 0
        vdate = str(voucher.get("date") or "")
        head = (voucher.get("counter_ledger") or voucher.get("category") or "").strip() or None
        narr = (voucher.get("narration") or voucher.get("notes") or "").strip()
        vid = str(voucher.get("id") or voucher.get("invoice_number")
                  or (narr[:40] if narr else party) or "confirmed")
        p = party or "n/a"
        amt_s = _fmt_amt(amt)
        ttxt = {
            "sales": f"Sales to '{p}' {amt_s} on {vdate}.",
            "purchase": f"Purchase from '{p}' {amt_s} on {vdate}.",
            "payment": f"Payment to '{p}' {amt_s} on {vdate}.",
            "receipt": f"Receipt from '{p}' {amt_s} on {vdate}.",
            "debit_note": f"Debit note to '{p}' {amt_s} on {vdate}.",
            "credit_note": f"Credit note from '{p}' {amt_s} on {vdate}.",
        }.get(canonical, f"Voucher {voucher.get('voucher_type') or 'entry'} {amt_s} on {vdate} for '{p}'.")
        if head:
            ttxt += f" Head: {head}."
        if narr:
            ttxt += f" Narration: {narr}"

        if _insert("tally_master_txn", f"txn::{canonical}::{vid}", ttxt, {
            "canonical_vtype": canonical, "raw_voucher_type": voucher.get("voucher_type"),
            "direction": direction, "party": party or None, "derived_head": head,
            "amount": float(amt or 0), "voucher_id": vid, "date": vdate,
            "narration": narr, "source": "confirmed_voucher",
        }):
            counts["txns"] += 1
        if narr and len(narr) > 10 and _insert(
                "tally_master_narration", f"narration::v2::{vid}", ttxt, {
                    "voucher_id": vid, "date": vdate, "voucher_type": voucher.get("voucher_type"),
                    "canonical_vtype": canonical, "party": party or None,
                    "amount": float(amt or 0), "source": "confirmed_voucher"}):
            counts["narrations"] += 1
        for it in (voucher.get("items") or []):
            if not isinstance(it, dict):
                continue
            nm = (it.get("description") or it.get("item") or it.get("name") or "").strip()
            if not nm:
                continue
            hsn = it.get("hsn_sac") or it.get("hsn") or "n/a"
            if _insert("tally_master_item", f"item::{nm}", f"Item '{nm}' with HSN {hsn}.",
                       {"name": nm, "hsn": it.get("hsn_sac") or it.get("hsn")}):
                counts["items"] += 1
        led = (voucher.get("counter_ledger") or voucher.get("category") or "").strip()
        if led and _insert("tally_master_ledger", f"ledger::{led}",
                           f"Ledger '{led}' used on a confirmed voucher.", {"name": led}):
            counts["ledgers"] += 1
        conn.commit()
    except Exception as e:
        conn.rollback(); print(f"[embed_confirmed_voucher] {e}")
    finally:
        cur.close(); conn.close()
    return counts

def embed_party(company_name, name, embed_fn, company_id=None, gstin=None):
    """Teach the RAG store a single party (e.g. one a human added in Bank Reco).
    Inserts a tally_master_party row into knowledge_base — same shape/kb_key as
    embed_confirmed_voucher — so the party (a) shows in Training Progress and
    (b) becomes a retrieval candidate for the AI's party suggestions. Idempotent
    by kb_key. embed_fn(text)->list[float] is supplied by the caller."""
    name = (name or "").strip()
    if not embed_fn or not name:
        return {"skipped": True}
    conn = get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        kb_key = f"party::{name}"
        cur.execute("SELECT 1 FROM knowledge_base WHERE type='tally_master_party' "
                    "AND data->>'company_name'=%s AND data->>'kb_key'=%s LIMIT 1",
                    (company_name, kb_key))
        if cur.fetchone():
            return {"skipped": True, "reason": "already_learned"}
        text = f"Party '{name}' (added by user)."
        try:
            emb = embed_fn(text)
        except Exception as e:
            print(f"[embed_party] embed err {name}: {e}"); return {"error": str(e)}
        if not emb:
            return {"error": "no_embedding"}
        emb_str = "[" + ",".join(map(str, emb)) + "]"
        p = {"party": name, "gstin": gstin, "company_name": company_name,
             "company_id": str(company_id) if company_id else None,
             "kb_key": kb_key, "content": text, "source": "user_added"}
        cur.execute("INSERT INTO knowledge_base (type, data, embedding) VALUES (%s, %s::jsonb, %s)",
                    ("tally_master_party", json.dumps(p), emb_str))
        conn.commit()
        return {"learned": True}
    except Exception as e:
        conn.rollback(); print(f"[embed_party] {e}"); return {"error": str(e)}
    finally:
        cur.close(); conn.close()


def learn_bank_party(company_name, narration, party, embed_fn, company_id=None, line_id=None):
    """Learn a bank-narration → party association so the reconciler proposes this
    party for SIMILAR narrations on the next re-run. Embeds the narration text under
    type tally_master_party (value = party), keyed to the source line so it can be
    cleanly unlearned if the user clears the cell. Re-setting a line's party replaces
    its prior learning. Returns {learned|skipped|error}."""
    narration = (narration or "").strip(); party = (party or "").strip()
    if not embed_fn or not party or not narration:
        return {"skipped": True}
    kb_key = f"bankline::{line_id}" if line_id else f"banknarr::{narration[:80]}::{party}"
    conn = get_conn(); cur = conn.cursor()
    try:
        # Drop any prior learning for this line (the party may have changed).
        cur.execute("DELETE FROM knowledge_base WHERE type='tally_master_party' "
                    "AND data->>'company_name'=%s AND data->>'kb_key'=%s", (company_name, kb_key))
        try:
            emb = embed_fn(narration)
        except Exception as e:
            conn.rollback(); print(f"[learn_bank_party] embed err: {e}"); return {"error": str(e)}
        if not emb:
            conn.rollback(); return {"error": "no_embedding"}
        emb_str = "[" + ",".join(map(str, emb)) + "]"
        data = {"party": party, "company_name": company_name,
                "company_id": str(company_id) if company_id else None,
                "kb_key": kb_key, "content": narration, "narration": narration,
                "source": "bank_manual"}
        cur.execute("INSERT INTO knowledge_base (type, data, embedding) VALUES (%s, %s::jsonb, %s)",
                    ("tally_master_party", json.dumps(data), emb_str))
        conn.commit()
        return {"learned": True}
    except Exception as e:
        conn.rollback(); print(f"[learn_bank_party] {e}"); return {"error": str(e)}
    finally:
        cur.close(); conn.close()


def unlearn_bank_party(company_name, line_id):
    """Remove the bank-narration→party learning that a line created (on clear). Does
    NOT touch the party's master ledger entry — only this line's learned association."""
    if not line_id:
        return {"deleted": 0}
    kb_key = f"bankline::{line_id}"
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM knowledge_base WHERE type='tally_master_party' "
                    "AND data->>'company_name'=%s AND data->>'kb_key'=%s", (company_name, kb_key))
        n = cur.rowcount; conn.commit()
        return {"deleted": n}
    except Exception as e:
        conn.rollback(); print(f"[unlearn_bank_party] {e}"); return {"error": str(e)}
    finally:
        cur.close(); conn.close()


# ── Voucher-type-aware training helpers ──────────────────────────────────────
# Map a raw Tally voucher_type (incl. custom voucher classes like "Bank Payment",
# "Sales GST", "Cr Note") to a canonical category. Ordered substring rules, first
# match wins; case-insensitive; always returns a value, never raises.
# Cash-movement types (payment/receipt/contra) are matched BEFORE document types
# (sales/purchase) on purpose: for bank reco the cash signal matters most, so a
# class named e.g. "Sales Payment" should bucket as a payment.
def canonicalize_voucher_type(raw):
    s = (raw or "").strip().lower()
    if not s:
        return "other"
    if "debit note" in s or "dr note" in s:
        return "debit_note"
    if "credit note" in s or "cr note" in s:
        return "credit_note"
    if "contra" in s:
        return "contra"
    if "payment" in s:
        return "payment"
    if "receipt" in s:
        return "receipt"
    if "purchase" in s or "purc" in s:
        return "purchase"
    if "sales" in s or "sale" in s or "invoice" in s:
        return "sales"
    if "journal" in s or "jrnl" in s:
        return "journal"
    return "other"


# Direction implied by a canonical voucher type (money in / out / internal).
_VTYPE_DIRECTION = {
    "payment": "outflow", "purchase": "outflow", "debit_note": "outflow",
    "receipt": "inflow", "sales": "inflow", "credit_note": "inflow",
    "contra": "internal", "journal": "none", "other": "none",
}


def _is_bank_or_cash(parent_group, name):
    """True if a ledger is a bank/cash account (so it can't be the 'head').
    Uses the parent_group when known, else falls back to the ledger NAME — needed
    when tally_ledgers isn't populated for the company (no parent_group lookup)."""
    pg = (parent_group or "").lower()
    n = (name or "").strip().lower()
    if ("bank" in pg) or ("cash" in pg):
        return True
    if "bank" in n:                       # "ICICI Bank", "HDFC Bank A/c"
        return True
    if n == "cash" or n.startswith("cash ") or n.startswith("cash-"):
        return True
    return False


def _is_tax_or_roundoff(name):
    """GST/TDS/round-off legs — never the accounting head we want to learn."""
    n = (name or "").lower()
    return any(k in n for k in ("gst", "tax", "tds", "tcs", "cess",
                                "round off", "round-off", "rounding"))


def _leg_name(e):
    return (e.get("ledger_name") or e.get("ledger") or "").strip()


def _leg_is_debit(e):
    """Resolve a leg's debit/credit using the SAME tri-state logic as the bank
    import path (db.py ~4060): explicit is_debit wins, else infer from the sign
    of `amount` (Tally convention: Cr=+, Dr=−). Returns True/False/None."""
    isd = e.get("is_debit")
    if isd is True or isd is False:
        return isd
    try:
        amt = float(e.get("amount") or 0)
    except (TypeError, ValueError):
        return None
    if amt > 0:
        return False   # Tally positive ⇒ credit
    if amt < 0:
        return True    # Tally negative ⇒ debit
    return None


def _extract_legs(ledger_entries, ledgers_by_name, party_name=None):
    """Split a voucher's legs into (bank_legs, party_legs, head_legs, derived_head).
    - bank_legs:  bank / cash accounts
    - party_legs: the voucher's party (PARTYLEDGERNAME) + Sundry Debtor/Creditor controls
    - head_legs:  real expense / revenue / asset accounts (the bookable head)
    - derived_head: the largest-|amount| head leg name (or None)
    `ledgers_by_name` maps ledger name -> {"parent_group": ...} (may be empty if the
    company hasn't synced ledger masters — name heuristics then carry the load).
    `party_name` (the voucher's ledger_name) is excluded from head candidates so a
    trade party never gets mistaken for the accounting head.
    """
    pn = (party_name or "").strip().lower()
    bank_legs, party_legs, head_legs = [], [], []
    for e in (ledger_entries or []):
        nm = _leg_name(e)
        if not nm:
            continue
        pg = (ledgers_by_name.get(nm, {}).get("parent_group") or "").lower()
        try:
            amt = abs(float(e.get("amount") or 0))
        except (TypeError, ValueError):
            amt = 0.0
        if _is_bank_or_cash(pg, nm):
            bank_legs.append((nm, amt))
        elif (pn and nm.strip().lower() == pn) or ("sundry" in pg) or ("debtor" in pg) or ("creditor" in pg):
            party_legs.append((nm, amt))
        elif _is_tax_or_roundoff(nm):
            continue   # tax / round-off — tracked by neither party nor head
        else:
            head_legs.append((nm, amt))
    derived_head = max(head_legs, key=lambda x: x[1])[0] if head_legs else None
    return bank_legs, party_legs, head_legs, derived_head


def _load_ledgers_by_name(cur, company_id, company_name):
    """{ledger_name: {'parent_group': ...}} for one company (legacy NULL company_id safe)."""
    cur.execute("""
        SELECT name, parent_group FROM tally_ledgers
        WHERE company_id = %s OR (company_id IS NULL AND company_name = %s)
    """, (company_id, company_name))
    return {r["name"]: {"parent_group": r["parent_group"]} for r in cur.fetchall()}


def _fmt_amt(a):
    try:
        return f"₹{float(a or 0):,.0f}"
    except (TypeError, ValueError):
        return "₹0"


def build_voucher_training(v, ledgers_by_name):
    """Given a tally_vouchers row (dict with id/date/voucher_type/voucher_number/
    ledger_name/amount/narration/ledger_entries[list]) build the per-type training
    artefacts. Returns a dict with:
      canonical, raw_voucher_type, direction, party, derived_head, amount,
      voucher_id, voucher_number, date, narration, bank_legs[names],
      txn_text  (always — the structured tally_master_txn embed text),
      narration_text (type-aware narration embed text, or None if narration too short).
    """
    raw = v.get("voucher_type") or ""
    canonical = canonicalize_voucher_type(raw)
    direction = _VTYPE_DIRECTION.get(canonical, "none")
    entries = v.get("ledger_entries") or []
    party_ln = (v.get("ledger_name") or "").strip()
    bank_legs, party_legs, head_legs, derived_head = _extract_legs(entries, ledgers_by_name, party_ln)

    # Party: PARTYLEDGERNAME (ledger_name) preferred, else first Sundry party leg.
    party = party_ln or (party_legs[0][0] if party_legs else None)

    amount = v.get("amount")
    date = str(v.get("date") or "")
    narration = (v.get("narration") or "").strip()
    amt_s = _fmt_amt(amount)
    bank_names = [b[0] for b in bank_legs]

    p = party or "n/a"
    if canonical == "payment":
        txt = f"Payment to '{p}' {amt_s} on {date}."
    elif canonical == "receipt":
        txt = f"Receipt from '{p}' {amt_s} on {date}."
    elif canonical == "contra":
        between = " and ".join(bank_names[:2]) if bank_names else "accounts"
        txt = f"Contra transfer {amt_s} on {date} between {between}."
    elif canonical == "sales":
        txt = f"Sales to '{p}' {amt_s} on {date}."
    elif canonical == "purchase":
        txt = f"Purchase from '{p}' {amt_s} on {date}."
    elif canonical == "journal":
        dr = ", ".join(n for n, _a in head_legs[:3]) or "—"
        txt = f"Journal {amt_s} on {date}: {dr}."
    elif canonical == "debit_note":
        txt = f"Debit note to '{p}' {amt_s} on {date}."
    elif canonical == "credit_note":
        txt = f"Credit note from '{p}' {amt_s} on {date}."
    else:
        txt = f"Voucher {raw or 'entry'} {amt_s} on {date} for '{p}'."
    if derived_head and canonical not in ("contra",):
        txt += f" Head: {derived_head}."
    if narration:
        txt += f" Narration: {narration}"

    narration_text = None
    if len(narration) > 10:
        # Type-aware narration channel — leads with the action so the vector
        # captures payment-vs-receipt-vs-sales intent, not just the raw text.
        narration_text = txt

    return {
        "canonical": canonical, "raw_voucher_type": raw, "direction": direction,
        "party": party, "derived_head": derived_head,
        "amount": float(amount or 0), "voucher_id": str(v.get("id")),
        "voucher_number": v.get("voucher_number"), "date": date,
        "narration": narration, "bank_legs": bank_names,
        "txn_text": txt, "narration_text": narration_text,
    }


def embed_tally_master(company_id, company_name, embed_fn, batch_log=None):
    """Embed Tally master data (ledgers, parties, vouchers, stock items) into knowledge_base
    so RAG / semantic search can recall them later.

    embed_fn(text) -> list[float] -- caller supplies the embedder (e.g. server.get_embedding)
    so we don't take a hard dependency on the Gemini SDK in db.py.

    Idempotent: skips rows whose (type, kb_key) is already embedded for this company.
    Returns counts dict.
    """
    if not embed_fn:
        return {"skipped_no_embed_fn": True}

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    def _already(kb_type, kb_key):
        cur.execute("""
            SELECT 1 FROM knowledge_base
            WHERE type = %s AND data->>'company_name' = %s AND data->>'kb_key' = %s
            LIMIT 1
        """, (kb_type, company_name, kb_key))
        return cur.fetchone() is not None

    def _insert(kb_type, kb_key, text_for_embed, payload_dict):
        try:
            emb = embed_fn(text_for_embed)
        except Exception as e:
            print(f"[embed_tally_master] embed error for {kb_type} {kb_key}: {e}")
            return False
        if not emb:
            return False
        emb_str = "[" + ",".join(map(str, emb)) + "]"
        payload_dict = dict(payload_dict)
        payload_dict["company_name"] = company_name
        payload_dict["company_id"] = str(company_id) if company_id else None
        payload_dict["kb_key"] = kb_key
        payload_dict["content"] = text_for_embed   # exact embedded text (RAG transparency)
        cur.execute(
            "INSERT INTO knowledge_base (type, data, embedding) VALUES (%s, %s::jsonb, %s)",
            (kb_type, json.dumps(payload_dict), emb_str),
        )
        return True

    counts = {"ledgers": 0, "parties": 0, "narrations": 0, "txns": 0, "stock_items": 0, "skipped": 0}
    ledgers_by_name = _load_ledgers_by_name(cur, company_id, company_name)

    # 1. Ledgers
    cur.execute("""
        SELECT name, parent_group, COALESCE(gstin,'') AS gstin,
               COALESCE(pan,'') AS pan, COALESCE(address,'') AS address,
               COALESCE(ledger_type,'') AS ledger_type,
               closing_balance
        FROM tally_ledgers
        WHERE company_id = %s OR (company_id IS NULL AND company_name = %s)
    """, (company_id, company_name))
    for r in cur.fetchall():
        key = f"ledger::{r['name']}"
        if _already("tally_master_ledger", key):
            counts["skipped"] += 1
            continue
        text = (
            f"Ledger '{r['name']}' under group '{r['parent_group'] or 'Unknown'}'. "
            f"Type: {r['ledger_type'] or 'general'}. "
            f"GSTIN: {r['gstin'] or 'n/a'}. PAN: {r['pan'] or 'n/a'}. "
            f"Closing balance: {r['closing_balance']}. "
            f"Address: {r['address'] or 'n/a'}."
        )
        if _insert("tally_master_ledger", key, text, {
            "name": r["name"],
            "parent_group": r["parent_group"],
            "gstin": r["gstin"],
            "pan": r["pan"],
            "ledger_type": r["ledger_type"],
        }):
            counts["ledgers"] += 1
            if batch_log: batch_log(f"  embedded ledger: {r['name']}")

    # 2. Unique parties — per-type transaction counts (richer than a flat total).
    cur.execute("""
        SELECT ledger_name AS party, voucher_type, COUNT(*) AS n
        FROM tally_vouchers
        WHERE (company_id = %s OR (company_id IS NULL AND company_name = %s))
          AND ledger_name IS NOT NULL AND ledger_name != ''
        GROUP BY ledger_name, voucher_type
    """, (company_id, company_name))
    party_agg = {}   # party -> {canonical_vtype: count}
    for r in cur.fetchall():
        bt = party_agg.setdefault(r["party"], {})
        c = canonicalize_voucher_type(r["voucher_type"])
        bt[c] = bt.get(c, 0) + r["n"]
    for party, by_type in party_agg.items():
        key = f"party::{party}"
        if _already("tally_master_party", key):
            counts["skipped"] += 1
            continue
        total = sum(by_type.values())
        parts = ", ".join(
            f"{n} {t}{'s' if n != 1 else ''}"
            for t, n in sorted(by_type.items(), key=lambda x: -x[1])
        )
        text = f"Party '{party}' has {total} historical transactions: {parts}."
        if _insert("tally_master_party", key, text, {
            "party": party,
            "transaction_count": total,
            "by_type": by_type,
            "voucher_types": list(by_type.keys()),
        }):
            counts["parties"] += 1

    # 3. Per-voucher training — a structured per-type txn channel (always) plus a
    #    type-aware narration channel (meaningful narrations only). One pass.
    cur.execute("""
        SELECT id, date, voucher_type, voucher_number, ledger_name,
               amount, narration, ledger_entries::text AS ledger_entries
        FROM tally_vouchers
        WHERE (company_id = %s OR (company_id IS NULL AND company_name = %s))
    """, (company_id, company_name))
    for r in cur.fetchall():
        v = dict(r)
        try:
            v["ledger_entries"] = json.loads(r["ledger_entries"]) if r["ledger_entries"] else []
        except Exception:
            v["ledger_entries"] = []
        t = build_voucher_training(v, ledgers_by_name)
        # 3a. structured txn row (every voucher)
        tkey = f"txn::{t['canonical']}::{t['voucher_id']}"
        if _already("tally_master_txn", tkey):
            counts["skipped"] += 1
        elif _insert("tally_master_txn", tkey, t["txn_text"], {
            "canonical_vtype": t["canonical"], "raw_voucher_type": t["raw_voucher_type"],
            "direction": t["direction"], "party": t["party"], "derived_head": t["derived_head"],
            "amount": t["amount"], "voucher_id": t["voucher_id"],
            "voucher_number": t["voucher_number"], "date": t["date"], "narration": t["narration"],
        }):
            counts["txns"] += 1
        # 3b. type-aware narration row (only meaningful narrations, >10 chars)
        if t["narration_text"]:
            nkey = f"narration::v2::{t['voucher_id']}"
            if _already("tally_master_narration", nkey):
                counts["skipped"] += 1
            elif _insert("tally_master_narration", nkey, t["narration_text"], {
                "voucher_id": t["voucher_id"], "date": t["date"],
                "voucher_type": t["raw_voucher_type"], "canonical_vtype": t["canonical"],
                "voucher_number": t["voucher_number"], "party": t["party"],
                "amount": t["amount"],
            }):
                counts["narrations"] += 1

    # 4. Stock items
    cur.execute("""
        SELECT name, parent_group, unit, hsn_code, gst_rate, closing_qty, closing_value
        FROM tally_stock_items
        WHERE company_id = %s OR (company_id IS NULL AND company_name = %s)
    """, (company_id, company_name))
    for r in cur.fetchall():
        key = f"stockitem::{r['name']}"
        if _already("tally_master_item", key):
            counts["skipped"] += 1
            continue
        text = (
            f"Stock item '{r['name']}' under '{r['parent_group'] or 'Primary'}'. "
            f"HSN {r['hsn_code'] or 'n/a'}, GST {r['gst_rate'] or 'n/a'}%, "
            f"unit {r['unit'] or 'unit'}, "
            f"closing qty {r['closing_qty'] or 0}, value ₹{r['closing_value'] or 0}."
        )
        if _insert("tally_master_item", key, text, {
            "name": r["name"],
            "hsn_code": r["hsn_code"],
            "gst_rate": float(r["gst_rate"] or 0),
            "unit": r["unit"],
        }):
            counts["stock_items"] += 1

    conn.commit()
    cur.close()
    conn.close()
    return counts


def _modal_head_info(ctr):
    """Counter(head->count) -> {head, confidence, n, candidates:[{head,share}]}."""
    total = sum(ctr.values())
    if total == 0:
        return {"head": None, "confidence": 0.0, "n": 0, "candidates": []}
    ranked = ctr.most_common()
    head, cnt = ranked[0]
    candidates = [{"head": h, "share": round(c / total, 3)} for h, c in ranked[:3]]
    return {"head": head, "confidence": round(cnt / total, 3), "n": total, "candidates": candidates}


def get_party_default_head(company_name, party, direction="any", company_id=None):
    """Most-frequent counter-head ledger for a party, derived deterministically from
    their own voucher history's ledger_entries (no embeddings). `direction` filters to
    'inflow' (receipt/sales/credit_note) or 'outflow' (payment/purchase/debit_note);
    'any' counts all. Returns {head, confidence (frequency share), n, candidates}.
    No usable history -> {head: None, ...}. Company-scoped."""
    if not party:
        return {"head": None, "confidence": 0.0, "n": 0, "candidates": []}
    if company_id is None:
        company_id = resolve_company_id(company_name)
    from collections import Counter
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        ledgers_by_name = _load_ledgers_by_name(cur, company_id, company_name)
        cur.execute("""
            SELECT voucher_type, ledger_entries::text AS ledger_entries
            FROM tally_vouchers
            WHERE (company_id = %s OR (company_id IS NULL AND company_name = %s))
              AND ledger_name = %s
        """, (company_id, company_name, party))
        ctr = Counter()
        for r in cur.fetchall():
            d = _VTYPE_DIRECTION.get(canonicalize_voucher_type(r["voucher_type"]), "none")
            if direction not in ("any", None) and d != direction:
                continue
            try:
                entries = json.loads(r["ledger_entries"]) if r["ledger_entries"] else []
            except Exception:
                entries = []
            _b, _p, _h, head = _extract_legs(entries, ledgers_by_name, party)
            if head:
                ctr[head] += 1
        return _modal_head_info(ctr)
    finally:
        cur.close()
        conn.close()


def get_company_party_heads(company_name, company_id=None):
    """One-pass precompute of every party's modal counter-head, split by direction, so
    the reconciler can fill the head once a party is known without N queries. Returns
    {party: {'inflow': info, 'outflow': info, 'any': info}} (info as _modal_head_info)."""
    if company_id is None:
        company_id = resolve_company_id(company_name)
    from collections import Counter
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        ledgers_by_name = _load_ledgers_by_name(cur, company_id, company_name)
        cur.execute("""
            SELECT ledger_name, voucher_type, ledger_entries::text AS ledger_entries
            FROM tally_vouchers
            WHERE (company_id = %s OR (company_id IS NULL AND company_name = %s))
              AND ledger_name IS NOT NULL AND ledger_name != ''
        """, (company_id, company_name))
        tally = {}   # party -> direction -> Counter(head)
        for r in cur.fetchall():
            party = r["ledger_name"]
            d = _VTYPE_DIRECTION.get(canonicalize_voucher_type(r["voucher_type"]), "none")
            try:
                entries = json.loads(r["ledger_entries"]) if r["ledger_entries"] else []
            except Exception:
                entries = []
            _b, _p, _h, head = _extract_legs(entries, ledgers_by_name, party)
            if not head:
                continue
            byd = tally.setdefault(party, {})
            byd.setdefault(d, Counter())[head] += 1
            byd.setdefault("any", Counter())[head] += 1
        return {party: {d: _modal_head_info(ctr) for d, ctr in dirs.items()}
                for party, dirs in tally.items()}
    finally:
        cur.close()
        conn.close()


def _refresh_party_rows(company_id, company_name, embed_fn):
    """UPDATE existing tally_master_party rows in place with the per-type text.
    embed_tally_master skips parties that already exist (idempotency), so a backfill
    needs this to refresh their text/embedding. Only touches rows that already exist
    (UPDATE, never delete). Returns count updated."""
    if not embed_fn:
        return 0
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    updated = 0
    try:
        cur.execute("""
            SELECT ledger_name AS party, voucher_type, COUNT(*) AS n
            FROM tally_vouchers
            WHERE (company_id = %s OR (company_id IS NULL AND company_name = %s))
              AND ledger_name IS NOT NULL AND ledger_name != ''
            GROUP BY ledger_name, voucher_type
        """, (company_id, company_name))
        party_agg = {}
        for r in cur.fetchall():
            bt = party_agg.setdefault(r["party"], {})
            c = canonicalize_voucher_type(r["voucher_type"])
            bt[c] = bt.get(c, 0) + r["n"]
        for party, by_type in party_agg.items():
            key = f"party::{party}"
            cur.execute("""
                SELECT 1 FROM knowledge_base
                WHERE type='tally_master_party' AND data->>'company_name'=%s AND data->>'kb_key'=%s
                LIMIT 1
            """, (company_name, key))
            if not cur.fetchone():
                continue   # missing rows were already inserted by embed_tally_master
            total = sum(by_type.values())
            parts = ", ".join(
                f"{n} {t}{'s' if n != 1 else ''}"
                for t, n in sorted(by_type.items(), key=lambda x: -x[1])
            )
            text = f"Party '{party}' has {total} historical transactions: {parts}."
            try:
                emb = embed_fn(text)
            except Exception as e:
                print(f"[reembed party] {e}")
                continue
            if not emb:
                continue
            emb_str = "[" + ",".join(map(str, emb)) + "]"
            payload = {
                "party": party, "transaction_count": total, "by_type": by_type,
                "voucher_types": list(by_type.keys()), "company_name": company_name,
                "company_id": str(company_id) if company_id else None,
                "kb_key": key, "content": text,
            }
            cur.execute("""
                UPDATE knowledge_base SET data=%s::jsonb, embedding=%s
                WHERE type='tally_master_party' AND data->>'company_name'=%s AND data->>'kb_key'=%s
            """, (json.dumps(payload), emb_str, company_name, key))
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        cur.close()
        conn.close()


def purge_old_narration_rows(company_name, dry_run=True):
    """DESTRUCTIVE — delete legacy generic narration rows (kb_key NOT 'narration::v2::*')
    for ONE company. Default dry_run=True only counts. Callers must obtain explicit user
    approval before passing dry_run=False (never-delete policy)."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        where = ("type='tally_master_narration' AND data->>'company_name'=%s "
                 "AND COALESCE(data->>'kb_key','') NOT LIKE 'narration::v2::%%'")
        cur.execute(f"SELECT COUNT(*) FROM knowledge_base WHERE {where}", (company_name,))
        n = cur.fetchone()[0]
        if dry_run:
            return {"would_delete": n, "dry_run": True}
        cur.execute(f"DELETE FROM knowledge_base WHERE {where}", (company_name,))
        deleted = cur.rowcount
        conn.commit()
        return {"deleted": deleted, "dry_run": False}
    finally:
        cur.close()
        conn.close()


def resolve_company_id(company_name):
    """Best-effort company_id for a company_name, read from its own Tally data
    (handles the legacy mix where some rows carry company_id and some are NULL).
    Returns the id so the `company_id=%s OR (company_id IS NULL AND company_name=%s)`
    filters match ALL of a company's rows. None if no id is recorded anywhere."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        for tbl in ("tally_vouchers", "tally_ledgers"):
            cur.execute(f"SELECT company_id FROM {tbl} WHERE company_name=%s AND company_id IS NOT NULL LIMIT 1",
                        (company_name,))
            r = cur.fetchone()
            if r and r[0]:
                return r[0]
        return None
    finally:
        cur.close()
        conn.close()


def reembed_company_tally(company_id, company_name, embed_fn, purge_old_narration=False):
    """Backfill a company's Tally training with the voucher-type-aware scheme:
    (a) additive embed (new tally_master_txn + narration::v2 + any missing masters),
    (b) refresh existing tally_master_party rows in place (UPDATE),
    (c) optionally purge legacy generic narration rows (DESTRUCTIVE — only when the
        caller passes purge_old_narration=True after explicit user approval).
    Re-runnable (idempotent). Returns merged counts."""
    if company_id is None:
        company_id = resolve_company_id(company_name)
    counts = embed_tally_master(company_id, company_name, embed_fn)
    counts["parties_refreshed"] = _refresh_party_rows(company_id, company_name, embed_fn)
    if purge_old_narration:
        counts["narration_purged"] = purge_old_narration_rows(company_name, dry_run=False)
    return counts


def mark_sensitive_ledgers(company_id=None):
    """Auto-flag tally ledgers whose name OR parent_group matches sensitive patterns.
    Catches partner capital accounts that don't have 'capital' in the ledger name itself
    (e.g. 'Charan Kaur' under parent_group 'Capital Account').
    Returns count flagged.
    """
    pattern = r'salary|drawings|partner.*capital|owner.*equity|loan.*director|cash.*hand|capital account|proprietor|directors.*remuneration'
    conn = get_conn()
    cur = conn.cursor()
    where_clause = """
        (LOWER(COALESCE(name,'')) ~ %s OR LOWER(COALESCE(parent_group,'')) ~ %s)
        AND is_sensitive = FALSE
    """
    if company_id:
        cur.execute(
            "UPDATE tally_ledgers SET is_sensitive = TRUE WHERE company_id = %s AND " + where_clause,
            (company_id, pattern, pattern),
        )
    else:
        cur.execute(
            "UPDATE tally_ledgers SET is_sensitive = TRUE WHERE " + where_clause,
            (pattern, pattern),
        )
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


# Initialize on start
try:
    init_db()
    seeded = seed_builtin_recon_templates()
    try:
        _seed_agents()  # ensure first-party catalog + core auto-installs (incl. Network)
    except Exception as _se:
        print(f"[startup _seed_agents] {_se}")
    try:
        _ensure_network_schema()  # create Network tables once at boot (avoids per-request DDL locks)
    except Exception as _ne:
        print(f"[startup _ensure_network_schema] {_ne}")
    print(f"Cloud Database Initialized Successfully. ({seeded} recon templates loaded)")
except Exception as e:
    print(f"Cloud DB Error: {e}")
