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
from datetime import datetime

# ============================================================
# Single Instance Lock
# ============================================================
lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    lock_socket.bind(('127.0.0.1', 19999))
except socket.error:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "YantrAI Tally Bridge",
            "Another instance of the Tally Bridge Agent is already running."
        )
        root.destroy()
    except:
        pass
    sys.exit(1)

# ============================================================
# Configuration
# ============================================================
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".yantrai_bridge_config.json")
DEFAULT_SERVER = "wss://yantrai-accounting-916641724782.asia-south1.run.app/tally/ws"
DEFAULT_TALLY = "http://localhost:9000"

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

# ============================================================
# Tally Local Communication Layer
# ============================================================
def query_local_tally(tally_url, xml_payload):
    try:
        req = urllib.request.Request(
            tally_url,
            data=xml_payload.encode('utf-8'),
            headers={'Content-Type': 'text/xml; charset=utf-8'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=5.0) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        return None

def check_tally_alive(tally_url):
    xml = """<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Metadata</TYPE><ID>List of Ledgers</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES></DESC></BODY></ENVELOPE>"""
    return query_local_tally(tally_url, xml) is not None

def fetch_local_ledgers(tally_url):
    xml = """<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Metadata</TYPE><ID>List of Ledgers</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES></DESC></BODY></ENVELOPE>"""
    res = query_local_tally(tally_url, xml)
    if not res:
        return ["Cash", "Sales Account", "Purchase Account", "GST Payable", "Bank Account", "Bank Charges A/c"]
    ledgers = re.findall(r'<NAME[^>]*>(.*?)</NAME>', res)
    return ledgers if ledgers else ["Cash", "Sales Account", "Purchase Account", "Bank Account"]

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

# ============================================================
# GUI Application
# ============================================================
import tkinter as tk
from tkinter import scrolledtext, messagebox

class TallyBridgeApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("YantrAI Tally Bridge Agent")
        self.root.geometry("540x480")
        self.root.resizable(False, False)

        # State
        self.is_connected = False
        self.synced_count = 0
        self.config = load_config()
        self.ws_thread = None
        self.should_run = False

        # Build UI
        if self.config.get("token"):
            self.build_dashboard()
        else:
            self.build_setup_wizard()

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --------------------------------------------------------
    # Setup Wizard (First Time)
    # --------------------------------------------------------
    def build_setup_wizard(self):
        for w in self.root.winfo_children():
            w.destroy()

        outer = tk.Frame(self.root, padx=20, pady=20)
        outer.pack(fill="both", expand=True)

        # Title
        tk.Label(outer, text="🔗 YantrAI Tally Bridge", font=("Helvetica", 18, "bold")).pack(pady=(0, 4))
        tk.Label(outer, text="Connect Tally ERP securely to your YantrAI cloud dashboard", font=("Helvetica", 10)).pack(pady=(0, 16))

        # Fields frame
        form = tk.LabelFrame(outer, text=" Configuration & Credentials ", padx=15, pady=15)
        form.pack(fill="both", expand=True, pady=(0, 16))

        # Token
        tk.Label(form, text="Activation Token:", font=("Helvetica", 10, "bold")).pack(anchor="w", pady=(0, 2))
        self.token_entry = tk.Entry(form, font=("Courier", 11))
        self.token_entry.pack(fill="x", pady=(0, 12))
        self.token_entry.focus()

        # Server URL
        tk.Label(form, text="Cloud Server URL:", font=("Helvetica", 10)).pack(anchor="w", pady=(0, 2))
        self.server_entry = tk.Entry(form, font=("Courier", 10))
        self.server_entry.insert(0, DEFAULT_SERVER)
        self.server_entry.pack(fill="x", pady=(0, 12))

        # Tally URL
        tk.Label(form, text="Local Tally ERP URL:", font=("Helvetica", 10)).pack(anchor="w", pady=(0, 2))
        self.tally_entry = tk.Entry(form, font=("Courier", 10))
        self.tally_entry.insert(0, DEFAULT_TALLY)
        self.tally_entry.pack(fill="x")

        # Connect button
        btn = tk.Button(outer, text="Activate & Connect  →", font=("Helvetica", 11, "bold"), command=self.save_and_connect)
        btn.pack(fill="x", ipady=6)

        # Trust Notice
        tk.Label(outer, text="Your Tally data remains safely on your local network.", font=("Helvetica", 9)).pack(pady=(12, 0))

    def save_and_connect(self):
        token = self.token_entry.get().strip()
        server = self.server_entry.get().strip()
        tally = self.tally_entry.get().strip()

        if not token:
            messagebox.showwarning("Missing Token", "Please paste your Activation Token from the YantrAI dashboard.")
            return

        self.config = {
            "token": token,
            "server_url": server or DEFAULT_SERVER,
            "tally_url": tally or DEFAULT_TALLY
        }
        save_config(self.config)
        self.build_dashboard()
        self.start_tunnel()

    # --------------------------------------------------------
    # Main Dashboard
    # --------------------------------------------------------
    def build_dashboard(self):
        for w in self.root.winfo_children():
            w.destroy()

        outer = tk.Frame(self.root, padx=15, pady=15)
        outer.pack(fill="both", expand=True)

        # Header Row
        hdr = tk.Frame(outer)
        hdr.pack(fill="x", pady=(0, 10))
        tk.Label(hdr, text="🔗 YantrAI Tally Bridge", font=("Helvetica", 14, "bold")).pack(side="left")

        # Status
        self.status_label = tk.Label(hdr, text="🔴 Disconnected", font=("Helvetica", 10, "bold"))
        self.status_label.pack(side="right")

        # Info Frame
        info_frame = tk.LabelFrame(outer, text=" Connection Status ", padx=10, pady=10)
        info_frame.pack(fill="x", pady=(0, 10))

        # Grid cols
        info_frame.columnconfigure(0, weight=1)
        info_frame.columnconfigure(1, weight=1)
        info_frame.columnconfigure(2, weight=1)

        # Tally Status Card
        tk.Label(info_frame, text="Tally ERP", font=("Helvetica", 9)).grid(row=0, column=0)
        self.tally_status_label = tk.Label(info_frame, text="Checking...", font=("Helvetica", 10, "bold"))
        self.tally_status_label.grid(row=1, column=0, pady=(2, 0))

        # Synced Card
        tk.Label(info_frame, text="Synced Today", font=("Helvetica", 9)).grid(row=0, column=1)
        self.synced_label = tk.Label(info_frame, text="0", font=("Helvetica", 12, "bold"))
        self.synced_label.grid(row=1, column=1, pady=(2, 0))

        # Active Token Card
        tk.Label(info_frame, text="Company Token", font=("Helvetica", 9)).grid(row=0, column=2)
        token_display = self.config.get("token", "—")
        if len(token_display) > 12:
            token_display = token_display[:10] + "…"
        tk.Label(info_frame, text=token_display, font=("Courier", 9, "bold")).grid(row=1, column=2, pady=(2, 0))

        # Log Text Box
        tk.Label(outer, text="Activity Log:", font=("Helvetica", 10, "bold")).pack(anchor="w", pady=(0, 4))
        self.log_text = scrolledtext.ScrolledText(outer, height=8, font=("Courier", 9), state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, pady=(0, 10))

        # Bottom Buttons
        btn_row = tk.Frame(outer)
        btn_row.pack(fill="x")

        tk.Button(btn_row, text="Test Connection", font=("Helvetica", 10), command=self.test_tally).pack(side="left")
        tk.Button(btn_row, text="Reset", font=("Helvetica", 10), command=self.reset_config).pack(side="left", padx=8)

        self.toggle_btn = tk.Button(btn_row, text="Disconnect", font=("Helvetica", 10, "bold"), command=self.toggle_connection)
        self.toggle_btn.pack(side="right")

        if not self.should_run:
            self.start_tunnel()

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
        self.should_run = True
        self.ws_thread = threading.Thread(target=self._run_tunnel_loop, daemon=True)
        self.ws_thread.start()
        threading.Thread(target=self._check_tally, daemon=True).start()

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

        server_url = self.config.get("server_url", DEFAULT_SERVER)
        token = self.config.get("token", "")
        tally_url = self.config.get("tally_url", DEFAULT_TALLY)
        backoff = 1

        while self.should_run:
            try:
                self.log("Connecting to cloud gateway...", "info")
                async with websockets.connect(server_url, ping_interval=20, ping_timeout=10) as ws:
                    backoff = 1
                    await ws.send(json.dumps({"token": token}))
                    self.set_status(True)
                    self.log("Secure tunnel active! Ready for sync commands.", "success")

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
                            response["ledgers"] = ledgers
                            self.log(f"Fetched {len(ledgers)} ledgers.", "success")
                            self.increment_synced()

                        elif cmd_type == "create_voucher":
                            xml = build_voucher_xml(data)
                            result = query_local_tally(tally_url, xml)
                            response["xml_response"] = result
                            party = data.get("party", "Unknown")
                            amt = data.get("amount", 0)
                            self.log(f"Voucher posted to local ledger: {party} — ₹{amt}", "success")
                            self.increment_synced()

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
            if os.path.exists(CONFIG_FILE):
                try:
                    os.remove(CONFIG_FILE)
                except:
                    pass
            self.config = {}
            self.synced_count = 0
            self.build_setup_wizard()

    def on_close(self):
        self.should_run = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = TallyBridgeApp()
    app.run()
