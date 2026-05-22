"""
Headless driver — does exactly what the Tally agent GUI does, end-to-end.
1. Auth as rahul
2. Open WebSocket with proper handshake
3. Trigger /tally/ingest (which loops back through WS)
4. Wait for sync + embedding to complete
5. Verify DB state
"""
import asyncio
import json
import time
import urllib.request
import urllib.error

import websockets
import db


SERVER = "http://localhost:8000"
WS = "ws://localhost:8000/tally/ws"
TALLY = "http://localhost:9000"
USERNAME = "rahul"
PASSWORD = "rahul"
TARGET_COMPANY = "Jai Mata Kalka Enterprises"


def step(s):
    print(f"\n{'=' * 70}\n{s}\n{'=' * 70}")


def auth():
    step("STEP 1 — Authenticate")
    req = urllib.request.Request(
        f"{SERVER}/api/agent/auth",
        data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    print(f"  user: {data['username']}  user_id: {data['user_id']}")
    company = next(
        c for m in data["memberships"] for c in m["companies"] if c["name"] == TARGET_COMPANY
    )
    print(f"  target company: {company['name']}  id: {company['id']}")
    return data["session_token"], company["id"], company["name"]


def fetch_tally_company_name():
    """Probe local Tally for actual company name."""
    xml = """<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export Data</TALLYREQUEST><TYPE>Collection</TYPE><ID>CompanyCol</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME="CompanyCol"><TYPE>Company</TYPE><FETCH>Name</FETCH></COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"""
    req = urllib.request.Request(TALLY, data=xml.encode(),
                                 headers={"Content-Type": "text/xml; charset=utf-8"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read().decode()
    import re
    m = re.search(r'<COMPANY NAME="([^"]+)"', body)
    return m.group(1).strip() if m else None


def db_snapshot(company_id, label):
    conn = db.get_conn()
    cur = conn.cursor(cursor_factory=db.RealDictCursor)
    snap = {}
    for tbl in ["tally_vouchers", "tally_ledgers", "tally_groups", "tally_stock_items"]:
        cur.execute(f"SELECT COUNT(*) AS n FROM {tbl} WHERE company_id = %s", (company_id,))
        snap[tbl] = cur.fetchone()["n"]
    cur.execute(
        "SELECT COUNT(*) AS n FROM knowledge_base WHERE data->>'company_id' = %s AND type LIKE 'tally_master_%%'",
        (str(company_id),),
    )
    snap["embeddings"] = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM tally_ledgers WHERE company_id = %s AND is_sensitive = TRUE", (company_id,))
    snap["sensitive_ledgers"] = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM tally_vouchers WHERE company_id = %s AND cost_centres IS NOT NULL AND cost_centres::text != '[]'", (company_id,))
    snap["vouchers_with_cc"] = cur.fetchone()["n"]
    cur.close()
    conn.close()
    print(f"  [{label}] {snap}")
    return snap


async def ws_handshake_and_listen(session_token, company_id, tally_company_name, done_event):
    """Open WS to the server. This is what the agent does — server uses this
    connection to pull data when /tally/ingest is fired."""
    async with websockets.connect(WS, ping_interval=20, ping_timeout=10) as ws:
        # Handshake
        await ws.send(json.dumps({
            "session_token": session_token,
            "company_id": company_id,
            "tally_company_name": tally_company_name,
        }))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        print(f"  WS handshake ack: {ack}")
        if ack.get("status") != "ok":
            raise RuntimeError(f"WS rejected: {ack}")

        # Listen for commands from server (this is where /tally/ingest's dispatch arrives)
        # We need to import the agent's fetcher functions to respond to the seed_baseline command.
        # Easiest: spawn a subprocess of fetcher commands? No — just inline the logic.

        from tally_bridge_agent import (
            fetch_rich_ledgers, fetch_groups, fetch_vouchers,
            fetch_stock_items, fetch_tally_company_info,
        )

        while not done_event.is_set():
            try:
                msg_str = await asyncio.wait_for(ws.recv(), timeout=120)
            except asyncio.TimeoutError:
                if done_event.is_set():
                    break
                continue

            msg = json.loads(msg_str)
            req_id = msg.get("request_id")
            cmd_type = msg.get("type")
            print(f"  ← Server sent cmd: {cmd_type} ({req_id})")

            response = {"request_id": req_id, "status": "success"}

            if cmd_type == "seed_baseline":
                print("    Pulling fresh data from Tally…")
                info = fetch_tally_company_info(TALLY)
                tally_company = info.get("company_name") or tally_company_name
                response["tally_company_name"] = tally_company
                response["pan"] = info.get("pan") or ""

                rich_ledgers = fetch_rich_ledgers(TALLY)
                groups = fetch_groups(TALLY)
                vouchers = fetch_vouchers(TALLY)
                stock_items = fetch_stock_items(TALLY)

                response["ledgers"] = rich_ledgers
                response["groups"] = groups
                response["vouchers"] = vouchers
                response["stock_items"] = stock_items
                response["ledger_count"] = len(rich_ledgers)
                response["voucher_count"] = len(vouchers)
                response["group_count"] = len(groups)
                response["stock_count"] = len(stock_items)
                print(f"    Sending: {len(rich_ledgers)} ledgers, {len(groups)} groups, {len(vouchers)} vouchers, {len(stock_items)} stock items")

            else:
                response = {"request_id": req_id, "status": "error", "message": f"Unknown cmd {cmd_type}"}

            await ws.send(json.dumps(response))


def fire_ingest(session_token, company_id, tally_company_name):
    """Fire HTTP POST /tally/ingest — this triggers the server to dispatch
    a seed_baseline command back through the WS."""
    req = urllib.request.Request(
        f"{SERVER}/tally/ingest",
        data=json.dumps({
            "session_token": session_token,
            "company_id": company_id,
            "company_name": tally_company_name,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


async def drive():
    # Step 0: probe Tally
    step("STEP 0 — Probe local Tally")
    tally_company = fetch_tally_company_name()
    if not tally_company:
        print("  ❌ Tally is not running or no company open. Aborting.")
        return
    print(f"  ✓ Tally is open: {tally_company}")

    # Step 1: auth
    session_token, company_id, company_name = auth()

    if tally_company.lower().strip() != company_name.lower().strip():
        print(f"  ⚠️ Tally company '{tally_company}' ≠ YantrAI '{company_name}' — server will reject")
        return

    step("STEP 2 — Pre-sync DB snapshot")
    before = db_snapshot(company_id, "BEFORE")

    step("STEP 3 — Open WS + trigger /tally/ingest")
    done_event = asyncio.Event()

    async def ws_task():
        try:
            await ws_handshake_and_listen(session_token, company_id, tally_company, done_event)
        except Exception as e:
            print(f"  WS task error: {e}")

    ws_handle = asyncio.create_task(ws_task())
    # Give WS a moment to register
    await asyncio.sleep(1.0)

    # Fire ingest in a thread (it blocks until WS round-trip completes)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        ingest_result = await asyncio.get_event_loop().run_in_executor(
            pool, fire_ingest, session_token, company_id, tally_company
        )
    print(f"  /tally/ingest returned: status={ingest_result.get('status')}")

    # Tell WS task we're done
    done_event.set()
    try:
        await asyncio.wait_for(ws_handle, timeout=3)
    except asyncio.TimeoutError:
        ws_handle.cancel()

    step("STEP 4 — Post-sync DB snapshot (immediate)")
    immediate = db_snapshot(company_id, "AFTER SAVE")

    step("STEP 5 — Wait for background embeddings to populate")
    # Embeddings run in a background thread on the server. Poll every 10s.
    last = immediate["embeddings"]
    stable_count = 0
    for i in range(30):  # up to 5 minutes
        await asyncio.sleep(10)
        snap = db_snapshot(company_id, f"poll {i+1}")
        if snap["embeddings"] == last:
            stable_count += 1
            if stable_count >= 3 and snap["embeddings"] > 0:
                print("  ✓ Embeddings count stable for 30s — assuming complete")
                break
        else:
            stable_count = 0
        last = snap["embeddings"]

    step("FINAL")
    final = db_snapshot(company_id, "FINAL")
    print()
    print("Δ since BEFORE:")
    for k in final:
        print(f"  {k:22s} {before[k]} → {final[k]}  (Δ {final[k] - before[k]:+d})")


if __name__ == "__main__":
    asyncio.run(drive())
