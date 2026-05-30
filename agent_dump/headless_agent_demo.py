"""
Headless Tally agent — runs the exact same auth + sync + WS flow as the GUI agent,
but without the Tkinter window. Used to verify Sprint 1 end-to-end:
  1. POST /api/agent/auth (rahul / rahul)
  2. Open WS /tally/ws with session_token + company_id + tally_company_name
  3. Auto-trigger /tally/ingest, which loops back through the WS asking for data
  4. Respond to seed_baseline by pulling real data from local Tally via the
     same fetch_* functions the GUI agent uses
  5. Print sync results
"""
import os
import asyncio
import json
import sys
import threading
import urllib.request

import websockets

# Reuse the agent's Tally fetch + cleaning logic
from tally_bridge_agent import (
    fetch_tally_company_info,
    fetch_rich_ledgers,
    fetch_groups,
    fetch_vouchers,
    fetch_stock_items,
    fetch_local_ledgers,
)


SERVER_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000/tally/ws"
TALLY_URL = "http://localhost:9000"
USERNAME = os.environ.get("YANTRAI_TEST_USERNAME", "rahul")
# Sprint 40 — no committed password. Set YANTRAI_TEST_PASSWORD in your shell to run this demo.
PASSWORD = os.environ.get("YANTRAI_TEST_PASSWORD")
if not PASSWORD:
    raise RuntimeError("Set YANTRAI_TEST_PASSWORD env var before running headless_agent_demo.py.")
TARGET_COMPANY = "Jai Mata Kalka Enterprises"


def auth():
    req = urllib.request.Request(
        f"{SERVER_URL}/api/agent/auth",
        data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def pick_company(auth_resp):
    for m in auth_resp["memberships"]:
        for c in m["companies"]:
            if c["name"] == TARGET_COMPANY:
                return c["id"], m["org_name"]
    raise RuntimeError(f"{TARGET_COMPANY!r} not in user's memberships")


def trigger_ingest(session_token, company_id):
    print("→ POST /tally/ingest …", flush=True)
    try:
        req = urllib.request.Request(
            f"{SERVER_URL}/tally/ingest",
            data=json.dumps({
                "session_token": session_token,
                "company_id": company_id,
                "company_name": TARGET_COMPANY,
                "username": USERNAME,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            print("← /tally/ingest:", r.read().decode()[:400], flush=True)
    except Exception as e:
        print(f"× /tally/ingest error: {e}", flush=True)


async def run():
    info = fetch_tally_company_info(TALLY_URL)
    print(f"Local Tally: state={info['state']} company={info.get('company_name')}")
    if info["state"] != "ok":
        print("ERROR: Tally not ready"); sys.exit(1)
    tally_company_name = info["company_name"]

    auth_resp = auth()
    if auth_resp.get("status") != "success":
        print("Auth failed:", auth_resp); sys.exit(1)
    session_token = auth_resp["session_token"]
    user_id = auth_resp["user_id"]
    print(f"✓ Authed as {USERNAME} -> user_id={user_id[:8]}…")

    company_id, org_name = pick_company(auth_resp)
    print(f"✓ Selected {TARGET_COMPANY!r} ({company_id}) under '{org_name}'")

    async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=None, max_size=50*1024*1024) as ws:
        # Phase B handshake
        await ws.send(json.dumps({
            "session_token": session_token,
            "company_id": company_id,
            "tally_company_name": tally_company_name,
        }))
        ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
        ack = json.loads(ack_raw)
        if ack.get("status") != "ok":
            print(f"✗ WS handshake rejected: {ack}"); return
        print(f"✓ WS handshake OK")

        # Fire HTTP /tally/ingest in a separate thread (server tunnels back via THIS ws)
        threading.Thread(target=trigger_ingest, args=(session_token, company_id), daemon=True).start()

        # Process server commands until ingest finishes
        seed_done = False
        try:
            while not seed_done:
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=300)
                msg = json.loads(msg_raw)
                req_id = msg.get("request_id")
                cmd_type = msg.get("type")
                print(f"← server command: {cmd_type} ({req_id})", flush=True)
                response = {"request_id": req_id, "status": "success"}

                if cmd_type == "get_summary":
                    ledgers = fetch_local_ledgers(TALLY_URL)
                    response["tally_company_name"] = tally_company_name
                    response["pan"] = info.get("pan") or ""
                    response["ledger_count"] = len(ledgers)
                    response["active_ledgers"] = ledgers
                    response["synced_today"] = 0
                elif cmd_type == "seed_baseline":
                    print("  pulling rich ledgers …", flush=True)
                    rich_ledgers = fetch_rich_ledgers(TALLY_URL)
                    print(f"    {len(rich_ledgers)} ledgers", flush=True)
                    print("  pulling groups …", flush=True)
                    groups = fetch_groups(TALLY_URL)
                    print(f"    {len(groups)} groups", flush=True)
                    print("  pulling vouchers …", flush=True)
                    vouchers = fetch_vouchers(TALLY_URL)
                    print(f"    {len(vouchers)} vouchers", flush=True)
                    print("  pulling stock items …", flush=True)
                    stock_items = fetch_stock_items(TALLY_URL)
                    print(f"    {len(stock_items)} stock items", flush=True)
                    response.update({
                        "tally_company_name": tally_company_name,
                        "pan": info.get("pan") or "",
                        "ledgers": rich_ledgers,
                        "groups": groups,
                        "vouchers": vouchers,
                        "stock_items": stock_items,
                        "ledger_count": len(rich_ledgers),
                        "voucher_count": len(vouchers),
                        "group_count": len(groups),
                        "stock_count": len(stock_items),
                    })
                    seed_done = True
                else:
                    response["status"] = "error"
                    response["message"] = f"unknown command: {cmd_type}"

                await ws.send(json.dumps(response))
                print(f"→ responded to {cmd_type}", flush=True)
        except asyncio.TimeoutError:
            if seed_done:
                print("✓ No more server commands (expected after seed_baseline)")
            else:
                print("✗ Timed out waiting for server command")

        # Keep WS open while server processes the data (saves 2800+ vouchers)
        # Wait for the POST /tally/ingest HTTP response, or timeout after 120s
        print("Keeping WS open while server saves data …", flush=True)
        try:
            while True:
                extra = await asyncio.wait_for(ws.recv(), timeout=120)
                extra_msg = json.loads(extra)
                print(f"  ← extra server message: {extra_msg.get('type', 'unknown')}", flush=True)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            pass

    print("✓ Done.")


if __name__ == "__main__":
    asyncio.run(run())
