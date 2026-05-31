#!/usr/bin/env python3
"""
YantrAI Tally Bridge Agent - Stable Native GUI
==============================================
A robust, native desktop bridge that securely connects your
local Tally ERP instance to the YantrAI Accounting Cloud.

Uses standard system default widgets to guarantee perfect rendering on macOS/Windows.
"""

import os
import sys
import json
import socket
import threading
import asyncio
import urllib.request
import re
import html
import time
from datetime import datetime


def _clean(s):
    """Decode HTML entities and strip stray control chars from Tally XML text."""
    if not s:
        return s
    try:
        s = html.unescape(str(s))
        # Strip non-printable control chars except tab/newline
        s = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F]', '', s)
        return s.strip()
    except Exception:
        return s

# ============================================================
# Single Instance Lock — with takeover behavior
# Rule: only one agent runs at a time. A new launch tells the old one
# to quit, waits briefly, then takes the lock itself.
# ============================================================
LOCK_PORT = 19999
LOCK_HOST = '127.0.0.1'

def _signal_existing_instance_to_quit():
    """If an agent is already running, send it a QUIT request over the lock port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((LOCK_HOST, LOCK_PORT))
        s.sendall(b"QUIT\n")
        try:
            s.recv(64)  # best-effort wait for ack
        except Exception:
            pass
        s.close()
        return True
    except Exception:
        return False


def _bind_lock_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # IMPORTANT: do NOT set SO_REUSEADDR — on Windows it lets two processes
    # bind the same port simultaneously, which would defeat single-instance lock.
    sock.bind((LOCK_HOST, LOCK_PORT))
    sock.listen(5)
    return sock


# Try to bind. If port is busy, ask the running instance to exit and retry.
lock_socket = None
try:
    lock_socket = _bind_lock_socket()
except socket.error:
    # Existing instance — tell it to quit, then retry up to ~3 seconds
    _signal_existing_instance_to_quit()
    import time as _time
    for _attempt in range(15):
        _time.sleep(0.2)
        try:
            lock_socket = _bind_lock_socket()
            break
        except socket.error:
            continue
    if lock_socket is None:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "YantrAI Tally Bridge",
                "Couldn't take over from the existing agent instance. "
                "Please close the old window manually, then try again."
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(1)


def _lock_listener_loop(sock):
    """Background thread: accept incoming QUIT signals from peer launches."""
    while True:
        try:
            conn, _ = sock.accept()
            try:
                data = conn.recv(64) or b""
                if b"QUIT" in data:
                    try:
                        conn.sendall(b"BYE\n")
                    except Exception:
                        pass
                    conn.close()
                    # Hard exit — don't bother with graceful Tk teardown,
                    # the new instance is already trying to bind.
                    os._exit(0)
                conn.close()
            except Exception:
                try: conn.close()
                except: pass
        except Exception:
            return


_lock_thread = threading.Thread(target=_lock_listener_loop, args=(lock_socket,), daemon=True)
_lock_thread.start()

# ============================================================
# Configuration
# ============================================================
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".yantrai_bridge_config.json")
DEFAULT_TALLY = "http://localhost:9000"

# Server preset list — agent UI dropdown.
# Cloud Run is FIRST so fresh installs default to the cloud, not localhost.
# v0.19.4 — fixed Cloud Run URL: the project-number host
# (yantrai-accounting-916641724782.asia-south1.run.app) returns Google's stock
# 404 because that hostname format wasn't enabled for this service. The
# canonical service URL from `gcloud run services describe` is the hash-format
# one below. Symptom: agent showed "Could not reach server: The read operation
# timed out" because POST to the wrong host hung instead of returning 404.
SERVER_PRESETS = [
    {"label": "Cloud Run",  "url": "https://yantrai-accounting-xjmqiwvd2a-el.a.run.app"},
    {"label": "Localhost",  "url": "http://localhost:8000"},
]

def http_to_ws(http_url: str) -> str:
    """Convert http://host/... to ws://host/tally/ws (and https → wss)."""
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):].rstrip("/") + "/tally/ws"
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://"):].rstrip("/") + "/tally/ws"
    return http_url

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except:
        pass


def _migrate_default_server(cfg):
    """One-time: machines that ran an OLDER agent build have a saved
    last_server_url=http://localhost:8000, so they'd keep landing on Localhost
    even after downloading the Cloud-Run-default build. Flip that saved default
    to Cloud Run ONCE. A dev who then re-picks Localhost keeps it (their save
    re-sets localhost; the flag prevents re-overriding)."""
    try:
        if cfg.get("server_migrated_cloud"):
            return
        if (cfg.get("last_server_url") or "").rstrip("/") == "http://localhost:8000":
            cfg["last_server_url"] = SERVER_PRESETS[0]["url"]  # Cloud Run
        cfg["server_migrated_cloud"] = True
        save_config(cfg)
    except Exception:
        pass

# ============================================================
# Tally Local Communication Layer
# ============================================================
# Sprint 32 — Tally Prime's HTTP-XML server is single-threaded and crashes
# (c0000005 Memory Access Violation) under rapid back-to-back POSTs. We
# serialise every call through a single lock AND enforce a minimum gap
# between consecutive requests so Tally's internal state has time to settle
# between imports.
import threading as _threading
_TALLY_HTTP_LOCK = _threading.Lock()
_TALLY_LAST_CALL_TS = [0.0]  # mutable singleton; written under the lock
_TALLY_MIN_GAP_S = 1.2       # empirically safe; raise if crashes persist

def query_local_tally(tally_url, xml_payload, timeout=10.0):
    with _TALLY_HTTP_LOCK:
        # Pace consecutive calls: never POST within _TALLY_MIN_GAP_S of the last one.
        elapsed = time.time() - _TALLY_LAST_CALL_TS[0]
        if elapsed < _TALLY_MIN_GAP_S:
            time.sleep(_TALLY_MIN_GAP_S - elapsed)
        try:
            req = urllib.request.Request(
                tally_url,
                data=xml_payload.encode('utf-8'),
                headers={'Content-Type': 'text/xml; charset=utf-8'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read().decode('utf-8')
        except Exception:
            return None
        finally:
            _TALLY_LAST_CALL_TS[0] = time.time()

def check_tally_alive(tally_url):
    """Sprint 32 — Lightweight alive check. A bare <ENVELOPE/> POST returns
    `<RESPONSE>Unknown Request, cannot be processed</RESPONSE>` in milliseconds
    from any running Tally. Avoids the heavyweight CompanyCol export that
    can fail if F11 export isn't perfectly configured."""
    resp = query_local_tally(tally_url, "<ENVELOPE/>", timeout=4.0)
    return resp is not None and ("RESPONSE" in resp.upper() or "ENVELOPE" in resp.upper())

def fetch_local_ledgers(tally_url):
    """Fetch ledger names only (lightweight, for quick queries)."""
    xml = """<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Collection</TYPE><ID>LedgerCol</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="LedgerCol"><TYPE>Ledger</TYPE><FETCH>Name, Parent, ClosingBalance</FETCH></COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    res = query_local_tally(tally_url, xml)
    if not res:
        return []   # never fabricate — empty/unreachable Tally yields no ledgers
    ledgers = re.findall(r'<LEDGER NAME="([^"]*)"', res)
    return ledgers

# Sprint 40 — TDL AlterId filter helper. When since>0, Tally returns only rows with
# AlterId greater than the watermark — same pattern fetch_vouchers already uses.
def _alter_id_filter(since_alter_id):
    n = int(since_alter_id or 0)
    if n <= 0:
        return "", ""
    return (
        "<FILTER>AlterIdFilter</FILTER>",
        f'<SYSTEM TYPE="Formula" NAME="AlterIdFilter">$AlterId &gt; {n}</SYSTEM>',
    )


def fetch_rich_ledgers(tally_url, since_alter_id=0):
    """Fetch full ledger details: name, parent group, closing balance, bank details,
    GSTIN, PAN, email, phone. Sprint 40 — also AlterId + Guid; supports incremental
    pulls (`since_alter_id > 0` adds `$AlterId > N` TDL filter)."""
    _use, _decl = _alter_id_filter(since_alter_id)
    xml = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Collection</TYPE><ID>LedgerCol</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="LedgerCol"><TYPE>Ledger</TYPE><FETCH>Name, Parent, ClosingBalance, OpeningBalance, BankingConfigBank, BankAccountNumber, IFSCCode, BankBranchName, GSTRegistrationType, PartyGSTIN, PANNo, Email, LedgerPhone, LedgerMobile, Address, CreditPeriod, AlterId, Guid</FETCH>{_use}</COLLECTION>{_decl}</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    res = query_local_tally(tally_url, xml, timeout=30.0)
    if not res:
        return []   # never fabricate — empty/unreachable Tally yields no ledgers
    # Parse each ledger block with all available fields
    results = []
    for _lm in re.finditer(r'<LEDGER NAME="([^"]*)"[^>]*>(.*?)</LEDGER>', res, re.DOTALL):
        name, block = _lm.group(1), _lm.group(2)
        def extract(tag, blk=block):
            m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', blk, re.DOTALL | re.IGNORECASE)
            return _clean(m.group(1)) if m else ""
        ledger = {
            "name": _clean(name),
            "parent": extract("PARENT"),
            "closing_balance": extract("CLOSINGBALANCE"),
            "opening_balance": extract("OPENINGBALANCE"),
            "bank_name": extract("BANKINGCONFIGBANK"),
            "bank_account_number": extract("BANKACCOUNTNUMBER"),
            "ifsc_code": extract("IFSCCODE"),
            "bank_branch": extract("BANKBRANCHNAME"),
            "gst_type": extract("GSTREGISTRATIONTYPE"),
            "gstin": extract("PARTYGSTIN"),
            "pan": extract("PANNO"),
            "email": extract("EMAIL"),
            "phone": extract("LEDGERPHONE"),
            "mobile": extract("LEDGERMOBILE"),
            "credit_period": extract("CREDITPERIOD"),
            # Sprint 40 — incremental watermark + GUID for deletion-reconcile.
            "alter_id": extract("ALTERID"),
            "guid": extract("GUID"),
        }
        ledger["raw_xml"] = _lm.group(0)   # verbatim Tally ledger XML (first-hand data)
        # Only include non-empty extras (raw_xml is always present)
        results.append({k: v for k, v in ledger.items() if v})
    return results

def fetch_groups(tally_url, since_alter_id=0):
    """Fetch all account groups with parent + nature (revenue / capital / asset etc.).
    Sprint 40 — supports incremental pulls + returns AlterId/Guid per row."""
    _use, _decl = _alter_id_filter(since_alter_id)
    xml = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Collection</TYPE><ID>GroupCol</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="GroupCol"><TYPE>Group</TYPE><FETCH>Name, Parent, IsRevenue, IsDeemedPositive, IsSubLedger, ReservedName, AlterId, Guid</FETCH>{_use}</COLLECTION>{_decl}</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    res = query_local_tally(tally_url, xml, timeout=15.0)
    if not res:
        return []   # never fabricate — empty/unreachable Tally yields no groups
    groups = []
    for _gm in re.finditer(r'<GROUP NAME="([^"]*)"[^>]*>(.*?)</GROUP>', res, re.DOTALL):
        name, block = _gm.group(1), _gm.group(2)
        def gext(tag, blk=block):
            m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', blk, re.DOTALL | re.IGNORECASE)
            return _clean(m.group(1)) if m else ""
        is_rev = gext("ISREVENUE").lower() in ("yes", "true", "1")
        is_dp = gext("ISDEEMEDPOSITIVE").lower() in ("yes", "true", "1")
        is_sub = gext("ISSUBLEDGER").lower() in ("yes", "true", "1")
        groups.append({
            "name": _clean(name),
            "parent": gext("PARENT"),
            "is_revenue": is_rev,
            "is_deemedpositive": is_dp,
            "is_subledger": is_sub,
            # Sprint 40 — incremental watermark + GUID.
            "alter_id": gext("ALTERID"),
            "guid": gext("GUID"),
            "raw_xml": _gm.group(0),   # verbatim Tally group XML (first-hand data)
        })
    return groups

def fetch_vouchers(tally_url, from_date="20000401", to_date=None, since_alter_id=0):
    """Fetch vouchers with full details: date, type, number, party, amount, narration, GUID,
    AlterId, ledger entries (with bank/bill allocations + cost-centre allocations).

    Tally's default Voucher collection returns the current fiscal year only. To pull
    ALL history we override SVFROMDATE/SVTODATE to a wide range.

    INCREMENTAL (Sprint — incremental download): when since_alter_id > 0 we add a TDL
    filter `$AlterId > N` so Tally returns ONLY vouchers created/edited since the last
    sync. AlterId is always fetched so the server can advance its watermark.
    """
    if to_date is None:
        to_date = datetime.now().strftime("%Y%m%d")
    _filter_use = _filter_decl = ""
    if since_alter_id and int(since_alter_id) > 0:
        _filter_use = "<FILTER>AlterIdFilter</FILTER>"
        _filter_decl = (f'<SYSTEM TYPE="Formula" NAME="AlterIdFilter">$AlterId &gt; '
                        f'{int(since_alter_id)}</SYSTEM>')
    xml = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Collection</TYPE><ID>VchCol</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT><SVFROMDATE TYPE="Date">{from_date}</SVFROMDATE><SVTODATE TYPE="Date">{to_date}</SVTODATE></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="VchCol"><TYPE>Voucher</TYPE><FETCH>AlterId, Date, VoucherTypeName, VoucherNumber, PartyLedgerName, Amount, Narration, GUID, ReferenceNumber, ReferenceDate, PlaceOfSupply, AllLedgerEntries, AllLedgerEntries.BankAllocations, AllLedgerEntries.BillAllocations, AllLedgerEntries.CategoryAllocations, AllLedgerEntries.CategoryAllocations.CostCentreAllocations</FETCH>{_filter_use}</COLLECTION>{_filter_decl}</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    res = query_local_tally(tally_url, xml, timeout=180.0)
    if not res:
        return []   # never fabricate — empty/unreachable Tally yields no vouchers
    # Parse voucher XML with regex for robustness (Tally XML is not always well-formed)
    vouchers = []
    for _vm in re.finditer(r'<VOUCHER[^>]*>(.*?)</VOUCHER>', res, re.DOTALL):
        vblock = _vm.group(1)
        def vext(tag, blk=vblock):
            m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', blk, re.DOTALL | re.IGNORECASE)
            return _clean(m.group(1)) if m else ""

        date_raw = vext("DATE")
        vtype = vext("VOUCHERTYPENAME")
        party = vext("PARTYLEDGERNAME")
        vnum = vext("VOUCHERNUMBER")
        narration = vext("NARRATION")
        guid = vext("GUID")
        ref_num = vext("REFERENCENUMBER")
        ref_date = vext("REFERENCEDATE")
        place_of_supply = vext("PLACEOFSUPPLY")

        # Parse amount
        amount_str = vext("AMOUNT")
        try:
            amount = abs(float(amount_str.replace(",", "")))
        except:
            amount = 0.0

        # Parse ledger entries
        ledger_entries = []
        le_blocks = re.findall(r'<ALLLEDGERENTRIES\.LIST>(.*?)</ALLLEDGERENTRIES\.LIST>', vblock, re.DOTALL)
        for le in le_blocks:
            def le_ext(tag, blk=le):
                m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', blk, re.DOTALL | re.IGNORECASE)
                return _clean(m.group(1)) if m else ""
            le_name = le_ext("LEDGERNAME")
            le_amt_str = le_ext("AMOUNT")
            try:
                le_amt = float(le_amt_str.replace(",", ""))
            except:
                le_amt = 0.0
            le_entry = {"ledger_name": le_name, "amount": le_amt}

            # Parse bank allocations
            ba_blocks = re.findall(r'<BANKALLOCATIONS\.LIST>(.*?)</BANKALLOCATIONS\.LIST>', le, re.DOTALL)
            if ba_blocks:
                bank_allocs = []
                for ba in ba_blocks:
                    def ba_ext(tag, blk=ba):
                        m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', blk, re.DOTALL | re.IGNORECASE)
                        return _clean(m.group(1)) if m else ""
                    ba_amt_str = ba_ext("AMOUNT")
                    try:
                        ba_amt = abs(float(ba_amt_str.replace(",", "")))
                    except:
                        ba_amt = 0.0
                    bank_allocs.append({
                        "instrument_number": ba_ext("INSTRUMENTNUMBER"),
                        "instrument_date": ba_ext("INSTRUMENTDATE"),
                        "bank_date": ba_ext("BANKERSDATE") or ba_ext("BANKDATE"),
                        "transaction_type": ba_ext("TRANSACTIONTYPE"),
                        "payment_favouring": ba_ext("PAYMENTFAVOURING"),
                        "amount": ba_amt
                    })
                le_entry["bank_allocations"] = bank_allocs

            # Parse bill allocations
            bill_blocks = re.findall(r'<BILLALLOCATIONS\.LIST>(.*?)</BILLALLOCATIONS\.LIST>', le, re.DOTALL)
            if bill_blocks:
                bill_allocs = []
                for bill in bill_blocks:
                    def bi_ext(tag, blk=bill):
                        m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', blk, re.DOTALL | re.IGNORECASE)
                        return _clean(m.group(1)) if m else ""
                    bi_amt_str = bi_ext("AMOUNT")
                    try:
                        bi_amt = float(bi_amt_str.replace(",", ""))
                    except:
                        bi_amt = 0.0
                    bill_allocs.append({
                        "bill_type": bi_ext("BILLTYPE"),
                        "name": bi_ext("NAME"),
                        "amount": bi_amt
                    })
                le_entry["bill_allocations"] = bill_allocs

            # Parse cost-centre allocations (departments / projects)
            cc_blocks = re.findall(r'<COSTCENTREALLOCATIONS\.LIST>(.*?)</COSTCENTREALLOCATIONS\.LIST>', le, re.DOTALL)
            if cc_blocks:
                cost_centres = []
                for cc in cc_blocks:
                    def cc_ext(tag, blk=cc):
                        m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', blk, re.DOTALL | re.IGNORECASE)
                        return _clean(m.group(1)) if m else ""
                    cc_amt_str = cc_ext("AMOUNT")
                    try:
                        cc_amt = float(cc_amt_str.replace(",", ""))
                    except Exception:
                        cc_amt = 0.0
                    cc_name = cc_ext("NAME") or cc_ext("COSTCENTRENAME")
                    if cc_name:
                        cost_centres.append({"name": cc_name, "amount": cc_amt})
                if cost_centres:
                    le_entry["cost_centres"] = cost_centres

            ledger_entries.append(le_entry)

        # date_raw and others are already cleaned by vext(); reassign defensively
        # AlterId — Tally's monotonically-increasing change counter (for incremental sync)
        try:
            alterid = int(vext("ALTERID") or 0)
        except Exception:
            alterid = 0
        voucher = {
            "date": date_raw, "type": vtype, "party": party,
            "number": vnum, "amount": amount, "narration": narration,
            "alterid": alterid,
        }
        if guid:
            voucher["guid"] = guid
        if ref_num:
            voucher["reference_number"] = ref_num
        if ref_date:
            voucher["reference_date"] = ref_date
        if place_of_supply:
            voucher["place_of_supply"] = place_of_supply
        if ledger_entries:
            voucher["ledger_entries"] = ledger_entries
        voucher["raw_xml"] = _vm.group(0)   # verbatim Tally voucher XML (first-hand data)
        vouchers.append(voucher)
    return vouchers

def fetch_stock_items(tally_url, since_alter_id=0):
    """Fetch full stock-item master with HSN, GST rate, units, closing qty/value.
    Returns a list of dicts (one per item). Empty list if Tally has no inventory.
    Sprint 40 — incremental + AlterId/Guid per row."""
    _use, _decl = _alter_id_filter(since_alter_id)
    xml = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Collection</TYPE><ID>StockItemCol</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="StockItemCol"><TYPE>StockItem</TYPE><FETCH>Name, Parent, BaseUnits, GSTApplicable, GSTTypeofSupply, HSNCode, GSTRate, OpeningBalance, OpeningValue, ClosingBalance, ClosingValue, StandardCost, StandardPrice, Description, AlterId, Guid</FETCH>{_use}</COLLECTION>{_decl}</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    res = query_local_tally(tally_url, xml, timeout=45.0)
    if not res:
        return []
    items = []
    for _sm in re.finditer(r'<STOCKITEM NAME="([^"]*)"[^>]*>(.*?)</STOCKITEM>', res, re.DOTALL):
        name, block = _sm.group(1), _sm.group(2)
        def sext(tag, blk=block):
            m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', blk, re.DOTALL | re.IGNORECASE)
            return _clean(m.group(1)) if m else ""
        item = {
            "name": _clean(name),
            "parent": sext("PARENT"),
            "unit": sext("BASEUNITS"),
            "gst_applicable": sext("GSTAPPLICABLE"),
            "gst_supply_type": sext("GSTTYPEOFSUPPLY"),
            "hsn_code": sext("HSNCODE") or sext("HSN"),
            "gst_rate": sext("GSTRATE"),
            "opening_qty": sext("OPENINGBALANCE"),
            "opening_value": sext("OPENINGVALUE"),
            "closing_qty": sext("CLOSINGBALANCE"),
            "closing_value": sext("CLOSINGVALUE"),
            "standard_cost": sext("STANDARDCOST"),
            "standard_rate": sext("STANDARDPRICE"),
            "description": sext("DESCRIPTION"),
            # Sprint 40 — incremental watermark + GUID.
            "alter_id": sext("ALTERID"),
            "guid": sext("GUID"),
        }
        # Drop empty extras but always keep name
        item = {k: v for k, v in item.items() if v or k == "name"}
        item["raw_xml"] = _sm.group(0)   # verbatim Tally stock-item XML (first-hand data)
        items.append(item)
    return items


def fetch_tally_company_info(tally_url):
    """Probe Tally and return the actual open company name.

    Returns a dict with one of three states in 'state':
      - 'unreachable'  : Tally isn't running / wrong URL (company_name=None)
      - 'no_company'   : Tally is running but no company is open (company_name=None)
      - 'ok'           : Company is open (company_name=<real name>)
    Plus 'pan' when available.
    """
    xml = """<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Collection</TYPE><ID>CompanyCol</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="CompanyCol"><TYPE>Company</TYPE><FETCH>Name, IncomeTaxNumber</FETCH></COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    res = query_local_tally(tally_url, xml, timeout=5.0)
    if not res:
        return {"state": "unreachable", "company_name": None, "pan": None}

    # Tally responds with a CMPINFO summary when no company is loaded.
    # That response has zero <COMPANY NAME="..."> entries.
    company_name = None
    pan = None

    m = re.search(r'<COMPANY NAME="([^"]+)"', res)
    if m:
        company_name = m.group(1).strip()
    else:
        # Some Tally builds wrap the name in <NAME> tags
        m2 = re.search(r'<NAME[^>]*>([^<]+)</NAME>', res)
        if m2 and m2.group(1).strip() and m2.group(1).strip().lower() != 'companycol':
            company_name = m2.group(1).strip()

    mpan = re.search(r'<INCOMETAXNUMBER[^>]*>([^<]*)</INCOMETAXNUMBER>', res, re.IGNORECASE)
    if mpan and mpan.group(1).strip():
        pan = mpan.group(1).strip()

    if not company_name:
        return {"state": "no_company", "company_name": None, "pan": None}
    return {"state": "ok", "company_name": company_name, "pan": pan}

def build_voucher_xml(voucher):
    v_type = voucher.get("type", "Receipt")
    date = voucher.get("date", "20260517")
    number = voucher.get("number", "101")
    party = voucher.get("party", "Generic Party")
    amount = voucher.get("amount", 0.0)
    cash_bank = voucher.get("cash_bank_ledger", "Bank Account")
    dr_ledger = cash_bank if v_type == "Receipt" else party
    cr_ledger = party if v_type == "Receipt" else cash_bank
    return f"""<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>All Vouchers</REPORTNAME></REQUESTDESC><REQUESTDATA><TALLYMESSAGE xmlns:UDF="TallyUDF"><VOUCHER VCHTYPE="{v_type}" ACTION="Create"><DATE>{date}</DATE><VOUCHERNUMBER>{number}</VOUCHERNUMBER><PARTYLEDGERNAME>{party}</PARTYLEDGERNAME><ALLLEDGERENTRIES.LIST><LEDGERNAME>{dr_ledger}</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-{amount}</AMOUNT></ALLLEDGERENTRIES.LIST><ALLLEDGERENTRIES.LIST><LEDGERNAME>{cr_ledger}</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>{amount}</AMOUNT></ALLLEDGERENTRIES.LIST></VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"""


# Sprint 42 — Tally Prime company creation envelope.
# Verified against Tally Prime 4.x running locally. Field-name caveats:
#   • STARTINGFROM + BOOKSFROM in YYYYMMDD (no separators).
#   • STATENAME must match Tally's master list exactly (case-sensitive titles —
#     "Haryana", "Delhi", "Tamil Nadu"). UI sends from a fixed dropdown to avoid
#     drift.
#   • COUNTRYNAME hardcoded to "India" — every YantrAI customer is Indian.
#   • BASECURRENCYSYMBOL is the rupee glyph; FORMALNAME "INR".
#   • <CREATED>1</CREATED> in Tally's response = success. Otherwise look for
#     <LINEERROR> / <EXCEPTIONS> for the human-readable failure reason.
def _xe(s):
    """Minimal XML escape for element text + attribute values."""
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def build_create_company_xml(data):
    """Build the Tally Prime Import Data envelope to CREATE a new company.

    Required keys in `data`:
        name        — Tally company name (e.g. "Acme Traders Pvt Ltd")
        state       — full Indian state name (e.g. "Haryana")
        books_from  — first day of accounting period as YYYY-MM-DD
    Optional keys:
        pan         — 10-char PAN  (Tally <INCOMETAXNUMBER>)
        gstin       — 15-char GSTIN (Tally <SALESTAXNUMBER>)
        address     — single-line street address
    """
    name = _xe(data.get("name") or "")
    state = _xe(data.get("state") or "")
    bf_raw = (data.get("books_from") or "").strip()
    # YYYY-MM-DD → YYYYMMDD (Tally's date wire format).
    bf = bf_raw.replace("-", "") if len(bf_raw) == 10 and bf_raw[4] == "-" else bf_raw

    pan = _xe(data.get("pan") or "")
    gstin = _xe(data.get("gstin") or "")
    address = _xe(data.get("address") or "")

    optional = ""
    if pan:
        optional += f"<INCOMETAXNUMBER>{pan}</INCOMETAXNUMBER>"
    if gstin:
        optional += f"<SALESTAXNUMBER>{gstin}</SALESTAXNUMBER>"
        optional += f"<GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>"
    if address:
        optional += f"<ADDRESS.LIST TYPE=\"String\"><ADDRESS>{address}</ADDRESS></ADDRESS.LIST>"

    # SPRINT 42 NOTE — Tally Prime company-creation XML is undocumented and
    # extremely sensitive. Tested envelopes so far:
    #   v1: <IMPORTDATA><REQUESTDESC><REPORTNAME>All Masters</REPORTNAME>...
    #       → Tally returns "DESC not found" (harmless reject, no crash).
    #   v2: <DESC><STATICVARIABLES>...</DESC><DATA>...
    #       → Tally CRASHED with c0000005 (Memory Access Violation).
    # Until we have a Tally instance we can crash safely, this builder returns
    # the harmless-reject v1 envelope so the rest of the pipeline (auth → WS →
    # dispatch → response parsing → UI) can be tested without ever risking the
    # user's live books. The create_company branch will return an error result;
    # the UI will show "DESC not found" in the raw-response details. That's the
    # current expected behaviour.
    return (
        f"<ENVELOPE>"
        f"<HEADER>"
        f"<VERSION>1</VERSION>"
        f"<TALLYREQUEST>Import Data</TALLYREQUEST>"
        f"<TYPE>Data</TYPE>"
        f"<ID>All Masters</ID>"
        f"</HEADER>"
        f"<BODY><IMPORTDATA>"
        f"<REQUESTDESC><REPORTNAME>All Masters</REPORTNAME></REQUESTDESC>"
        f"<REQUESTDATA><TALLYMESSAGE xmlns:UDF=\"TallyUDF\">"
        f"<COMPANY NAME=\"{name}\" ACTION=\"Create\">"
        f"<NAME>{name}</NAME>"
        f"<BASICCOMPANYFORMALNAME>{name}</BASICCOMPANYFORMALNAME>"
        f"<STATENAME>{state}</STATENAME>"
        f"<COUNTRYNAME>India</COUNTRYNAME>"
        f"<STARTINGFROM>{bf}</STARTINGFROM>"
        f"<BOOKSFROM>{bf}</BOOKSFROM>"
        f"<BASECURRENCYSYMBOL>₹</BASECURRENCYSYMBOL>"
        f"<FORMALNAME>INR</FORMALNAME>"
        f"<DECIMALSYMBOL>.</DECIMALSYMBOL>"
        f"<DECIMALPLACES>2</DECIMALPLACES>"
        f"<DECIMALPLACESFORPRINTING>2</DECIMALPLACESFORPRINTING>"
        f"{optional}"
        f"</COMPANY>"
        f"</TALLYMESSAGE></REQUESTDATA>"
        f"</IMPORTDATA></BODY>"
        f"</ENVELOPE>"
    )


def parse_create_company_response(raw):
    """Pull the success/failure verdict out of Tally's import response.

    Tally's response shape:
      <RESPONSE>
        <CREATED>1</CREATED>      # or 0 on failure
        <ALTERED>0</ALTERED>
        <LASTVCHID>...</LASTVCHID>
        <LINEERROR>...</LINEERROR> # only on failure
      </RESPONSE>
    Returns (created: bool, error_message: str|None).
    """
    if not raw:
        return False, "Empty response from Tally (is it running?)"
    try:
        m_created = re.search(r'<CREATED>\s*(\d+)\s*</CREATED>', raw, re.IGNORECASE)
        created_n = int(m_created.group(1)) if m_created else 0
        m_err = re.search(r'<LINEERROR>(.*?)</LINEERROR>', raw, re.IGNORECASE | re.DOTALL)
        if created_n >= 1 and not m_err:
            return True, None
        if m_err:
            return False, _clean(m_err.group(1)) or "Tally reported LINEERROR with no detail"
        # Generic error fallback — surface a slice of the raw response.
        m_exc = re.search(r'<EXCEPTIONS>(.*?)</EXCEPTIONS>', raw, re.IGNORECASE | re.DOTALL)
        if m_exc and m_exc.group(1).strip():
            return False, _clean(m_exc.group(1)) or "Tally exception (no detail)"
        return False, f"Tally returned CREATED={created_n}; raw response truncated"
    except Exception as e:
        return False, f"Could not parse Tally response: {e}"

# ============================================================
# GUI Application
# ============================================================
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk

# Optional deps for branding + tray (degrade gracefully if missing).
try:
    from PIL import Image as _PILImage, ImageTk as _PILImageTk
except Exception:
    _PILImage = None
    _PILImageTk = None
try:
    import pystray as _pystray
except Exception:
    _pystray = None
try:
    import winreg as _winreg
except Exception:
    _winreg = None


# ── YantrAI brand palette (matches the web app) ──────────────────
THEME = {
    "bg": "#2a2623", "surface": "#1e1b18", "card": "#221f1c",
    "primary": "#da7756", "primary_light": "#e8a87c", "accent": "#38bdf8",
    "text": "#f5f1ec", "muted": "#a8a199", "border": "#3a3530",
    "ok": "#4ade80", "warn": "#f59e0b", "err": "#ef4444",
    "console_bg": "#16130f", "console_fg": "#cbd5e1",
}
APP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_RUN_NAME = "YantrAITallyBridge"


def resource_path(rel):
    """Resolve a bundled asset path (works in dev + PyInstaller onefile)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(base, rel)
    if os.path.exists(p):
        return p
    # dev fallback: assets/ next to this file
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


def _icon_pil():
    """Load the branded icon as a PIL image (for the tray), or None."""
    if _PILImage is None:
        return None
    try:
        return _PILImage.open(resource_path(os.path.join("assets", "yantrai_256.png")))
    except Exception:
        return None


def apply_theme(root):
    """Dark, Claude-style ttk theming on top of the 'clam' base."""
    root.configure(bg=THEME["bg"])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    t = THEME
    style.configure(".", background=t["bg"], foreground=t["text"], fieldbackground=t["card"],
                    bordercolor=t["border"], font=("Segoe UI", 10))
    style.configure("TFrame", background=t["bg"])
    style.configure("Card.TFrame", background=t["card"])
    style.configure("Surface.TFrame", background=t["surface"])
    style.configure("TLabel", background=t["bg"], foreground=t["text"])
    style.configure("Card.TLabel", background=t["card"], foreground=t["text"])
    style.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"])
    style.configure("CardMuted.TLabel", background=t["card"], foreground=t["muted"])
    style.configure("Title.TLabel", background=t["surface"], foreground=t["text"], font=("Segoe UI", 16, "bold"))
    style.configure("H2.TLabel", background=t["bg"], foreground=t["text"], font=("Segoe UI", 13, "bold"))
    style.configure("TEntry", fieldbackground=t["card"], foreground=t["text"], insertcolor=t["text"],
                    bordercolor=t["border"], lightcolor=t["border"], darkcolor=t["border"])
    style.map("TEntry", bordercolor=[("focus", t["primary"])])
    style.configure("TButton", background=t["card"], foreground=t["text"], bordercolor=t["border"],
                    focuscolor=t["card"], padding=(12, 7), font=("Segoe UI", 10))
    style.map("TButton", background=[("active", t["border"])])
    style.configure("Accent.TButton", background=t["primary"], foreground="#ffffff",
                    bordercolor=t["primary"], padding=(14, 9), font=("Segoe UI", 11, "bold"))
    style.map("Accent.TButton", background=[("active", t["primary_light"])])
    style.configure("TCombobox", fieldbackground=t["card"], background=t["card"], foreground=t["text"],
                    arrowcolor=t["muted"], bordercolor=t["border"], padding=(8, 6))
    style.map("TCombobox",
              fieldbackground=[("readonly", t["card"]), ("focus", t["card"])],
              foreground=[("readonly", t["text"])],
              selectbackground=[("readonly", t["card"])],
              selectforeground=[("readonly", t["text"])],
              bordercolor=[("focus", t["primary"])])
    style.configure("TCheckbutton", background=t["bg"], foreground=t["muted"])
    style.map("TCheckbutton", background=[("active", t["bg"])])
    # Theme the Combobox drop-down list (a tk Listbox) — fixes the white-on-hover.
    root.option_add("*TCombobox*Listbox.background", t["card"])
    root.option_add("*TCombobox*Listbox.foreground", t["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["primary"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    return style


# ── Windows auto-start (HKCU Run) ────────────────────────────────
def _agent_launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --autostart'


def set_autostart(enable):
    """Add/remove the HKCU Run entry. No-op on non-Windows."""
    if _winreg is None:
        return False
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, APP_RUN_KEY, 0,
                              _winreg.KEY_SET_VALUE | _winreg.KEY_QUERY_VALUE)
    except FileNotFoundError:
        key = _winreg.CreateKey(_winreg.HKEY_CURRENT_USER, APP_RUN_KEY)
    try:
        if enable:
            _winreg.SetValueEx(key, APP_RUN_NAME, 0, _winreg.REG_SZ, _agent_launch_command())
        else:
            try:
                _winreg.DeleteValue(key, APP_RUN_NAME)
            except FileNotFoundError:
                pass
        return True
    finally:
        _winreg.CloseKey(key)


def is_autostart_enabled():
    if _winreg is None:
        return False
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, APP_RUN_KEY, 0, _winreg.KEY_QUERY_VALUE)
        try:
            _winreg.QueryValueEx(key, APP_RUN_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            _winreg.CloseKey(key)
    except Exception:
        return False


# ============================================================
# SPRINT 31 — Push direction: YantrAI books → Tally Prime
# Implements the outbox contract built by Sprint 28:
#   GET  /api/tally/queue?company_name=…   ← claim pending rows
#   POST /api/tally/queue/{id}/ack         ← confirm successful push
#   POST /api/tally/queue/{id}/fail        ← report a failed push
#   POST /api/tally/heartbeat              ← keep the sidebar dot green
# ============================================================
AGENT_VERSION = "0.20.1"  # v0.20.1 — never fabricate: removed all mock/simulator fallbacks in fetch_vouchers/fetch_rich_ledgers/fetch_local_ledgers/fetch_groups that injected fake data ("coffee beans", VCH-GUID, Gupta & Sons) when Tally returned empty — that leaked fabricated vouchers into a real workspace. All now return []. v0.20.0 — CRITICAL accounting fix: _build_voucher_xml AMOUNT sign was inverted (Dr=+, Cr=-), so every pushed Sales landed in Tally's CREDIT column (party credited not debited). Now Tally-correct: Dr=negative, Cr=positive (confirmed vs native vouchers). v0.19.4 — Cloud Run preset URL fix (project-number host returned Google 404; switched to hash-format canonical host). v0.19.3 — Sprint 44.3 query-then-act Alter/Delete via TDL collection on $Narration CONTAINS "[YAI:<uid>]" → keyed envelope on Tally-native MASTERID + DATE + VCHTYPE + VCHNUMBER; lookup-miss falls through to Create.


def _post_json(url, body, timeout=15.0):
    """Tiny JSON POST helper using stdlib urllib (matches the no-extra-deps style)."""
    try:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try: return json.loads(raw) if raw else {}
            except: return {"_raw": raw}
    except Exception as e:
        return {"_error": str(e)}


def _get_json(url, timeout=15.0):
    try:
        req = urllib.request.Request(url, method="GET",
                                      headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try: return json.loads(raw) if raw else {}
            except: return {"_raw": raw}
    except Exception as e:
        return {"_error": str(e)}


def _xml_escape(s):
    """Minimal XML escape for content nodes (Tally is strict about &, <, >)."""
    if s is None: return ""
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;"))


def _to_tally_date(s):
    """Convert YYYY-MM-DD (or YYYY/MM/DD) → YYYYMMDD (Tally format)."""
    if not s: return datetime.now().strftime("%Y%m%d")
    s = str(s)[:10].replace("-", "").replace("/", "")
    return s if len(s) == 8 and s.isdigit() else datetime.now().strftime("%Y%m%d")


def _build_voucher_xml(payload, company_name):
    """Build a Tally Import-Data envelope for ONE voucher.

    Supports Sales / Purchase / Payment / Receipt vouchers. Falls back to a
    generic 2-leg journal entry if voucher_type is unrecognised.

    Tally's XML import schema accepts a `VOUCHER ACTION="Create"` block inside
    an `IMPORTDATA` envelope. AMOUNT sign convention (confirmed against native
    Tally vouchers): a DEBIT leg = ISDEEMEDPOSITIVE=Yes + NEGATIVE amount; a
    CREDIT leg = ISDEEMEDPOSITIVE=No + POSITIVE amount. So a Sales debits the
    party (negative) and credits Sales+tax (positive); a Purchase credits the
    party (positive) and debits Purchase+tax (negative)."""
    vt_raw = (payload.get("voucher_type") or payload.get("category") or "Sales").strip()
    vt = vt_raw.capitalize()
    # Map to Tally's canonical voucher type names
    type_map = {"Sale": "Sales", "Sales": "Sales", "Purchase": "Purchase",
                "Payment": "Payment", "Receipt": "Receipt",
                "Journal": "Journal", "Contra": "Contra"}
    vt_tally = type_map.get(vt, vt)
    is_outflow = vt_tally in ("Purchase", "Payment")
    # Sprint 33 — caller-supplied ledgers for Payment/Receipt
    payment_mode = (payload.get("payment_mode") or "").strip()   # Cash / Bank ledger
    counter_ledger_in = (payload.get("counter_ledger") or "").strip()
    # Counter ledger: Sales by default for Sales voucher; Purchase Account for Purchase
    counter_default = {
        "Sales":    "Sales Account",
        "Purchase": "Purchase Account",
        "Payment":  payment_mode or "Cash",
        "Receipt":  payment_mode or "Cash",
        "Journal":  "Suspense A/c",
        "Contra":   "Cash",
    }.get(vt_tally, "Sales Account")

    date_str = _to_tally_date(payload.get("date"))
    # Sprint 44 FIX — pick `party_name` (the real counter-party: customer for
    # Sales/Receipt, vendor for Purchase/Payment) FIRST. `billing_party_name`
    # carries the seller's own legal name (i.e. our own workspace on a Sales
    # invoice, or the vendor's bill header on a Purchase) and is only a last-
    # resort fallback. Pre-Sprint-44 code preferred `billing_party_name` and
    # ended up booking sales vouchers against our own ledger.
    party = (payload.get("party_name")
             or payload.get("party")
             or payload.get("billing_party_name")
             or "Cash")
    voucher_num = payload.get("invoice_number") or payload.get("voucher_number") or ""
    narration = payload.get("narration") or f"Synced from YantrAI on {datetime.now().strftime('%Y-%m-%d')}"
    # Sticky-origin marker: stamp YantrAI's immutable voucher id into the narration so it
    # round-trips through Tally. On the next sync the server reads [YAI:<uid>] to keep origin
    # = YantrAI and collapse the sync-back onto the original (zero duplicates). Server strips
    # the tag before display, so users never see it.
    _yuid = (payload.get("yantrai_uid") or "").strip()
    if _yuid and "[YAI:" not in narration:
        narration = f"{narration} [YAI:{_yuid}]"
    total = float(payload.get("total_amount") or payload.get("amount") or 0)

    # Tax breakdown if present
    cgst = float(payload.get("cgst_amount") or 0)
    sgst = float(payload.get("sgst_amount") or 0)
    igst = float(payload.get("igst_amount") or 0)
    taxable = float(payload.get("taxable_value") or 0)
    if taxable == 0:
        taxable = max(total - (cgst + sgst + igst), 0)

    legs = []

    # Sprint 32 — Journal/Contra: caller controls the legs via payload['ledger_entries'].
    # Each entry: {ledger_name, amount, is_debit}. We honour those verbatim, skipping
    # the default party+counter+tax leg shape (which doesn't apply to Journal/Contra).
    raw_entries = payload.get("ledger_entries")
    if vt_tally in ("Journal", "Contra") and isinstance(raw_entries, list) and raw_entries:
        for e in raw_entries:
            nm = e.get("ledger_name") or e.get("ledger") or ""
            if not nm: continue
            amt = float(e.get("amount") or 0)
            is_debit = bool(e.get("is_debit"))
            # In Tally: Dr → ISDEEMEDPOSITIVE=Yes + NEGATIVE amount; Cr → No + POSITIVE.
            legs.append((nm, -abs(amt) if is_debit else abs(amt), "Yes" if is_debit else "No"))
    else:
        # Tally voucher leg AMOUNT convention — CONFIRMED against this firm's NATIVE
        # vouchers (e.g. JMK/2026-27/042: party "Hindustan Unilever" Dr =
        # ISDEEMEDPOSITIVE=Yes, AMOUNT=-226605.75; "Sale" Cr = No, AMOUNT=+215815)
        # AND the build_voucher_xml sibling above:
        #   DEBIT  (Dr) leg → ISDEEMEDPOSITIVE=Yes, AMOUNT **NEGATIVE**
        #   CREDIT (Cr) leg → ISDEEMEDPOSITIVE=No,  AMOUNT **POSITIVE**
        # Sprint 53.1 FIX: the prior code emitted the OPPOSITE amount sign (Dr=+,
        # Cr=-), so every pushed Sales landed in Tally's CREDIT column (party
        # credited instead of debited). ISDEEMEDPOSITIVE was already correct; only
        # the AMOUNT sign was inverted. Signs below are now Tally-correct.
        #
        # Sales voucher  (cash IN):  Dr Party (debtor) + Cr Sales + Cr CGST/SGST/IGST Output
        # Purchase       (cash OUT): Cr Party (creditor) + Dr Purchase + Dr CGST/SGST/IGST Input
        # Payment        (cash OUT): Dr Party/Expense + Cr Cash/Bank
        # Receipt        (cash IN):  Dr Cash/Bank + Cr Party/Income
        # Sprint 32 — Force Dr=Cr exactly. Party leg = sum of other legs so
        # 0.08-rupee rounding mismatches between taxable_value and total_amount
        # don't trigger Tally's silent EXCEPTIONS=1 rejection.
        gross = round(taxable + cgst + sgst + igst, 2) or total
        if vt_tally == "Payment":
            # Sprint 33 — Payment (money OUT): Dr Party/Expense, Cr Cash/Bank.
            cr_name = payment_mode or "Cash"
            dr_name = counter_ledger_in or party
            # Guard: counter must not collapse onto the cash/bank leg (e.g. AI
            # mistakenly set counter_ledger=Cash). Fall back to the party.
            if not dr_name or dr_name.strip().lower() == cr_name.strip().lower():
                dr_name = party if party.strip().lower() != cr_name.strip().lower() else (counter_ledger_in or party)
            legs.append((dr_name, -gross, "Yes"))   # Dr → negative
            legs.append((cr_name, gross, "No"))     # Cr → positive
        elif vt_tally == "Receipt":
            # Sprint 33 — Receipt (money IN): Dr Cash/Bank, Cr Party/Income.
            dr_name = payment_mode or "Cash"
            cr_name = counter_ledger_in or party
            if not cr_name or cr_name.strip().lower() == dr_name.strip().lower():
                cr_name = party if party.strip().lower() != dr_name.strip().lower() else (counter_ledger_in or party)
            legs.append((dr_name, -gross, "Yes"))   # Dr → negative
            legs.append((cr_name, gross, "No"))     # Cr → positive
        elif is_outflow:
            # Purchase: party is Cr (+), counter+tax are Dr (-)
            legs.append((party, gross, "No"))
            if taxable > 0:
                legs.append((counter_default, -taxable, "Yes"))
            # Tax ledgers: prefer the customer's REAL ledger names resolved server-side
            # from their Tally dump (payload.*_ledger). Hard-coded names are only a
            # fallback when the server couldn't resolve one.
            if cgst > 0: legs.append((payload.get("cgst_ledger") or "CGST Input", -cgst, "Yes"))
            if sgst > 0: legs.append((payload.get("sgst_ledger") or "SGST Input", -sgst, "Yes"))
            if igst > 0: legs.append((payload.get("igst_ledger") or "IGST Input", -igst, "Yes"))
        else:
            # Sales: party is Dr (-), counter+tax are Cr (+)
            legs.append((party, -gross, "Yes"))
            if taxable > 0:
                legs.append((counter_default, taxable, "No"))
            # Tax ledgers: prefer the customer's REAL ledger names resolved server-side
            # from their Tally dump (payload.*_ledger). Hard-coded names are only a
            # fallback when the server couldn't resolve one.
            if cgst > 0: legs.append((payload.get("cgst_ledger") or "CGST Output", cgst, "No"))
            if sgst > 0: legs.append((payload.get("sgst_ledger") or "SGST Output", sgst, "No"))
            if igst > 0: legs.append((payload.get("igst_ledger") or "IGST Output", igst, "No"))

    legs_xml = "".join([
        f"<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{_xml_escape(name)}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{is_pos}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{amt:.2f}</AMOUNT>"
        f"</ALLLEDGERENTRIES.LIST>"
        for (name, amt, is_pos) in legs
    ])

    # Sprint 44.2 — every voucher we CREATE in Tally gets a YantrAI-controlled
    # REMOTEID set to the invoice's yantrai_uid. Tally Prime indexes vouchers
    # by REMOTEID, so subsequent ALTER / DELETE can target the same voucher by
    # the same yantrai_uid without depending on Tally's auto-assigned LASTVCHID
    # or post-hoc GUID lookup.
    #
    # Sprint 44.1 (kept) — for vouchers that lack yantrai_uid (e.g. JMK's
    # production vouchers that were pushed BEFORE this code shipped, or a
    # Tally-originated voucher we never created), fall back to the previous
    # branching: numeric LASTVCHID → <MASTERID> element; full GUID → REMOTEID
    # from tally_master_id. The original Sprint 44.1 bug (sending a GUID into
    # <MASTERID> and silently getting a duplicate) stays fixed.
    action = (payload.get("tally_action") or "Create").strip().capitalize()
    if action not in ("Create", "Alter"):
        action = "Create"
    master_id = payload.get("tally_master_id")
    masterid_node = ""
    remoteid_attr = ""
    # Sprint 44.3 — preferred Alter targeting: numeric MASTERID via the
    # <MASTERID> element (Tally honors this; REMOTEID attr is ignored as
    # we proved in Sprint 44.2). push_voucher_to_tally's Alter pre-flight
    # runs the [YAI:<uid>] narration lookup against Tally and populates
    # tally_master_id with the numeric Tally MASTERID — so this branch is
    # the one that actually fires for YantrAI-originated vouchers.
    if action == "Alter" and master_id and str(master_id).strip().isdigit():
        masterid_node = f"<MASTERID>{_xml_escape(str(master_id).strip())}</MASTERID>"
    elif _yuid:
        # Sprint 44.2 harmless tagging — on Create this stamps a REMOTEID
        # Tally discards but is still useful as a fingerprint if we ever
        # write a custom-TDL lookup. On Alter for vouchers that pre-date
        # 44.3 this is also our only signal (still ignored by Tally).
        remoteid_attr = f' REMOTEID="{_xml_escape(_yuid)}"'
    elif action == "Alter" and master_id:
        # Sprint 44.1 fallback for GUID-shape master_id without a yantrai_uid.
        # Tally still won't honor it, but it's the least-wrong option.
        remoteid_attr = f' REMOTEID="{_xml_escape(str(master_id).strip())}"'

    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ENVELOPE>'
          '<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>'
          '<BODY>'
            '<IMPORTDATA>'
              '<REQUESTDESC>'
                '<REPORTNAME>Vouchers</REPORTNAME>'
                '<STATICVARIABLES>'
                  f'<SVCURRENTCOMPANY>{_xml_escape(company_name)}</SVCURRENTCOMPANY>'
                '</STATICVARIABLES>'
              '</REQUESTDESC>'
              '<REQUESTDATA>'
                '<TALLYMESSAGE xmlns:UDF="TallyUDF">'
                  f'<VOUCHER VCHTYPE="{_xml_escape(vt_tally)}" ACTION="{action}"{remoteid_attr} OBJVIEW="Accounting Voucher View">'
                    f'{masterid_node}'
                    f'<DATE>{date_str}</DATE>'
                    f'<VOUCHERNUMBER>{_xml_escape(voucher_num)}</VOUCHERNUMBER>'
                    f'<VOUCHERTYPENAME>{_xml_escape(vt_tally)}</VOUCHERTYPENAME>'
                    f'<PARTYLEDGERNAME>{_xml_escape(party)}</PARTYLEDGERNAME>'
                    f'<NARRATION>{_xml_escape(narration)}</NARRATION>'
                    f'{legs_xml}'
                  '</VOUCHER>'
                '</TALLYMESSAGE>'
              '</REQUESTDATA>'
            '</IMPORTDATA>'
          '</BODY>'
        '</ENVELOPE>'
    )
    return envelope


def _find_voucher_by_yantrai_uid(tally_url, yantrai_uid, timeout=10.0):
    """Sprint 44.3 — query Tally for a voucher whose narration contains the
    `[YAI:<uid>]` marker we stamp on every Create push. Returns a dict with
    {date, voucher_type, voucher_number, master_id, guid} or None if no
    matching voucher exists (likely because the user deleted it in Tally
    manually, or it was never pushed).

    Uses Tally's TDL `CONTAINS` filter on `$Narration`. Single round-trip;
    paced by the global Tally HTTP lock like every other query."""
    if not yantrai_uid:
        return None
    needle = f"[YAI:{yantrai_uid}]"
    # CDATA-safe substring inside the formula. Tally TDL's CONTAINS is
    # case-sensitive; our stamp is lowercase UUIDs.
    needle_xml = needle.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = (
        '<ENVELOPE>'
          '<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST>'
            '<TYPE>Collection</TYPE><ID>YaiVchLookup</ID></HEADER>'
          '<BODY><DESC>'
            '<STATICVARIABLES>'
              '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
              '<SVFROMDATE TYPE="Date">20000401</SVFROMDATE>'
              '<SVTODATE TYPE="Date">20991231</SVTODATE>'
            '</STATICVARIABLES>'
            '<TDL><TDLMESSAGE>'
              '<COLLECTION NAME="YaiVchLookup">'
                '<TYPE>Voucher</TYPE>'
                '<FETCH>Date, VoucherTypeName, VoucherNumber, MasterId, GUID, Narration</FETCH>'
                '<FILTER>YaiNarrationFilter</FILTER>'
              '</COLLECTION>'
              f'<SYSTEM TYPE="Formula" NAME="YaiNarrationFilter">'
                f'$Narration CONTAINS "{needle_xml}"'
              f'</SYSTEM>'
            '</TDLMESSAGE></TDL>'
          '</DESC></BODY>'
        '</ENVELOPE>'
    )
    res = query_local_tally(tally_url, xml, timeout=timeout)
    if not res:
        return None
    # Parse the first real voucher block. Tally's reply also contains a stats
    # element `<VOUCHER>16</VOUCHER>` inside CMPINFO (counting vouchers in the
    # company) — require at least one attribute on the opening tag to skip it.
    m = re.search(r"<VOUCHER\s+[^>]+>(.*?)</VOUCHER>", res, re.DOTALL)
    if not m:
        return None
    blk = m.group(1)

    def _ext(tag):
        mm = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", blk, re.DOTALL | re.IGNORECASE)
        return _clean(mm.group(1)) if mm else ""

    return {
        "date":           _ext("DATE"),               # "20260515"
        "voucher_type":   _ext("VOUCHERTYPENAME"),    # "Sales"
        "voucher_number": _ext("VOUCHERNUMBER"),      # Tally's own number (often "5", "8", ...)
        "master_id":      _ext("MASTERID"),           # numeric "8"
        "guid":           _ext("GUID"),               # full GUID
    }


def _build_voucher_delete_xml(tally_master_id, voucher_type, company_name,
                              yantrai_uid=None,
                              tally_native=None):
    """Sprint 44 / 44.1 / 44.2 — Build a Tally Import-Data envelope that
    DELETES one voucher.

    Sprint 44.2 (preferred): when the voucher was CREATEd by this agent on or
    after v0.19.2, it carries a YantrAI-controlled REMOTEID = yantrai_uid.
    Delete by that REMOTEID. Tally always resolves it correctly because we
    set it ourselves.

    Sprint 44.3 (preferred): if tally_native is provided, build the delete
    envelope keyed on Tally's own DATE + VOUCHERTYPENAME + VOUCHERNUMBER —
    the standard TDL way to identify an existing voucher for ALTER/DELETE.
    Tally resolves these reliably; REMOTEID supplied by us doesn't.

    Sprint 44.1 (legacy fallback): for older vouchers where we haven't done
    the lookup, branch by tally_master_id shape — numeric LASTVCHID →
    <MASTERID>2</MASTERID>; UUID-shape → REMOTEID="<guid>" attribute.

    Tally responds with <DELETED>1</DELETED> on success."""
    vt_tally = (voucher_type or "Sales").strip().capitalize()
    if isinstance(tally_native, dict) and tally_native.get("voucher_number"):
        # Sprint 44.3 happy path — delete by Tally-native identifiers.
        # Tally's delete prefers REMOTEID="<full-GUID>" on the <VOUCHER> tag
        # PLUS DATE / VOUCHERTYPENAME / VOUCHERNUMBER in the body (belt + braces).
        nat_vt = (tally_native.get("voucher_type") or vt_tally).strip().capitalize()
        nat_dt = (tally_native.get("date") or "").strip()
        nat_vn = (tally_native.get("voucher_number") or "").strip()
        nat_mid = (tally_native.get("master_id") or "").strip()
        nat_guid = (tally_native.get("guid") or "").strip()
        ident_attr = (f' REMOTEID="{_xml_escape(nat_guid)}"' if nat_guid else "")
        ident_node = (
            f"<DATE>{_xml_escape(nat_dt)}</DATE>"
            f"<VOUCHERTYPENAME>{_xml_escape(nat_vt)}</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>{_xml_escape(nat_vn)}</VOUCHERNUMBER>"
        )
        if nat_mid.isdigit():
            ident_node += f"<MASTERID>{_xml_escape(nat_mid)}</MASTERID>"
        vt_tally = nat_vt
    elif yantrai_uid and str(yantrai_uid).strip():
        # Pre-Sprint-44.3 path kept as belt-and-braces; Tally ignores
        # user-supplied REMOTEID so this almost never resolves, but doesn't
        # actively harm anything if the lookup is skipped.
        ident_attr = f' REMOTEID="{_xml_escape(str(yantrai_uid).strip())}"'
        ident_node = ""
    else:
        mid_s = str(tally_master_id or "").strip()
        if mid_s.isdigit():
            ident_attr = ""
            ident_node = f"<MASTERID>{_xml_escape(mid_s)}</MASTERID>"
        else:
            ident_attr = f' REMOTEID="{_xml_escape(mid_s)}"'
            ident_node = ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ENVELOPE>'
          '<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>'
          '<BODY>'
            '<IMPORTDATA>'
              '<REQUESTDESC>'
                '<REPORTNAME>Vouchers</REPORTNAME>'
                '<STATICVARIABLES>'
                  f'<SVCURRENTCOMPANY>{_xml_escape(company_name)}</SVCURRENTCOMPANY>'
                '</STATICVARIABLES>'
              '</REQUESTDESC>'
              '<REQUESTDATA>'
                '<TALLYMESSAGE xmlns:UDF="TallyUDF">'
                  f'<VOUCHER VCHTYPE="{_xml_escape(vt_tally)}" ACTION="Delete"{ident_attr}>'
                    f'{ident_node}'
                  '</VOUCHER>'
                '</TALLYMESSAGE>'
              '</REQUESTDATA>'
            '</IMPORTDATA>'
          '</BODY>'
        '</ENVELOPE>'
    )


def _parse_tally_push_response(xml_str):
    """Tally returns an envelope with <CREATED>1</CREATED> on success or
    <LINEERROR>…</LINEERROR> on failure. Returns (ok: bool, info: str).

    Sprint 32 — <ALTERED> is also a success indicator (re-import of an
    existing master returns ALTERED).
    Sprint 44 — <DELETED> is also a success indicator (ACTION="Delete")."""
    if not xml_str:
        return False, "Empty response from Tally (Prime not running on :9000?)"
    txt = xml_str
    # Success indicators
    m_created = re.search(r"<CREATED>(\d+)</CREATED>", txt)
    created = int(m_created.group(1)) if m_created else 0
    m_altered = re.search(r"<ALTERED>(\d+)</ALTERED>", txt)
    altered = int(m_altered.group(1)) if m_altered else 0
    m_deleted = re.search(r"<DELETED>(\d+)</DELETED>", txt)
    deleted = int(m_deleted.group(1)) if m_deleted else 0
    m_lastvch = re.search(r"<LASTVCHID>([^<]+)</LASTVCHID>", txt)
    guid = m_lastvch.group(1).strip() if m_lastvch else None
    # Error indicators
    m_line_err = re.search(r"<LINEERROR>([^<]+)</LINEERROR>", txt)
    m_desc_err = re.search(r"<DESC>([^<]+)</DESC>", txt) if "<DESC>" in txt else None
    err = (m_line_err.group(1) if m_line_err else
           (m_desc_err.group(1) if m_desc_err else None))
    if created > 0 or altered > 0 or deleted > 0:
        if deleted > 0 and created == 0:
            return True, "deleted"
        return True, guid or ("altered" if altered > 0 else "ok")
    return False, (err or txt[:300])


def _build_ledger_master_xml(name, parent_group, company_name, gstin=None, pan=None):
    """Sprint 32 — Build a Tally Import-Data envelope that CREATES one new
    ledger master. Used when a voucher push fails because the party doesn't
    yet exist in Tally."""
    gstin_node = f"<PARTYGSTIN>{_xml_escape(gstin)}</PARTYGSTIN>" if gstin else ""
    pan_node = f"<INCOMETAXNUMBER>{_xml_escape(pan)}</INCOMETAXNUMBER>" if pan else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ENVELOPE>'
          '<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>'
          '<BODY><IMPORTDATA>'
            '<REQUESTDESC>'
              '<REPORTNAME>All Masters</REPORTNAME>'
              '<STATICVARIABLES>'
                f'<SVCURRENTCOMPANY>{_xml_escape(company_name)}</SVCURRENTCOMPANY>'
              '</STATICVARIABLES>'
            '</REQUESTDESC>'
            '<REQUESTDATA><TALLYMESSAGE xmlns:UDF="TallyUDF">'
              f'<LEDGER NAME="{_xml_escape(name)}" ACTION="Create">'
                f'<NAME.LIST><NAME>{_xml_escape(name)}</NAME></NAME.LIST>'
                f'<PARENT>{_xml_escape(parent_group)}</PARENT>'
                f'{gstin_node}'
                f'{pan_node}'
                '<ISBILLWISEON>Yes</ISBILLWISEON>'
                '<ISCOSTCENTRESON>No</ISCOSTCENTRESON>'
              '</LEDGER>'
            '</TALLYMESSAGE></REQUESTDATA>'
          '</IMPORTDATA></BODY>'
        '</ENVELOPE>'
    )


# ── Sprint 43 — system ledger XML builder + classification ──────────────────
# Used by the "propose-then-create" flow: the agent never silently creates
# Tally masters. Instead it builds an approval bundle, posts it to the server,
# the UI shows the user, and ONLY on approval do these builders run.

def _build_system_ledger_xml(name, kind, company_name):
    """Build a Tally Import-Data envelope that CREATES one system ledger
    (GST output/input, Sales/Purchase Account, or Round Off). Reuses the
    same proven IMPORTDATA wrapper as `_build_ledger_master_xml` — only the
    inner classification fields differ per kind.

    `kind` is one of:
       cgst_out, sgst_out, igst_out    → Duties & Taxes, Central|State|Integrated Tax
       cgst_in,  sgst_in,  igst_in     → Duties & Taxes, Central|State|Integrated Tax
       sales_account                   → Sales Accounts (revenue)
       purchase_account                → Purchase Accounts (expense)
       round_off                       → Indirect Expenses
    """
    name_x = _xml_escape(name)
    company_x = _xml_escape(company_name)

    # Map kind → (parent_group, tax_head, is_deemed_positive, is_billwise)
    GST_HEAD = {
        "cgst_out": "Central Tax", "cgst_in": "Central Tax",
        "sgst_out": "State Tax",   "sgst_in": "State Tax",
        "igst_out": "Integrated Tax", "igst_in": "Integrated Tax",
    }
    KIND_META = {
        "cgst_out": ("Duties & Taxes", "No",  "No"),
        "sgst_out": ("Duties & Taxes", "No",  "No"),
        "igst_out": ("Duties & Taxes", "No",  "No"),
        "cgst_in":  ("Duties & Taxes", "Yes", "No"),
        "sgst_in":  ("Duties & Taxes", "Yes", "No"),
        "igst_in":  ("Duties & Taxes", "Yes", "No"),
        "sales_account":    ("Sales Accounts",     "No",  "No"),
        "purchase_account": ("Purchase Accounts",  "Yes", "No"),
        "round_off":        ("Indirect Expenses",  "No",  "No"),
    }
    parent, deemed_pos, billwise = KIND_META.get(kind, ("Sundry Debtors", "Yes", "Yes"))
    parent_x = _xml_escape(parent)

    # GST-specific classification: only for cgst_*/sgst_*/igst_*
    gst_nodes = ""
    if kind in GST_HEAD:
        head = GST_HEAD[kind]
        gst_nodes = (
            "<TAXTYPE>GST</TAXTYPE>"
            f"<GSTDUTYHEAD>{head}</GSTDUTYHEAD>"
            "<ROUNDINGMETHOD/>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ENVELOPE>'
          '<HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>'
          '<BODY><IMPORTDATA>'
            '<REQUESTDESC>'
              '<REPORTNAME>All Masters</REPORTNAME>'
              '<STATICVARIABLES>'
                f'<SVCURRENTCOMPANY>{company_x}</SVCURRENTCOMPANY>'
              '</STATICVARIABLES>'
            '</REQUESTDESC>'
            '<REQUESTDATA><TALLYMESSAGE xmlns:UDF="TallyUDF">'
              f'<LEDGER NAME="{name_x}" ACTION="Create">'
                f'<NAME.LIST><NAME>{name_x}</NAME></NAME.LIST>'
                f'<PARENT>{parent_x}</PARENT>'
                f'{gst_nodes}'
                f'<ISDEEMEDPOSITIVE>{deemed_pos}</ISDEEMEDPOSITIVE>'
                f'<ISBILLWISEON>{billwise}</ISBILLWISEON>'
                '<ISCOSTCENTRESON>No</ISCOSTCENTRESON>'
              '</LEDGER>'
            '</TALLYMESSAGE></REQUESTDATA>'
          '</IMPORTDATA></BODY>'
        '</ENVELOPE>'
    )


def _classify_missing_ledger(name):
    """Map a missing ledger name → a 'kind' string the system-ledger builder
    understands, or None when the agent can't safely classify it. Conservative
    whitelist; anything unrecognised goes to the UI bundle as kind='unknown'
    so the user is told to create it manually."""
    if not name:
        return None
    n = (name or "").strip().lower()
    # GST tax ledgers — require an explicit Output/Input cue.
    is_out = any(w in n for w in (" output", " out", "output", "payable"))
    is_in  = any(w in n for w in (" input",  " in",  "input",  "receivable"))
    if "cgst" in n:
        if is_out: return "cgst_out"
        if is_in:  return "cgst_in"
    if "sgst" in n:
        if is_out: return "sgst_out"
        if is_in:  return "sgst_in"
    if "igst" in n:
        if is_out: return "igst_out"
        if is_in:  return "igst_in"
    # Revenue / cost heads
    if re.search(r"\bsales\b.*\baccount\b|\bsales a/c\b", n):
        return "sales_account"
    if re.search(r"\bpurchase\b.*\baccount\b|\bpurchase a/c\b", n):
        return "purchase_account"
    if re.search(r"round[\s-]*off|rounding", n):
        return "round_off"
    return None  # caller treats as party (Sundry Debtors/Creditors) or 'unknown'


def _propose_create_bundle(missing_names, payload):
    """Build the JSON bundle the agent POSTs to /needs_approval.
    Each entry tells the UI exactly what would be created in Tally.

    Bundle entry shape:
       {name, kind, group, tax_head, gstin, blocks_voucher}
    """
    vt = (payload.get("voucher_type") or payload.get("category") or "Sales").strip()
    is_purchase = vt.lower() in ("purchase", "payment")
    voucher_num = (payload.get("invoice_number") or payload.get("voucher_number") or "").strip()
    party_gstin = (payload.get("billed_to_party_gstin") or payload.get("billing_party_gstin")
                   or payload.get("party_gstin") or "")

    bundle = []
    for name in missing_names:
        kind = _classify_missing_ledger(name)
        if kind:
            # Mirror the constants in _build_system_ledger_xml for the UI's display.
            group_map = {
                "cgst_out": "Duties & Taxes", "cgst_in": "Duties & Taxes",
                "sgst_out": "Duties & Taxes", "sgst_in": "Duties & Taxes",
                "igst_out": "Duties & Taxes", "igst_in": "Duties & Taxes",
                "sales_account":    "Sales Accounts",
                "purchase_account": "Purchase Accounts",
                "round_off":        "Indirect Expenses",
            }
            head_map = {
                "cgst_out": "Central Tax", "cgst_in": "Central Tax",
                "sgst_out": "State Tax",   "sgst_in": "State Tax",
                "igst_out": "Integrated Tax", "igst_in": "Integrated Tax",
            }
            bundle.append({
                "name": name, "kind": kind,
                "group": group_map.get(kind, ""),
                "tax_head": head_map.get(kind, ""),
                "gstin": "",
                "blocks_voucher": voucher_num,
            })
            continue
        # Party — Sundry Debtors for Sales/Receipt, Sundry Creditors otherwise.
        party_group = "Sundry Creditors" if is_purchase else "Sundry Debtors"
        # Attach the invoice's party GSTIN if this missing name matches the
        # billed-to/billing party (one of the two extremes).
        party_name = (payload.get("billed_to_party_name") or payload.get("billing_party_name")
                      or payload.get("party_name") or payload.get("party") or "")
        gstin_for_this = party_gstin if (name.strip().lower() == party_name.strip().lower()) else ""
        bundle.append({
            "name": name, "kind": "party",
            "group": party_group, "tax_head": "",
            "gstin": gstin_for_this,
            "blocks_voucher": voucher_num,
        })
    return bundle


def _create_one_approved_ledger(entry_or_name, payload, tally_url, company_name):
    """Create ONE ledger in Tally based on an approved-bundle entry.

    `entry_or_name` is either a dict from the bundle (preferred — has kind +
    group + gstin already classified) OR a bare string. We re-classify the
    string case defensively, so the function works whether the UI sent us the
    full bundle entries or just the names.

    Returns (ok: bool, info: str). Treats "ledger already exists" as success
    (idempotency: a second poll after an approval shouldn't fail just because
    the create has already landed)."""
    if isinstance(entry_or_name, dict):
        name = (entry_or_name.get("name") or "").strip()
        kind = (entry_or_name.get("kind") or "").strip()
        bundle_gstin = (entry_or_name.get("gstin") or "").strip()
    else:
        name = (entry_or_name or "").strip()
        kind = _classify_missing_ledger(name) or "party"
        bundle_gstin = ""
    if not name:
        return False, "empty ledger name"

    # Idempotency: if Tally already has this ledger, skip the CREATE (Tally
    # returns 'already exists' for a duplicate ACTION="Create"). We check the
    # live chart cheaply — fetch_local_ledgers is one HTTP call, paced by the
    # global lock, vs. another import write that would only fail anyway.
    try:
        live = fetch_local_ledgers(tally_url) or []
        if name.strip().lower() in {n.strip().lower() for n in live}:
            return True, f"already exists ({name})"
    except Exception:
        pass  # fall through to a normal create attempt

    try:
        if kind and kind != "party" and kind != "unknown":
            xml = _build_system_ledger_xml(name, kind, company_name)
        else:
            # Party — fall back to existing party builder.
            parent = _guess_parent_group(name, payload)
            gstin = bundle_gstin or (payload.get("billing_party_gstin")
                                     or payload.get("party_gstin") or "")
            pan = payload.get("pan") or payload.get("party_pan")
            xml = _build_ledger_master_xml(name, parent, company_name,
                                            gstin=gstin or None, pan=pan)
        resp = query_local_tally(tally_url, xml, timeout=15.0)
        ok, info = _parse_tally_push_response(resp or "")
        # Belt-and-braces: if Tally still reports the ledger as already
        # existing (race against our pre-check), accept it.
        if not ok and isinstance(info, str) and "already exist" in info.lower():
            return True, f"already exists ({name})"
        return ok, info
    except Exception as e:
        return False, f"Auto-create master failed: {e}"


# Patterns of common Tally "this ledger doesn't exist" error messages.
# Real Tally returns several variants depending on context.
_MISSING_LEDGER_PATTERNS = [
    re.compile(r"Ledger\s+'([^']+)'\s+does not exist", re.I),
    re.compile(r"LEDGER\s+([A-Za-z0-9 .,&/'-]+?)\s+cannot be found", re.I),
    re.compile(r"No such ledger[:\s]+([A-Za-z0-9 .,&/'-]+)", re.I),
    re.compile(r"could not find LEDGER[:\s]+'?([^'<]+)'?", re.I),
]


def _extract_missing_ledger(error_text):
    """If Tally's error mentions a missing ledger, return its name.
    Sprint 32 — Tally encodes single-quotes as `&apos;` in its XML output;
    decode HTML entities before pattern-matching so the regex catches them."""
    if not error_text: return None
    txt = error_text
    # Decode the few HTML entities Tally actually uses
    for ent, ch in (("&apos;", "'"), ("&quot;", '"'),
                    ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        txt = txt.replace(ent, ch)
    for rx in _MISSING_LEDGER_PATTERNS:
        m = rx.search(txt)
        if m: return m.group(1).strip()
    return None


def _guess_parent_group(ledger_name, payload):
    """Pick the Tally parent group for an auto-created ledger.
    Tax names → Duties & Taxes.
    Sales Account / Purchase Account → Sales/Purchase Accounts (plural — Tally's group name).
    Cash → Cash-in-hand. Anything Bank → Bank Accounts.
    Otherwise default to party group based on voucher type."""
    vt = (payload.get("voucher_type") or payload.get("category") or "Sales").strip().lower()
    n = (ledger_name or "").lower().strip()
    if any(t in n for t in ("cgst", "sgst", "igst", "tax", "duty", "tds")):
        return "Duties & Taxes"
    if "sales" in n and "account" in n:    return "Sales Accounts"
    if "purchase" in n and "account" in n: return "Purchase Accounts"
    if n == "cash":                          return "Cash-in-hand"
    if "bank" in n:                          return "Bank Accounts"
    if vt in ("purchase", "payment"): return "Sundry Creditors"
    return "Sundry Debtors"


# Sprint 32 — Names we should NEVER auto-create. These are system ledgers
# whose proper setup (GST classification, opening balance, percentage,
# narration mode, etc.) only the accountant can configure correctly. Trying
# to create them programmatically with our best-guess parent group corrupts
# Tally's chart of accounts and (in practice) crashes Tally Prime.
_SYSTEM_LEDGER_PATTERNS = re.compile(
    r"^(sales account|purchase account|cgst|sgst|igst|cgst output|sgst output|"
    r"igst output|cgst input|sgst input|igst input|cash|bank|tds|duty|cess|"
    r"round\s*off|suspense)",
    re.I,
)


def _is_system_ledger(name):
    """True if `name` looks like a system ledger we shouldn't auto-create."""
    return bool(_SYSTEM_LEDGER_PATTERNS.match((name or "").strip()))


def _gst_ledger_meta(name, payload):
    """Structured hint for a missing GST/tax ledger the USER must create in Tally:
    group, GST duty-head + Output/Input inferred from the name, and the blocking voucher."""
    n = (name or "").lower()
    head = ("Central Tax" if "cgst" in n else
            "State Tax" if "sgst" in n else
            "Integrated Tax" if "igst" in n else "")
    io = ("Output" if "output" in n else "Input" if "input" in n else "")
    vnum = (payload.get("invoice_number") or payload.get("voucher_number") or "").strip()
    return {"ledger_name": name, "group": "Duties & Taxes",
            "gst_head": head, "io": io, "voucher_number": vnum}


def _try_push_once(payload, tally_url, company_name):
    """One push attempt — returns (ok, info)."""
    try:
        xml = _build_voucher_xml(payload, company_name)
    except Exception as e:
        return False, f"XML build failed: {e}"
    response = query_local_tally(tally_url, xml, timeout=30.0)
    if response is None:
        return False, "Tally Prime rejected the push (no response)"
    return _parse_tally_push_response(response)


# Sprint 32 — Per-push cache of the YantrAI ledger snapshot, so we only fetch
# once per voucher push rather than per ledger name.
_LEDGER_SNAPSHOT_CACHE = {"server": None, "company": None, "names": None, "by_lc": None}

# Sprint 41 — phase separation between baseline pull (seed_baseline) and outbox
# push (push_voucher_to_tally). Tally's HTTP server is single-threaded; a 40 s
# fetch_vouchers running concurrently with a write is what crashed JMK with
# c0000005. The seed_baseline handler sets this Event while it's fetching;
# outbox_poll_loop checks it and skips the claim+push for one cycle.
_sync_in_progress = threading.Event()


def _fetch_ledger_snapshot(server_url, company_name):
    """Fetch YantrAI's authoritative copy of Tally's chart of accounts for
    this company (populated by the original ingestion). Returns (names_list,
    lowercase_dict). Cached for the duration of a push."""
    if (_LEDGER_SNAPSHOT_CACHE["server"] == server_url
        and _LEDGER_SNAPSHOT_CACHE["company"] == company_name
        and _LEDGER_SNAPSHOT_CACHE["names"] is not None):
        return _LEDGER_SNAPSHOT_CACHE["names"], _LEDGER_SNAPSHOT_CACHE["by_lc"]
    url = f"{server_url}/api/tally/ledgers?company_name={urllib.parse.quote(company_name)}"
    try:
        res = _get_json(url, timeout=15.0) or {}
        names = [d.get("name") for d in (res.get("data") or []) if d.get("name")]
    except Exception:
        names = []
    by_lc = {n.lower(): n for n in names}
    _LEDGER_SNAPSHOT_CACHE.update({"server": server_url, "company": company_name,
                                    "names": names, "by_lc": by_lc})
    return names, by_lc


def _resolve_ledger_in_snapshot(ledger_name, snapshot_names, by_lc):
    """Return the canonical ledger name from YantrAI's snapshot that matches
    `ledger_name`, or None if no match. Tries: exact (case-insensitive),
    then substring match (e.g. 'SUN PHARMACEUTICAL INDUSTRIES LTD' matches
    'SUN PHARMACEUTICAL INDUSTRIES LTD.- HL')."""
    if not ledger_name: return None
    needle = ledger_name.strip().lower()
    # 1. Exact (case-insensitive)
    if needle in by_lc:
        return by_lc[needle]
    # 2. Snapshot name STARTS WITH the needle (handles ".- HL" / ".- PB" suffixes)
    for n in snapshot_names:
        nl = n.lower()
        if nl.startswith(needle) or needle.startswith(nl):
            return n
    # 3. Needle is a substring of a snapshot name (or vice-versa) — last resort
    for n in snapshot_names:
        nl = n.lower()
        if needle in nl or nl in needle:
            return n
    return None


def _check_ledger_exists(tally_url, company_name, ledger_name):
    """DEPRECATED in Sprint 32 — kept for back-compat. Use the snapshot-based
    pre-flight in _ensure_ledgers_exist instead. Returns True if name appears
    in YantrAI's ledger snapshot (which we trust as the source of truth) —
    falls back to a Tally HTTP probe if no server context is set."""
    if not ledger_name: return True
    # Try cached snapshot first
    names = _LEDGER_SNAPSHOT_CACHE.get("names")
    by_lc = _LEDGER_SNAPSHOT_CACHE.get("by_lc")
    if names:
        return _resolve_ledger_in_snapshot(ledger_name, names, by_lc) is not None
    # Fallback (legacy) — original HTTP probe
    probe = (
        '<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST>'
        '<TYPE>Object</TYPE><SUBTYPE>Ledger</SUBTYPE>'
        f'<ID TYPE="Name">{_xml_escape(ledger_name)}</ID></HEADER>'
        '<BODY><DESC><STATICVARIABLES>'
        '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
        f'<SVCURRENTCOMPANY>{_xml_escape(company_name)}</SVCURRENTCOMPANY>'
        '</STATICVARIABLES></DESC></BODY></ENVELOPE>'
    )
    resp = query_local_tally(tally_url, probe, timeout=8.0) or ""
    return f'NAME="{ledger_name}"' in resp or f"<NAME>{ledger_name}</NAME>" in resp


def _ensure_ledgers_exist(payload, tally_url, company_name, server_url=None):
    """Sprint 32 — Resolve & pre-create ledgers using YantrAI's authoritative
    snapshot of Tally's chart (instead of HTTP-probing Tally, which crashed it).

    Steps:
      1. Fetch the snapshot from /api/tally/ledgers (cached per push).
      2. For each ledger referenced in the voucher XML:
         a. If exact/fuzzy match exists → rewrite the payload to use the
            canonical name (handles e.g. "SUN ... LTD" → "SUN ... LTD.- HL").
         b. If not in snapshot AND not a system ledger → auto-create (party).
         c. If a system ledger is truly missing → fail fast with instructions.
      3. Return the (possibly-rewritten) payload, created list, errors.
    """
    try:
        xml = _build_voucher_xml(payload, company_name)
    except Exception as e:
        return payload, [], [f"XML build failed: {e}"]
    needed = re.findall(r"<LEDGERNAME>([^<]+)</LEDGERNAME>", xml)
    # de-dup while preserving order
    seen = set(); ordered = []
    for n in needed:
        if n not in seen:
            seen.add(n); ordered.append(n)

    # Snapshot from YantrAI server (this dev-box: the server already has JMK's
    # full chart of accounts from the original ingestion).
    snapshot_names, by_lc = ([], {})
    if server_url:
        snapshot_names, by_lc = _fetch_ledger_snapshot(server_url, company_name)

    # Build a name-rewrite map: requested name → canonical name in JMK
    rewrites = {}
    for nm in ordered:
        canonical = _resolve_ledger_in_snapshot(nm, snapshot_names, by_lc)
        if canonical and canonical != nm:
            rewrites[nm] = canonical

    # Re-write the payload's party + ledger_entries to use canonical names
    if rewrites:
        for fld in ("billing_party_name", "party_name", "party"):
            v = payload.get(fld)
            if v and v in rewrites:
                payload[fld] = rewrites[v]
        if isinstance(payload.get("ledger_entries"), list):
            for e in payload["ledger_entries"]:
                ln = e.get("ledger_name") or e.get("ledger")
                if ln and ln in rewrites:
                    if "ledger_name" in e: e["ledger_name"] = rewrites[ln]
                    if "ledger" in e:      e["ledger"] = rewrites[ln]

    # Re-derive the needed list against post-rewrite XML
    try:
        xml2 = _build_voucher_xml(payload, company_name)
    except Exception:
        xml2 = xml
    needed = re.findall(r"<LEDGERNAME>([^<]+)</LEDGERNAME>", xml2)
    seen = set(); ordered = []
    for n in needed:
        if n not in seen:
            seen.add(n); ordered.append(n)

    # Sprint 43 — This pre-flight no longer creates anything. Returns the
    # (rewritten) payload plus empty `created` + `errors` lists. The actual
    # missing-ledger detection + propose-then-create flow lives in
    # push_voucher_to_tally (live pre-flight → NEEDS_APPROVAL bundle).
    # We keep this function in the call graph because the rewrite logic above
    # still maps payload names → canonical company-specific names.
    return payload, [], []


def _collect_payload_ledger_refs(payload, company_name):
    """Return every ledger name the to-be-sent Import XML would reference, by
    building the XML and extracting <LEDGERNAME> tags. Used by the live-Tally
    pre-flight so we can refuse to send a payload that names a ledger Tally
    doesn't actually have (Tally crashes with c0000005 on such imports — it
    doesn't reject them cleanly)."""
    try:
        xml = _build_voucher_xml(payload, company_name)
    except Exception:
        return []
    seen, out = set(), []
    for n in re.findall(r'<LEDGERNAME>([^<]+)</LEDGERNAME>', xml):
        n = (n or "").strip()
        k = n.lower()
        if n and k not in seen:
            seen.add(k); out.append(n)
    return out


def push_voucher_to_tally(payload, tally_url, company_name, server_url=None,
                          approved_ledgers=None):
    """Sprint 31 + 32 + 36 + 43 — Pre-flight against LIVE Tally first (cheap,
    authoritative). If every referenced ledger exists in live Tally, skip the
    flakier snapshot-based pre-flight (which can fail with SVCurrentCompany on
    Tally state issues) and push straight away. Only fall back to the snapshot
    path when something looks missing.

    Sprint 43 — `approved_ledgers` (list of name strings OR bundle dicts) is the
    user-approved subset of a previous propose-then-create bundle. If provided,
    those ledgers are created in Tally BEFORE pre-flight (with 2.5 s settle
    between each), then we fall through to the normal push. If pre-flight STILL
    reports missing ledgers, return `NEEDS_APPROVAL|<bundle_json>|<message>` so
    the outbox-poll caller can POST the new bundle to /needs_approval and the UI
    can ask the user again.

    Sprint 44 — when `payload.tally_action == "Delete"` we run a totally
    separate, simpler path: no ledger pre-flight, no snapshot rewrite, no
    consent gate (the UI already showed the user a destructive-action confirm
    modal). Just build the Delete XML, POST it, parse the response.

    Returns (ok, guid_or_error)."""
    if not check_tally_alive(tally_url):
        return False, f"Tally Prime not reachable on {tally_url} (open Tally and try again)"

    # Sprint 44 / 44.2 / 44.3 — Delete branch. Short-circuit everything else.
    # Sprint 48 — DORMANT / UNREACHABLE: the server no longer enqueues Delete
    # outbox rows (the /delete-from-tally + /vouchers/delete endpoints now
    # return 410, and the UI delete buttons were removed) because Tally Prime's
    # XML API can't reliably delete a pushed voucher. This branch + its helpers
    # (_build_voucher_delete_xml, _find_voucher_by_yantrai_uid) are kept only to
    # avoid an unnecessary .exe rebuild; delete them on the next agent rebuild.
    if (payload.get("tally_action") or "").strip().capitalize() == "Delete":
        master_id = (payload.get("tally_master_id") or "").strip()
        yuid = (payload.get("yantrai_uid") or "").strip()
        if not master_id and not yuid:
            return False, ("Cannot delete: voucher has neither tally_master_id "
                           "nor yantrai_uid. Remove from YantrAI via the YantrAI-"
                           "side delete path instead.")
        vt = payload.get("voucher_type") or payload.get("category") or "Sales"

        # Sprint 44.3 — find the voucher in Tally by the [YAI:<uid>] narration
        # marker so we can target it by its Tally-native DATE + VOUCHERTYPENAME
        # + VOUCHERNUMBER. The legacy identifier-shape fallbacks remain inside
        # _build_voucher_delete_xml but are nearly always wrong, so we prefer
        # this query-then-act path whenever yuid is available.
        tally_native = None
        if yuid:
            try:
                tally_native = _find_voucher_by_yantrai_uid(tally_url, yuid)
            except Exception as _ne:
                print(f"[push_voucher_to_tally] yantrai_uid lookup failed: {_ne}",
                      flush=True)
                tally_native = None
            if tally_native is None:
                # No matching voucher in Tally — treat as "already gone".
                # The server's ack handler will still soft-delete the YantrAI
                # mirror so the row renders "Deleted (synced)" in the UI.
                return True, "deleted"

        xml = _build_voucher_delete_xml(master_id, vt, company_name,
                                         yantrai_uid=yuid or None,
                                         tally_native=tally_native)
        resp = query_local_tally(tally_url, xml, timeout=30.0)
        ok, info = _parse_tally_push_response(resp or "")
        if ok:
            return True, "deleted"
        ident_for_log = (tally_native and tally_native.get("voucher_number")) \
                         or yuid or master_id
        return False, f"Tally rejected delete of voucher {ident_for_log}: {info}"

    # Sprint 44.3 — Alter pre-flight. If this is an Alter and we have a
    # yantrai_uid, look up the real Tally voucher first and overwrite the
    # identifier fields in the payload (date, voucher_type, voucher_number)
    # with Tally-native values BEFORE _build_voucher_xml runs. _build_voucher_xml
    # will then naturally emit the right DATE + VOUCHERTYPENAME + VOUCHERNUMBER
    # in the envelope — the standard TDL way for Tally to recognise an
    # existing voucher for ALTER.
    if (payload.get("tally_action") or "").strip().capitalize() == "Alter" \
            and (payload.get("yantrai_uid") or "").strip():
        try:
            _native = _find_voucher_by_yantrai_uid(
                tally_url, payload["yantrai_uid"].strip())
        except Exception as _ne:
            print(f"[push_voucher_to_tally] alter yantrai_uid lookup failed: {_ne}",
                  flush=True)
            _native = None
        if _native and _native.get("voucher_number"):
            payload = dict(payload)  # copy — don't mutate the caller's dict
            payload["date"] = _native.get("date") or payload.get("date")
            payload["voucher_type"] = (
                _native.get("voucher_type") or payload.get("voucher_type"))
            payload["voucher_number"] = _native.get("voucher_number")
            # Promote the numeric MASTERID so _build_voucher_xml's Sprint 44.1
            # branching emits <MASTERID>N</MASTERID> alongside the DATE +
            # VCHTYPE + VCHNUMBER (extra-belt-and-braces voucher identification).
            payload["tally_master_id"] = (
                _native.get("master_id") or payload.get("tally_master_id"))
        elif _native is None:
            # Voucher isn't in Tally (probably deleted manually). Fall through
            # to a Create — the agent's next ack will stamp a fresh GUID.
            payload = dict(payload)
            payload["tally_action"] = "Create"
            payload.pop("tally_master_id", None)

    # Sprint 43 — pre-create user-approved ledgers FIRST. The 2.5 s settle is
    # the same proven cadence Sprint 32's party auto-create has been using since
    # the c0000005 crash class was diagnosed.
    if approved_ledgers:
        for entry in approved_ledgers:
            ok, info = _create_one_approved_ledger(entry, payload, tally_url, company_name)
            if not ok:
                _nm = entry.get("name") if isinstance(entry, dict) else entry
                return False, (f"Tally rejected create of approved ledger '{_nm}'. "
                               f"Tally said: {info}")
            time.sleep(2.5)
        # Settle a touch more before the voucher push.
        time.sleep(2.0)

    # Sprint 36 — LIVE-Tally pre-flight runs FIRST. The snapshot-based pre-flight below
    # has been observed failing on "Could not set SVCurrentCompany" even when the
    # payload is perfectly valid (Tally state quirk). The live check is cheaper, more
    # authoritative, and lets a good payload sail through without touching the snapshot
    # path at all. We also keep this as the crash safety net: Tally throws c0000005
    # when an Import-Data XML references a missing ledger, so we ALWAYS verify against
    # live Tally before sending.
    skip_snapshot_preflight = False
    try:
        refs = _collect_payload_ledger_refs(payload, company_name)
        if refs:
            live = fetch_local_ledgers(tally_url) or []
            live_lc = {n.strip().lower() for n in live}
            if live_lc:  # only act when we actually got a live list back
                missing_refs = [r for r in refs if r.strip().lower() not in live_lc]
                if not missing_refs:
                    # All refs exist in live Tally → safe to push WITHOUT the flaky
                    # snapshot pre-flight. This is the happy path.
                    skip_snapshot_preflight = True
                else:
                    # Sprint 43 — propose-then-create. The agent does NOT write
                    # anything to Tally yet. It bundles up everything that's
                    # missing, hands the bundle off to the server, and lets the
                    # UI ask the user for explicit approval. No silent creates,
                    # including for parties.
                    bundle = _propose_create_bundle(missing_refs, payload)
                    human = (f"Tally is missing {len(missing_refs)} ledger(s) for "
                             f"this voucher. Awaiting your approval to create them.")
                    return False, ("NEEDS_APPROVAL|" + json.dumps(bundle) + "|" + human)
    except Exception as _e:
        print(f"[push_voucher_to_tally] live pre-flight skipped: {_e}", flush=True)

    # Snapshot-based pre-flight — only rewrites payload names to match the company's
    # actual ledger names (e.g. "SUN PHARMA LTD" → "SUN PHARMACEUTICAL INDUSTRIES LTD.- HL").
    # In Sprint 43 it no longer silently creates anything; if names are still missing
    # after the rewrite, the post-rewrite live pre-flight on the retry loop will
    # raise NEEDS_APPROVAL.
    if not skip_snapshot_preflight:
        payload, created, errs = _ensure_ledgers_exist(payload, tally_url, company_name,
                                                        server_url=server_url)
        # Sprint 43 — ignore `errs` from the snapshot pre-flight; the live check
        # above (and the retry loop below) are the authoritative gates.

    # Sprint 43 — try the push ONCE. If Tally still complains about a missing
    # ledger we couldn't see in the live pre-flight (rare race; e.g. user
    # deleted a ledger after we read the chart), surface a fresh NEEDS_APPROVAL
    # bundle for whatever Tally just named. No silent creates here.
    ok, info = _try_push_once(payload, tally_url, company_name)
    if ok:
        return True, info

    # If Tally named a missing ledger, build a single-entry bundle and ask.
    missing = _extract_missing_ledger(info) if isinstance(info, str) else None
    if not missing and isinstance(info, str) and re.search(r"<EXCEPTIONS>[^<]*[1-9]", info):
        # Opaque EXCEPTIONS=1 — best-guess the party as the culprit.
        cand = (payload.get("billing_party_name") or payload.get("party_name")
                 or payload.get("party") or "").strip()
        if cand:
            missing = cand
    if missing:
        bundle = _propose_create_bundle([missing], payload)
        human = (f"Tally reported missing ledger '{missing}' after the push. "
                 f"Awaiting your approval to create it and retry.")
        return False, ("NEEDS_APPROVAL|" + json.dumps(bundle) + "|" + human)

    return False, info or "Push failed (Tally returned no actionable error)"


def heartbeat_loop(server_url, company_name, stop_event, token_provider=None, refresh_fn=None):
    """Ping /api/tally/heartbeat every 30s so the web sidebar pill turns 🟢.

    Reads the session token LIVE each iteration via token_provider (NOT a value
    captured at launch) — so when the session refreshes the heartbeat immediately
    uses the new token, exactly like the tunnel loop. On a 401 (stale session, e.g.
    after a server redeploy) it self-heals by calling refresh_fn (device-token
    resume), then the next ping uses the refreshed token."""
    def _tok():
        return token_provider() if callable(token_provider) else token_provider
    while not stop_event.is_set():
        try:
            resp = _post_json(
                f"{server_url}/api/tally/heartbeat",
                {"company_name": company_name, "agent_version": AGENT_VERSION,
                 "session_token": _tok()},
                timeout=8.0,
            )
            if isinstance(resp, dict) and "401" in str(resp.get("_error", "")) and callable(refresh_fn):
                refresh_fn()
        except Exception:
            pass
        stop_event.wait(30)


def outbox_poll_loop(server_url, tally_url, company_name, stop_event, log_fn=None,
                     token_provider=None, refresh_fn=None):
    """Claim pending rows from /api/tally/queue, push each to Tally, ack/fail.

    Reads the session token LIVE each iteration (token_provider) and self-heals on
    a 401 (refresh_fn → device-token resume), so a stale session never permanently
    wedges the pusher — it recovers on the next poll without a manual restart."""
    def log(msg):
        if log_fn: log_fn(msg)
        else: print(f"[outbox] {msg}", flush=True)
    def _tok():
        return token_provider() if callable(token_provider) else token_provider
    while not stop_event.is_set():
        # Phase separation (Sprint 41): if a baseline seed is currently fetching
        # from Tally, don't issue a competing write. Tally's HTTP server is
        # single-threaded; a write during the 40 s fetch_vouchers is what
        # crashed JMK with c0000005. Skip this cycle and recheck shortly.
        if _sync_in_progress.is_set():
            stop_event.wait(2)
            continue
        try:
            session_token = _tok()
            _qs = f"company_name={urllib.parse.quote(company_name)}&limit=10"
            if session_token:
                _qs += f"&session_token={urllib.parse.quote(session_token)}"
            res = _get_json(
                f"{server_url}/api/tally/queue?{_qs}",
                timeout=10.0,
            )
            # Self-heal: a 401 means the session token went stale (e.g. server
            # redeploy). Refresh via the durable device token and retry immediately.
            if isinstance(res, dict) and "401" in str(res.get("_error", "")):
                if callable(refresh_fn) and refresh_fn():
                    continue
            rows = (res or {}).get("data") or []
            if rows:
                log(f"Claimed {len(rows)} voucher(s) for push to Tally.")
            for row in rows:
                oid = row.get("id")
                payload = row.get("payload") or {}
                approved_ledgers = row.get("approved_ledgers")
                if not oid:
                    continue
                try:
                    # Clear the per-push ledger snapshot cache so each row
                    # re-fetches a fresh chart (in case a sibling push just
                    # created a master).
                    _LEDGER_SNAPSHOT_CACHE.update({"server": None, "company": None,
                                                    "names": None, "by_lc": None})
                    ok, info = push_voucher_to_tally(payload, tally_url, company_name,
                                                      server_url=server_url,
                                                      approved_ledgers=approved_ledgers)
                except Exception as e:
                    ok, info = False, f"Unexpected agent error: {e}"

                # Sprint 43 — NEEDS_APPROVAL is NOT a hard fail. It means the
                # agent needs explicit user opt-in to create one or more
                # missing Tally ledgers before it can push the voucher. We
                # forward the proposed bundle to the server (which flips the
                # outbox row to 'pending_approval') and move on — the next
                # poll will re-claim the row only after the user has approved.
                if (not ok) and isinstance(info, str) and info.startswith("NEEDS_APPROVAL|"):
                    try:
                        _, _bj, _human = info.split("|", 2)
                        bundle = json.loads(_bj)
                    except Exception:
                        bundle = []
                        _human = info
                    log(f"  ?? Outbox row {oid[:8]}... needs user approval for "
                        f"{len(bundle)} ledger(s).")
                    try:
                        _post_json(
                            f"{server_url}/api/tally/queue/{oid}/needs_approval",
                            {"bundle": bundle, "session_token": session_token},
                            timeout=10.0,
                        )
                    except Exception as _ne:
                        log(f"  ?? needs_approval POST failed: {_ne}")
                    continue   # do NOT call /ack or /fail

                if ok:
                    log(f"  OK Pushed outbox row {oid[:8]}... -- Tally GUID {info[:20] if info else 'ok'}")
                    _post_json(f"{server_url}/api/tally/queue/{oid}/ack",
                               {"tally_voucher_guid": info, "session_token": session_token},
                               timeout=10.0)
                else:
                    log(f"  XX Failed outbox row {oid[:8]}... -- {info}")
                    fail_body = {"error": str(info)[:1000], "session_token": session_token,
                                 "company_name": company_name}
                    # Legacy NEEDS_LEDGER pathway (Sprint 32) — kept as a fallback
                    # signal but no longer produced by Sprint 43's push. Server
                    # still handles it via add_tally_cleanup if it ever shows up.
                    if isinstance(info, str) and info.startswith("NEEDS_LEDGER|"):
                        try:
                            _, _j, _human = info.split("|", 2)
                            fail_body["needs_ledger"] = json.loads(_j)
                            fail_body["error"] = _human[:1000]
                        except Exception:
                            pass
                    _post_json(f"{server_url}/api/tally/queue/{oid}/fail", fail_body, timeout=10.0)
        except Exception as e:
            log(f"poll error: {e}")
        # Poll every 12s — fast enough that the web UI sees state transitions
        # within one polling badge cycle (web polls /api/tally/outbox every 3s).
        stop_event.wait(12)


# Make urllib.parse available where the poller needs it (urllib alone doesn't
# import .parse in Python 3; import explicitly so the build picks it up).
import urllib.parse


class TallyBridgeApp:
    def __init__(self, start_minimized=False):
        self.root = tk.Tk()
        self.root.title(f"YantrAI Tally Bridge · v{AGENT_VERSION}")
        self.root.geometry("560x600")
        self.root.minsize(520, 560)

        # Theme + window/taskbar icon
        apply_theme(self.root)
        self._icon_photo = None     # full-size, for the window/taskbar icon
        self._header_logo = None    # small, for the in-app header bar
        try:
            ico = resource_path(os.path.join("assets", "yantrai.ico"))
            if os.path.exists(ico):
                self.root.iconbitmap(default=ico)
        except Exception:
            pass
        try:
            png = resource_path(os.path.join("assets", "yantrai_256.png"))
            if _PILImage is not None and _PILImageTk is not None and os.path.exists(png):
                src = _PILImage.open(png).convert("RGBA")
                self._icon_photo = _PILImageTk.PhotoImage(src)
                self.root.iconphoto(True, self._icon_photo)
                small = src.resize((40, 40), _PILImage.LANCZOS)
                self._header_logo = _PILImageTk.PhotoImage(small)
        except Exception:
            pass

        # State
        self.is_connected = False
        self.synced_count = 0
        self.config = load_config()
        _migrate_default_server(self.config)
        self.ws_thread = None
        self.should_run = False
        self.start_minimized = start_minimized

        # Self-heal the Windows auto-start entry on every launch so the Run key always
        # points at the *current* exe path (fixes a stale path after reinstall/move).
        # Only when running as the packaged .exe — never register the dev .py.
        if getattr(sys, "frozen", False) and (self.config.get("autostart") or self.config.get("device_token")):
            try: set_autostart(True)
            except Exception: pass

        # Tray
        self.tray_icon = None
        self._tray_notified = False
        self._quitting = False

        # Auth state — populated after successful login
        self.session_token = None
        self.user_id = None
        self.username = None
        self.user_name = None
        self.memberships = []
        self.selected_company_id = None
        self.selected_company_name = None
        self.tally_company_name = None  # Cached from Tally
        # Durable device token (persisted) — used to auto-resume without a password.
        self.device_token = self.config.get("device_token")

        # Auto-resume the previous session via the durable device token (no password,
        # no login screen). Falls back to the login wizard if not viable / it fails.
        # Passwords are still never persisted.
        if self._can_auto_resume():
            self.build_reconnecting_screen()
            threading.Thread(target=self._try_auto_resume, daemon=True).start()
        else:
            self.build_login_wizard()

        # System tray (background running). Built once; survives screen swaps.
        self._init_tray()

        # Handle window close → minimize to tray instead of quitting
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        if self.start_minimized:
            self.root.after(300, self._hide_to_tray)

    # --------------------------------------------------------
    # System tray + background lifecycle
    # --------------------------------------------------------
    def _init_tray(self):
        if _pystray is None or _PILImage is None:
            return
        img = _icon_pil()
        if img is None:
            return
        try:
            menu = _pystray.Menu(
                _pystray.MenuItem("Open YantrAI Bridge", lambda *a: self._show_from_tray(), default=True),
                _pystray.MenuItem("Test Tally connection", lambda *a: self.root.after(0, self.test_tally)),
                _pystray.MenuItem("Sign out", lambda *a: self.root.after(0, self.sign_out)),
                _pystray.Menu.SEPARATOR,
                _pystray.MenuItem("Quit", lambda *a: self.root.after(0, self.quit_app)),
            )
            self.tray_icon = _pystray.Icon("yantrai_bridge", img, "YantrAI Tally Bridge", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception as e:
            print("tray init failed:", e)

    def _hide_to_tray(self):
        self.root.withdraw()
        if self.tray_icon and not self._tray_notified:
            self._tray_notified = True
            try:
                self.tray_icon.notify("Still running in the background. Right-click the tray icon to quit.",
                                      "YantrAI Tally Bridge")
            except Exception:
                pass

    def _show_from_tray(self):
        def _do():
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        self.root.after(0, _do)

    def quit_app(self):
        self._quitting = True
        self.should_run = False
        try:
            if getattr(self, "_outbox_stop_event", None):
                self._outbox_stop_event.set()
            if getattr(self, "_heartbeat_stop_event", None):
                self._heartbeat_stop_event.set()
        except Exception:
            pass
        try:
            if self.tray_icon:
                self.tray_icon.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    # --------------------------------------------------------
    # Branded header bar (reused across screens)
    # --------------------------------------------------------
    def _build_header(self, parent, subtitle=""):
        bar = tk.Frame(parent, bg=THEME["surface"])
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=THEME["surface"])
        inner.pack(fill="x", padx=20, pady=12)
        if self._header_logo is not None:
            tk.Label(inner, image=self._header_logo, bg=THEME["surface"]).pack(side="left", padx=(0, 12))
        txt = tk.Frame(inner, bg=THEME["surface"])
        txt.pack(side="left", fill="y")
        tk.Label(txt, text="YantrAI Tally Bridge", bg=THEME["surface"], fg=THEME["text"],
                 font=("Segoe UI", 15, "bold")).pack(anchor="w")
        if subtitle:
            tk.Label(txt, text=subtitle, bg=THEME["surface"], fg=THEME["muted"],
                     font=("Segoe UI", 9)).pack(anchor="w")
        tk.Label(inner, text=f"v{AGENT_VERSION}", bg=THEME["surface"], fg=THEME["muted"],
                 font=("Segoe UI", 8)).pack(side="right", anchor="ne")
        # accent rule
        tk.Frame(parent, bg=THEME["primary"], height=2).pack(fill="x")
        return bar

    # --------------------------------------------------------
    # Step 1 — Login Wizard (Server + Username + Password)
    # --------------------------------------------------------
    def build_login_wizard(self):
        for w in self.root.winfo_children():
            w.destroy()

        self._build_header(self.root, "Connect your local Tally to YantrAI Cloud")

        outer = tk.Frame(self.root, bg=THEME["bg"], padx=22, pady=16)
        outer.pack(fill="both", expand=True)

        # Tally status banner — quick check
        self.tally_banner = tk.Label(outer, text="⏳ Checking local Tally…", bg=THEME["bg"], fg=THEME["muted"],
                                     font=("Segoe UI", 9), wraplength=480, justify="center")
        self.tally_banner.pack(pady=(0, 4))
        ttk.Button(outer, text="↻ Recheck Tally",
                   command=lambda: threading.Thread(target=self._async_tally_probe, daemon=True).start()).pack(pady=(0, 12))
        threading.Thread(target=self._async_tally_probe, daemon=True).start()

        # Sign-in card
        form = tk.Frame(outer, bg=THEME["card"], highlightbackground=THEME["border"],
                        highlightthickness=1, padx=16, pady=16)
        form.pack(fill="both", expand=True, pady=(0, 12))

        def _flbl(text, big=False):
            tk.Label(form, text=text, bg=THEME["card"], fg=THEME["text"] if big else THEME["muted"],
                     font=("Segoe UI", 10, "bold") if big else ("Segoe UI", 9)).pack(anchor="w", pady=(0, 3))

        _flbl("Server", big=True)
        self.server_var = tk.StringVar(self.root)
        last_url = self.config.get("last_server_url") or SERVER_PRESETS[0]["url"]
        initial_label = next((p["label"] for p in SERVER_PRESETS if p["url"] == last_url), "Custom URL…")
        labels = [p["label"] for p in SERVER_PRESETS] + ["Custom URL…"]
        self.server_var.set(initial_label)
        server_cb = ttk.Combobox(form, textvariable=self.server_var, values=labels, state="readonly")
        server_cb.pack(fill="x", pady=(0, 4))
        server_cb.bind("<<ComboboxSelected>>", lambda e: self._on_server_changed(self.server_var.get()))
        self.server_url_entry = ttk.Entry(form, font=("Consolas", 9))
        self.server_url_entry.insert(0, last_url)
        self.server_url_entry.pack(fill="x", pady=(0, 12))

        _flbl("Username", big=True)
        self.username_entry = ttk.Entry(form, font=("Segoe UI", 11))
        last_username = self.config.get("last_username", "")
        if last_username:
            self.username_entry.insert(0, last_username)
        self.username_entry.pack(fill="x", pady=(0, 12))

        _flbl("Password", big=True)
        self.password_entry = ttk.Entry(form, font=("Segoe UI", 11), show="•")
        self.password_entry.pack(fill="x", pady=(0, 12))

        _flbl("Local Tally URL")
        self.tally_entry = ttk.Entry(form, font=("Consolas", 9))
        self.tally_entry.insert(0, self.config.get("tally_url", DEFAULT_TALLY))
        self.tally_entry.pack(fill="x", pady=(0, 2))

        self.login_error = tk.Label(outer, text="", bg=THEME["bg"], fg=THEME["err"], font=("Segoe UI", 9), wraplength=480)
        self.login_error.pack(pady=(2, 0))

        ttk.Button(outer, text="Authenticate  →", style="Accent.TButton", command=self._do_login).pack(fill="x", pady=(8, 0))

        tk.Label(outer, text="Your Tally data stays local. Only what you sync is sent.",
                 bg=THEME["bg"], fg=THEME["muted"], font=("Segoe UI", 8)).pack(pady=(10, 0))

        if last_username:
            self.password_entry.focus()
        else:
            self.username_entry.focus()
        self.root.bind("<Return>", lambda e: self._do_login())

    def _on_server_changed(self, label):
        """Server dropdown selection handler."""
        for p in SERVER_PRESETS:
            if p["label"] == label:
                self.server_url_entry.delete(0, tk.END)
                self.server_url_entry.insert(0, p["url"])
                return
        # Custom — leave field as-is for user to edit
        self.server_url_entry.focus()

    def _async_tally_probe(self):
        """Background: check Tally and surface one of three states clearly."""
        tally_url = self.tally_entry.get().strip() if hasattr(self, 'tally_entry') else DEFAULT_TALLY
        info = fetch_tally_company_info(tally_url)
        state = info.get("state")
        if state == "ok":
            self.tally_company_name = info["company_name"]
            self.tally_banner.config(
                text=f"✓ Tally connected — Company: {self.tally_company_name}",
                fg="#1f7a3a",
            )
        elif state == "no_company":
            self.tally_company_name = None
            self.tally_banner.config(
                text="⚠ Tally is running but no company is open. Open a company in Tally, then click 'Recheck Tally'.",
                fg="#b07a16",
                wraplength=480,
                justify="center",
            )
        else:  # 'unreachable'
            self.tally_company_name = None
            self.tally_banner.config(
                text=f"❌ No Tally detected at {tally_url}. Start TallyPrime, enable ODBC on port 9000, then click 'Recheck Tally'.",
                fg="#cc3333",
                wraplength=480,
                justify="center",
            )

    def _do_login(self):
        """POST /api/agent/auth with entered credentials."""
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        server_url = self.server_url_entry.get().strip().rstrip("/")
        tally_url = self.tally_entry.get().strip() or DEFAULT_TALLY

        if not username or not password:
            self.login_error.config(text="Please enter both username and password.")
            return
        if not server_url:
            self.login_error.config(text="Please pick a server.")
            return
        if not self.tally_company_name:
            self.login_error.config(
                text="Cannot authenticate without a Tally company open. Open a company in Tally, then click 'Recheck Tally'.",
                fg="#cc3333",
            )
            return

        self.login_error.config(text="Authenticating…", fg="#666666")
        self.root.update_idletasks()

        try:
            req = urllib.request.Request(
                f"{server_url}/api/agent/auth",
                data=json.dumps({"username": username, "password": password}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            if he.code == 401:
                self.login_error.config(text="Invalid username or password.", fg="#cc3333")
            else:
                self.login_error.config(text=f"Server error: {he.code}", fg="#cc3333")
            return
        except Exception as e:
            self.login_error.config(text=f"Could not reach server: {e}", fg="#cc3333")
            return

        # Success
        self.session_token = data["session_token"]
        self.user_id = data["user_id"]
        self.username = data["username"]
        self.user_name = data.get("name", username)
        self.memberships = data.get("memberships", [])
        # Durable device token — lets the agent auto-resume on boot without a password.
        # Persisted to config (passwords/session tokens are never written to disk).
        self.device_token = data.get("device_token")

        self.config["last_server_url"] = server_url
        self.config["last_username"] = username
        self.config["tally_url"] = tally_url
        if self.device_token:
            self.config["device_token"] = self.device_token
        save_config(self.config)

        self.root.unbind("<Return>")
        self.build_company_picker(server_url, tally_url)

    # --------------------------------------------------------
    # Step 2 — Company Picker
    # --------------------------------------------------------
    def build_company_picker(self, server_url, tally_url):
        for w in self.root.winfo_children():
            w.destroy()

        self._build_header(self.root, f"Welcome, {self.user_name}")

        outer = tk.Frame(self.root, bg=THEME["bg"], padx=22, pady=18)
        outer.pack(fill="both", expand=True)

        firm_names = ", ".join(m["org_name"] for m in self.memberships) or "(no firms)"
        tk.Label(outer, text=firm_names, bg=THEME["bg"], fg=THEME["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 12))

        # Tally company readout
        tally_banner_text = (
            f"📂 Tally company:  {self.tally_company_name}"
            if self.tally_company_name else
            "⚠ Tally not detected — using simulator company name"
        )
        tk.Label(outer, text=tally_banner_text, bg=THEME["bg"],
                 fg=(THEME["ok"] if self.tally_company_name else THEME["warn"]),
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 14))

        # Company picker card
        frame = tk.Frame(outer, bg=THEME["card"], highlightbackground=THEME["border"],
                         highlightthickness=1, padx=16, pady=16)
        frame.pack(fill="x", pady=(0, 12))
        tk.Label(frame, text="Push this Tally data to which YantrAI company?", bg=THEME["card"],
                 fg=THEME["text"], font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))

        self.company_choices = []
        for m in self.memberships:
            for c in m.get("companies", []):
                self.company_choices.append({
                    "label": f"{m['org_name']} — {c['name']}",
                    "company_id": c["id"],
                    "company_name": c["name"],
                    "org_name": m["org_name"],
                })

        if not self.company_choices:
            tk.Label(frame, text="(No companies linked to this account)", bg=THEME["card"], fg=THEME["err"]).pack(anchor="w")
        else:
            self.company_var = tk.StringVar(self.root)
            last_cid = self.config.get("last_company_id")
            default_label = self.company_choices[0]["label"]
            for ch in self.company_choices:
                if ch["company_id"] == last_cid:
                    default_label = ch["label"]
                    break
            self.company_var.set(default_label)
            ttk.Combobox(frame, textvariable=self.company_var, state="readonly",
                         values=[c["label"] for c in self.company_choices]).pack(fill="x")

        self.picker_error = tk.Label(outer, text="", bg=THEME["bg"], fg=THEME["err"], font=("Segoe UI", 9), wraplength=480)
        self.picker_error.pack(anchor="w", pady=(6, 0))

        btn_frame = tk.Frame(outer, bg=THEME["bg"])
        btn_frame.pack(fill="x", pady=(12, 0))
        ttk.Button(btn_frame, text="← Back", command=lambda: self.build_login_wizard()).pack(side="left")
        ttk.Button(btn_frame, text="Continue  →", style="Accent.TButton",
                   command=lambda: self._on_company_selected(server_url, tally_url)).pack(side="right")

    def _on_company_selected(self, server_url, tally_url):
        """User clicked Continue on company picker — enforce name match, then dashboard."""
        sel = self.company_var.get()
        chosen = next((c for c in self.company_choices if c["label"] == sel), None)
        if not chosen:
            self.picker_error.config(text="Please pick a company.")
            return

        # Client-side name match check
        if self.tally_company_name:
            def _norm(s):
                return " ".join((s or "").strip().lower().split())
            if _norm(self.tally_company_name) != _norm(chosen["company_name"]):
                # Show mismatch modal
                msg = (
                    f"Your Tally company is named '{self.tally_company_name}' but you "
                    f"selected '{chosen['company_name']}' in YantrAI.\n\n"
                    f"To fix: rename either the Tally company or the YantrAI company "
                    f"so they match exactly, then try again."
                )
                resp = messagebox.askyesnocancel(
                    "Company name mismatch",
                    msg + "\n\nOpen YantrAI Settings to rename the company there?"
                )
                if resp is True:
                    import webbrowser
                    webbrowser.open(server_url)
                return

        # Save context, advance to dashboard
        self.selected_company_id = chosen["company_id"]
        self.selected_company_name = chosen["company_name"]
        self.config["last_company_id"] = chosen["company_id"]
        self.config["last_server_url"] = server_url
        self.config["tally_url"] = tally_url
        # Store legacy config keys too so existing dashboard code works
        self.config["server_url"] = http_to_ws(server_url)
        self.config["token"] = chosen["company_id"]  # legacy field — kept for compatibility
        self.config["last_company_name"] = chosen["company_name"]
        # Enable Windows auto-start by default on first successful setup
        # (unless the user has explicitly turned it off before).
        if "autostart" not in self.config:
            self.config["autostart"] = True
            try: set_autostart(True)
            except Exception: pass
        save_config(self.config)

        # Bind the chosen company onto the durable device token so boot auto-resume
        # lands directly on this company (best-effort).
        if getattr(self, "device_token", None):
            try:
                _post_json(f"{server_url}/api/agent/device/bind", {
                    "device_token": self.device_token,
                    "company_id": chosen["company_id"],
                    "company_name": chosen["company_name"],
                }, timeout=10.0)
            except Exception:
                pass

        self._enter_running_state(server_url, tally_url)

    def _enter_running_state(self, server_url, tally_url):
        """Build the dashboard, start the tunnel, and launch the heartbeat/outbox
        background threads. Shared by first-time setup and boot auto-resume."""
        self.build_dashboard()
        self.start_tunnel()

        # Sprint 31 — start outbound (web → Tally) push pipeline in background.
        # Heartbeat keeps the web sidebar dot 🟢; poller picks up outbox rows
        # enqueued by /push-to-tally and writes them into Tally Prime.
        if not getattr(self, "_outbox_stop_event", None):
            self._outbox_stop_event = threading.Event()
            # Pass a LIVE token provider (lambda) + a self-heal callback, so the loops
            # always use the current session and recover from a 401 on their own.
            _token_provider = lambda: getattr(self, "session_token", None)
            threading.Thread(
                target=heartbeat_loop,
                args=(server_url, self.selected_company_name, self._outbox_stop_event,
                      _token_provider, self._refresh_session),
                daemon=True, name="yantrai-heartbeat",
            ).start()
            threading.Thread(
                target=outbox_poll_loop,
                args=(server_url, tally_url, self.selected_company_name,
                      self._outbox_stop_event, getattr(self, "log", None),
                      _token_provider, self._refresh_session),
                daemon=True, name="yantrai-outbox-poll",
            ).start()

    def _refresh_session(self):
        """Self-heal: re-mint a session via the durable device token (no password).
        Called by the heartbeat/poll loops when they hit a 401 — e.g. the server was
        redeployed and dropped the session. Rate-limited (~once/15s) so we don't hammer
        /api/agent/resume. Returns True if the session token was refreshed."""
        tok = self.config.get("device_token") or getattr(self, "device_token", None)
        if not tok:
            return False
        now = time.time()
        if now - getattr(self, "_last_session_refresh", 0) < 15:
            return False
        self._last_session_refresh = now
        server_url = (self.config.get("last_server_url") or "").rstrip("/")
        if not server_url:
            return False
        data = _post_json(f"{server_url}/api/agent/resume", {"device_token": tok}, timeout=20.0)
        if isinstance(data, dict) and data.get("session_token"):
            self.session_token = data["session_token"]
            try: self.log("Reconnected — session refreshed automatically.", "success")
            except Exception: pass
            return True
        return False

    # --------------------------------------------------------
    # Boot auto-resume (durable device token)
    # --------------------------------------------------------
    def _can_auto_resume(self):
        return bool(
            self.config.get("device_token")
            and self.config.get("last_server_url")
            and self.config.get("last_company_name")
        )

    def _try_auto_resume(self):
        """Background-thread attempt to resume the previous session via the durable
        device token. On success → dashboard + tunnel; on failure → login wizard.
        All Tk UI work is marshalled back to the main thread via root.after()."""
        server_url = (self.config.get("last_server_url") or "").rstrip("/")
        tally_url = self.config.get("tally_url") or DEFAULT_TALLY
        token = self.config.get("device_token")
        data = _post_json(f"{server_url}/api/agent/resume", {"device_token": token}, timeout=20.0)

        if not isinstance(data, dict) or data.get("_error") or not data.get("session_token"):
            # Revoked / network error / older server → fall back to the login screen.
            # Keep the device token in config so the next boot can retry.
            self.root.after(0, self.build_login_wizard)
            return

        def _finish():
            self.session_token = data["session_token"]
            self.user_id = data.get("user_id")
            self.username = data.get("username")
            self.user_name = data.get("name", self.username)
            self.memberships = data.get("memberships", [])
            self.device_token = token
            self.selected_company_id = data.get("company_id") or self.config.get("last_company_id")
            self.selected_company_name = data.get("company_name") or self.config.get("last_company_name")
            # Keep legacy config keys consistent for the dashboard/tunnel code.
            self.config["server_url"] = http_to_ws(server_url)
            if self.selected_company_id:
                self.config["token"] = self.selected_company_id
            save_config(self.config)
            self._enter_running_state(server_url, tally_url)
        self.root.after(0, _finish)

    def build_reconnecting_screen(self):
        """Lightweight splash shown while auto-resume runs, so the login form doesn't flash."""
        for w in self.root.winfo_children():
            w.destroy()
        self._build_header(self.root, "Reconnecting…")
        outer = tk.Frame(self.root, bg=THEME["bg"], padx=22, pady=40)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="🔄  Resuming your YantrAI connection…",
                 bg=THEME["bg"], fg=THEME["text"], font=("Segoe UI", 11, "bold")).pack(pady=(20, 8))
        tk.Label(outer, text="No need to sign in — restoring your saved session.",
                 bg=THEME["bg"], fg=THEME["muted"], font=("Segoe UI", 9)).pack()

    # --------------------------------------------------------
    # Main Dashboard
    # --------------------------------------------------------
    def build_dashboard(self):
        for w in self.root.winfo_children():
            w.destroy()

        self._build_header(self.root, "Two-way sync · running in the background")

        outer = tk.Frame(self.root, bg=THEME["bg"], padx=18, pady=14)
        outer.pack(fill="both", expand=True)

        # Status pill row
        pill_row = tk.Frame(outer, bg=THEME["bg"])
        pill_row.pack(fill="x", pady=(0, 12))
        self.status_label = tk.Label(pill_row, text="🔴 Disconnected", bg=THEME["card"], fg=THEME["text"],
                                     font=("Segoe UI", 10, "bold"), padx=12, pady=5)
        self.status_label.pack(side="left")
        if self.username and self.selected_company_name:
            tk.Label(pill_row, text=f"  {self.username} → {self.selected_company_name}",
                     bg=THEME["bg"], fg=THEME["muted"], font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))

        # Stat cards
        cards = tk.Frame(outer, bg=THEME["bg"])
        cards.pack(fill="x", pady=(0, 12))
        cards.columnconfigure((0, 1, 2), weight=1, uniform="c")

        def _stat_card(col, caption, value_attr, value_text, value_color=None):
            card = tk.Frame(cards, bg=THEME["card"], highlightbackground=THEME["border"], highlightthickness=1)
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
            tk.Label(card, text=caption, bg=THEME["card"], fg=THEME["muted"], font=("Segoe UI", 8)).pack(pady=(10, 0))
            lbl = tk.Label(card, text=value_text, bg=THEME["card"], fg=value_color or THEME["text"],
                           font=("Segoe UI", 12, "bold"), wraplength=150)
            lbl.pack(pady=(2, 10), padx=8)
            setattr(self, value_attr, lbl)

        _stat_card(0, "TALLY ERP", "tally_status_label", "Checking…", THEME["accent"])
        _stat_card(1, "SYNCED TODAY", "synced_label", "0", THEME["ok"])
        _stat_card(2, "PUSHING TO", "_company_card_label", self.selected_company_name or "—", THEME["primary_light"])

        # Activity log (dark console)
        tk.Label(outer, text="Activity Log", bg=THEME["bg"], fg=THEME["muted"], font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 4))
        self.log_text = scrolledtext.ScrolledText(outer, height=8, font=("Consolas", 9), state="disabled",
                                                  wrap="word", bg=THEME["console_bg"], fg=THEME["console_fg"],
                                                  insertbackground=THEME["console_fg"], relief="flat",
                                                  highlightbackground=THEME["border"], highlightthickness=1, bd=0)
        self.log_text.pack(fill="both", expand=True, pady=(0, 10))

        # Auto-start toggle
        self.autostart_var = tk.BooleanVar(value=self.config.get("autostart", is_autostart_enabled()))
        ttk.Checkbutton(outer, text="Start automatically on Windows (keep syncing in the background)",
                        variable=self.autostart_var, command=self._on_autostart_toggle).pack(anchor="w", pady=(0, 10))

        # Bottom buttons
        btn_row = tk.Frame(outer, bg=THEME["bg"])
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Test connection", command=self.test_tally).pack(side="left")
        ttk.Button(btn_row, text="Reset", command=self.reset_config).pack(side="left", padx=8)
        self.toggle_btn = ttk.Button(btn_row, text="Disconnect", style="Accent.TButton", command=self.toggle_connection)
        self.toggle_btn.pack(side="right")
        ttk.Button(btn_row, text="Sign out", command=self.sign_out).pack(side="right", padx=(0, 6))

        # start_tunnel is idempotent — caller (e.g. _on_company_selected) handles the actual launch

    def _on_autostart_toggle(self):
        enabled = bool(self.autostart_var.get())
        self.config["autostart"] = enabled
        try:
            set_autostart(enabled)
        except Exception as e:
            self.log(f"Auto-start change failed: {e}")
        save_config(self.config)
        self.log(f"Auto-start on Windows {'enabled' if enabled else 'disabled'}.")

    def sign_out(self):
        """Clear in-memory session and return to login wizard."""
        self.should_run = False
        self.is_connected = False
        # Drop the thread handle so next login spawns a fresh tunnel
        self.ws_thread = None
        # Revoke the durable device token server-side so the next boot won't auto-resume.
        _tok = self.config.get("device_token")
        if _tok:
            _srv = (self.config.get("last_server_url") or "").rstrip("/")
            try: _post_json(f"{_srv}/api/agent/device/revoke", {"device_token": _tok}, timeout=8.0)
            except Exception: pass
        self.session_token = None
        self.device_token = None
        self.user_id = None
        self.selected_company_id = None
        self.selected_company_name = None
        self.memberships = []
        # Reset the once-per-session baseline-sync gate so a fresh login
        # re-triggers a baseline pull (Sprint 41).
        self._baseline_done = False
        # Keep last_username and last_server_url in config for convenience
        self.config.pop("token", None)
        self.config.pop("device_token", None)
        save_config(self.config)
        self.build_login_wizard()

    # --------------------------------------------------------
    # Logging
    # --------------------------------------------------------
    def log(self, message, tag="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        def _do():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{ts}] {message}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _do)

    # --------------------------------------------------------
    # Status Updates
    # --------------------------------------------------------
    def set_status(self, connected):
        self.is_connected = connected
        def _do():
            if connected:
                self.status_label.configure(text="🟢 Connected")
                self.toggle_btn.configure(text="Disconnect")
            else:
                self.status_label.configure(text="🔴 Disconnected")
                self.toggle_btn.configure(text="Connect")
        self.root.after(0, _do)

    def set_tally_status(self, alive):
        def _do():
            if alive:
                self.tally_status_label.configure(text="Online ✓")
            else:
                self.tally_status_label.configure(text="Offline ✗")
        self.root.after(0, _do)

    def increment_synced(self):
        self.synced_count += 1
        def _do():
            self.synced_label.configure(text=str(self.synced_count))
        self.root.after(0, _do)

    # --------------------------------------------------------
    # Tunnel Connection Loops
    # --------------------------------------------------------
    def start_tunnel(self):
        # Idempotent: if a tunnel thread is already alive, just no-op.
        if self.ws_thread is not None and self.ws_thread.is_alive():
            return
        self.should_run = True
        self.ws_thread = threading.Thread(target=self._run_tunnel_loop, daemon=True)
        self.ws_thread.start()
        threading.Thread(target=self._check_tally, daemon=True).start()

    def _trigger_baseline_sync(self):
        """Call HTTP /tally/ingest so the server pulls a full baseline of Tally data.
        The server tunnels the request back through our active WS to fetch the data.
        """
        try:
            http_url = self.config.get("last_server_url") or "http://localhost:8000"
            self.log("Triggering baseline sync to YantrAI Cloud…", "info")
            req = urllib.request.Request(
                f"{http_url}/tally/ingest",
                data=json.dumps({
                    "session_token": self.session_token,
                    "company_id": self.selected_company_id,
                    "company_name": self.selected_company_name,  # back-compat
                    "username": self.username,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.loads(r.read().decode("utf-8"))
            counts = resp.get("counts") or {}
            v = counts.get("vouchers") or resp.get("vouchers_count") or "?"
            l = counts.get("ledgers") or resp.get("ledger_count") or "?"
            self.log(f"✓ Baseline sync complete: {v} vouchers, {l} ledgers pushed.", "success")
        except urllib.error.HTTPError as he:
            try:
                body = he.read().decode("utf-8")[:300]
            except Exception:
                body = ""
            self.log(f"❌ Baseline sync failed (HTTP {he.code}): {body}", "error")
        except Exception as e:
            self.log(f"❌ Baseline sync error: {e}", "error")

    def _check_tally(self):
        tally_url = self.config.get("tally_url", DEFAULT_TALLY)
        alive = check_tally_alive(tally_url)
        self.set_tally_status(alive)
        if alive:
            self.log("Local Tally ERP online & responding.", "success")
        else:
            self.log(f"Tally not detected at {tally_url}. Using simulator.", "warn")

    def _run_tunnel_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._tunnel_loop())

    async def _tunnel_loop(self):
        import websockets

        # Derive WS URL from selected server (already normalized in config)
        http_url = self.config.get("last_server_url") or "http://localhost:8000"
        server_url = http_to_ws(http_url)
        tally_url = self.config.get("tally_url", DEFAULT_TALLY)
        backoff = 1

        while self.should_run:
            try:
                self.log(f"Connecting to {http_url} …", "info")
                async with websockets.connect(server_url, ping_interval=300, ping_timeout=300, max_size=50*1024*1024) as ws:
                    backoff = 1
                    # Phase B handshake — authenticated
                    await ws.send(json.dumps({
                        "session_token": self.session_token,
                        "company_id": self.selected_company_id,
                        "tally_company_name": self.tally_company_name or self.selected_company_name,
                    }))

                    # Wait for the server's ack
                    ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    ack = json.loads(ack_raw)
                    if ack.get("status") != "ok":
                        code = ack.get("code", "?")
                        msg = ack.get("message", "Auth failed")
                        self.log(f"❌ Connection rejected ({code}): {msg}", "error")
                        self.set_status(False)
                        # Stop loop — user needs to re-auth
                        self.should_run = False
                        # Drop back to login screen on the UI thread
                        self.root.after(0, self.build_login_wizard)
                        return

                    self.set_status(True)
                    self.log(f"Secure tunnel active for {self.selected_company_name}. Ready for sync.", "success")

                    # Sprint 41 — auto-baseline sync is RE-ENABLED, but safer this time:
                    #   1. Only fires once per process lifetime (`_baseline_done`), so
                    #      reconnects after a flaky tunnel don't keep re-triggering it.
                    #   2. The server now decides full vs incremental from per-entity
                    #      watermarks — within 7 days of a successful full sync it sends
                    #      since_voucher_alterid > 0 and the fetch_vouchers TDL filter
                    #      returns only changed rows (typically tens, not thousands).
                    #   3. The pull runs under the `_sync_in_progress` Event so the
                    #      outbox pusher pauses for the duration — no concurrent
                    #      Tally writes (which is what crashed JMK in Sprint 36).
                    if not getattr(self, "_baseline_done", False):
                        self._baseline_done = True
                        threading.Thread(
                            target=self._trigger_baseline_sync,
                            name="auto-baseline-sync",
                            daemon=True,
                        ).start()

                    while self.should_run:
                        msg_str = await ws.recv()
                        msg = json.loads(msg_str)
                        req_id = msg.get("request_id")
                        cmd_type = msg.get("type")
                        data = msg.get("data")

                        self.log(f"Received sync command: {cmd_type}", "info")
                        response = {"request_id": req_id, "status": "success"}

                        if cmd_type == "get_ledgers":
                            ledgers = fetch_local_ledgers(tally_url)
                            info = fetch_tally_company_info(tally_url)
                            tally_company = info.get("company_name") or self.selected_company_name or "Unknown"
                            info["pan"] = info.get("pan") or ""
                            response["ledgers"] = ledgers
                            response["tally_company_name"] = tally_company
                            response["pan"] = info["pan"]
                            self.log(f"Fetched {len(ledgers)} ledgers from '{tally_company}' (PAN: {info['pan']}).", "success")
                            self.increment_synced()

                        elif cmd_type == "get_summary":
                            ledgers = fetch_local_ledgers(tally_url)
                            info = fetch_tally_company_info(tally_url)
                            tally_company = info.get("company_name") or self.selected_company_name or "Unknown"
                            info["pan"] = info.get("pan") or ""
                            response["tally_company_name"] = tally_company
                            response["pan"] = info["pan"]
                            response["ledger_count"] = len(ledgers)
                            response["active_ledgers"] = ledgers
                            response["synced_today"] = self.synced_count
                            self.log(f"Transmitted Tally summary for '{tally_company}'.", "success")

                        elif cmd_type == "seed_baseline":
                            # Per-entity incremental sync (Sprint 41).
                            # The server tracks 4 AlterId watermarks (vouchers, ledgers, groups,
                            # stock_items) and a `last_full_sync_at` timestamp. On every
                            # /tally/ingest it decides:
                            #   • full pull  → since_* = 0 for every entity AND `full` = True
                            #     (sent when the watermark is stale > 7 days or absent)
                            #   • incremental → since_* = last-seen AlterId per entity, `full` = False
                            # Legacy clients (server v < 230) only send `since_alter_id` — treat that
                            # as the voucher since and leave master tables on full pull.
                            _data = data or {}
                            full_pull = bool(_data.get("full"))
                            def _int_since(key, fallback=0):
                                try:
                                    return int(_data.get(key) or fallback)
                                except Exception:
                                    return fallback
                            legacy_since = _int_since("since_alter_id", 0)
                            since_v = _int_since("since_voucher_alterid", legacy_since)
                            since_l = _int_since("since_ledger_alterid", 0)
                            since_g = _int_since("since_group_alterid", 0)
                            since_s = _int_since("since_stock_alterid", 0)
                            # If server didn't say `full` explicitly, infer it from since values.
                            if "full" not in _data:
                                full_pull = (since_v == 0 and since_l == 0
                                             and since_g == 0 and since_s == 0)

                            # Phase separation — don't let an outbox push race a baseline pull
                            # for Tally's single-threaded HTTP server.
                            _sync_in_progress.set()
                            try:
                                mode = "full" if full_pull else "incremental"
                                self.log(
                                    f"Starting {mode} data pull from Tally "
                                    f"(voucher>{since_v}, ledger>{since_l}, "
                                    f"group>{since_g}, stock>{since_s})...",
                                    "info",
                                )
                                info = fetch_tally_company_info(tally_url)
                                tally_company = info.get("company_name") or self.selected_company_name or "Unknown"
                                info["pan"] = info.get("pan") or ""
                                self.log(f"Company: {tally_company} (PAN: {info['pan']})", "info")

                                rich_ledgers = fetch_rich_ledgers(tally_url, since_alter_id=since_l)
                                groups = fetch_groups(tally_url, since_alter_id=since_g)
                                vouchers = fetch_vouchers(tally_url, since_alter_id=since_v)
                                stock_items = fetch_stock_items(tally_url, since_alter_id=since_s)
                            finally:
                                _sync_in_progress.clear()

                            self.log(f"Pulled {len(rich_ledgers)} ledgers, {len(groups)} groups, "
                                     f"{len(vouchers)} vouchers ({mode}), {len(stock_items)} stock items.", "info")

                            # Per-entity max AlterIds → server advances each watermark independently.
                            max_v = max((int(v.get("alterid") or 0) for v in vouchers), default=since_v)
                            max_l = max((int(x.get("alter_id") or 0) for x in rich_ledgers), default=since_l)
                            max_g = max((int(x.get("alter_id") or 0) for x in groups), default=since_g)
                            max_s = max((int(x.get("alter_id") or 0) for x in stock_items), default=since_s)

                            response["tally_company_name"] = tally_company
                            response["pan"] = info["pan"]
                            response["ledgers"] = rich_ledgers
                            response["groups"] = groups
                            response["vouchers"] = vouchers
                            response["stock_items"] = stock_items
                            response["ledger_count"] = len(rich_ledgers)
                            response["voucher_count"] = len(vouchers)
                            response["group_count"] = len(groups)
                            response["stock_count"] = len(stock_items)
                            response["incremental"] = not full_pull
                            response["full"] = full_pull
                            # Per-entity watermarks (new schema).
                            response["max_voucher_alterid"] = max_v
                            response["max_ledger_alterid"] = max_l
                            response["max_group_alterid"] = max_g
                            response["max_stock_alterid"] = max_s
                            # Legacy single field kept for any older server build.
                            response["max_alter_id"] = max_v

                            # On a full pull collect live GUIDs per master entity so the server
                            # can soft-delete rows that no longer exist in Tally. Skipped on
                            # incrementals — a partial fetch cannot prove absence.
                            if full_pull:
                                response["live_guids"] = {
                                    "ledgers":     [x.get("guid") for x in rich_ledgers if x.get("guid")],
                                    "groups":      [x.get("guid") for x in groups       if x.get("guid")],
                                    "stock_items": [x.get("guid") for x in stock_items  if x.get("guid")],
                                }

                            self.log(
                                f"{mode.capitalize()} seed complete "
                                f"(max AlterIds  v={max_v} l={max_l} g={max_g} s={max_s}).",
                                "success",
                            )
                            self.increment_synced()

                        elif cmd_type == "create_voucher":
                            xml = build_voucher_xml(data)
                            result = query_local_tally(tally_url, xml)
                            response["xml_response"] = result
                            party = data.get("party", "Unknown")
                            amt = data.get("amount", 0)
                            self.log(f"Voucher posted to local ledger: {party} — ₹{amt}", "success")
                            self.increment_synced()

                        elif cmd_type == "create_company":
                            # Sprint 42 — create a new company inside the user's local Tally
                            # Prime. One-shot import (no outbox). Required fields validated
                            # both client-side (in the UI) and here.
                            _d = data or {}
                            _name = (_d.get("name") or "").strip()
                            _state = (_d.get("state") or "").strip()
                            _bf = (_d.get("books_from") or "").strip()
                            if not _name or not _state or not _bf:
                                response["status"] = "error"
                                response["message"] = (
                                    "Missing required field(s): name + state + books_from."
                                )
                                self.log("create_company rejected — missing fields.", "error")
                            else:
                                # Phase separation — block the outbox pusher while we hold the
                                # Tally HTTP server. Company creation is the heaviest write
                                # Tally takes; co-running a voucher push is asking for c0000005.
                                _sync_in_progress.set()
                                try:
                                    self.log(
                                        f"Building create-company XML for '{_name}' "
                                        f"(state={_state}, books_from={_bf})…",
                                        "info",
                                    )
                                    xml = build_create_company_xml(_d)
                                    self.log("Posting create-company to Tally Prime…", "info")
                                    result = query_local_tally(tally_url, xml, timeout=30.0)
                                finally:
                                    _sync_in_progress.clear()

                                ok, err = parse_create_company_response(result)
                                response["xml_response"] = result
                                if ok:
                                    response["status"] = "success"
                                    response["created_company"] = _name
                                    self.log(
                                        f"✓ Tally Prime created company '{_name}'.",
                                        "success",
                                    )
                                    self.increment_synced()
                                else:
                                    response["status"] = "error"
                                    response["message"] = err or "Tally rejected create_company"
                                    self.log(
                                        f"✗ create_company failed: {response['message']}",
                                        "error",
                                    )

                        else:
                            response["status"] = "error"
                            response["message"] = f"Unknown command: {cmd_type}"
                            self.log(f"Unknown command received.", "error")

                        await ws.send(json.dumps(response))

            except Exception as e:
                self.set_status(False)
                if self.should_run:
                    self.log(f"Tunnel connection lost: {e}", "error")
                    self.log(f"Reconnecting in {backoff}s...", "warn")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)

    def stop_tunnel(self):
        self.should_run = False
        self.set_status(False)
        self.log("Tunnel disconnected.", "warn")

    def toggle_connection(self):
        if self.is_connected or self.should_run:
            self.stop_tunnel()
        else:
            self.start_tunnel()

    def test_tally(self):
        tally_url = self.config.get("tally_url", DEFAULT_TALLY)
        self.log(f"Testing local Tally connection...", "info")
        def _test():
            alive = check_tally_alive(tally_url)
            self.set_tally_status(alive)
            if alive:
                self.log("Tally ERP test PASSED ✓", "success")
            else:
                self.log("Tally ERP test FAILED. Check port 9000 settings.", "error")
        threading.Thread(target=_test, daemon=True).start()

    def reset_config(self):
        if messagebox.askyesno("Reset", "Return to setup wizard?\n\nThis will clear current tokens."):
            self.stop_tunnel()
            # Revoke the durable device token server-side before wiping local config.
            _tok = self.config.get("device_token")
            if _tok:
                _srv = (self.config.get("last_server_url") or "").rstrip("/")
                try: _post_json(f"{_srv}/api/agent/device/revoke", {"device_token": _tok}, timeout=8.0)
                except Exception: pass
            if os.path.exists(CONFIG_FILE):
                try:
                    os.remove(CONFIG_FILE)
                except:
                    pass
            self.config = {}
            self.device_token = None
            self.synced_count = 0
            self.build_login_wizard()

    def on_close(self):
        # X button → keep running in the background (tray), don't quit.
        # If the tray isn't available (no pystray), fall back to a real quit.
        if self.tray_icon is not None:
            self._hide_to_tray()
        else:
            self.quit_app()

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    _autostart = ("--autostart" in sys.argv) or ("--minimized" in sys.argv)
    app = TallyBridgeApp(start_minimized=_autostart)
    app.run()
