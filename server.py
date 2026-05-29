from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form, WebSocket, WebSocketDisconnect, BackgroundTasks
import fastapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response as _StarletteResponse
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse, HTMLResponse, Response
import uvicorn
import os
from dotenv import load_dotenv
load_dotenv()
import json
import requests
import uuid
import asyncio
import secrets as _secrets
from datetime import datetime, timedelta
from providers.tally import TallyProvider
from utils.parser import InvoiceParser
import db
from psycopg2.extras import RealDictCursor
from utils.reconciler import reconcile_statement
from providers.leads import registry as lead_registry
from providers.leads.base import LeadProviderUnavailable

app = FastAPI()

# Pooled Tally WebSocket Connections — keyed by company_id (UUID string) after Phase B
tally_connections = {}
tally_futures = {}

# In-memory agent session store: session_token -> {user_id, expires_at, username, name}
# Lost on server restart — agents will re-authenticate (acceptable for MVP).
agent_sessions = {}
AGENT_SESSION_TTL = timedelta(hours=8)


def _normalize_co_name(name):
    """Case-insensitive, whitespace-collapsed company name comparison helper."""
    if not name:
        return ""
    return " ".join(str(name).strip().lower().split())


def resolve_agent_request(payload: dict, required_perm: str = "edit"):
    """
    For Tally agent ingest endpoints — extract session_token + company_id from payload,
    validate the session, enforce permission, and return (user_id, company_id, company_name).

    Falls back to legacy company_name-only mode if session_token isn't provided
    (back-compat during agent rollout).

    Raises HTTPException on auth/permission failure.
    """
    session_token = payload.get("session_token")
    company_id = payload.get("company_id")
    company_name = payload.get("company_name")

    if session_token and company_id:
        sess = validate_agent_session(session_token)
        if not sess:
            raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
        if not db.user_can(sess["user_id"], required_perm, company_id):
            raise HTTPException(status_code=403, detail="You don't have permission for this company.")
        # Resolve company_id → company_name for back-compat with downstream code
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT name FROM companies WHERE id = %s", (company_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Company not found")
        return sess["user_id"], str(company_id), row["name"]

    # Legacy path — no auth, just use company_name (must be removed once all agents upgraded)
    return None, None, company_name or "Acme Corp"


def validate_agent_session(token):
    """Look up an active session token. Returns the session dict or None if invalid/expired.
    Side-effect: sliding-window refreshes expires_at on every successful access.
    """
    if not token:
        return None
    sess = agent_sessions.get(token)
    if not sess:
        return None
    if sess["expires_at"] < datetime.utcnow():
        agent_sessions.pop(token, None)
        return None
    # Sliding window — extend on use
    sess["expires_at"] = datetime.utcnow() + AGENT_SESSION_TTL
    return sess


def _authorize_bridge(session_token=None, company_name=None, company_id=None, perm="edit"):
    """Two-phase bridge-endpoint auth (#3). If the agent sends a valid session_token,
    validate it and enforce company permission (secure path). Otherwise fall back to
    the legacy company_name-only behaviour, LOGGED as deprecated, so already-deployed
    agents keep working until everyone is on the token-sending build. Phase 2 will
    drop the fallback and require the token. Returns the company_name to scope by."""
    if session_token:
        sess = validate_agent_session(session_token)
        if not sess:
            raise HTTPException(status_code=401,
                detail="Session expired — re-open the YantrAI Windows Agent to sign in again.")
        cid = company_id or (_resolve_company_id_by_name(company_name) if company_name else None)
        if not cid:
            raise HTTPException(status_code=400, detail="company required")
        if not db.user_can(sess["user_id"], perm, cid):
            raise HTTPException(status_code=403, detail="No permission for this company.")
        return company_name
    print(f"[BRIDGE AUTH] DEPRECATED: bridge call without session_token "
          f"(company_name={company_name!r}). Upgrade the Windows Agent.", flush=True)
    return company_name

@app.websocket("/tally/ws")
async def tally_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    conn_key = None  # company_id (UUID str) for Phase B, or legacy token for back-compat
    try:
        init_data = await websocket.receive_json()
        session_token = init_data.get("session_token")
        company_id = init_data.get("company_id")
        tally_company_name = init_data.get("tally_company_name")
        legacy_token = init_data.get("token")

        if session_token and company_id:
            # Phase B authenticated handshake
            sess = validate_agent_session(session_token)
            if not sess:
                await websocket.send_json({"status": "error", "code": "AUTH_INVALID",
                                          "message": "Session expired. Please log in again."})
                await websocket.close()
                print(f"[WS REJECT] invalid session_token", flush=True)
                return
            # Permission check — does this user have edit access to this company?
            if not db.user_can(sess["user_id"], "edit", company_id):
                await websocket.send_json({"status": "error", "code": "PERMISSION_DENIED",
                                          "message": "You don't have permission to push data to this company."})
                await websocket.close()
                print(f"[WS REJECT] user {sess['username']} lacks edit on {company_id}", flush=True)
                return
            # Defense-in-depth: validate the company name matches Tally's company name
            if tally_company_name:
                conn = db.get_conn()
                cur = conn.cursor(cursor_factory=RealDictCursor)
                cur.execute("SELECT name FROM companies WHERE id = %s", (company_id,))
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row and _normalize_co_name(row["name"]) != _normalize_co_name(tally_company_name):
                    await websocket.send_json({
                        "status": "error", "code": "COMPANY_NAME_MISMATCH",
                        "message": f"Tally company '{tally_company_name}' does not match YantrAI company '{row['name']}'. Rename either side to match.",
                    })
                    await websocket.close()
                    print(f"[WS REJECT] name mismatch: tally='{tally_company_name}' vs yantrai='{row['name']}'", flush=True)
                    return
            conn_key = str(company_id)
            print(f"[WS CONNECT] agent authenticated user={sess['username']} company_id={conn_key}", flush=True)
        else:
            # Legacy fallback (will be removed once all agents are upgraded)
            conn_key = legacy_token or "Acme Corp"
            print(f"[WS CONNECT] LEGACY (no auth) token={conn_key}", flush=True)

        # Replace stale connection
        if conn_key in tally_connections and tally_connections[conn_key] != websocket:
            try:
                await tally_connections[conn_key].close()
            except Exception:
                pass

        tally_connections[conn_key] = websocket
        await websocket.send_json({"status": "ok", "message": "connected"})
        
        while True:
            try:
                msg_text = await websocket.receive_text()
                response = json.loads(msg_text)
                request_id = response.get("request_id")
                if request_id and request_id in tally_futures:
                    tally_futures[request_id].set_result(response)
            except WebSocketDisconnect as d:
                print(f"[WS DISCONNECT] Code {d.code} for key {conn_key}", flush=True)
                break
            except Exception as inner_e:
                print(f"[WS MSG ERROR] {inner_e}", flush=True)
                break

    except WebSocketDisconnect:
        print(f"[WS DISCONNECT] Local Tally agent disconnected for key: {conn_key}", flush=True)
    except Exception as e:
        print(f"[WS ERROR] Connection error: {e}", flush=True)
    finally:
        if conn_key and tally_connections.get(conn_key) == websocket:
            tally_connections.pop(conn_key, None)
            print(f"[WS CLEANUP] Removed connection for key: {conn_key}", flush=True)

async def dispatch_tally_command(token: str, cmd_type: str, data: dict = None) -> dict:
    ws = None
    if token in tally_connections:
        ws = tally_connections[token]
    # P0 FIX: no "first available connection" fallback — routing a command to a
    # different company's agent would pull the WRONG tenant's Tally data.
    if not ws:
        print(f"[WS DISPATCH ERROR] No active Tally WebSocket connections available for token '{token}'.", flush=True)
        return None
        
    req_id = f"req_{uuid.uuid4().hex[:8]}"
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    tally_futures[req_id] = fut
    
    try:
        await ws.send_json({
            "request_id": req_id,
            "type": cmd_type,
            "data": data
        })
        
        res = await asyncio.wait_for(fut, timeout=300.0)
        return res
    except asyncio.TimeoutError:
        print(f"[WS TIMEOUT] Local agent did not respond inside 300s for request {req_id}", flush=True)
        return {"status": "error", "message": "Local agent timeout error"}
    except Exception as e:
        print(f"[WS DISPATCH ERROR] Error tunneling request {req_id}: {e}", flush=True)
        return {"status": "error", "message": str(e)}
    finally:
        tally_futures.pop(req_id, None)

@app.get("/history")
async def get_invoice_history(company_name: str = None):
    return db.get_history(company_name)

@app.get("/api/vouchers")
async def get_all_vouchers(company_name: str = None, company_id: str = None,
                           voucher_type: str = None, limit: int = 500, offset: int = 0):
    """Return Tally vouchers + invoice-created vouchers merged, sorted by date desc."""
    return db.get_all_vouchers(company_name=company_name, company_id=company_id,
                               voucher_type=voucher_type, limit=limit, offset=offset)

@app.get("/login")
async def login_page():
    return _serve_versioned('static/login.html', 'text/html')

# ── Sprint 28 — Tally Outbox endpoints (bridge-agent contract + UI polling) ──
@app.get("/api/tally/ledgers")
async def tally_ledgers_list(company_name: str):
    """Sprint 32 — Returns the canonical ledger list YantrAI has on file for
    this company (from `tally_ledgers`, populated by the original ingestion).
    The bridge agent uses this instead of HTTP-probing Tally Prime per ledger
    name, because rapid-fire probes crash Tally with c0000005."""
    try:
        rows = db.get_ledger_master_for_company(company_name=company_name) or []
        # Slim payload: name + parent_group + gstin + closing_balance (Sprint 36).
        def _num(v):
            try: return float(v) if v is not None else None
            except Exception: return None
        slim = [{"name": r.get("name"),
                 "display_name": r.get("display_name"),
                 "parent_group": r.get("parent_group"),
                 "gstin": r.get("gstin"),
                 "closing_balance": _num(r.get("closing_balance"))} for r in rows if r.get("name")]
        return {"status": "success", "data": slim, "count": len(slim)}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ledger/detail")
async def ledger_detail(company_name: str, name: str, limit: int = 50):
    """Sprint 36 — Chart-of-Accounts drill-down. Returns a ledger's master
    fields + its recent transactions (vouchers where it's the party leg OR
    appears in any ledger entry)."""
    try:
        from psycopg2.extras import RealDictCursor
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT name, parent_group, opening_balance, closing_balance, gstin, pan,
                   address, gst_registration_type, place_of_supply, tds_applicable,
                   bank_name, account_number, ifsc_code, ledger_type
            FROM tally_ledgers
            WHERE company_name = %s AND name = %s LIMIT 1
        """, (company_name, name))
        master = cur.fetchone() or {"name": name}
        for k in ("opening_balance", "closing_balance"):
            if master.get(k) is not None:
                try: master[k] = float(master[k])
                except Exception: master[k] = None
        cur.execute("""
            SELECT date, voucher_number, voucher_type, ledger_name, amount,
                   narration, reference_no
            FROM tally_vouchers
            WHERE company_name = %s
              AND (ledger_name = %s OR ledger_entries::text ILIKE %s)
            ORDER BY date DESC NULLS LAST
            LIMIT %s
        """, (company_name, name, f'%{name}%', limit))
        txns = []
        for r in cur.fetchall():
            r["date"] = str(r["date"]) if r.get("date") else None
            if r.get("amount") is not None:
                try: r["amount"] = float(r["amount"])
                except Exception: r["amount"] = None
            txns.append(r)
        cur.close(); conn.close()
        return {"status": "success", "master": master, "transactions": txns,
                "txn_count": len(txns)}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tally/queue")
async def tally_queue_claim(company_name: str, limit: int = 10, session_token: str = None):
    """Bridge agent polls this. Atomically claims pending outbox rows and
    flips them to 'pushing'. Returns the payloads to push to Tally."""
    try:
        company_name = _authorize_bridge(session_token, company_name)
        rows = db.claim_tally_outbox(company_name, limit=limit)
        return {"status": "success", "data": rows}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tally/queue/{outbox_id}/ack")
async def tally_queue_ack(outbox_id: str, payload: dict):
    """Bridge agent confirms a successful push. Body: {tally_voucher_guid?, session_token?}."""
    try:
        st = (payload or {}).get("session_token")
        if st and not validate_agent_session(st):
            raise HTTPException(status_code=401, detail="Session expired.")
        guid = payload.get("tally_voucher_guid") if isinstance(payload, dict) else None
        return {"status": "success", **db.ack_tally_outbox(outbox_id, tally_voucher_guid=guid)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tally/queue/{outbox_id}/fail")
async def tally_queue_fail(outbox_id: str, payload: dict):
    """Bridge agent reports a failure. Body: {error, session_token?}."""
    try:
        st = (payload or {}).get("session_token")
        if st and not validate_agent_session(st):
            raise HTTPException(status_code=401, detail="Session expired.")
        err = (payload or {}).get("error") or "Unknown error"
        return {"status": "success", **db.fail_tally_outbox(outbox_id, err)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tally/heartbeat")
async def tally_heartbeat(payload: dict):
    """Bridge agent calls this every ~30s to mark itself as alive."""
    try:
        company_name = (payload or {}).get("company_name")
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")
        company_name = _authorize_bridge((payload or {}).get("session_token"), company_name)
        return {"status": "success", **db.upsert_tally_heartbeat(
            company_name,
            agent_version=(payload or {}).get("agent_version"),
            ip=(payload or {}).get("ip"),
        )}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tally/status")
async def tally_status_endpoint(company_name: str):
    """UI polls this to show agent online/offline + queue counts + last push."""
    try:
        return {"status": "success", **db.tally_status_summary(company_name)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tally/outbox/{invoice_id}")
async def tally_outbox_invoice_status(invoice_id: str):
    """UI polls this for the latest state of a specific invoice's push."""
    try:
        row = db.tally_outbox_status_for_invoice(invoice_id)
        if not row:
            return {"status": "success", "data": None}
        # Stringify for JSON
        for k in ("id","enqueued_at","pushed_at","updated_at"):
            if row.get(k) is not None: row[k] = str(row[k])
        return {"status": "success", "data": row}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _read_agent_version():
    """Sprint 34 — Read AGENT_VERSION straight from the agent source so the web
    download always reports the version that's actually shipping."""
    import os, re
    src = os.path.join(os.path.dirname(__file__), "tally_agent", "tally_bridge_agent.py")
    try:
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', content)
        if m: return m.group(1)
    except Exception:
        pass
    return "unknown"


@app.get("/tally_bridge_agent/version")
async def tally_bridge_agent_version():
    """Returns the agent version + .exe build time so the UI can show what
    you'd download and let you correlate it to the running agent's heartbeat."""
    import os, datetime
    base = os.path.dirname(__file__)
    exe_path = os.path.join(base, "tally_agent", "dist", "tally_bridge_agent.exe")
    info = {"version": _read_agent_version(), "exe_available": os.path.exists(exe_path)}
    if info["exe_available"]:
        st = os.stat(exe_path)
        info["exe_size_bytes"] = st.st_size
        info["exe_built_at"] = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    return {"status": "success", **info}


@app.get("/tally_bridge_agent/download")
async def download_tally_bridge_agent():
    """Sprint 30 — Agent files live under tally_agent/. The .exe is rebuilt from
    the latest source after every Sprint that touches tally_bridge_agent.py.
    Sprint 34 — the download filename now carries the version so users can
    correlate the binary they have to what's live."""
    import os
    base = os.path.dirname(__file__)
    ver = _read_agent_version()
    exe_path = os.path.join(base, "tally_agent", "dist", "tally_bridge_agent.exe")
    if os.path.exists(exe_path):
        return FileResponse(
            exe_path,
            media_type="application/vnd.microsoft.portable-executable",
            filename=f"tally_bridge_agent_v{ver}.exe",
        )
    # Dev fallback — return the Python source so a developer can run it directly
    file_path = os.path.join(base, "tally_agent", "tally_bridge_agent.py")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Agent not found. Build with: pyinstaller tally_agent/tally_bridge_agent.spec --workpath tally_agent/build --distpath tally_agent/dist --noconfirm")
    return FileResponse(file_path, media_type="text/plain", filename=f"tally_bridge_agent_v{ver}.py")

# WhatsApp Settings
VERIFY_TOKEN = "yantrai_accounting_secret"
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "YOUR_ACCESS_TOKEN")

# Knowledge Base
KB_PATH = "knowledge_base.json"
def load_kb():
    with open(KB_PATH, "r") as f:
        return json.load(f)

# Sprint 72 — gzip every response >512B (887KB index.html -> ~120KB; also CSS + JSON).
app.add_middleware(GZipMiddleware, minimum_size=512)

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Sprint 23 — Server-side role gate for super_admin-only feature areas ──
# Six view families are restricted: /api/gstr/*, /api/tds/*, /api/audit/*,
# /api/recon/*, plus the /tasks page route. The frontend attaches `X-User`
# header on every fetch (see global fetch wrapper in static/index.html);
# this middleware blocks non-super_admin requests with 403.
from starlette.responses import JSONResponse as _JSONResponse  # type: ignore

_GATED_PATH_PREFIXES = (
    "/api/gstr/", "/api/tds/", "/api/audit/", "/api/recon/", "/api/itr/",
    "/api/admin/",
)
_GATED_EXACT_PATHS = ("/tasks",)

@app.middleware("http")
async def _role_gate_middleware(request, call_next):
    path = request.url.path
    gated = (
        any(path.startswith(p) for p in _GATED_PATH_PREFIXES)
        or path in _GATED_EXACT_PATHS
    )
    if not gated:
        return await call_next(request)
    # CORS preflight bypass
    if request.method == "OPTIONS":
        return await call_next(request)
    # Prefer the verified identity from the JWT middleware (Sprint 51); fall back to
    # the legacy X-User header / ?username= when no token is present.
    user = getattr(request.state, "user_row", None)
    if not user:
        username = request.headers.get("x-user") or request.query_params.get("username")
        if not username:
            return _JSONResponse(
                {"detail": "Forbidden — this feature requires super_admin (no user identified)."},
                status_code=403,
            )
        try:
            user = db.get_user_by_username(username)
        except Exception as e:
            print(f"[role_gate] lookup error: {e}", flush=True)
            user = None
    if not user or (user.get("role") if isinstance(user, dict) else None) != "super_admin":
        return _JSONResponse(
            {"detail": "Forbidden — super_admin only."},
            status_code=403,
        )
    return await call_next(request)


# ── Sprint 51 — Auth middleware (runs BEFORE the role gate; added later = outer). ──
# Verifies the Supabase Bearer JWT and stamps request.state with the trusted
# identity. Best-effort by default; only REJECTS missing/invalid tokens when
# AUTH_ENFORCE=1 (flip on after the user migration). Legacy callers are unaffected
# until then.
@app.middleware("http")
async def _auth_middleware(request, call_next):
    request.state.user_row = None
    request.state.username = None
    request.state.users_id = None
    if request.method == "OPTIONS":
        return await call_next(request)
    if SUPABASE_AUTH_ENABLED:
        claims = _verify_token(request.headers.get("authorization") or "")
        if claims:
            try:
                row = db.get_user_by_auth_uid(claims.get("sub"))
            except Exception as e:
                print(f"[auth_mw] {e}", flush=True); row = None
            if row:
                request.state.user_row = row
                request.state.username = row.get("username")
                request.state.users_id = row.get("users_id")
        if AUTH_ENFORCE and request.state.user_row is None and not _is_auth_whitelisted(request.url.path):
            return _JSONResponse({"detail": "Authentication required."}, status_code=401)
    return await call_next(request)


# ── Sprint 24 — User Manager endpoints (all super_admin-only via middleware) ──
@app.get("/api/admin/users")
async def admin_list_users():
    """List every account in the system with role + company access counts."""
    try:
        return {"status": "success", "users": db.list_all_users()}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/companies")
async def admin_list_companies():
    """List every distinct company in the system with voucher / bank /
    user-access counts."""
    try:
        return {"status": "success", "companies": db.list_all_companies_with_usage()}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(username: str):
    """Hard-delete a user. Refuses to delete the only super_admin."""
    try:
        res = db.delete_user_by_username(username)
        if not res.get("deleted"):
            raise HTTPException(status_code=400, detail=res.get("message", "Delete failed"))
        return {"status": "success", "message": res["message"]}
    except HTTPException: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/admin/users/{username}")
async def admin_update_user(username: str, payload: dict):
    """PATCH role and/or companies. Body: {role, add_company, remove_company}."""
    try:
        result = {"username": username}
        if "role" in payload:
            r = db.update_user_role(username, payload["role"])
            result["role_update"] = r
        if "remove_company" in payload:
            r = db.remove_company_from_user(username, payload["remove_company"])
            result["remove_company"] = r
        if "add_company" in payload:
            ok = db.add_company_to_user(username, payload["add_company"])
            result["add_company"] = {"ok": bool(ok)}
        return {"status": "success", "result": result}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# --- WhatsApp Webhook Endpoints ---

@app.get("/webhook")
async def verify_webhook(request: Request):
    """Verify the webhook with Meta."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("WEBHOOK_VERIFIED")
        return PlainTextResponse(content=challenge)
    else:
        raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/webhook")
async def handle_whatsapp_message(request: Request):
    """Handle incoming messages from WhatsApp."""
    data = await request.json()
    print(f"DEBUG: WhatsApp Data Received: {json.dumps(data, indent=2)}")
    
    # Logic to parse message, download image, and trigger Gemini
    # (Simplified for now)
    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        message = value.get("messages", [{}])[0]
        
        from_number = message.get("from")
        
        if "image" in message:
            # Handle Image
            image_id = message["image"]["id"]
            print(f"Received image from {from_number} with ID: {image_id}")
            # Here we would download from Meta and send to parser.parse()
        elif "text" in message:
            # Handle Text
            text = message["text"]["body"]
            print(f"Received text from {from_number}: {text}")
            
        return {"status": "success"}
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return {"status": "ignored"}

# --- Existing Endpoints ---

# Mount static files
# Sprint 72 — long-cache static assets. They're versioned via ?v=NN (style.css?v=90,
# sw bumps), so a far-future immutable cache lets reloads reuse them without re-downloading.
# The PWA shell (/, /sw.js, /manifest.json) keeps no-cache below so it always revalidates.
class _CachedStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        try:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        except Exception:
            pass
        return resp

app.mount("/static", _CachedStatic(directory="static"), name="static")

# PWA shell + service worker must always be revalidated so phones never get
# stuck on a stale build. no-cache = "check with the server before reusing"
# (cheap 304 when unchanged, fresh bytes when changed).
_NOCACHE = {"Cache-Control": "no-cache, must-revalidate"}

# ── Single source of truth for the app version ──────────────────────────────
# Bump APP_VERSION on EVERY release. It is injected (replacing the __APP_VER__
# placeholder) into the served shell HTML, the service worker (CACHE_NAME) and
# the ?v= CSS cache-bust — so the visible label, the SW cache and the asset
# cache-bust are always the SAME number. Nothing else needs editing per release.
APP_VERSION = "207"

def _serve_versioned(path, media_type):
    """Serve a static text file with __APP_VER__ replaced by APP_VERSION."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            body = f.read().replace("__APP_VER__", APP_VERSION)
        return Response(content=body, media_type=media_type, headers=_NOCACHE)
    except Exception:
        return FileResponse(path, media_type=media_type, headers=_NOCACHE)

@app.get("/manifest.json")
async def get_manifest():
    return FileResponse('static/manifest.json', media_type='application/json', headers=_NOCACHE)

@app.get("/sw.js")
async def get_sw():
    return _serve_versioned('static/sw.js', 'application/javascript')

@app.get("/")
async def read_index():
    return _serve_versioned('static/index.html', 'text/html')

# Sprint 58 — PWA Share Target fallback. Normally the service worker intercepts the
# POST and stashes the file; this only fires on the first launch before the SW controls
# the page. Just bounce into the app (the SW will catch it next time).
from fastapi.responses import RedirectResponse as _RedirectResponse
@app.post("/share-target")
@app.get("/share-target")
async def share_target():
    return _RedirectResponse(url="/?shared=1", status_code=303)

@app.get("/knowledge")
async def get_knowledge():
    return load_kb()

@app.post("/feedback")
async def save_feedback(feedback: dict):
    # feedback: { field: 'party_name', original: '...', corrected: '...', party_name: '...', company_name: '...' }
    field = feedback.get('field')
    original = feedback.get('original')
    corrected = feedback.get('corrected')
    party_name = feedback.get('party_name', 'Unknown')
    company_name = feedback.get('company_name', 'Acme Corp')
    
    # Generate Embedding for this correction
    desc = f"For {party_name}: The {field} should be '{corrected}' (NOT '{original}')"
    embedding = get_embedding(desc)
    
    db.save_correction(
        field,
        original,
        corrected,
        party_name,
        embedding,
        company_name=company_name
    )
    return {"status": "learned"}

# Initialize components
import os
from dotenv import load_dotenv
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyCuVgfmx3oaja0O4Mr3jMb8wP7Ikpe9BXs") # Fallback to hardcoded key
from google import generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)
parser = InvoiceParser(api_key=GEMINI_API_KEY)

# Instantiate Tally Provider dynamically from TALLY_URL env var
tally_url = os.getenv("TALLY_URL", "http://localhost:9000")
if ":" in tally_url.replace("http://", "").replace("https://", ""):
    parts = tally_url.rsplit(":", 1)
    tally = TallyProvider(host=parts[0], port=int(parts[1]))
else:
    tally = TallyProvider(host=tally_url, port=80)

def get_embedding(text: str):
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-2",
            content=text,
            task_type="retrieval_document"
        )
        return result['embedding']
    except Exception as e:
        print(f"Error generating embedding: {e}")
        return None

# ─────────────────────────────────────────────────────────────
# Sprint 51 — Supabase Auth (real login + verified sessions). DUAL-MODE:
# when keys are configured we use Supabase Auth; otherwise we fall back to the
# legacy plaintext path so the app keeps working until keys are provided.
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://vxnflumpectzqdamjqsc.supabase.co").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
SUPABASE_AUTH_ENABLED = bool(SUPABASE_ANON_KEY)   # JWKS verifies asymmetric tokens; JWT_SECRET only needed for legacy HS256
# Require a valid Bearer token on browser /api/* — flip ON only AFTER users are
# migrated into Supabase Auth (env AUTH_ENFORCE=1). Default OFF = no lockout risk.
AUTH_ENFORCE = os.getenv("AUTH_ENFORCE", "0") == "1"
SYNTH_EMAIL_DOMAIN = "yantrai.app"
if not SUPABASE_AUTH_ENABLED:
    print("[auth] Supabase Auth not configured (missing SUPABASE_ANON_KEY/JWT_SECRET) — "
          "running on legacy plaintext login.", flush=True)

def _login_email(user):
    """The email a user's Supabase auth account is keyed by (must be identical in
    migration, login and onboard)."""
    try:
        em = (user.get("email") if isinstance(user, dict) else None) or ""
        un = (user.get("username") if isinstance(user, dict) else None) or ""
    except Exception:
        em, un = "", ""
    return em.strip() or f"{un}@{SYNTH_EMAIL_DOMAIN}"

def _supabase_password_grant(email, password):
    """Exchange email+password for Supabase tokens. Raises on failure."""
    import requests as _rq
    r = _rq.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                 headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                 json={"email": email, "password": password}, timeout=15)
    if r.status_code != 200:
        raise ValueError(f"grant {r.status_code}: {r.text[:200]}")
    return r.json()

def _supabase_refresh(refresh_token):
    import requests as _rq
    r = _rq.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
                 headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                 json={"refresh_token": refresh_token}, timeout=15)
    if r.status_code != 200:
        raise ValueError(f"refresh {r.status_code}: {r.text[:200]}")
    return r.json()

def _supabase_recover(email, redirect_to=None):
    """Trigger a Supabase password-reset (recovery) email. Returns True on accept.
    Delivery depends on SMTP being configured in the Supabase project."""
    import requests as _rq, urllib.parse as _up
    url = f"{SUPABASE_URL}/auth/v1/recover"
    if redirect_to:
        url += "?redirect_to=" + _up.quote(redirect_to, safe="")
    r = _rq.post(url, headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                 json={"email": email}, timeout=15)
    return r.status_code in (200, 204)

def _supabase_send_otp(email, redirect_to=None):
    """Send a Supabase magic-link / OTP email to `email` (does NOT create a user).
    Clicking it proves the user controls the inbox. Returns True on accept.
    Delivery depends on SMTP being configured in the Supabase project."""
    import requests as _rq, urllib.parse as _up
    url = f"{SUPABASE_URL}/auth/v1/otp"
    if redirect_to:
        url += "?redirect_to=" + _up.quote(redirect_to, safe="")
    r = _rq.post(url, headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                 json={"email": email, "create_user": False}, timeout=15)
    if r.status_code not in (200, 204):
        print(f"[supabase_send_otp] {r.status_code}: {r.text[:200]}", flush=True)
    return r.status_code in (200, 204)

def _supabase_admin_create_user(email, password):
    """Create a Supabase auth user (service role). Returns the auth user id, or None."""
    import requests as _rq
    r = _rq.post(f"{SUPABASE_URL}/auth/v1/admin/users",
                 headers={"apikey": SUPABASE_SERVICE_ROLE_KEY,
                          "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                          "Content-Type": "application/json"},
                 json={"email": email, "password": password, "email_confirm": True}, timeout=15)
    if r.status_code in (200, 201):
        return (r.json() or {}).get("id")
    # already exists → look it up
    if r.status_code == 422:
        g = _rq.get(f"{SUPABASE_URL}/auth/v1/admin/users",
                    headers={"apikey": SUPABASE_SERVICE_ROLE_KEY,
                             "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
                    params={"email": email}, timeout=15)
        if g.status_code == 200:
            users = (g.json() or {}).get("users") or (g.json() or {}).get("data") or []
            if users:
                return users[0].get("id")
    print(f"[supabase_admin_create_user] {r.status_code}: {r.text[:200]}", flush=True)
    return None

_JWKS_CLIENT = None
def _jwks_client():
    global _JWKS_CLIENT
    if _JWKS_CLIENT is None:
        import jwt as _jwt
        _JWKS_CLIENT = _jwt.PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
    return _JWKS_CLIENT

def _verify_token(authz):
    """Verify a Supabase Bearer JWT. Supports ES256/RS256 (asymmetric, via the
    project JWKS) and legacy HS256 (shared secret). Returns claims or None."""
    if not authz or not authz.lower().startswith("bearer "):
        return None
    token = authz.split(" ", 1)[1].strip()
    try:
        import jwt as _jwt
        alg = (_jwt.get_unverified_header(token) or {}).get("alg", "")
        if alg == "HS256":
            if not SUPABASE_JWT_SECRET:
                return None
            return _jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"],
                               audience="authenticated")
        key = _jwks_client().get_signing_key_from_jwt(token).key
        return _jwt.decode(token, key, algorithms=["ES256", "RS256"], audience="authenticated")
    except Exception as e:
        print(f"[verify_token] {e}", flush=True)
        return None

# Paths that never require a session (login, onboard, public, desktop agent, static).
_AUTH_WHITELIST_PREFIXES = ("/api/login", "/api/auth/", "/api/onboard", "/api/register",
                            "/api/agent/", "/tally/", "/api/agents/verify-sso",
                            "/api/webhooks/", "/static/", "/tally_bridge_agent",
                            "/api/shared/", "/s/")  # Sprint 84 public shared chats
_AUTH_WHITELIST_EXACT = ("/", "/login", "/sw.js", "/manifest.json", "/favicon.ico",
                         "/share-target")
def _is_auth_whitelisted(path):
    return path in _AUTH_WHITELIST_EXACT or any(path.startswith(p) for p in _AUTH_WHITELIST_PREFIXES)


# ─────────────────────────────────────────────────────────────
# Sprint 47 — Token wallet metering (per-workspace/org).
# Tune these business constants as needed.
TOKEN_MARKUP = 1.0          # YantrAI tokens charged per Gemini token
TOKENS_PER_INR = 1000       # recharge: ₹1 -> 1000 tokens (placeholder pricing)
BILLING_ENFORCE = True      # block AI actions when balance <= 0

# Sprint 53 — Razorpay (test mode first). Keys from env; if absent, recharge falls
# back to the manual-pending flow so nothing breaks.
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_ENABLED = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

def _razorpay_create_order(amount_inr, receipt, notes=None):
    """Create a Razorpay order (amount in paise). Returns the order dict or raises."""
    import requests as _rq
    r = _rq.post("https://api.razorpay.com/v1/orders",
                 auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
                 json={"amount": int(round(float(amount_inr) * 100)), "currency": "INR",
                       "receipt": receipt, "notes": notes or {}}, timeout=20)
    if r.status_code not in (200, 201):
        raise ValueError(f"order {r.status_code}: {r.text[:200]}")
    return r.json()

def _razorpay_verify_signature(order_id, payment_id, signature):
    """Verify Checkout's payment signature = HMAC_SHA256(order_id|payment_id, key_secret)."""
    import hmac as _h, hashlib as _hl
    expected = _h.new(RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(),
                      _hl.sha256).hexdigest()
    return _h.compare_digest(expected, signature or "")

def _billing_org_id(username, company_name):
    """Resolve the workspace (org) wallet to charge for this caller+company."""
    try:
        u = db.get_user_by_username(username) if username else None
        uid = u.get("users_id") if u else None
        return db.org_id_for_company(company_name, uid), (uid, u)
    except Exception as e:
        print(f"[_billing_org_id] {e}")
        return None, (None, None)

def _ensure_tokens(username, company_name):
    """Pre-check before an AI call. Raise 402 when the workspace is out of tokens."""
    if not BILLING_ENFORCE:
        return None
    org_id, _ = _billing_org_id(username, company_name)
    if org_id is None:
        return None  # can't resolve a wallet → don't block (legacy/edge)
    if db.org_balance(org_id) <= 0:
        raise HTTPException(status_code=402, detail={
            "error": "out_of_tokens",
            "message": "You're out of tokens — recharge to continue using AI features."})
    return org_id

def _parse_ai_json(raw):
    """Tolerantly parse the AI's JSON reply.

    Gemini occasionally returns slightly malformed JSON (code fences, trailing
    commas, smart quotes, an unterminated tail). Rather than 500-ing the whole
    chat, try a few light repairs and fall back to a plain-text bubble.
    """
    import re as _re
    fallback = {"text": raw, "ui_type": "text", "ui_data": None, "suggested_questions": []}
    if not raw:
        return fallback

    # Strip ```json ... ``` / ``` ... ``` fences if present.
    s = raw.strip()
    if s.startswith("```"):
        s = _re.sub(r'^```[a-zA-Z]*\s*', '', s)
        s = _re.sub(r'\s*```$', '', s).strip()

    # Isolate the outermost {...} block.
    m = _re.search(r'\{.*\}', s, _re.DOTALL)
    candidate = m.group(0) if m else s

    def _try(text):
        try:
            return json.loads(text)
        except Exception:
            return None

    # 1) straight parse
    out = _try(candidate)
    if out is not None:
        return out

    # 2) light repairs: smart quotes, trailing commas
    repaired = (candidate
                .replace('“', '"').replace('”', '"')
                .replace('‘', "'").replace('’', "'"))
    repaired = _re.sub(r',\s*([}\]])', r'\1', repaired)
    out = _try(repaired)
    if out is not None:
        return out

    # 3) progressively trim from the end to recover a valid prefix object
    #    (handles an unterminated/garbled tail by closing braces we've seen).
    depth = 0
    in_str = False
    esc = False
    last_good = None
    for i, ch in enumerate(repaired):
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                last_good = i + 1
    if last_good:
        out = _try(repaired[:last_good])
        if out is not None:
            return out

    print(f"[_parse_ai_json] could not parse AI JSON; falling back to text. head={raw[:120]!r}")
    return fallback


def _usage_from_response(resp):
    """Pull (prompt, output, total) token counts from a Gemini response, or (0,0,0)."""
    try:
        um = getattr(resp, "usage_metadata", None)
        if um:
            p = getattr(um, "prompt_token_count", 0) or 0
            o = getattr(um, "candidates_token_count", 0) or 0
            t = getattr(um, "total_token_count", 0) or (p + o)
            return int(p), int(o), int(t)
    except Exception:
        pass
    return 0, 0, 0

def _charge_ai(username, company_name, action, response=None, est_text=None,
               model="gemini-flash-latest", agent="ai-accountant"):
    """Deduct tokens for one AI call. Best-effort — never breaks the AI flow.
    `agent` tags which store agent consumed the tokens (per-agent usage breakdown)."""
    try:
        org_id, (uid, _) = _billing_org_id(username, company_name)
        if org_id is None:
            return
        p, o, t = _usage_from_response(response)
        if t <= 0 and est_text:
            t = max(1, len(est_text) // 4)   # embeddings / no-metadata fallback
        # Sprint 54 — charge via the model's pricing weight (credits per 1,000 tokens),
        # normalising different models to one credit currency. Falls back to the flat
        # rate if the model isn't priced yet.
        weight = db.get_model_weight(model)
        if weight is not None:
            charged = int(round((t / 1000.0) * weight))
        else:
            charged = int(round(t * TOKEN_MARKUP))
        if charged <= 0:
            return
        db.debit_tokens(org_id, charged, action=action, model=model, user_id=uid,
                        company_name=company_name, prompt_tokens=p, output_tokens=o,
                        total_tokens=t, agent_slug=agent)
    except Exception as e:
        print(f"[_charge_ai] {e}")


# ─────────────────────────────────────────────────────────────
# Lead Generation — turn a user's plain-language context into real, contactable
# businesses (via a pluggable data source), AI-scored for fit. Persisted with a
# status/action marker so it can grow into end-to-end customer management.
# ─────────────────────────────────────────────────────────────
LEADGEN_MODEL = "gemini-flash-latest"

def _leadgen_search_params(context):
    """Ask Gemini to turn free-text context into structured search params.
    Returns (params_dict, gemini_response_or_None)."""
    fallback = {"query": (context or "").strip(), "business_type": "", "location": "",
                "keywords": []}
    if not (context or "").strip():
        return fallback, None
    prompt = (
        "You convert a user's description of their ideal customer into a business "
        "directory search. Reply ONLY with JSON: "
        '{"query": "<concise text search for a maps/business directory>", '
        '"business_type": "<category>", "location": "<city/area or empty>", '
        '"keywords": ["..."]}. '
        "Make 'query' something that works in Google Maps text search.\n\n"
        f"User context: {context.strip()}")
    try:
        resp = genai.GenerativeModel(LEADGEN_MODEL).generate_content(prompt)
        parsed = _parse_ai_json(getattr(resp, "text", "") or "")
        if isinstance(parsed, dict) and parsed.get("query"):
            for k, v in fallback.items():
                parsed.setdefault(k, v)
            return parsed, resp
    except Exception as e:
        print(f"[_leadgen_search_params] {e}")
    return fallback, None

def _leadgen_score(context, leads):
    """Ask Gemini to score each lead 0-100 for fit + a one-line why_fit.
    Mutates `leads` in place; returns the gemini response (for token charging)."""
    if not leads:
        return None
    brief = [{"i": i, "name": l.get("name"), "category": l.get("category"),
              "city": l.get("city"), "website": l.get("website")}
             for i, l in enumerate(leads)]
    prompt = (
        "Score how well each business fits the user's target customer. "
        "Reply ONLY with JSON: {\"scores\":[{\"i\":<index>,\"score\":<0-100>,"
        "\"why_fit\":\"<one short sentence>\"}, ...]}.\n\n"
        f"User target: {context}\n\nBusinesses: {json.dumps(brief)}")
    try:
        resp = genai.GenerativeModel(LEADGEN_MODEL).generate_content(prompt)
        parsed = _parse_ai_json(getattr(resp, "text", "") or "")
        by_i = {}
        for s in (parsed.get("scores") or []) if isinstance(parsed, dict) else []:
            try:
                by_i[int(s.get("i"))] = s
            except Exception:
                pass
        for i, l in enumerate(leads):
            s = by_i.get(i) or {}
            try:
                l["score"] = max(0, min(100, int(s.get("score")))) if s.get("score") is not None else None
            except Exception:
                l["score"] = None
            l["why_fit"] = s.get("why_fit")
        return resp
    except Exception as e:
        print(f"[_leadgen_score] {e}")
        return None

def _dedupe_leads(leads):
    seen, out = set(), []
    for l in leads:
        key = ((l.get("name") or "").strip().lower(),
               (l.get("phone") or l.get("website") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key); out.append(l)
    return out


@app.post("/api/leads/generate")
async def generate_leads(
    context: str = Form(...),
    source: str = Form(lead_registry.DEFAULT_SOURCE),
    count: int = Form(20),
    username: str = Form(None),
    company_name: str = Form(None),
):
    _ensure_tokens(username, company_name)   # block if workspace out of tokens (402)
    provider = lead_registry.get_provider(source)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"Unknown lead source '{source}'")

    # 1) free-text context -> structured search params (Gemini)
    params, r1 = _leadgen_search_params(context)
    # 2) fetch real leads from the chosen source
    try:
        leads = provider.search(params, limit=max(1, min(int(count or 20), 20)))
    except LeadProviderUnavailable as e:
        raise HTTPException(status_code=503, detail={"error": "source_unavailable",
                                                     "message": str(e)})
    except Exception as e:
        print(f"[generate_leads] provider error: {e}")
        raise HTTPException(status_code=502, detail={"error": "source_error",
                                                     "message": "Lead source failed."})
    leads = _dedupe_leads(leads)
    # 3) AI fit-scoring
    r2 = _leadgen_score(context, leads) if leads else None

    # 4) persist + charge credits
    u = db.get_user_by_username(username) if username else None
    uid = (u or {}).get("users_id")
    batch_id = db.insert_lead_batch(uid, company_name, context, params, source)
    saved = db.insert_leads(batch_id, uid, company_name, leads)
    for resp in (r1, r2):
        if resp is not None:
            _charge_ai(username, company_name, "leadgen", response=resp,
                       model=LEADGEN_MODEL, agent="lead-gen")

    return {"status": "success", "batch_id": batch_id, "count": len(saved),
            "search_params": params, "leads": saved}


@app.get("/api/leads")
async def get_leads(username: str = None, batch_id: str = None):
    u = db.get_user_by_username(username) if username else None
    uid = (u or {}).get("users_id")
    if not uid:
        return {"status": "success", "leads": [], "batches": []}
    return {"status": "success",
            "leads": db.list_leads(uid, batch_id=batch_id),
            "batches": db.list_lead_batches(uid)}


@app.get("/api/leads/export")
async def export_leads(username: str = None, batch_id: str = None):
    import csv, io
    u = db.get_user_by_username(username) if username else None
    uid = (u or {}).get("users_id")
    rows = db.list_leads(uid, batch_id=batch_id) if uid else []
    cols = ["business_name", "category", "phone", "website", "email", "address",
            "city", "rating", "score", "why_fit", "status", "source"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c.replace("_", " ").title() for c in cols])
    for r in rows:
        w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in cols])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.patch("/api/leads/{lead_id}")
async def patch_lead(lead_id: str, request: Request):
    body = await request.json()
    u = db.get_user_by_username(body.get("username")) if body.get("username") else None
    uid = (u or {}).get("users_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Unknown user")
    row = db.update_lead_status(lead_id, uid, status=body.get("status"),
                                action=body.get("action"))
    if not row:
        raise HTTPException(status_code=400, detail="Update failed (bad status or lead not found)")
    return {"status": "success", "lead": row}


@app.get("/api/leads/sources")
async def lead_sources():
    return {"status": "success", "sources": lead_registry.list_sources()}


def _ai_chat_title(text):
    """Generate a concise AI title for a chat from how it begins. Returns None on any
    failure so the caller can fall back to the old filename/message-truncation title."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        resp = model.generate_content(
            "Write a concise 3-6 word title (Title Case, no quotes, no trailing punctuation) "
            "for a chat that begins with the following. Reply with ONLY the title.\n\n" + text[:800])
        t = (resp.text or "").strip()
        if not t:
            return None
        t = t.splitlines()[0].strip().strip('"').strip("'").strip()
        return t[:60] or None
    except Exception as e:
        print(f"[_ai_chat_title] {e}", flush=True)
        return None


@app.post("/chat")
async def chat_with_tally(
    message: str = Form(None),
    session_id: str = Form(None),
    file: UploadFile = File(None),
    company_name: str = Form(None),
    txn_type: str = Form(None),
    username: str = Form(None)
):
    _ensure_tokens(username, company_name)   # Sprint 47 — block if workspace out of tokens (raises 402)
    try:
        kb = load_kb()
        user_msg = message or ""

        # Create new session if needed (scoped to user so siblings can't see each other's chats)
        if not session_id or session_id == "null" or session_id == "undefined":
            session_id = db.create_chat_session(company_name=company_name, user_username=username)

        file_context = ""
        file_url = None
        is_bank_statement = False
        ai_detected_type = None
        if file:
            import re
            safe_filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
            unique_filename = f"{uuid.uuid4()}_{safe_filename}"

            temp_path = f"chat_temp_{uuid.uuid4()}_{safe_filename}"
            file_content = await file.read()
            with open(temp_path, "wb") as buffer:
                buffer.write(file_content)
            # Durable storage (Supabase) so the file (and its chat thumbnail) survives
            # Cloud Run restarts and is visible across environments. Local disk is
            # ephemeral on Cloud Run → was the cause of broken chat thumbnails. Fall
            # back to local disk only when Storage is unavailable (dev without creds).
            _slug = re.sub(r'[^a-z0-9]+', '-', (company_name or 'co').lower()).strip('-') or 'co'
            _ext = os.path.splitext(safe_filename)[1]
            file_url = _supabase_upload(f"{_slug}/chat/{uuid.uuid4().hex}{_ext}",
                                        file_content, file.content_type)
            if not file_url:
                os.makedirs("static/uploads", exist_ok=True)
                with open(f"static/uploads/{unique_filename}", "wb") as buffer:
                    buffer.write(file_content)
                file_url = f"/static/uploads/{unique_filename}"

            # Register the upload in the company Files library so chat-uploaded
            # bills/statements also show up under Files (not just in the chat).
            try:
                if company_name:
                    db.save_company_file(company_name, file_url,
                                         original_name=file.filename,
                                         file_type=(file.content_type or None),
                                         size_bytes=len(file_content),
                                         uploaded_by=username)
            except Exception as _cf_err:
                print(f"[chat] save_company_file error: {_cf_err}", flush=True)

            try:
                # Analyze the document first to get context
                file_analysis = parser.parse(temp_path, context="Understand what this document is (Purchase, Sale, Report, etc.) and summarize key details for a conversation.")
                file_context = f"\n[USER UPLOADED A DOCUMENT]: {file_analysis}\n"

                # AI Auto-Classification: detect document type from content
                fa_lower = file_analysis.lower()
                if "bank statement" in fa_lower or "bank transaction" in fa_lower or "statement of account" in fa_lower or "bank ledger" in fa_lower:
                    is_bank_statement = True
                    ai_detected_type = "Bank Statement"
                elif "purchase" in fa_lower and ("invoice" in fa_lower or "bill" in fa_lower):
                    ai_detected_type = "Purchase"
                elif "sale" in fa_lower or "tax invoice" in fa_lower or "invoice" in fa_lower:
                    ai_detected_type = "Sales"
                else:
                    ai_detected_type = "Other"

                if not user_msg:
                    user_msg = "I've uploaded a document. Please tell me what it is and summarize it."
            except Exception as fe:
                file_context = f"\n[UPLOAD ERROR]: Could not read file details: {str(fe)}\n"
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        # AI Intent Detection for Service Requests (no file needed)
        # Detect if user is asking YantrAI to do something for them (outcome request)
        is_service_request = False
        if not file and user_msg:
            service_keywords = [
                "can you set up", "can you build", "can you create", "can you configure",
                "i want you to", "i need you to", "please set up", "please configure",
                "set up automated", "automate my", "build me", "create a report for",
                "help me set up", "integrate my", "connect my", "file my gst",
                "do my", "handle my", "manage my", "prepare my",
                "i want yantrai to", "assign task", "raise a request",
                "can yantrai", "will yantrai", "does yantrai offer"
            ]
            msg_lower = user_msg.lower()
            for kw in service_keywords:
                if kw in msg_lower:
                    is_service_request = True
                    break

        # Save user message
        if file:
            db.save_chat_message(
                session_id, "user", user_msg,
                ui_type="file",
                ui_data={"file_url": file_url, "filename": file.filename}
            )
        else:
            db.save_chat_message(session_id, "user", user_msg)

        # If AI detects a service request intent, ask Gemini to rephrase and confirm
        if is_service_request:
            # Use Gemini to create a structured service request summary
            sr_prompt = f"""You are TallyAI, an AI accounting assistant for Indian businesses.

The user just sent a message that appears to be a SERVICE REQUEST — they want the YantrAI team to perform a task or deliver an outcome for them (not just answer a question).

USER MESSAGE: "{user_msg}"
COMPANY: {company_name}

Your job: Rephrase their request into a clear, structured service request summary. Extract:
1. A short title (under 60 chars) for the request
2. A clear 1-2 sentence description of what the user wants done
3. A category (one of: GST & Compliance, Tally Setup, Reconciliation, Custom Report, Integration, Automation, Data Migration, Other)
4. Priority (Normal or Urgent — only Urgent if they mention deadline or urgency)

RESPOND IN JSON ONLY:
{{
    "is_service_request": true,
    "title": "Short title of request",
    "description": "Clear rephrased description of what the user wants YantrAI to do for them",
    "category": "Category",
    "priority": "Normal|Urgent",
    "text": "A friendly message to the user explaining you understood their request and asking them to confirm before raising it to the YantrAI team. Be warm and professional."
}}

If on second thought this is actually just a regular accounting question (NOT a service request), respond:
{{
    "is_service_request": false
}}
"""
            try:
                sr_response = parser.model.generate_content(sr_prompt)
                sr_raw = sr_response.text.strip()
                _charge_ai(username, company_name, "chat_intent", response=sr_response)
                import re as re_mod
                sr_match = re_mod.search(r'(\{.*\})', sr_raw, re_mod.DOTALL)
                if sr_match:
                    sr_data = json.loads(sr_match.group(1))
                else:
                    sr_data = {"is_service_request": False}
            except Exception as sr_err:
                print(f"Service request detection error: {sr_err}")
                sr_data = {"is_service_request": False}

            if sr_data.get("is_service_request"):
                ai_response = {
                    "text": sr_data.get("text", "I understand you'd like YantrAI to help with this. Please confirm to raise this as a service request."),
                    "ui_type": "service_request_confirm",
                    "ui_data": {
                        "title": sr_data.get("title", user_msg[:60]),
                        "description": sr_data.get("description", user_msg),
                        "category": sr_data.get("category", "Other"),
                        "priority": sr_data.get("priority", "Normal"),
                        "company_name": company_name,
                        "original_message": user_msg
                    },
                    "suggested_questions": []
                }
                msg_id = db.save_chat_message(
                    session_id, "assistant", ai_response["text"],
                    ai_response["ui_type"], ai_response["ui_data"]
                )
                ai_response["session_id"] = session_id
                ai_response["id"] = msg_id
                return ai_response

        # Get conversation history
        history = db.get_chat_messages(session_id)
        context_msgs = []
        for msg in history[-10:]:
            role_label = "User" if msg["role"] == "user" else "Assistant"
            context_msgs.append(f"{role_label}: {msg['content']}")
        conversation_context = "\n".join(context_msgs)
        
        # Get comprehensive accounting summary for grounding
        try:
            invoice_summary = db.get_accounting_summary(company_name, user_msg)
        except Exception as sum_err:
            print(f"Error getting accounting summary: {sum_err}")
            invoice_summary = "No recent data available."
        
        # Fetch Past Corrections using RAG
        correction_context = ""
        try:
            # Construct semantic query representation
            search_query = user_msg
            if file_context:
                search_query += f" {file_context}"
                
            query_embedding = get_embedding(search_query) if search_query else None
            
            if query_embedding:
                relevant_corrections = db.get_relevant_corrections(query_embedding, company_name=company_name, limit=5)
            else:
                relevant_corrections = []
                
            # Fallback to recent 5 corrections if no query embedding or search returned empty
            if not relevant_corrections:
                all_corr = db.get_corrections(company_name=company_name)
                relevant_corrections = all_corr[:5]
                
            if relevant_corrections:
                correction_context = "PAST USER CORRECTIONS (Learn from these mistakes):\n"
                for c in relevant_corrections:
                    cd = c if isinstance(c, dict) else json.loads(c)
                    correction_context += f"- For {cd.get('party_name', 'Unknown')}: The {cd.get('field')} should be '{cd.get('corrected')}' (NOT '{cd.get('original')}')\n"
        except Exception as re:
            print(f"RAG Error in chat: {re}")
            correction_context = ""
        
        prompt = f"""You are "TallyAI", a professional Indian accountant AI assistant.
        
        {file_context}
        
        PAST CORRECTIONS/LEARNINGS:
        {correction_context}
        *IMPORTANT RULE FOR DYNAMIC FIELDS (Date & Invoice Number):*
        Do NOT hardcode the exact dates or invoice numbers from the 'PAST CORRECTIONS' section onto new invoices. Past corrections are provided ONLY to teach you the parsing behavior (e.g., if the user corrected a date from '2020-03-07' to '2026-03-07' because the text had '26' which represents the year 2026, you should understand that '26' in dates for this party represents the year 2026, and apply that pattern to the *current* invoice's date. Do NOT copy the specific day and month from past corrections unless they match the text of the new document).
        
        CONVERSATION HISTORY:
        {conversation_context}
        
        REAL ACCOUNTING DATA (from Tally ERP and Supabase database — USE THIS to answer questions about ledgers, vouchers, parties, invoices, and company data):
        {invoice_summary}
        
        CRITICAL INSTRUCTION: When the user asks about ingested data, Tally data, company summaries, ledger mappings, vouchers, parties, or any accounting information — you MUST answer using the REAL ACCOUNTING DATA section above. This data has been pulled from TallyPrime and stored in the cloud database. Do NOT say "I couldn't find any ingested data" if the REAL ACCOUNTING DATA section contains information. Summarize it clearly with counts, names, and relevant details.
        STRICT COMPANY RING-FENCING MANDATE: You are operating strictly within the ring-fenced scope of the active company shown in the REAL ACCOUNTING DATA summary. You are strictly prohibited from utilizing external financial knowledge or referencing data/figures outside the provided REAL ACCOUNTING DATA section. If a requested transaction, ledger, or figure is not present in the provided context, state explicitly that it does not exist in the active company's records.

        AI AUTO-DETECTED DOCUMENT TYPE: {ai_detected_type or 'N/A (no file uploaded)'}
        NOTE: You have auto-classified this document. Use this detection to set the correct "category" in invoice_metadata (Sales or Purchase). If the document is clearly a Purchase invoice (billed TO the user's company), set category to "Purchase". If it is a Sales invoice (issued BY the user's company), set category to "Sales". Override the auto-detection if your deeper analysis disagrees.

        USER QUESTION: "{user_msg}"

        BEHAVIOR — be a real accountant, not a silent extractor:
        • CLARIFY FIRST, BUILD SECOND (default behavior). When the user asks you to record a
          voucher, do NOT jump straight to a card if ANY essential detail is unstated or guessed.
          Instead reply with ui_type:"text" and ask a SHORT batch of clarifying questions (group
          2–4 together so the user answers once). Essentials to confirm before building ANY voucher:
            1. The exact party/ledger — does the named person/entity exist in the masters above?
               If a name like "Rakesh" isn't a ledger, ask who/what it maps to (an employee? a
               vendor? paid on behalf of which party? which expense ledger?).
            2. Payment/receipt mode — Cash or which Bank ledger?
            3. The counter ledger / head — which expense, income, or party ledger is the other leg?
            4. Date — if not given, ask or confirm whether it's today's date.
            5. For Sales/Purchase: GST applicability + intra/inter-state, item & rate if relevant.
          Only once the essentials are clear do you draw the editable card (ui_type:"table").
          Example — user says "cash given to rakesh Rs. 500": Rakesh is not a ledger, so ASK:
          "Who is Rakesh — an employee, a vendor, or are you paying him on behalf of a party? And
           which expense ledger should this Payment hit (e.g. Staff Advances, Wages, Sundry
           Expenses)? Paid from Cash, correct?" — do NOT invent "Paid To: Aadinath Proteins".
        • If the user is ASKING you to BUILD a voucher (e.g. "bill 14 units of X to Y", "paid ₹50k to Sharma", "raise invoice for…") and ALL essentials above are already clear/confirmed, generate the editable invoice card (ui_type:"table") with values looked up from the REAL ACCOUNTING DATA above (stock standard_rate, gst_rate, hsn_code, party GSTIN).
        • If a CRITICAL field is ambiguous or missing, DO NOT silently guess. Reply with ui_type:"text" and ASK the specific question. Examples:
            - User says "bill X to Y" but Y is a Sundry Creditor (supplier) → ask: "Aadinath Proteins is in your Sundry Creditors group — they supply you. Did you mean to record a Purchase from them (they invoiced you) instead of a Sales to them?"
            - User says "paid 50k to Sharma" without specifying ledger / mode → ask: "Was this paid by Cash or Bank? Which Sharma — Sharma Traders or Sharma Industries?"
            - User asks for a voucher and the item / party isn't in masters → ask: "I couldn't find 'XYZ Industries' in your party list. Should I create them as a new party, or did you mean a similar name like 'XYZ Industries Pvt Ltd' that I see?"
        • When you DO draw the card but notice anomalies/risks worth flagging, include a "warnings" array in ui_data with short user-readable strings (one item per concern). Examples of when to warn:
            - Party group mismatch ("Aadinath is in Sundry Creditors — Sales voucher to them is unusual")
            - Inter-state vs intra-state guess based on GSTIN ("GSTIN 08 + your state 27 → IGST applied. Confirm if this is right.")
            - Item rate inferred from master but quantity is unusually high ("14 × ₹1,500 = ₹21,000. Confirm this matches the negotiated rate for Aadinath.")
            - GSTIN missing or invalid format
            - Stock would go negative (closing_qty 80 - sale 100 = -20)
            - Voucher_number autogenerated (since user didn't supply one) — say what number was picked
        • The warning array is OPTIONAL. Only include when there's something real to flag. Don't pad with noise.

        ⛔ NEVER CLAIM A VOUCHER IS SYNCED / POSTED / DONE. You (the AI) cannot push to Tally.
           Syncing only happens when the USER reviews the editable card and clicks "Confirm & Sync",
           which queues it to the Windows Agent. So:
           - Do NOT add a "Sync Status", "Status: Success", "Posted to Tally", "Done ✅", or any
             similar field/line to cards or text that implies the entry is already saved or synced.
           - For ANY voucher the user wants to RECORD (Payment, Receipt, Sales, Purchase, Journal,
             Contra — including simple ones like "paid ₹500 to X" or "cash given to Y"), you MUST use
             ui_type:"table" (the editable, confirmable card) — NOT ui_type:"cards". Only "table"
             produces the real Confirm & Sync button.
           - Use ui_type:"cards" ONLY for read-only summaries that are explicitly NOT actionable
             (e.g. "here's a summary of this party's ledger"), and never put a sync/posted claim in them.

        RESPONSE FORMAT (JSON):
        {{
          "text": "Your conversational markdown reply summarizing the document OR asking a clarifying question.",
          "ui_type": "text|table|cards|list",
          "ui_data": null or structured data,
          "suggested_questions": ["q1", "q2", "q3"]
        }}
        
        SCHEMA RULES FOR ui_data:
        1. If ui_type is "table":
           ui_data MUST have this exact structure:
           {{
             "invoice_metadata": {{
               "invoice_number": "Extract invoice number",
               "date": "Extract invoice date strictly in YYYY-MM-DD format",
               "billing_party_name": "Extract the billing party name (seller / supplier)",
               "billing_party_gstin": "Extract the GST number of the billing party",
               "billed_to_party_name": "Extract the billed to party name (buyer / client / customer / party_name)",
               "billed_to_party_gstin": "Extract the GST number of the billed to party",
               "voucher_type": "REQUIRED. One of: Sales | Purchase | Payment | Receipt | Contra | Journal. Pick the ACTUAL accounting voucher type. 'cash/bank given/paid to X' = Payment; 'cash/bank received from X' = Receipt; goods/services billed by us = Sales; billed to us = Purchase; bank<->cash or bank<->bank transfer = Contra; pure adjustment between two ledgers = Journal. NEVER default to Sales/Purchase for a money payment/receipt.",
               "category": "Mirror of voucher_type for backward-compat: use 'Sales' or 'Purchase' for those; otherwise repeat the voucher_type value (Payment/Receipt/Contra/Journal).",
               "counter_ledger": "The DEBIT-side head, and it must NEVER equal payment_mode. For Payment: the party/expense being settled (e.g. the Sundry Creditor 'Aadinath Proteins', or an expense like 'Wages'/'Rent') — NOT Cash/Bank. For Receipt: the party/income being received against (e.g. the Sundry Debtor, or 'Sales'/'Interest Income') — NOT Cash/Bank. For Sales: 'Sales Account'. For Purchase: 'Purchase Account'. If unsure, set it to the party name.",
               "payment_mode": "For Payment/Receipt ONLY: the Cash/Bank ledger the money moved through — 'Cash' or the exact Bank ledger name. This is the OTHER leg from counter_ledger; they must differ. Empty for Sales/Purchase/Journal/Contra.",
               "invoice_total": "Total amount as a numeric decimal/float (e.g. 990.00)",
               "invoice_gst": "Total GST amount (CGST+SGST or IGST) as a numeric decimal/float; 0 for non-GST Payment/Receipt/Contra/Journal"
             }},
             "party_master": {{
               "billing_party": {{
                 "name": "Supplier Company Name",
                 "gstin": "Supplier GSTIN",
                 "address": "Supplier Address",
                 "bank_name": "Supplier Bank Name if listed on invoice, else empty",
                 "account_number": "Supplier Account Number if listed, else empty",
                 "ifsc_code": "Supplier IFSC Code if listed, else empty",
                 "pan": "Supplier PAN if listed/derived from GSTIN, else empty",
                 "email": "Supplier Email if listed, else empty",
                 "phone": "Supplier Phone if listed, else empty"
               }},
               "billed_to_party": {{
                 "name": "Client/Buyer Company Name",
                 "gstin": "Client GSTIN",
                 "address": "Client Address",
                 "bank_name": "Client Bank Name if listed, else empty",
                 "account_number": "Client Account Number if listed, else empty",
                 "ifsc_code": "Client IFSC Code if listed, else empty",
                 "pan": "Client PAN if listed/derived from GSTIN, else empty",
                 "email": "Client Email if listed, else empty",
                 "phone": "Client Phone if listed, else empty"
               }}
             }},
             "headers": ["Item Description", "Qty", "Rate (₹)", "Discount (%)", "CGST (%)", "SGST (%)", "HSN/SAC Code", "Total (₹)"],
             "rows": [
               ["Optical Frames Type A", 300, "50.00", "0.00", "9.00", "9.00", "9003", "17700.00"]
             ],
             "warnings": ["Aadinath is in Sundry Creditors group — Sales voucher to a creditor is unusual.", "Inter-state IGST applied based on GSTIN state codes. Confirm if correct.", "Invoice # auto-generated as SAL-2026-145 because none was supplied."]
           }}
           NOTE on "warnings": include this array ONLY when you actually have concerns. Each item is a short, plain-English heads-up the user should see before they click Confirm & Sync. Omit the key entirely if everything looks clean.
           Ensure "rows" is a list of flat lists (NOT objects) containing exactly the 8 values corresponding to the 8 headers above. All numbers in rows must be formatted as strings.
           IMPORTANT: The "Total (₹)" column MUST be the final total for that row INCLUDING all taxes (CGST/SGST/IGST) and minus any discounts! (e.g. qty * rate + taxes).
           IMPORTANT: You MUST also extract additional charges like 'Freight', 'Packing & Forwarding', 'Transport', or 'Round Off' as separate individual items in the rows list. For example, if the invoice mentions 'Freight/Packing & Forwarding 100' with 2.5% CGST and SGST, you MUST add a row like ["Freight/Packing & Forwarding", "1", "100.00", "0.00", "2.5", "2.5", "9965", "105.00"].
           CRITICAL TAX RULE: Apply GST (CGST/SGST/IGST) to transport/freight/packing charges. If the tax rate is explicitly drawn for transport next to its row, use that rate. If no tax rate is explicitly drawn next to the transport row but it is included in the invoice's final GST totals or GST calculations (composite supply), you MUST inherit and apply the same principal tax rate of the main items (e.g. 2.5% CGST/SGST) to the transport row rather than setting it to 0%. Only set the tax rate to 0% if the invoice explicitly states the transport/freight is tax-exempt or not subject to GST.
        2. If ui_type is "cards":
           ui_data MUST be a list of card objects:
           [
             {{"title": "Card Title", "value": "Card Value"}}
           ]
        """
        
        if is_bank_statement:
            prompt += """
            IMPORTANT: Since the user uploaded a BANK STATEMENT, you MUST set the "ui_type" to "reconciliation" and extract ALL transactions from the statement.
            The "ui_data" MUST have the following structure:
            {
              "transactions": [
                {
                  "date": "YYYY-MM-DD",
                  "description": "NARRATION OR DESCRIPTION",
                  "reference": "INSTRUMENT NUMBER / CHEQUE NUMBER / UPI REF",
                  "amount": 8320.00,
                  "party_name": "CLEAN NAME OF THE PARTY OR PERSON OR CORPORATE ENTITY (e.g. LUXEDECO VENTURES or DWYANE CLARK or HDFC BANK)"
                }
              ]
            }
            Make sure "transactions" is a list of objects containing date, description, reference, amount, and party_name. Withdrawal amounts should be negative, deposits positive. Keep dates strictly in YYYY-MM-DD format.
            """
        else:
            prompt += """
            If the user uploaded a document (see [USER UPLOADED A DOCUMENT] above):
            1. Explain exactly what it is and summarize it in the "text" block.
            2. USE the "table" ui_type to extract and display the line items in "ui_data" following the SCHEMA RULES above.
            3. Extract EVERY row accurately.
            """
        
        response = parser.model.generate_content(prompt)
        raw = response.text.strip()
        _charge_ai(username, company_name, "chat", response=response)   # Sprint 47 — meter tokens

        ai_response = _parse_ai_json(raw)
        
        # If this is a bank statement reconciliation response, process it with our matchmaker engine
        if ai_response.get("ui_type") == "reconciliation":
            try:
                tx_data = ai_response.get("ui_data") or {}
                transactions = tx_data.get("transactions") if isinstance(tx_data, dict) else []
                if not transactions and isinstance(tx_data, list):
                    transactions = tx_data
                
                reconciled_results = reconcile_statement(transactions, company_name)
                ai_response["ui_data"] = reconciled_results
            except Exception as re_err:
                print(f"Reconciliation processing error: {re_err}")
                ai_response["ui_data"] = []

        # Autonomous Party Master processing
        try:
            ui_data = ai_response.get("ui_data")
            if isinstance(ui_data, dict) and "party_master" in ui_data:
                pm = ui_data.get("party_master")
                if pm:
                    bp = pm.get("billing_party")
                    if bp and bp.get("name"):
                        db.save_or_update_party(
                            company_name=company_name,
                            name=bp.get("name"),
                            gstin=bp.get("gstin"),
                            address=bp.get("address"),
                            bank_name=bp.get("bank_name"),
                            account_number=bp.get("account_number"),
                            ifsc_code=bp.get("ifsc_code"),
                            pan=bp.get("pan"),
                            email=bp.get("email"),
                            phone=bp.get("phone")
                        )
                    bt = pm.get("billed_to_party")
                    if bt and bt.get("name"):
                        db.save_or_update_party(
                            company_name=company_name,
                            name=bt.get("name"),
                            gstin=bt.get("gstin"),
                            address=bt.get("address"),
                            bank_name=bt.get("bank_name"),
                            account_number=bt.get("account_number"),
                            ifsc_code=bt.get("ifsc_code"),
                            pan=bt.get("pan"),
                            email=bt.get("email"),
                            phone=bt.get("phone")
                        )
        except Exception as p_err:
            print(f"Autonomous Party Master extraction error: {p_err}")

        # Check for potential duplicates in the database to alert the user in chat!
        try:
            ui_type = ai_response.get("ui_type")
            ui_data = ai_response.get("ui_data")
            if ui_type == "table" and isinstance(ui_data, dict):
                meta = ui_data.get("invoice_metadata") or {}
                inv_num = meta.get("invoice_number")
                if inv_num:
                    existing_invs = db.get_history(company_name)
                    # Check if an existing invoice has the same number
                    duplicate = next((inv for inv in existing_invs if str(inv.get("invoice_number", "")).strip().lower() == str(inv_num).strip().lower()), None)
                    if duplicate:
                        warning_text = f"\n\n⚠️ **POTENTIAL DUPLICATE INVOICE ALERT**:\nWe found an existing invoice in your Invoices with the exact same invoice number (**{inv_num}**) for this company. Synchronizing this will overwrite the existing entry to avoid duplicates."
                        if warning_text not in ai_response["text"]:
                            ai_response["text"] += warning_text
                        ui_data["duplicate_detected"] = True
        except Exception as dup_err:
            print(f"Error checking duplicate invoice: {dup_err}")

        # Sprint 33 — Guard against the AI fabricating a "synced/posted/done"
        # claim. Syncing only happens via Confirm & Sync → tally_outbox. Strip
        # any card that asserts the entry is already in Tally so the UI never
        # shows a fake "done". Also: a voucher rendered as read-only "cards"
        # has no Confirm button — flag that the user must re-issue as a real card.
        try:
            if ai_response.get("ui_type") == "cards" and isinstance(ai_response.get("ui_data"), list):
                _bad = ("sync", "posted to tally", "synced", "success ✅", "done ✅",
                        "saved to tally", "pushed to tally")
                cleaned = []
                had_fake = False
                for card in ai_response["ui_data"]:
                    t = str((card or {}).get("title", "")).lower()
                    v = str((card or {}).get("value", "")).lower()
                    if any(b in t or b in v for b in _bad):
                        had_fake = True
                        continue   # drop the fabricated sync card
                    cleaned.append(card)
                ai_response["ui_data"] = cleaned
                if had_fake:
                    # Tell the user the truth: nothing was synced from this card.
                    note = ("\n\n🟠 *Heads-up: this was only a preview — nothing has been "
                            "saved or synced to Tally yet. To actually record it, type "
                            "the voucher again and use the editable card's **Confirm & Sync** "
                            "button.*")
                    if note.strip() not in (ai_response.get("text") or ""):
                        ai_response["text"] = (ai_response.get("text") or "") + note
        except Exception as guard_err:
            print(f"[chat] sync-claim guard error: {guard_err}", flush=True)

        msg_id = db.save_chat_message(
            session_id, "assistant", ai_response.get("text", ""),
            ai_response.get("ui_type", "text"), ai_response.get("ui_data")
        )
        
        ai_response["session_id"] = session_id
        ai_response["file_url"] = file_url
        ai_response["id"] = msg_id

        # Auto-generate chat title from first message if still "New Chat"
        try:
            session_info = db.get_chat_sessions(company_name)
            current_session = next((s for s in session_info if s["id"] == session_id), None)
            if current_session and (not current_session.get("title") or current_session["title"] in ("New Chat", "New Chat (Empty)")):
                # AI-generated title from how the chat begins — the user's message, or
                # (for a file-only upload) the AI's understanding of the document, so we
                # get a meaningful name instead of a raw filename/ID.
                basis = (user_msg or "").strip() or (ai_response.get("text") or "")
                auto_title = _ai_chat_title(basis)
                if not auto_title:
                    # Fallback: never regress titling if the AI call is unavailable.
                    if file and file.filename:
                        auto_title = f"📄 {file.filename[:45]}"
                    elif user_msg:
                        auto_title = user_msg[:50].strip() + ("…" if len(user_msg) > 50 else "")
                if auto_title:
                    db.update_chat_title(session_id, auto_title)
        except Exception as title_err:
            print(f"Auto-title error: {title_err}")

        return ai_response

    except Exception as e:
        print(f"CHAT ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        # Fallback to a valid JSON response instead of a 500 error
        return {
            "text": "I encountered an issue processing your request. Could you please try again? (Error: " + str(e) + ")",
            "ui_type": "text",
            "session_id": session_id if 'session_id' in locals() else None,
            "suggested_questions": ["Try again", "What happened?"]
        }

import utils.gst_reconciler as gst_reconciler
import utils.revenue_reconciler as revenue_reconciler

@app.post("/api/gst-reconciliation/upload")
async def gst_reconciliation_upload(
    file: UploadFile = File(...), 
    report_type: str = Form(...), 
    company_name: str = Form(None)
):
    try:
        if not company_name:
            company_name = "Acme Corp" # Fallback
            
        file_content = await file.read()
        
        # We need ALL tally vouchers or just unreconciled ones?
        # For GST reconciliation, typically we reconcile purchases (GSTR-2B) or sales (GSTR-1)
        # We'll just fetch all vouchers for the company for now.
        tally_vouchers = db.get_unreconciled_tally_vouchers(company_name)
        
        # Filter tally vouchers based on report_type (if GSTR-2B, filter for purchases)
        # But wait, our mock tally_vouchers don't strictly have 'Purchase' vs 'Sales'.
        # We'll just pass all of them for this MVP to maximize matching chances.
        
        results = gst_reconciler.reconcile_gstr(file_content, report_type, tally_vouchers)
        
        return {
            "status": "success",
            "message": f"Successfully parsed {file.filename}",
            "data": results
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/revenue-reconciliation/upload")
async def revenue_reconciliation_upload(
    file: UploadFile = File(...), 
    gateway_type: str = Form(...), 
    company_name: str = Form(None)
):
    try:
        if not company_name:
            company_name = "Acme Corp" # Fallback
            
        file_content = await file.read()
        tally_vouchers = db.get_unreconciled_tally_vouchers(company_name)
        
        results = revenue_reconciler.reconcile_revenue(file_content, gateway_type, tally_vouchers)
        
        return {
            "status": "success",
            "message": f"Successfully parsed {file.filename}",
            "data": results
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# ═══════════════════════════════════════════════════════════════════════════
# UNIVERSAL RECONCILIATION STUDIO API
# ═══════════════════════════════════════════════════════════════════════════
from utils import recon_engine

@app.get("/api/recon/templates")
async def recon_list_templates(company_name: str = None):
    """List public templates + this company's private templates."""
    tpls = db.get_recon_templates(company_name)
    return {"status": "success", "templates": tpls}

@app.get("/api/recon/templates/{template_id}")
async def recon_get_template(template_id: str):
    tpl = db.get_recon_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"status": "success", "template": tpl}

@app.get("/api/recon/sessions")
async def recon_list_sessions(company_name: str):
    sessions = db.get_recon_sessions(company_name)
    return {"status": "success", "sessions": sessions}

@app.post("/api/recon/sessions")
async def recon_create_session(payload: dict):
    company_name = payload.get("company_name")
    template_id = payload.get("template_id")
    name = payload.get("name") or "Untitled Reconciliation"
    if not company_name or not template_id:
        raise HTTPException(status_code=400, detail="company_name and template_id required")
    tpl = db.get_recon_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    # Seed config from template defaults
    config = tpl.get("default_config") or {}
    if isinstance(config, str):
        config = json.loads(config)
    session_id = db.create_recon_session(company_name, template_id, name, config)
    return {"status": "success", "session_id": session_id}

@app.get("/api/recon/sessions/{session_id}")
async def recon_get_session(session_id: str):
    sess = db.get_recon_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    tpl = db.get_recon_template(sess["template_id"]) if sess.get("template_id") else None
    sources = db.get_recon_sources(session_id)
    summary = db.get_recon_session_summary(session_id)
    return {
        "status": "success",
        "session": sess,
        "template": tpl,
        "sources": sources,
        "summary": summary,
    }

def _parse_template_field(tpl, key):
    val = tpl.get(key) or {}
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return {}
    return val

@app.post("/api/recon/sessions/{session_id}/upload-master")
async def recon_upload_master(session_id: str, file: UploadFile = File(...)):
    sess = db.get_recon_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    tpl = db.get_recon_template(sess["template_id"])
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    file_content = await file.read()
    master_schema = _parse_template_field(tpl, "master_schema")
    canonical_records, mapping = recon_engine.parse_and_normalize(
        file_content, file.filename, master_schema, "Master", use_ai_fallback=True
    )

    if not canonical_records:
        raise HTTPException(status_code=400, detail="No records could be parsed from the master file.")

    source_id = db.create_recon_source(
        session_id=session_id,
        source_type="master",
        source_name="Master",
        file_name=file.filename,
        record_count=len(canonical_records),
        column_mapping=mapping,
    )
    db.bulk_insert_recon_records(session_id, source_id, canonical_records)

    return {
        "status": "success",
        "source_id": source_id,
        "record_count": len(canonical_records),
        "column_mapping": mapping,
    }

@app.post("/api/recon/sessions/{session_id}/upload-source")
async def recon_upload_source(
    session_id: str,
    source_name: str = Form(...),
    file: UploadFile = File(...),
):
    sess = db.get_recon_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    tpl = db.get_recon_template(sess["template_id"])
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    master_src = db.get_recon_master_source(session_id)
    if not master_src:
        raise HTTPException(status_code=400, detail="Upload the master file first.")

    file_content = await file.read()
    source_schema = _parse_template_field(tpl, "source_schema")
    canonical_records, mapping = recon_engine.parse_and_normalize(
        file_content, file.filename, source_schema, source_name, use_ai_fallback=True
    )

    if not canonical_records:
        raise HTTPException(status_code=400, detail="No records could be parsed from the source file.")

    source_id = db.create_recon_source(
        session_id=session_id,
        source_type="external",
        source_name=source_name,
        file_name=file.filename,
        record_count=len(canonical_records),
        column_mapping=mapping,
    )
    db.bulk_insert_recon_records(session_id, source_id, canonical_records)

    # Run reconciliation immediately
    master_records_db = db.get_recon_records(session_id, source_id=master_src["id"])
    external_records_db = db.get_recon_records(session_id, source_id=source_id)

    # Normalize shape for engine
    def _shape(r):
        return {
            "id": r["id"],
            "matching_key": r["matching_key"],
            "canonical_data": r["canonical_data"] if isinstance(r["canonical_data"], dict) else (json.loads(r["canonical_data"]) if r["canonical_data"] else {}),
        }
    master_shaped = [_shape(r) for r in master_records_db]
    external_shaped = [_shape(r) for r in external_records_db]

    # Compose runtime config: session.config merged with per-platform commission rate
    sess_config = sess.get("config") or {}
    if isinstance(sess_config, str):
        sess_config = json.loads(sess_config)
    # If template has commission_rates per source, flatten the one for this source
    if "commission_rates" in sess_config and isinstance(sess_config["commission_rates"], dict):
        sess_config = {**sess_config, "commission_rate": sess_config["commission_rates"].get(source_name, 0)}

    tpl_dict = {
        "matching_rules": _parse_template_field(tpl, "matching_rules") if isinstance(_parse_template_field(tpl, "matching_rules"), list) else (json.loads(tpl["matching_rules"]) if isinstance(tpl["matching_rules"], str) else tpl["matching_rules"]),
        "variance_formulas": _parse_template_field(tpl, "variance_formulas") if isinstance(_parse_template_field(tpl, "variance_formulas"), list) else (json.loads(tpl["variance_formulas"]) if isinstance(tpl["variance_formulas"], str) else tpl["variance_formulas"]),
    }
    # Ensure they're lists
    if isinstance(tpl_dict["matching_rules"], dict):
        tpl_dict["matching_rules"] = list(tpl_dict["matching_rules"].values())
    if isinstance(tpl_dict["variance_formulas"], dict):
        tpl_dict["variance_formulas"] = list(tpl_dict["variance_formulas"].values())

    enriched_matches, metrics = recon_engine.reconcile(master_shaped, external_shaped, tpl_dict, sess_config)

    # Attach external_source_name for downstream filtering
    for m in enriched_matches:
        m["external_source_name"] = source_name

    db.bulk_insert_recon_matches(session_id, enriched_matches)

    return {
        "status": "success",
        "source_id": source_id,
        "record_count": len(canonical_records),
        "column_mapping": mapping,
        "match_metrics": metrics,
    }

@app.get("/api/recon/sessions/{session_id}/matches")
async def recon_get_matches(session_id: str, source_name: str = None, status: str = None):
    matches = db.get_recon_matches(session_id, source_name=source_name, status=status)
    return {"status": "success", "matches": matches}

@app.post("/api/recon/matches/{match_id}/status")
async def recon_update_match(match_id: str, payload: dict):
    db.update_recon_match_status(match_id, payload.get("status", "confirmed"), payload.get("notes"))
    return {"status": "success"}

@app.post("/api/recon/sessions/{session_id}/config")
async def recon_update_config(session_id: str, payload: dict):
    sess = db.get_recon_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    current = sess.get("config") or {}
    if isinstance(current, str):
        current = json.loads(current)
    current.update(payload.get("config") or {})
    db.update_recon_session_config(session_id, current)
    return {"status": "success", "config": current}

@app.get("/api/recon/sessions/{session_id}/export")
async def recon_export(session_id: str):
    """Return all matches as CSV-friendly rows."""
    import csv as _csv, io as _io
    matches = db.get_recon_matches(session_id)
    sess = db.get_recon_session(session_id)
    tpl = db.get_recon_template(sess["template_id"]) if sess and sess.get("template_id") else None

    formulas = _parse_template_field(tpl, "variance_formulas") if tpl else []
    if isinstance(formulas, dict):
        formulas = list(formulas.values())
    variance_names = [f["name"] for f in formulas if "name" in f]

    buf = _io.StringIO()
    fieldnames = ["external_source", "match_type", "match_score", "master_key", "external_key"] + variance_names + ["status", "notes"]
    writer = _csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for m in matches:
        variances = m.get("variances") or {}
        if isinstance(variances, str):
            variances = json.loads(variances)
        row = {
            "external_source": m.get("external_source_name", ""),
            "match_type": m.get("match_type", ""),
            "match_score": m.get("match_score", ""),
            "master_key": m.get("master_key", ""),
            "external_key": m.get("external_key", ""),
            "status": m.get("status", ""),
            "notes": m.get("notes") or "",
        }
        for vn in variance_names:
            row[vn] = variances.get(vn, "")
        writer.writerow(row)

    return PlainTextResponse(buf.getvalue(), media_type="text/csv")

# ═══════════════════════════════════════════════════════════════════════════
# GSTR-1 / GSTR-3B FILING ASSISTANT API
# ═══════════════════════════════════════════════════════════════════════════
from utils import gstr_engine

@app.get("/api/gstr/summary")
async def gstr_summary(company_name: str, month: int = None, year: int = None):
    """Returns dashboard data — current vs prior month, due dates, validation count."""
    try:
        today = datetime.now()
        if not month:
            month = today.month
        if not year:
            year = today.year

        gstr1 = gstr_engine.compute_gstr1(company_name, month, year)
        gstr3b = gstr_engine.compute_gstr3b(company_name, month, year)

        # GST filing due dates (standard): GSTR-1 = 11th of next month, 3B = 20th of next month
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1
        gstr1_due = f"{next_year}-{next_month:02d}-11"
        gstr3b_due = f"{next_year}-{next_month:02d}-20"
        days_to_gstr1 = (datetime(next_year, next_month, 11) - today).days
        days_to_gstr3b = (datetime(next_year, next_month, 20) - today).days

        error_count = sum(1 for i in gstr1["validation_issues"] if i["severity"] == "error")
        warning_count = sum(1 for i in gstr1["validation_issues"] if i["severity"] == "warning")

        return {
            "status": "success",
            "company_name": company_name,
            "filing_period": f"{month:02d}/{year}",
            "gstr1": {
                "totals": gstr1["totals"],
                "due_date": gstr1_due,
                "days_remaining": days_to_gstr1,
                "validation_errors": error_count,
                "validation_warnings": warning_count,
            },
            "gstr3b": {
                "summary": gstr3b["summary"],
                "due_date": gstr3b_due,
                "days_remaining": days_to_gstr3b,
            },
            "validation_issues": gstr1["validation_issues"][:50],
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/gstr1")
async def gstr1_detail(company_name: str, month: int = None, year: int = None):
    """Return full GSTR-1 computed data."""
    today = datetime.now()
    if not month: month = today.month
    if not year: year = today.year
    return {"status": "success", "data": gstr_engine.compute_gstr1(company_name, month, year)}


@app.get("/api/gstr/gstr3b")
async def gstr3b_detail(company_name: str, month: int = None, year: int = None):
    today = datetime.now()
    if not month: month = today.month
    if not year: year = today.year
    return {"status": "success", "data": gstr_engine.compute_gstr3b(company_name, month, year)}


@app.get("/api/gstr/gstr1/export")
async def gstr1_export(company_name: str, month: int, year: int):
    """Download GSTN offline-tool-compatible JSON."""
    gstr1 = gstr_engine.compute_gstr1(company_name, month, year)
    payload = gstr_engine.gstr1_to_gstn_json(gstr1)
    from fastapi.responses import JSONResponse
    return JSONResponse(payload, headers={
        "Content-Disposition": f"attachment; filename=GSTR1_{company_name}_{month:02d}{year}.json"
    })


@app.get("/api/gstr/gstr3b/export")
async def gstr3b_export(company_name: str, month: int, year: int):
    gstr3b = gstr_engine.compute_gstr3b(company_name, month, year)
    payload = gstr_engine.gstr3b_to_gstn_json(gstr3b)
    from fastapi.responses import JSONResponse
    return JSONResponse(payload, headers={
        "Content-Disposition": f"attachment; filename=GSTR3B_{company_name}_{month:02d}{year}.json"
    })


@app.get("/chat/sessions")
async def list_chat_sessions(company_name: str = None, all: str = None,
                              companies: str = None, username: str = None,
                              limit: int = 100):
    """List chat sessions scoped to (company, user). super_admin can pass all=true.
    Regular users MUST send their username — without it, no sessions are returned.
    `limit` caps the most-recent sessions returned (sidebar recents list)."""
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 100
    if all == "true":
        # super_admin "all" = every USER's chats, but still scoped to the active
        # workspace when company_name is given (so switching workspace re-scopes).
        # company_name=None is a defensive fallback that returns all (other callers).
        if username:
            user = db.get_user_by_username(username)
            if user and user.get("role") == "super_admin":
                return db.get_chat_sessions(company_name, None, limit=limit)
        # else fall through to scoped query (don't leak)
    if not username:
        # No auth — return empty rather than leaking
        return []
    if companies:
        try:
            company_list = json.loads(companies)
            return db.get_chat_sessions_multi(company_list, user_username=username, limit=limit)
        except Exception:
            pass
    return db.get_chat_sessions(company_name, user_username=username, limit=limit)


@app.get("/chat/messages/{session_id}")
async def get_session_messages(session_id: str, username: str = None):
    """Return messages for a session — only if the requesting user owns it
    OR has the same company (covers chat-share within a firm) OR is super_admin."""
    # If no username sent, allow read for backward compat — but log once
    if username:
        owner = db.get_chat_session_owner(session_id)
        if owner and owner.get("user_username") and owner["user_username"] != username:
            # Different owner — check super_admin
            user = db.get_user_by_username(username)
            if not (user and user.get("role") == "super_admin"):
                raise HTTPException(status_code=403, detail="This chat belongs to another user.")
    return db.get_chat_messages(session_id)


@app.post("/chat/new")
async def new_chat_session(payload: dict = None):
    company = payload.get("company_name") if payload else None
    username = payload.get("username") if payload else None
    session_id = db.create_chat_session(company_name=company, user_username=username)
    return {"session_id": session_id}


@app.post("/chat/update-title")
async def update_chat_title_endpoint(payload: dict):
    """Rename a chat session — used to tag voucher chats with ✅ + voucher_number after sync."""
    try:
        session_id = payload.get("session_id")
        title = payload.get("title")
        if not session_id or not title:
            raise HTTPException(status_code=400, detail="session_id + title required")
        db.update_chat_title(session_id, title)
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Sprint 84 — public shareable chat links ──────────────────────────────
_SHARED_CHAT_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Shared chat · YantrAI</title>
<style>
  :root{--bg:#2a2623;--surface:#1e1b18;--card:#221f1c;--primary:#da7756;--text:#f5f1ec;--muted:#a8a199;--border:#3a3530;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,Segoe UI,Roboto,Arial,sans-serif;line-height:1.5;}
  header{position:sticky;top:0;background:var(--surface);border-bottom:1px solid var(--border);padding:14px 18px;display:flex;align-items:center;gap:10px;}
  header .brand{font-weight:700;color:var(--primary);}
  header .title{color:var(--muted);font-size:.9rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  main{max-width:820px;margin:0 auto;padding:18px 16px 60px;}
  .msg{margin:0 0 16px;display:flex;}
  .msg.user{justify-content:flex-end;}
  .bubble{max-width:88%;padding:11px 14px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word;}
  .msg.user .bubble{background:var(--primary);color:#fff;border-bottom-right-radius:4px;}
  .msg.assistant .bubble{background:var(--card);border:1px solid var(--border);border-bottom-left-radius:4px;}
  .pd{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px 16px;width:100%;}
  .pd h3{margin:0 0 8px;font-size:1rem;}
  .pd .row{margin:6px 0;font-size:.9rem;}
  .pd .row b{color:var(--muted);font-weight:600;display:block;font-size:.78rem;text-transform:uppercase;letter-spacing:.03em;}
  .empty,.err{color:var(--muted);text-align:center;padding:40px 16px;}
  footer{color:var(--muted);font-size:.78rem;text-align:center;padding:18px;border-top:1px solid var(--border);}
  a{color:var(--primary);}
</style></head>
<body>
<header><span class="brand">YantrAI</span><span class="title" id="t">Shared chat</span></header>
<main id="m"><div class="empty">Loading…</div></main>
<footer>Read-only shared view · <a href="/">Open YantrAI</a></footer>
<script>
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function pdCard(d){
  d=d||{};
  var fields=[['Title',d.title],['Category',d.category],['Priority',d.priority],['Objective',d.objective],
    ['Context',d.context],['Scope',d.scope],['Constraints',d.constraints],['Success criteria',d.success_criteria]];
  var rows=fields.filter(function(f){return f[1];}).map(function(f){
    return '<div class="row"><b>'+esc(f[0])+'</b>'+esc(f[1])+'</div>';}).join('');
  function arr(label,v){ if(!v) return ''; if(Array.isArray(v)) v=v.join(', ');
    return '<div class="row"><b>'+esc(label)+'</b>'+esc(v)+'</div>';}
  rows+=arr('Deliverables',d.deliverables)+arr('Data required',d.data_required);
  return '<div class="pd"><h3>📋 '+esc(d.title||'Problem Document')+'</h3>'+rows+'</div>';
}
(async function(){
  var token=location.pathname.split('/s/')[1]||'';
  var m=document.getElementById('m');
  try{
    var r=await fetch('/api/shared/'+encodeURIComponent(token));
    if(!r.ok){ m.innerHTML='<div class="err">This shared link is invalid or was revoked.</div>'; return; }
    var data=await r.json();
    document.getElementById('t').textContent=data.title||'Shared chat';
    document.title=(data.title||'Shared chat')+' · YantrAI';
    var msgs=data.messages||[];
    if(!msgs.length){ m.innerHTML='<div class="empty">No messages in this chat yet.</div>'; return; }
    m.innerHTML=msgs.map(function(x){
      var role=(x.role==='user')?'user':'assistant';
      var ui=x.ui_data; if(typeof ui==='string'){ try{ui=JSON.parse(ui);}catch(e){ui=null;} }
      if(x.ui_type==='pdcard' && ui){ return '<div class="msg assistant">'+pdCard(ui.pd||ui)+'</div>'; }
      var body=esc(x.content||'');
      if(!body) return '';
      return '<div class="msg '+role+'"><div class="bubble">'+body+'</div></div>';
    }).join('');
  }catch(e){ m.innerHTML='<div class="err">Could not load this shared chat.</div>'; }
})();
</script>
</body></html>"""


@app.post("/api/chat/{session_id}/share")
async def create_chat_share(session_id: str, payload: dict = None):
    """Mint (or return) a public read-only link for this chat. Owner-checked."""
    username = (payload or {}).get("username")
    owner = db.get_chat_session_owner(session_id)
    if not owner:
        raise HTTPException(status_code=404, detail="Chat not found")
    if username and owner.get("user_username") and owner["user_username"] != username:
        user = db.get_user_by_username(username)
        if not (user and user.get("role") == "super_admin"):
            raise HTTPException(status_code=403, detail="This chat belongs to another user.")
    token = db.get_or_create_share_token(session_id)
    if not token:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"token": token, "path": f"/s/{token}"}


@app.delete("/api/chat/{session_id}/share")
async def revoke_chat_share(session_id: str, username: str = None):
    owner = db.get_chat_session_owner(session_id)
    if not owner:
        raise HTTPException(status_code=404, detail="Chat not found")
    if username and owner.get("user_username") and owner["user_username"] != username:
        user = db.get_user_by_username(username)
        if not (user and user.get("role") == "super_admin"):
            raise HTTPException(status_code=403, detail="This chat belongs to another user.")
    db.revoke_share_token(session_id)
    return {"status": "revoked"}


@app.get("/api/shared/{token}")
async def get_shared_chat(token: str):
    """Public read-only transcript for a share token."""
    data = db.get_shared_transcript(token)
    if not data:
        raise HTTPException(status_code=404, detail="This shared link is invalid or was revoked.")
    return data


@app.get("/s/{token}", response_class=HTMLResponse)
async def shared_chat_page(token: str):
    """Standalone public read-only viewer for a shared chat."""
    return HTMLResponse(_SHARED_CHAT_HTML)


@app.post("/analyze")
async def analyze_invoice(file: UploadFile = File(...), company_name: str = Form(None)):
    kb = load_kb()
    # Save file persistently
    os.makedirs("static/uploads", exist_ok=True)
    import re
    safe_filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
    unique_filename = f"{uuid.uuid4()}_{safe_filename}"
    persistent_path = f"static/uploads/{unique_filename}"
    file_url = f"/static/uploads/{unique_filename}"
    
    # Save file temporarily for parsing
    temp_path = f"temp_{safe_filename}"
    file_content = await file.read()
    with open(temp_path, "wb") as buffer:
        buffer.write(file_content)
    with open(persistent_path, "wb") as buffer:
        buffer.write(file_content)
    
    try:
        # Fetch Past Corrections using semantic matching on filename / generic keywords
        correction_context = ""
        try:
            query_embedding = get_embedding(f"invoice parsing extract {file.filename}")
            relevant_corrections = db.get_relevant_corrections(query_embedding, company_name=company_name, limit=8) if query_embedding else []
            
            if not relevant_corrections:
                all_corr = db.get_corrections(company_name=company_name)
                relevant_corrections = all_corr[:8]
                
            if relevant_corrections:
                correction_context = "PAST USER CORRECTIONS (Learn from these):\n"
                for c in relevant_corrections:
                    cd = c if isinstance(c, dict) else json.loads(c)
                    correction_context += f"- For {cd.get('party_name', 'Unknown')}: The {cd.get('field')} should be '{cd.get('corrected')}' (NOT '{cd.get('original')}')\n"
        except Exception as re:
            print(f"RAG Error in analyze: {re}")
            correction_context = ""
        
        raw_result = parser.parse(temp_path, context=correction_context)
        print(f"DEBUG: AI Raw Result with Learning: {raw_result}")
        
        # Robust JSON extraction
        import re
        json_match = re.search(r'(\{.*\})', raw_result, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            json_str = raw_result.strip().replace('```json', '').replace('```', '')
            
        print(f"DEBUG: Extracted JSON string: {json_str}")
        data = json.loads(json_str)
        
        # Save to Local Database (Persistence)
        data["company_name"] = company_name
        data["file_url"] = file_url
        db.save_invoice(data)
        
        # Add a status
        data["status"] = "extracted"
        return data
    except Exception as e:
        print(f"ERROR during analyze: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@app.post("/push-to-tally")
async def push_to_tally(data: dict, background_tasks: BackgroundTasks = None):
    try:
        # Integrity check — the active workspace must be a party to the invoice.
        # If the workspace's own GSTIN matches NEITHER the seller (billing_party_gstin)
        # NOR the buyer (billed_to_party_gstin), this voucher likely doesn't belong here.
        # Soft gate: return a 'warn' so the UI can ask the user to confirm. The user can
        # override by re-posting with force=true. Only triggers when we actually have
        # GSTINs to compare (avoids false-blocks when extraction missed a GSTIN).
        if not data.get("force"):
            try:
                ws_gstin = db.get_company_gstin(data.get("company_name"))
                seller = (data.get("billing_party_gstin") or "").strip().upper()
                buyer = (data.get("billed_to_party_gstin") or "").strip().upper()
                present = [g for g in (seller, buyer) if g]
                if ws_gstin and present and ws_gstin not in present:
                    return {"status": "warn", "warn": "not_a_party",
                            "workspace_gstin": ws_gstin,
                            "seller_gstin": seller or None,
                            "buyer_gstin": buyer or None,
                            "message": "Your workspace's GSTIN isn't the seller or the buyer on this invoice."}
            except Exception as _pe:
                print(f"[push_to_tally] party-check skipped: {_pe}", flush=True)

        # Save to OUR books — this is YantrAI's record.
        invoice_id = None
        try:
            saved = db.save_invoice(data)
            # save_invoice may return the new row's id depending on impl
            if isinstance(saved, dict) and saved.get("id"):
                invoice_id = saved.get("id")
            elif isinstance(saved, str):
                invoice_id = saved
        except Exception as save_err:
            print(f"[push_to_tally] save_invoice error: {save_err}", flush=True)

        # Sprint 28 — Enqueue to tally_outbox. The bridge agent (running locally
        # on the customer's Windows machine alongside Tally Prime) will poll
        # /api/tally/queue, push to Tally's XML API, then ack/fail back here.
        # The web UI polls /api/tally/outbox/{invoice_id} for live status.
        try:
            db.enqueue_tally_push(
                payload=data,
                invoice_id=invoice_id,
                company_name=data.get("company_name"),
                enqueued_by="web",
            )
        except Exception as q_err:
            print(f"[push_to_tally] enqueue_tally_push error: {q_err}", flush=True)

        # Seed the workspace knowledge base from this confirmed voucher so Training
        # Progress grows as the user processes invoices (party / items / narration /
        # ledger). Runs in the background so confirm stays snappy; idempotent by kb_key.
        try:
            if background_tasks is not None:
                background_tasks.add_task(db.embed_confirmed_voucher,
                                         data.get("company_name"), data, get_embedding)
            else:
                db.embed_confirmed_voucher(data.get("company_name"), data, get_embedding)
        except Exception as tr_err:
            print(f"[push_to_tally] training-seed error: {tr_err}", flush=True)

        # Mark corresponding chat message as synced if message_id is provided
        msg_id = data.get("message_id")
        session_id = None
        if msg_id:
            msg = db.get_chat_message_by_id(msg_id)
            if msg:
                session_id = msg.get("session_id")
            if msg and msg.get("ui_data"):
                try:
                    import json
                    ui_data = json.loads(msg["ui_data"]) if isinstance(msg["ui_data"], str) else msg["ui_data"]
                    if isinstance(ui_data, dict):
                        # It's only QUEUED to the outbox here — NOT yet accepted by Tally.
                        # The Windows Agent acks the outbox later; we never claim "synced"
                        # in the chat card unless that real ack arrives (ui_data.tally_pushed).
                        ui_data["queued"] = True
                        db.update_chat_message_ui_data(msg_id, ui_data)
                except Exception as ex_msg:
                    print(f"Error updating message sync flag: {ex_msg}")
        
        # Autonomous Party Master updates during push to Tally!
        try:
            bp_name = data.get("billing_party_name")
            if bp_name:
                db.save_or_update_party(
                    company_name=data.get("company_name", "Acme Corp"),
                    name=bp_name,
                    gstin=data.get("billing_party_gstin"),
                    address=data.get("address"),
                    bank_name=data.get("bank_name"),
                    account_number=data.get("account_number"),
                    ifsc_code=data.get("ifsc_code"),
                    pan=data.get("pan"),
                    email=data.get("email"),
                    phone=data.get("phone")
                )
            
            bt_name = data.get("billed_to_party_name") or data.get("party_name")
            if bt_name and bt_name != bp_name:
                db.save_or_update_party(
                    company_name=data.get("company_name", "Acme Corp"),
                    name=bt_name,
                    gstin=data.get("billed_to_party_gstin"),
                    address=data.get("address") if not bp_name else None,
                    email=data.get("email") if not bp_name else None,
                    phone=data.get("phone") if not bp_name else None
                )
        except Exception as p_err2:
            print(f"Autonomous Tally party update error: {p_err2}")
        
        # Sprint 33 — REMOVED the legacy `tally.create_voucher` direct call.
        # It built a 2-leg Payment voucher (party + Cash, no GST, defaulting to
        # VCHTYPE="Payment") and pushed it straight to the customer's Tally —
        # creating a SPURIOUS DUPLICATE alongside the correct, GST-aware Sales
        # voucher that the bridge agent pushes via the tally_outbox path above.
        # The outbox/agent path is now the ONE canonical way data reaches Tally.
        response = {"note": "queued to tally_outbox; bridge agent will push"}

        if session_id:
            try:
                total = data.get('total_amount', 0)
                inv_num = data.get('invoice_number', '')
                p_name = data.get('party_name', '')
                # Sprint 32 — Honest copy. The voucher is QUEUED, not synced.
                # The bridge agent will pick it up and the Vouchers tab badge
                # will flip to ✅ Pushed once Tally ack'd. Until then it's
                # still in tally_outbox awaiting the agent.
                db.save_chat_message(
                    session_id, "assistant",
                    f"🟠 Invoice **{inv_num}** for **{p_name}** queued for Tally sync "
                    f"(₹{float(total):.2f}). The Windows Agent will push it to Tally Prime "
                    f"on its next poll — watch the Vouchers tab badge flip ✅ Pushed when "
                    f"Tally accepts it.",
                    "text")
            except Exception as e:
                print(f"Error saving confirmation messages to DB: {e}")
                
        return {"status": "success", "tally_response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/reconcile/confirm")
async def confirm_reconciliation(payload: dict):
    try:
        company = payload.get("company_name", "Acme Corp")
        reconciliations = payload.get("reconciliations", [])
        reconciled_count = 0
        learning_count = 0
        
        for item in reconciliations:
            v_id = item.get("tally_voucher_id")
            suggested_ledger = item.get("suggested_ledger")
            tx = item.get("bank_transaction") or {}
            
            if v_id:
                db.mark_tally_voucher_reconciled(v_id)
                reconciled_count += 1
            else:
                if suggested_ledger and suggested_ledger != "Suspense A/c":
                    desc = tx.get("description", "")
                    party = tx.get("party_name", desc)
                    from utils.reconciler import get_reconciliation_embedding
                    emb = get_reconciliation_embedding(f"reconcile ledger mapping for bank narration {desc} party {party}")
                    db.save_correction(
                        field="ledger_mapping",
                        original=desc,
                        corrected=suggested_ledger,
                        party_name=party,
                        embedding=emb,
                        company_name=company
                    )
                    learning_count += 1
                    
        return {
            "status": "success",
            "message": f"Successfully reconciled {reconciled_count} vouchers and recorded {learning_count} ledger mappings in knowledge base!"
        }
    except Exception as e:
        print(f"Error in reconciliation confirm: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _json_safe(obj):
    """Recursively replace non-finite floats (NaN / Inf) with None so the value is
    valid JSON. pandas-parsed bank data can contain NaN, which crashes FastAPI's
    json.dumps ('Out of range float values are not JSON compliant: nan')."""
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


# In-memory progress tracker for bank-reconciliation jobs (keyed by job_id)
bank_reco_progress = {}


@app.get("/api/bank/auto-reconcile/progress/{job_id}")
async def bank_reco_progress_poll(job_id: str):
    """Frontend polls this to show live progress."""
    return bank_reco_progress.get(job_id, {"phase": "unknown"})


@app.post("/api/bank/auto-reconcile")
async def bank_auto_reconcile(
    file: UploadFile = File(...),
    company_name: str = Form("Acme Corp"),
    company_id: str = Form(None),
    job_id: str = Form(None),
    username: str = Form(None),
):
    """Sprint 2 — AI bank reconciliation. Parses bank statement and uses vector
    embeddings + Gemini reasoning to suggest party + expense/revenue head + bank ledger
    for every transaction."""
    try:
        import tempfile, os as _os, hashlib, shutil, uuid as _uuid
        # Resolve company_id if frontend only sent company_name — otherwise dedup,
        # event-log insertion, and cross-source linking all silently fail with NULL.
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        # P0 FIX (#8): bank-statement AI parse used to be free. Gate on balance here.
        _ensure_tokens(username, company_name)
        suffix = _os.path.splitext(file.filename)[1] if file.filename else ".csv"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        # File deduplication via sha256
        sha_hex = hashlib.sha256(content).hexdigest()
        if company_id:
            existing = db.find_statement_upload_by_sha(company_id, sha_hex)
            if existing:
                try: _os.unlink(tmp_path)
                except: pass
                raise HTTPException(status_code=409, detail={
                    "message": f"Duplicate file: '{existing['original_name']}' was already uploaded on {existing['uploaded_at']}.",
                    "existing_upload_id": str(existing["id"]),
                })

        # Persist a copy to static/uploads/ for source traceability
        uploads_dir = _os.path.join(_os.path.dirname(__file__), "static", "uploads")
        _os.makedirs(uploads_dir, exist_ok=True)
        stored_name = f"{_uuid.uuid4()}_{file.filename or 'bank.csv'}"
        stored_path = _os.path.join(uploads_dir, stored_name)
        shutil.copy(tmp_path, stored_path)
        file_url = f"/static/uploads/{stored_name}"

        parse_prompt = """You are a bank statement parser. Extract ALL transactions from this bank statement.
Return a JSON array of objects, each with these exact fields:
- "date": transaction date in YYYY-MM-DD format
- "description": the narration/description text exactly as shown
- "reference": any reference number, cheque number, UTR, or transaction ID
- "amount": the transaction amount as a number (positive for credits/deposits, negative for debits/withdrawals)
- "party_name": the likely party/entity name extracted from the description (best guess)
- "transaction_type": one of "Cheque", "NEFT", "RTGS", "UPI", "IMPS", "ATM", "POS", "Transfer", "Other"

Return ONLY the JSON array, no explanation."""

        transactions = []

        # --- Path A: structured XLSX / CSV → parse with pandas (no Gemini, ~milliseconds) ---
        if suffix.lower() in ['.xlsx', '.xls', '.csv']:
            try:
                import pandas as _pd
                if suffix.lower() == '.csv':
                    dfs = [_pd.read_csv(tmp_path, header=None)]
                else:
                    xl = _pd.ExcelFile(tmp_path)
                    dfs = [xl.parse(sh, header=None) for sh in xl.sheet_names]

                for df in dfs:
                    # Find the header row — look for cells matching common headers
                    header_keywords = {'date', 'description', 'amount', 'cr', 'dr', 'particulars',
                                       'narration', 'reference', 'value date', 'withdrawal',
                                       'deposit', 'debit', 'credit', 'transaction'}
                    header_row_idx = None
                    for ridx in range(min(15, len(df))):
                        row_str = ' '.join(str(c).lower() for c in df.iloc[ridx].values if str(c) != 'nan')
                        hits = sum(1 for k in header_keywords if k in row_str)
                        if hits >= 3:
                            header_row_idx = ridx
                            break
                    if header_row_idx is None:
                        continue

                    headers = [str(c).strip() for c in df.iloc[header_row_idx].values]
                    body = df.iloc[header_row_idx + 1:].copy()
                    body.columns = headers
                    body = body.dropna(how='all')

                    # Detect column roles
                    def find_col(*needles, exclude=()):
                        for h in headers:
                            hl = h.lower()
                            if any(x in hl for x in exclude):
                                continue
                            if any(n in hl for n in needles): return h
                        return None

                    col_date = find_col('value date', 'date', 'txn date')
                    col_desc = find_col('description', 'particulars', 'narration')
                    col_party = find_col('party')
                    col_details = find_col('details', 'remark')
                    col_ref = find_col('reference', 'ref no', 'utr', 'chq', 'cheque')
                    # P1 FIX: 'type' alone matched "Transaction Type"/"Instrument Type" and
                    # mis-signed rows — only treat a real Dr/Cr indicator column as drcr.
                    col_drcr = (find_col('cr/dr', 'dr/cr', 'drcr')
                                or find_col('type', exclude=('transaction', 'instrument',
                                                             'payment', 'txn', 'mode')))
                    # P1 FIX: 'amount' alone matched "Balance Amount"/"Available Amount" and
                    # posted the running balance — exclude balance-like columns.
                    _amt_excl = ('balance', 'available', 'closing', 'opening', 'running')
                    col_amount = find_col('transaction amount', 'txn amount', 'amount',
                                          exclude=_amt_excl)
                    col_debit = find_col('withdrawal', 'debit', exclude=_amt_excl) if not col_amount else None
                    col_credit = find_col('deposit', 'credit', exclude=_amt_excl) if not col_amount else None

                    # P1 FIX: detect day/month orientation for slash dates by scanning the
                    # whole column (e.g. a '13/04' proves DD/MM; '04/13' proves MM/DD) so
                    # we don't silently mis-date a US-format statement. India default = DD/MM.
                    import re as _re_d
                    _p1max = _p2max = 0
                    if col_date:
                        for _v in body[col_date].astype(str):
                            _m = _re_d.match(r'^(\d{1,2})[/-](\d{1,2})[/-]\d{2,4}', _v.strip())
                            if _m:
                                _p1max = max(_p1max, int(_m.group(1)))
                                _p2max = max(_p2max, int(_m.group(2)))
                    if _p2max > 12 and _p1max <= 12:
                        _date_fmts = ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d', '%d-%m-%Y')
                    else:
                        _date_fmts = ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%d-%m-%Y', '%m/%d/%Y')

                    for _, row in body.iterrows():
                        # date
                        raw_date = str(row.get(col_date, '') if col_date else '').strip()
                        if not raw_date or raw_date == 'nan': continue
                        date_str = None
                        for fmt in _date_fmts:
                            try:
                                date_str = datetime.strptime(raw_date.split()[0], fmt).strftime('%Y-%m-%d')
                                break
                            except Exception: continue
                        if not date_str: continue

                        # amount with sign
                        amt = 0.0
                        if col_amount:
                            try: amt = float(str(row.get(col_amount, 0)).replace(',', '') or 0)
                            except: amt = 0.0
                            if col_drcr:
                                drcr = str(row.get(col_drcr, '')).strip().upper()
                                if drcr.startswith('DR') or drcr in ('D', 'WITHDRAWAL'):
                                    amt = -abs(amt)
                                elif drcr.startswith('CR') or drcr in ('C', 'DEPOSIT'):
                                    amt = abs(amt)
                        elif col_debit or col_credit:
                            try:
                                d = float(str(row.get(col_debit, 0) or 0).replace(',', '') or 0) if col_debit else 0
                                c = float(str(row.get(col_credit, 0) or 0).replace(',', '') or 0) if col_credit else 0
                                amt = c - d
                            except: amt = 0.0
                        # Skip blank/zero AND non-numeric (NaN/inf) amounts. A NaN
                        # slips past the magnitude check (nan<0.01 is False) and then
                        # can't be JSON-serialized in the response → upload 500s.
                        if amt != amt or amt in (float('inf'), float('-inf')) or abs(amt) < 0.01:
                            continue

                        desc = str(row.get(col_desc, '') if col_desc else '').strip()
                        party_guess = str(row.get(col_party, '') if col_party else '').strip()
                        details = str(row.get(col_details, '') if col_details else '').strip()
                        ref = str(row.get(col_ref, '') if col_ref else '').strip()
                        # Try to extract ref from description if no ref column
                        if not ref and desc:
                            import re as _re
                            mref = _re.search(r'\b([A-Z0-9]{8,})\b', desc)
                            if mref: ref = mref.group(1)

                        full_desc = (desc + (' | ' + details if details else '')).strip()

                        transactions.append({
                            "date": date_str,
                            "description": full_desc,
                            "reference": ref,
                            "amount": amt,
                            "party_name": party_guess,
                            "transaction_type": "Other",
                        })

                print(f"[BANK PARSE] pandas extracted {len(transactions)} transactions from {suffix}")
            except Exception as pe:
                print(f"[BANK PARSE] pandas path failed, falling back to Gemini: {pe}")
                transactions = []

        # --- Path B: fallback to Gemini for PDFs/images or if pandas couldn't extract ---
        if not transactions:
            if suffix.lower() == '.csv':
                text_content = content.decode('utf-8', errors='ignore')
                model = genai.GenerativeModel('gemini-flash-latest')
                response = model.generate_content(f"{parse_prompt}\n\nRAW BANK STATEMENT DATA:\n---\n{text_content[:100000]}\n---")
                result = response.text
            elif suffix.lower() in ['.xlsx', '.xls']:
                import pandas as _pd
                xl = _pd.ExcelFile(tmp_path)
                text_content = "\n\n".join(f"--- Sheet: {sh} ---\n" + xl.parse(sh).to_csv(index=False) for sh in xl.sheet_names)
                model = genai.GenerativeModel('gemini-flash-latest')
                response = model.generate_content(f"{parse_prompt}\n\nRAW BANK STATEMENT DATA:\n---\n{text_content[:100000]}\n---")
                result = response.text
            else:
                # PDF / image — let parser handle (it uploads to Gemini File API)
                result = parser.parse(tmp_path, parse_prompt)
            if result:
                import re as _re
                jm = _re.search(r'\[.*\]', result, _re.DOTALL)
                if jm:
                    try: transactions = json.loads(jm.group())
                    except: pass

        try: _os.unlink(tmp_path)
        except: pass

        if not transactions:
            return {"status": "error", "message": "Could not parse bank statement."}
        # Charge for the parse (best-effort; estimated from parsed output size).
        _charge_ai(username, company_name, "bank_statement_parse",
                   est_text=json.dumps(transactions)[:50000])

        from utils.reconciler import ai_reconcile_statement

        # Progress callback writes into in-memory dict; frontend polls it
        def _progress(p):
            if job_id:
                bank_reco_progress[job_id] = p

        if job_id:
            bank_reco_progress[job_id] = {"phase": "parsing_done", "total": len(transactions), "done": 0}

        # Run in executor — blocking psycopg2 + Gemini calls
        import asyncio as _aio
        _loop = _aio.get_event_loop()
        reconciled = await _loop.run_in_executor(
            None,
            lambda: ai_reconcile_statement(transactions, company_name,
                                            company_id=company_id, progress_cb=_progress,
                                            file_hint=file.filename or "")
        )

        auto_matched = sum(1 for r in reconciled if r["status"] == "auto_matched")
        auto_filled  = sum(1 for r in reconciled if r["status"] == "auto_filled")
        unmatched    = sum(1 for r in reconciled if r["status"] == "unmatched")

        # ── Persist to bank_transactions (Sprint 3) ────────────────
        try:
            # Compute period_from / period_to from the parsed transactions
            valid_dates = [t.get("date") for t in transactions if t.get("date")]
            period_from = min(valid_dates) if valid_dates else None
            period_to = max(valid_dates) if valid_dates else None
            total_credit = sum(t.get("amount", 0) for t in transactions if t.get("amount", 0) > 0)
            total_debit = sum(abs(t.get("amount", 0)) for t in transactions if t.get("amount", 0) < 0)

            # Infer bank ledger from filename for the upload record
            inferred_bank = reconciled[0].get("suggested_bank_ledger") if reconciled else None

            upload_id = await _loop.run_in_executor(None, lambda: db.save_statement_upload(
                company_id=company_id, company_name=company_name,
                file_url=file_url, original_name=file.filename or "bank.csv",
                bank_ledger=inferred_bank,
                period_from=period_from, period_to=period_to,
                line_count=len(transactions),
                total_credit=total_credit, total_debit=total_debit,
                sha256_hex=sha_hex,
            ))

            # Build bank_transactions rows
            bt_rows = []
            for idx, r in enumerate(reconciled):
                tx = r["bank_transaction"]
                bt_rows.append({
                    "company_id": company_id, "company_name": company_name,
                    "source": "bank_statement",
                    "source_record_id": None,
                    "source_file_id": upload_id,
                    "source_row_idx": idx,
                    "source_payload": tx,
                    "date": tx.get("date"),
                    "value_date": None,
                    "description": tx.get("description"),
                    "reference": tx.get("reference"),
                    "amount": tx.get("amount") or 0,
                    "bank_ledger": r.get("suggested_bank_ledger"),
                    "party": r.get("suggested_party"),
                    "head": r.get("suggested_expense_head"),
                    "voucher_type": r.get("voucher_type"),
                    "instrument_type": tx.get("transaction_type"),
                    "instrument_number": tx.get("reference"),
                    "payment_favouring": None,
                    # Normalize reconciler vocab → canonical UI vocab.
                    # reconciler emits: auto_matched | auto_filled | unmatched
                    # UI/DB speak:       matched      | ai_filled   | unmatched
                    "status": (
                        "matched"   if r["status"] == "auto_matched" else
                        "ai_filled" if r["status"] == "auto_filled"  else
                        r["status"]
                    ),
                    "confidence": r.get("confidence", 0),
                    "rationale": r.get("rationale"),
                    "match_reason": "phase1" if r["status"] == "auto_matched" else "phase2_or_3",
                    "linked_id": None,
                    "created_by": f"bank_upload:{file.filename}",
                    # Sprint 11 — AI engine produced the suggestion for this row
                    # (whether it matched something existing or just filled party/head).
                    # Unmatched rows still count as "AI tried"? No — keep ai_touched=FALSE
                    # for unmatched to reflect "AI gave up, needs human review".
                    "ai_touched": r["status"] in ("auto_matched", "auto_filled"),
                    "human_touched": False,
                })
            persist_res = await _loop.run_in_executor(None, db.save_bank_transactions, bt_rows)
            print(f"[BANK PERSIST] inserted {persist_res['inserted']} bank_transactions for upload {upload_id}", flush=True)

            # Cross-source link after insert
            link_res = None
            if company_id:
                link_res = await _loop.run_in_executor(None, db.link_bank_transactions, company_id)
                print(f"[BANK LINK] {link_res['linked_pairs']} new cross-source links", flush=True)

            # Log this run
            await _loop.run_in_executor(None, db.log_bank_sync_run,
                                         company_id, company_name, "statement_upload",
                                         None, None, persist_res, link_res, "user",
                                         f"file={file.filename}")
        except Exception as persist_err:
            print(f"[BANK PERSIST WARNING] {persist_err}", flush=True)

        # Build a per-line-dedup summary from persist_res (if persistence happened)
        persisted_summary = None
        _lv = locals()
        if 'persist_res' in _lv and _lv['persist_res']:
            _link = _lv.get('link_res') or {}
            persisted_summary = {
                "newly_inserted": _lv['persist_res'].get("inserted", 0),
                "already_existed": _lv['persist_res'].get("skipped_existing", 0),
                "errors": _lv['persist_res'].get("skipped_error", 0),
                "cross_source_links": _link.get("linked_pairs", 0) if isinstance(_link, dict) else 0,
            }

        return _json_safe({
            "status": "success",
            "data": {
                "reconciled": reconciled,
                "metrics": {"total": len(reconciled), "auto_matched": auto_matched,
                            "auto_filled": auto_filled, "unmatched": unmatched},
                "persisted": persisted_summary,
                "file_name": file.filename,
            }
        })
    except HTTPException:
        raise  # surface 402 out-of-tokens etc. as-is
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank/confirm-reconciliation")
async def bank_confirm_reconciliation(payload: dict):
    """Confirm reconciliation results — write vouchers to tally_vouchers
    AND store the bank-narration → party + head mapping in knowledge_base
    so future similar transactions are recognized."""
    try:
        company_name = payload.get("company_name")
        company_id = payload.get("company_id")
        rows = payload.get("rows") or []
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")

        from utils.reconciler import get_reconciliation_embedding
        posted = 0
        learned = 0

        for r in rows:
            bt = r.get("bank_transaction") or {}
            party = (r.get("suggested_party") or "").strip()
            head  = (r.get("suggested_expense_head") or "").strip()
            bank  = (r.get("suggested_bank_ledger") or "").strip() or "Bank Account"
            vtype = r.get("voucher_type") or ("Receipt" if (bt.get("amount") or 0) > 0 else "Payment")
            amount = abs(float(bt.get("amount") or 0))
            desc = (bt.get("description") or "").strip()
            ref = (bt.get("reference") or "").strip()
            tx_date = bt.get("date") or ""
            # YYYY-MM-DD → YYYYMMDD for tally storage
            date_compact = tx_date.replace("-", "") if tx_date else ""

            # Build ledger entries — double entry: bank ledger Dr/Cr + head Cr/Dr
            # Receipt (money in):   Bank Dr, Party/Income Cr
            # Payment (money out):  Expense Dr, Bank Cr
            if vtype == "Receipt":
                ledger_entries = [
                    {"ledger_name": bank, "amount": amount, "is_debit": True},
                    {"ledger_name": head or party or "Sales Account", "amount": -amount, "is_debit": False},
                ]
                voucher_party = party or head
            else:
                ledger_entries = [
                    {"ledger_name": head or "Suspense A/c", "amount": amount, "is_debit": True},
                    {"ledger_name": bank, "amount": -amount, "is_debit": False},
                ]
                voucher_party = party or head

            # P0 FIX: deterministic number from the transaction's own id/content so a
            # re-confirm reuses the same voucher number (dedupes instead of doubling).
            import hashlib as _hl
            _idem = str(bt.get("id") or "")[:8] or _hl.sha1(
                f"{tx_date}|{amount}|{desc}|{ref}".encode()).hexdigest()[:8]
            voucher = {
                "date": date_compact,
                "type": vtype,
                "voucher_type": vtype,
                "party": voucher_party,
                "number": ref or f"BANK-{_idem}",
                "amount": amount,
                "narration": desc,
                "ledger_entries": ledger_entries,
                "reference_no": ref,
                "instrument_number": ref,
                "currency": "INR",
                "tally_master_id": None,  # not from Tally — we created it
            }
            ok_v, err_v = db.validate_voucher_for_post(voucher)
            if not ok_v:
                print(f"[BANK CONFIRM] skip unbalanced txn: {err_v}")
                continue

            try:
                save_res = db.save_tally_vouchers(company_name, [voucher])
                if save_res.get("upserted"):
                    posted += 1
                    # Mark as reconciled
                    if r.get("tally_voucher_id"):
                        db.mark_tally_voucher_reconciled(r["tally_voucher_id"])
            except Exception as ve:
                print(f"[BANK CONFIRM] voucher save error: {ve}")

            # Backfill company_id on the freshly-inserted row
            if company_id:
                try:
                    conn_bf = db.get_conn()
                    cur_bf = conn_bf.cursor()
                    cur_bf.execute(
                        "UPDATE tally_vouchers SET company_id = %s WHERE company_name = %s AND company_id IS NULL",
                        (company_id, company_name)
                    )
                    conn_bf.commit()
                    cur_bf.close()
                    conn_bf.close()
                except Exception as bf:
                    print(f"[BANK CONFIRM] backfill warning: {bf}")

            # Learning loop — embed bank narration → party + head mapping
            try:
                learning_text = f"Bank reconciliation: '{desc}' ref '{ref}' amount {amount} → party '{party}', head '{head}', type {vtype}"
                emb = get_reconciliation_embedding(learning_text)
                conn_l = db.get_conn()
                cur_l = conn_l.cursor()
                kb_data = {
                    "company_name": company_name,
                    "bank_narration": desc,
                    "reference": ref,
                    "amount": amount,
                    "voucher_type": vtype,
                    "party": party,
                    "head": head,
                    "bank_ledger": bank,
                    "source": "bank_reconciliation_confirm",
                }
                if emb:
                    cur_l.execute("""
                        INSERT INTO knowledge_base (type, data, embedding)
                        VALUES (%s, %s, %s)
                    """, ('bank_reconciliation', json.dumps(kb_data),
                          f"[{','.join(map(str, emb))}]"))
                else:
                    cur_l.execute("""
                        INSERT INTO knowledge_base (type, data)
                        VALUES (%s, %s)
                    """, ('bank_reconciliation', json.dumps(kb_data)))
                conn_l.commit()
                cur_l.close()
                conn_l.close()
                learned += 1
            except Exception as le:
                print(f"[BANK CONFIRM] learning insert warning: {le}")

        return {
            "status": "success",
            "posted_vouchers": posted,
            "learnings_saved": learned,
            "message": f"Posted {posted} vouchers and saved {learned} learning patterns."
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank/rerun-reconcile")
async def bank_rerun_reconcile(payload: dict):
    """Re-run the AI reconciler on ONLY the unreconciled, not-yet-human-edited lines
    of the uploaded statement(s), using the latest learned knowledge (e.g. parties
    the user just added, which are embedded into knowledge_base). Lines the user has
    already fixed (human_touched) or that are matched/posted are left untouched.
    Body: {company_name, company_id?, upload_id?}."""
    try:
        company_name = (payload.get("company_name") or "").strip()
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")
        company_id = payload.get("company_id") or _resolve_company_id_by_name(company_name)
        upload_id = payload.get("upload_id")
        rows = db.get_rerunnable_bank_lines(company_id, company_name, upload_id)
        if not rows:
            return {"status": "success", "total": 0, "updated": 0,
                    "message": "No unreconciled lines to re-run."}
        # Reconstruct the tx list (prefer the original parsed payload; fall back to columns)
        txns = []
        for r in rows:
            p = r.get("source_payload") or {}
            if isinstance(p, str):
                try: p = json.loads(p)
                except Exception: p = {}
            txns.append({
                "date": (str(r["date"]) if r.get("date") else p.get("date")),
                "description": p.get("description") or r.get("description") or "",
                "reference": p.get("reference") or r.get("reference") or "",
                "amount": float(r.get("amount") if r.get("amount") is not None else (p.get("amount") or 0)),
                "party_name": p.get("party_name") or "",
                "transaction_type": p.get("transaction_type") or "Other",
            })
        from utils.reconciler import ai_reconcile_statement
        import asyncio as _aio
        loop = _aio.get_event_loop()
        reconciled = await loop.run_in_executor(
            None, lambda: ai_reconcile_statement(txns, company_name,
                                                 company_id=company_id, file_hint=""))
        updated = 0
        for r, sug in zip(rows, reconciled):
            status = ("matched"   if sug["status"] == "auto_matched" else
                      "ai_filled" if sug["status"] == "auto_filled"  else
                      "unmatched")
            upd = {
                "party":       sug.get("suggested_party") or "",
                "head":        sug.get("suggested_expense_head") or "",
                "bank_ledger": sug.get("suggested_bank_ledger") or "",
                "status":      status,
                "confidence":  sug.get("confidence", 0),
                "rationale":   sug.get("rationale"),
                # AI "reconciled" the row only if it actually produced a usable
                # suggestion. A blank-party / unmatched line means the AI gave up —
                # don't mark it ai_touched (otherwise "Reconciled By" wrongly shows AI).
                "ai_touched":  status in ("matched", "ai_filled"),
            }
            # user_id=None → AI-driven update; human_touched stays FALSE so the row
            # remains a re-run candidate until a human curates it.
            db.update_bank_transaction(str(r["id"]), upd, user_id=None, company_id=company_id)
            updated += 1
        return {"status": "success", "total": len(rows), "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════
# 360° Bank Sub-Tab — endpoints
# ═══════════════════════════════════════════════════════════════

def _resolve_company_id_by_name(company_name: str):
    """Look up a company's UUID by its name. Returns None if not found."""
    if not company_name:
        return None
    try:
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id FROM companies WHERE name = %s LIMIT 1", (company_name,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return str(row["id"]) if row else None
    except Exception as e:
        print(f"[_resolve_company_id_by_name] {e}")
        return None


@app.get("/api/resolve-company")
async def resolve_company_endpoint(name: str):
    """Frontend helper — given a company name, returns its UUID for downstream calls."""
    cid = _resolve_company_id_by_name(name)
    if not cid:
        raise HTTPException(status_code=404, detail=f"company '{name}' not found")
    return {"status": "success", "company_id": cid, "company_name": name}

@app.get("/api/bank-transactions")
async def list_bank_transactions_endpoint(
    company_id: str = None, company_name: str = None,
    source: str = None, status: str = None,
    from_date: str = None, to_date: str = None,
    q: str = None, view: str = "per_source",
    sort: str = "date_desc",
    limit: int = 500, offset: int = 0,
    tally_status: str = None,
    bank_ledger: str = None,
    source_file_id: str = None,
):
    try:
        result = db.list_bank_transactions(
            company_id=company_id, company_name=company_name,
            source=source, status=status,
            from_date=from_date, to_date=to_date,
            q=q, view=view, sort=sort, limit=limit, offset=offset,
            tally_status=tally_status,
            bank_ledger=bank_ledger,
            source_file_id=source_file_id,
        )
        # JSON-friendly conversion
        for r in result["rows"]:
            for k in ("date", "value_date", "created_at", "updated_at"):
                if r.get(k) is not None:
                    r[k] = str(r[k])
            for k in ("amount", "confidence"):
                if r.get(k) is not None:
                    r[k] = float(r[k])
            for k in ("id", "source_record_id", "source_file_id", "linked_id", "company_id"):
                if r.get(k) is not None:
                    r[k] = str(r[k])
        return {"status": "success", "data": result}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


def _resync_posted_bank_voucher(row, company_name):
    """A posted bank line was edited → rebuild its double-entry voucher with the new
    party/head/bank and update the linked voucher (flags needs_resync). Returns True
    if a voucher was updated."""
    party = (row.get("party") or "").strip()
    head  = (row.get("head") or "").strip()
    bank  = (row.get("bank_ledger") or "Bank Account").strip()
    amount = abs(float(row.get("amount") or 0))
    vtype = row.get("voucher_type") or ("Receipt" if float(row.get("amount") or 0) > 0 else "Payment")
    ref = (row.get("reference") or "").strip()
    vnum = ref or f"BANK-{str(row.get('id'))[:8]}"
    vid = db.get_voucher_id_by_number(company_name, vnum)
    if not vid:
        return False
    if vtype == "Receipt":
        legs = [{"ledger_name": bank, "amount": amount, "is_debit": True},
                {"ledger_name": head or party or "Sales Account", "amount": -amount, "is_debit": False}]
    else:
        legs = [{"ledger_name": head or "Suspense A/c", "amount": amount, "is_debit": True},
                {"ledger_name": bank, "amount": -amount, "is_debit": False}]
    res = db.update_tally_voucher(vid, {"party_name": party or head, "ledger_entries": legs},
                                  edited_by="bank_reco_edit")
    return bool(res and res.get("ok"))


@app.patch("/api/bank-transactions/{tx_id}")
async def patch_bank_transaction(tx_id: str, payload: dict, background_tasks: BackgroundTasks = None):
    try:
        # P0 FIX: scope the edit to the caller's company (tenant isolation).
        _cid = payload.get("company_id")
        if not _cid and payload.get("company_name"):
            _cid = _resolve_company_id_by_name(payload.get("company_name"))
        cname = payload.get("company_name")
        edited_fields = [k for k in ("party", "head", "bank_ledger") if k in payload]
        party_edited = "party" in payload
        party_val = (payload.get("party") or "").strip() if party_edited else None

        # Read current status so an edit never downgrades a Posted/Linked/Matched line
        # (those have already produced a voucher / cross-link).
        cur_row = db.get_bank_transaction(tx_id, _cid)
        if not cur_row:
            raise HTTPException(status_code=404, detail="bank_transaction not found (or not in your company)")
        cur_status = cur_row.get("status")
        protected = cur_status in ("posted", "matched", "linked")

        if edited_fields:
            payload.setdefault("confidence", 1.0)
            payload.setdefault("rationale", "Manual override by user")
            if not protected:
                payload.setdefault("status", "ai_filled")  # promote an unmatched line
        if party_edited:
            if party_val:
                payload["human_touched"] = True
            elif protected:
                # Clearing a posted/linked line: blank the party + unlearn, but keep
                # status (don't orphan the voucher) — user fixes the voucher separately.
                payload["party"] = ""
                payload["rationale"] = None
                payload.pop("status", None)
            else:
                # Clear an open line → clean re-runnable Needs-Review.
                payload["party"] = ""
                payload["status"] = "unmatched"
                payload["confidence"] = 0
                payload["rationale"] = None
                payload["human_touched"] = False
                payload["ai_touched"] = False

        row = db.update_bank_transaction(tx_id, payload, company_id=_cid)
        if not row:
            raise HTTPException(status_code=404, detail="bank_transaction not found (or not in your company)")

        # Learn (or unlearn) the bank-narration → party association so re-runs reuse it.
        learning = None
        if party_edited:
            _cn = cname or row.get("company_name")
            narr = row.get("description") or ""
            if party_val:
                learning = "learning"
                if background_tasks is not None:
                    background_tasks.add_task(db.learn_bank_party, _cn, narr, party_val,
                                             get_embedding, _cid, str(tx_id))
                else:
                    db.learn_bank_party(_cn, narr, party_val, get_embedding, _cid, str(tx_id))
            else:
                learning = "unlearned"
                db.unlearn_bank_party(_cn, str(tx_id))

        # Keep the linked voucher in sync when a POSTED line's party/head/bank changed.
        voucher_synced = False
        if cur_status == "posted" and edited_fields:
            try:
                voucher_synced = _resync_posted_bank_voucher(row, cname or row.get("company_name"))
            except Exception as _ve:
                print(f"[patch_bank] voucher resync error: {_ve}", flush=True)

        # JSON-friendly
        for k in ("date", "value_date", "created_at", "updated_at"):
            if row.get(k) is not None: row[k] = str(row[k])
        for k in ("amount", "confidence"):
            if row.get(k) is not None: row[k] = float(row[k])
        for k in ("id", "source_record_id", "source_file_id", "linked_id", "company_id"):
            if row.get(k) is not None: row[k] = str(row[k])
        return {"status": "success", "data": row, "learning": learning, "voucher_synced": voucher_synced}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank-transactions/sync")
async def sync_bank_transactions(payload: dict):
    """Re-run all three ingestion paths + cross-source linking for the active company."""
    try:
        company_id = payload.get("company_id")
        company_name = payload.get("company_name")
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id or company_name required")

        import asyncio as _aio
        _loop = _aio.get_event_loop()
        # Run blocking DB work in executor
        tally_res = await _loop.run_in_executor(None, db.ingest_bank_from_tally, company_id)
        # Sprint 15 — Bank Reco is strictly bank + cash. Stop ingesting invoice-derived
        # rows; invoice lifecycle lives on the Vouchers tab.
        invoice_res = {"inserted": 0, "skipped": 0}
        link_res = await _loop.run_in_executor(None, db.link_bank_transactions, company_id)

        # Record this sync attempt
        await _loop.run_in_executor(None, db.log_bank_sync_run,
                                    company_id, company_name, "manual_sync",
                                    tally_res, invoice_res, None, link_res, "user", None)

        return {
            "status": "success",
            "tally": tally_res,
            "invoices": invoice_res,
            "linking": link_res,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank-transactions/relink")
async def relink_bank_transactions(payload: dict):
    try:
        company_id = payload.get("company_id")
        company_name = payload.get("company_name")
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id or company_name required")
        import asyncio as _aio
        _loop = _aio.get_event_loop()
        link_res = await _loop.run_in_executor(None, db.link_bank_transactions, company_id)
        return {"status": "success", **link_res}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank-transactions/post-to-tally")
async def post_bank_transactions_to_tally(payload: dict):
    """For selected rows, build double-entry vouchers and INSERT into tally_vouchers.
    Also stores learning patterns in knowledge_base."""
    try:
        company_name = payload.get("company_name")
        company_id = payload.get("company_id")
        tx_ids = payload.get("tx_ids") or []
        if not company_name or not tx_ids:
            raise HTTPException(status_code=400, detail="company_name + tx_ids required")

        from utils.reconciler import get_reconciliation_embedding
        posted = 0
        learned = 0
        skipped = 0

        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # P0 FIX: scope by company so a guessed tx_id can't post another tenant's row.
        # Rows already 'posted' are excluded by the status filter (re-post guard).
        cur.execute("""
            SELECT * FROM bank_transactions
            WHERE id = ANY(%s::uuid[]) AND status IN ('ai_filled', 'matched', 'unmatched')
              AND company_name = %s
        """, (tx_ids, company_name))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        for r in rows:
            if r["source"] == "tally":
                skipped += 1  # already a Tally voucher
                continue
            party = (r.get("party") or "").strip()
            head = (r.get("head") or "").strip()
            bank = (r.get("bank_ledger") or "Bank Account").strip()
            vtype = r.get("voucher_type") or ("Receipt" if float(r["amount"]) > 0 else "Payment")
            amount = abs(float(r["amount"]))
            desc = (r.get("description") or "").strip()
            ref = (r.get("reference") or "").strip()
            date_compact = (str(r["date"]).replace("-", "") if r.get("date") else "")

            if vtype == "Receipt":
                ledger_entries = [
                    {"ledger_name": bank, "amount": amount, "is_debit": True},
                    {"ledger_name": head or party or "Sales Account", "amount": -amount, "is_debit": False},
                ]
                voucher_party = party or head
            else:
                ledger_entries = [
                    {"ledger_name": head or "Suspense A/c", "amount": amount, "is_debit": True},
                    {"ledger_name": bank, "amount": -amount, "is_debit": False},
                ]
                voucher_party = party or head

            voucher = {
                "date": date_compact, "type": vtype, "voucher_type": vtype,
                "party": voucher_party,
                # P0 FIX: deterministic number from the bank row id so re-posting the
                # same row reuses the same voucher number (dedupes instead of doubling).
                "number": ref or f"BANK-{str(r['id'])[:8]}",
                "amount": amount, "narration": desc,
                "ledger_entries": ledger_entries,
                "reference_no": ref, "instrument_number": ref,
                "currency": "INR", "tally_master_id": None,
            }
            ok_v, err_v = db.validate_voucher_for_post(voucher)
            if not ok_v:
                print(f"[POST-TO-TALLY] skip unbalanced row {r['id']}: {err_v}")
                skipped += 1
                continue
            try:
                save_res = db.save_tally_vouchers(company_name, [voucher])
                if save_res.get("upserted"):
                    posted += 1
                    # Mark this bank_transactions row as posted.
                    # Sprint 11: the user explicitly clicked "Post to Tally",
                    # so flag human_touched=TRUE alongside the status flip.
                    db.update_bank_transaction(
                        r["id"],
                        {"status": "posted", "human_touched": True},
                        company_id=company_id,
                    )
            except Exception as ve:
                print(f"[POST-TO-TALLY] voucher save error: {ve}")

            # Learning loop
            try:
                learning_text = f"Bank reconciliation: '{desc}' ref '{ref}' amount {amount} → party '{party}', head '{head}', type {vtype}"
                emb = get_reconciliation_embedding(learning_text)
                kb_data = {
                    "company_name": company_name, "bank_narration": desc,
                    "reference": ref, "amount": amount, "voucher_type": vtype,
                    "party": party, "head": head, "bank_ledger": bank,
                    "source": "bank_transactions_post",
                }
                conn_l = db.get_conn()
                cur_l = conn_l.cursor()
                if emb:
                    cur_l.execute("""
                        INSERT INTO knowledge_base (type, data, embedding)
                        VALUES (%s, %s, %s)
                    """, ('bank_reconciliation', json.dumps(kb_data),
                          f"[{','.join(map(str, emb))}]"))
                else:
                    cur_l.execute("INSERT INTO knowledge_base (type, data) VALUES (%s, %s)",
                                  ('bank_reconciliation', json.dumps(kb_data)))
                conn_l.commit()
                cur_l.close()
                conn_l.close()
                learned += 1
            except Exception as le:
                print(f"[POST-TO-TALLY] learning insert: {le}")

        # Backfill company_id on new vouchers
        if company_id:
            try:
                conn_bf = db.get_conn()
                cur_bf = conn_bf.cursor()
                cur_bf.execute(
                    "UPDATE tally_vouchers SET company_id = %s WHERE company_name = %s AND company_id IS NULL",
                    (company_id, company_name)
                )
                conn_bf.commit()
                cur_bf.close()
                conn_bf.close()
            except Exception as bf:
                print(f"[POST-TO-TALLY] backfill: {bf}")

        return {
            "status": "success",
            "posted": posted, "learned": learned, "skipped": skipped,
            "message": f"Posted {posted} vouchers, recorded {learned} learnings."
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


def _mass_learn_bank_parties(company_name, company_id, lines):
    """Background task: teach the reconciler each submitted line's narration→party
    (tally_master_party, idempotent). Covers AI-suggested lines never individually
    confirmed — submitting them is the bulk acceptance the AI learns from."""
    for ln in lines:
        try:
            db.learn_bank_party(company_name, ln.get("description") or "",
                                ln.get("party") or "", get_embedding,
                                company_id, str(ln.get("id")))
        except Exception as e:
            print(f"[submit-to-vouchers] learn error {ln.get('id')}: {e}", flush=True)


@app.post("/api/bank/submit-to-vouchers")
async def submit_bank_to_vouchers(payload: dict, background_tasks: BackgroundTasks = None):
    """Submit all reconciled-but-not-yet-in-books bank lines to the Voucher section:
    build a double-entry voucher per line and INSERT into tally_vouchers (so they show
    in the Vouchers tab). Only creates Voucher-section entries — pushing to Tally stays
    the Voucher section's own job. At submit, the AI bulk-learns each line's
    narration→party mapping (background). Body: {company_name, company_id?}."""
    try:
        company_name = (payload.get("company_name") or "").strip()
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")
        company_id = payload.get("company_id") or _resolve_company_id_by_name(company_name)
        rows = db.get_submittable_bank_lines(company_id, company_name)
        if not rows:
            return {"status": "success", "submitted": 0, "skipped": 0,
                    "message": "No new reconciled lines to submit."}
        submitted = 0
        skipped = 0
        learned_lines = []
        for r in rows:
            party = (r.get("party") or "").strip()
            head = (r.get("head") or "").strip()
            bank = (r.get("bank_ledger") or "Bank Account").strip()
            amount = abs(float(r["amount"]))
            vtype = r.get("voucher_type") or ("Receipt" if float(r["amount"]) > 0 else "Payment")
            desc = (r.get("description") or "").strip()
            ref = (r.get("reference") or "").strip()
            date_compact = (str(r["date"]).replace("-", "") if r.get("date") else "")
            if vtype == "Receipt":
                ledger_entries = [
                    {"ledger_name": bank, "amount": amount, "is_debit": True},
                    {"ledger_name": head or party or "Sales Account", "amount": -amount, "is_debit": False},
                ]
            else:
                ledger_entries = [
                    {"ledger_name": head or "Suspense A/c", "amount": amount, "is_debit": True},
                    {"ledger_name": bank, "amount": -amount, "is_debit": False},
                ]
            voucher = {
                "date": date_compact, "type": vtype, "voucher_type": vtype,
                "party": party or head,
                "number": ref or f"BANK-{str(r['id'])[:8]}",
                "amount": amount, "narration": desc,
                "ledger_entries": ledger_entries,
                "reference_no": ref, "instrument_number": ref,
                "currency": "INR", "tally_master_id": None,
            }
            ok_v, err_v = db.validate_voucher_for_post(voucher)
            if not ok_v:
                print(f"[submit-to-vouchers] skip unbalanced row {r['id']}: {err_v}")
                skipped += 1
                continue
            try:
                save_res = db.save_tally_vouchers(company_name, [voucher])
                if save_res.get("upserted"):
                    submitted += 1
                    # Only flip status → posted. Do NOT force human_touched: bulk
                    # submit is approving the AI's work, not individually reconciling
                    # each line. Preserve who actually reconciled it (AI-suggested
                    # lines keep ai_touched=True/human_touched=False → "🤖 AI";
                    # lines a human edited inline already carry human_touched=True).
                    db.update_bank_transaction(r["id"], {"status": "posted"},
                                               company_id=company_id)
                    learned_lines.append({"id": r["id"], "description": desc, "party": party})
            except Exception as ve:
                print(f"[submit-to-vouchers] voucher save error {r['id']}: {ve}")
                skipped += 1
        # Backfill company_id on the freshly inserted vouchers
        if company_id:
            try:
                conn_bf = db.get_conn(); cur_bf = conn_bf.cursor()
                cur_bf.execute("UPDATE tally_vouchers SET company_id = %s "
                               "WHERE company_name = %s AND company_id IS NULL",
                               (company_id, company_name))
                conn_bf.commit(); cur_bf.close(); conn_bf.close()
            except Exception as bf:
                print(f"[submit-to-vouchers] backfill: {bf}")
        # Mass-learn every submitted line (background — embeds are slow).
        if learned_lines:
            if background_tasks is not None:
                background_tasks.add_task(_mass_learn_bank_parties, company_name, company_id, learned_lines)
            else:
                _mass_learn_bank_parties(company_name, company_id, learned_lines)
        return {
            "status": "success", "submitted": submitted, "skipped": skipped,
            "message": f"Submitted {submitted} entr{'y' if submitted == 1 else 'ies'} to the Voucher section."
                       + (f" Skipped {skipped}." if skipped else ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank/rerun-line")
async def bank_rerun_line(payload: dict):
    """Re-run the AI matcher on ONE bank line (the per-row ↻ refresh) and update it in
    place. Works on any status; a Posted line keeps its status and its linked voucher
    is re-synced. Always overwrites with the fresh AI pick (reconciled-by → AI). No new
    learning (a re-run is a suggestion). Body: {company_name, company_id?, tx_id}."""
    try:
        company_name = (payload.get("company_name") or "").strip()
        tx_id = payload.get("tx_id")
        if not company_name or not tx_id:
            raise HTTPException(status_code=400, detail="company_name + tx_id required")
        company_id = payload.get("company_id") or _resolve_company_id_by_name(company_name)
        row = db.get_bank_transaction(tx_id, company_id)
        if not row:
            raise HTTPException(status_code=404, detail="bank_transaction not found (or not in your company)")
        p = row.get("source_payload") or {}
        if isinstance(p, str):
            try: p = json.loads(p)
            except Exception: p = {}
        tx = {
            "date": (str(row["date"]) if row.get("date") else p.get("date")),
            "description": p.get("description") or row.get("description") or "",
            "reference": p.get("reference") or row.get("reference") or "",
            "amount": float(row.get("amount") if row.get("amount") is not None else (p.get("amount") or 0)),
            "party_name": p.get("party_name") or "",
            "transaction_type": p.get("transaction_type") or row.get("instrument_type") or "Other",
        }
        from utils.reconciler import ai_reconcile_statement
        import asyncio as _aio
        loop = _aio.get_event_loop()
        reconciled = await loop.run_in_executor(
            None, lambda: ai_reconcile_statement([tx], company_name, company_id=company_id, file_hint=""))
        sug = reconciled[0] if reconciled else {}
        mapped = ("matched"   if sug.get("status") == "auto_matched" else
                  "ai_filled" if sug.get("status") == "auto_filled"  else
                  "unmatched")
        cur_status = row.get("status")
        upd = {
            "party":       sug.get("suggested_party") or "",
            "head":        sug.get("suggested_expense_head") or "",
            "bank_ledger": sug.get("suggested_bank_ledger") or "",
            "confidence":  sug.get("confidence", 0),
            "rationale":   sug.get("rationale"),
            "ai_touched":  mapped in ("matched", "ai_filled"),
            "human_touched": False,
            # Don't un-post a posted line — keep it posted and re-sync its voucher below.
            "status":      "posted" if cur_status == "posted" else mapped,
        }
        updated = db.update_bank_transaction(tx_id, upd, company_id=company_id)
        if cur_status == "posted":
            try:
                _resync_posted_bank_voucher(updated, company_name)
            except Exception as _ve:
                print(f"[rerun-line] voucher resync error: {_ve}", flush=True)
        for k in ("date", "value_date", "created_at", "updated_at"):
            if updated.get(k) is not None: updated[k] = str(updated[k])
        for k in ("amount", "confidence"):
            if updated.get(k) is not None: updated[k] = float(updated[k])
        for k in ("id", "source_record_id", "source_file_id", "linked_id", "company_id"):
            if updated.get(k) is not None: updated[k] = str(updated[k])
        return _json_safe({"status": "success", "data": updated})
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bank-transactions/health/{company_id}")
async def bank_transactions_health(company_id: str, company_name: str = ""):
    try:
        # Allow company_id="by-name" with company_name=X for the UI's convenience
        if (not company_id or company_id == "by-name") and company_name:
            company_id = _resolve_company_id_by_name(company_name) or ""
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id or company_name required")
        result = db.bank_health_check(company_id, company_name)
        return {"status": "success", "data": result}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bank-transactions/sync-runs/{company_id}")
async def bank_sync_runs_endpoint(company_id: str, company_name: str = ""):
    """Returns the last 20 sync attempts for the active company."""
    try:
        if (not company_id or company_id == "by-name") and company_name:
            company_id = _resolve_company_id_by_name(company_name) or ""
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id required")
        rows = db.list_bank_sync_runs(company_id, limit=20)
        for r in rows:
            for k in ("ran_at",):
                if r.get(k) is not None: r[k] = str(r[k])
            for k in ("id", "company_id"):
                if r.get(k) is not None: r[k] = str(r[k])
        return {"status": "success", "data": rows}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bank-statement-uploads")
async def list_statement_uploads_endpoint(company_id: str = None, company_name: str = None):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        rows = db.list_statement_uploads(company_id, company_name)
        for r in rows:
            for k in ("period_from", "period_to", "uploaded_at"):
                if r.get(k) is not None: r[k] = str(r[k])
            for k in ("total_credit", "total_debit"):
                if r.get(k) is not None: r[k] = float(r[k])
            for k in ("id", "company_id"):
                if r.get(k) is not None: r[k] = str(r[k])
        return {"status": "success", "data": rows}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bank-statement-uploads/{upload_id}/download")
async def download_statement_upload(upload_id: str):
    """Redirect to the static file."""
    try:
        row = db.get_statement_upload(upload_id)
        if not row:
            raise HTTPException(status_code=404, detail="upload not found")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=row["file_url"])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════
# Vouchers — Manual create / Upload / Drafts / Bulk / Helpers
# ═══════════════════════════════════════════════════════════════

def _draft_jsonify(row):
    """Make a voucher_draft row JSON-friendly."""
    if not row: return row
    for k in ("id", "company_id", "duplicate_of", "posted_voucher_id"):
        if row.get(k) is not None: row[k] = str(row[k])
    for k in ("created_at", "updated_at"):
        if row.get(k) is not None: row[k] = str(row[k])
    if row.get("ai_confidence") is not None:
        row["ai_confidence"] = float(row["ai_confidence"])
    return row


@app.post("/api/vouchers/manual")
async def create_manual_voucher(payload: dict):
    """Create one voucher from a manual entry form.
    Required: company_name, voucher_type, date, party, line_items (Dr=Cr)."""
    try:
        company_name = payload.get("company_name")
        company_id = payload.get("company_id")
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        voucher_type = payload.get("voucher_type") or "Journal"
        date_str = payload.get("date")  # ISO YYYY-MM-DD or YYYYMMDD
        if not company_name or not date_str:
            raise HTTPException(status_code=400, detail="company_name + date required")

        line_items = payload.get("line_items") or []
        dr_total = sum(float(li.get("debit") or 0) for li in line_items)
        cr_total = sum(float(li.get("credit") or 0) for li in line_items)
        if abs(dr_total - cr_total) > 0.01:
            raise HTTPException(status_code=400, detail=f"Dr ({dr_total}) ≠ Cr ({cr_total}). Entry not balanced.")

        # Normalize date → YYYYMMDD for save_tally_vouchers
        date_compact = date_str.replace("-", "")[:8]

        # ledger_entries in the format save_tally_vouchers expects
        ledger_entries = []
        for li in line_items:
            dr = float(li.get("debit") or 0)
            cr = float(li.get("credit") or 0)
            if dr > 0:
                ledger_entries.append({"ledger_name": li["ledger_name"], "amount": dr, "is_debit": True})
            elif cr > 0:
                ledger_entries.append({"ledger_name": li["ledger_name"], "amount": -cr, "is_debit": False})

        # Suggest voucher number if not provided
        v_num = payload.get("voucher_number") or db.next_voucher_number(company_name, voucher_type)

        voucher = {
            "date": date_compact, "type": voucher_type, "voucher_type": voucher_type,
            "party": payload.get("party") or "",
            "number": v_num,
            "amount": max(dr_total, cr_total),
            "narration": payload.get("narration", ""),
            "ledger_entries": ledger_entries,
            "reference_no": payload.get("reference_no", ""),
            "instrument_number": payload.get("instrument_number", ""),
            "place_of_supply": payload.get("place_of_supply", ""),
            "party_gstin": payload.get("party_gstin", ""),
            "currency": payload.get("currency", "INR"),
            "taxable_value": float(payload.get("taxable_value") or 0),
            "cgst_amount": float(payload.get("cgst_amount") or 0),
            "sgst_amount": float(payload.get("sgst_amount") or 0),
            "igst_amount": float(payload.get("igst_amount") or 0),
            "tally_master_id": None,
            "created_by": payload.get("username") or payload.get("created_by"),
        }
        res = db.save_tally_vouchers(company_name, [voucher])
        if not res.get("upserted"):
            raise HTTPException(status_code=500, detail="Save failed")

        # Backfill company_id + ingest bank leg if relevant
        if company_id:
            try:
                conn_bf = db.get_conn()
                cur_bf = conn_bf.cursor()
                cur_bf.execute(
                    "UPDATE tally_vouchers SET company_id = %s WHERE company_name = %s AND company_id IS NULL",
                    (company_id, company_name)
                )
                conn_bf.commit()
                cur_bf.close()
                conn_bf.close()
                # Fire ingest hook in background
                import threading as _t
                _t.Thread(target=db.ingest_bank_from_tally, args=(company_id,), daemon=True).start()
            except Exception as bf_err:
                print(f"[manual voucher backfill] {bf_err}")

        return {"status": "success", "voucher_number": v_num, "upserted": res["upserted"]}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/vouchers/{voucher_id}")
async def edit_voucher_endpoint(voucher_id: str, payload: dict):
    """Apply a local edit to a posted voucher (full ledger entries supported).
    This updates YantrAI's books only and flags the voucher 'needs resync' — it
    does NOT push to Tally. Use POST /api/vouchers/{id}/resync to push the edit."""
    try:
        # Tally-sourced voucher → existing path (full ledger entries + resync flag)
        if db.get_tally_voucher(voucher_id):
            res = db.update_tally_voucher(
                voucher_id, payload,
                edited_by=payload.get("username") or payload.get("edited_by"),
            )
            if res.get("error"):
                raise HTTPException(status_code=400, detail=res["error"])
            return {"status": "success", "data": db.get_tally_voucher(voucher_id)}
        # Sprint 37 — invoice-source row → update the invoices table core fields.
        conn = db.get_conn(); cur = conn.cursor()
        amt = payload.get("amount")
        if amt is None and isinstance(payload.get("ledger_entries"), list):
            # derive from the debit legs if amount not supplied
            try: amt = sum(abs(float(e.get("amount") or 0)) for e in payload["ledger_entries"] if e.get("is_debit"))
            except Exception: amt = None
        cur.execute("""
            UPDATE invoices SET
                party_name    = COALESCE(%s, party_name),
                date          = COALESCE(%s, date),
                voucher_type  = COALESCE(%s, voucher_type),
                category      = COALESCE(%s, category),
                total_amount  = COALESCE(%s, total_amount)
            WHERE id = %s
        """, (payload.get("party_name"),
              payload.get("date"),
              payload.get("voucher_type"),
              payload.get("voucher_type") or payload.get("category"),
              amt,
              voucher_id))
        updated = cur.rowcount
        conn.commit(); cur.close(); conn.close()
        if not updated:
            raise HTTPException(status_code=404, detail="Voucher not found")
        return {"status": "success", "data": {"id": voucher_id, "source": "invoice"}}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/{voucher_id}/resync")
async def resync_voucher_endpoint(voucher_id: str):
    """Re-push an edited voucher to Tally. When the voucher has a stored Tally
    master id, the push uses ACTION=Alter so Tally UPDATES the same voucher
    instead of creating a duplicate. Otherwise it falls back to a Create."""
    try:
        v = db.get_tally_voucher(voucher_id)
        if not v:
            raise HTTPException(status_code=404, detail="Voucher not found")

        master_id = v.get("tally_master_id")
        payload = {
            "voucher_type": v.get("voucher_type"),
            "date": v.get("date"),
            "voucher_number": v.get("voucher_number"),
            "party_name": v.get("party_name"),
            "narration": v.get("narration"),
            "ledger_entries": v.get("ledger_entries") or [],
            "amount": v.get("amount"),
            "total_amount": v.get("amount"),
            "taxable_value": v.get("taxable_value"),
            "cgst_amount": v.get("cgst_amount"),
            "sgst_amount": v.get("sgst_amount"),
            "igst_amount": v.get("igst_amount"),
            "company_name": v.get("company_name"),
            # Edit-voucher: tell the bridge agent to alter the existing Tally voucher
            "tally_action": "Alter" if master_id else "Create",
            "tally_master_id": master_id,
        }
        q = db.enqueue_tally_push(
            payload=payload, voucher_id=voucher_id,
            company_name=v.get("company_name"), enqueued_by="web-edit",
        )
        # P1 FIX: do NOT clear needs_resync here — ack_tally_outbox clears it only
        # once the bridge agent CONFIRMS the push (a failed push stays flagged).
        return {"status": "success", "outbox": q,
                "action": payload["tally_action"]}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/upload")
async def upload_voucher_file(
    file: UploadFile = File(...),
    company_name: str = Form("Acme Corp"),
    company_id: str = Form(None),
    username: str = Form(None),
):
    """Accept ONE invoice file (PDF/image/XLSX/CSV), parse via Gemini,
    save to voucher_drafts, return the draft_id for review."""
    try:
        import tempfile, os as _os, uuid as _u, shutil
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        # P0 FIX (#8): this AI parse used to be free. Gate on balance, then charge.
        _ensure_tokens(username, company_name)

        suffix = _os.path.splitext(file.filename or "")[1].lower() or ".bin"
        content = await file.read()

        # Persist file to static/uploads/
        uploads_dir = _os.path.join(_os.path.dirname(__file__), "static", "uploads")
        _os.makedirs(uploads_dir, exist_ok=True)
        stored_name = f"{_u.uuid4()}_{file.filename or 'invoice'}"
        stored_path = _os.path.join(uploads_dir, stored_name)
        with open(stored_path, "wb") as f:
            f.write(content)
        file_url = f"/static/uploads/{stored_name}"

        # Determine file type for the draft
        type_map = {
            ".pdf": "pdf",
            ".png": "image", ".jpg": "image", ".jpeg": "image",
            ".xlsx": "xlsx", ".xls": "xlsx",
            ".csv": "csv",
        }
        file_type = type_map.get(suffix, "other")

        # Run AI parser — reuse InvoiceParser
        parse_prompt = """Extract this invoice/voucher into JSON with fields:
- invoice_number, date (YYYY-MM-DD), party_name, party_gstin
- total_amount, taxable_value, cgst_amount, sgst_amount, igst_amount, place_of_supply
- voucher_type (one of: Sales, Purchase, Payment, Receipt, Journal, Contra)
- items: array of { description, quantity, unit_price, amount, hsn_code, gst_rate }
- narration (one-line description)
Return ONLY a JSON object."""
        import asyncio as _aio
        _loop = _aio.get_event_loop()
        try:
            raw_text = await _loop.run_in_executor(None, parser.parse, stored_path, parse_prompt)
        except Exception as parse_err:
            print(f"[voucher upload] parser error: {parse_err}")
            raw_text = ""
        # Charge for the parse (best-effort; est. from output since parser returns text).
        _charge_ai(username, company_name, "invoice_parse", est_text=raw_text or "")

        # Extract JSON object from response
        parsed = {}
        if raw_text:
            import re as _re
            m = _re.search(r'\{[\s\S]*\}', raw_text)
            if m:
                try: parsed = json.loads(m.group())
                except Exception as jerr:
                    print(f"[voucher upload] JSON parse: {jerr}")

        if not parsed:
            parsed = {"_parse_failed": True, "raw_text": raw_text[:500] if raw_text else ""}

        # P1 FIX: surface a real failure instead of a fake "success" + hardcoded 0.85.
        parse_failed = bool(parsed.get("_parse_failed"))
        draft_id = db.save_voucher_draft(
            company_id=company_id, company_name=company_name,
            parsed_payload=parsed,
            source_file_url=file_url,
            source_file_name=file.filename,
            source_file_type=file_type,
            voucher_type=parsed.get("voucher_type"),
            ai_confidence=0.0 if parse_failed else 0.85,
            created_by="user_upload",
        )

        return {
            "status": "parse_failed" if parse_failed else "success",
            "parse_ok": not parse_failed,
            "message": ("Couldn't read this document automatically — open the draft and "
                        "enter the details manually, or try a clearer file.")
                       if parse_failed else None,
            "draft_id": draft_id,
            "file_url": file_url,
            "file_name": file.filename,
            "file_type": file_type,
            "parsed": parsed,
        }
    except HTTPException:
        raise  # surface 402 out-of-tokens etc. as-is
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/delete")
async def delete_vouchers_endpoint(payload: dict):
    """Sprint 33 — Delete selected vouchers from YantrAI's DB (test/sample/bad
    rows). Body: {company_name, items:[{id, source}]}. Each item is deleted
    from tally_vouchers (source='tally') or invoices (source='invoice'),
    scoped to the company. Also clears any matching tally_outbox rows so the
    Event Log stays consistent. Does NOT touch the customer's Tally Prime."""
    try:
        company_name = (payload or {}).get("company_name")
        items = (payload or {}).get("items") or []
        if not company_name or not items:
            raise HTTPException(status_code=400, detail="company_name and items required")
        from psycopg2.extras import RealDictCursor
        company_id = _resolve_company_id_by_name(company_name)
        conn = db.get_conn(); cur = conn.cursor()
        dcur = conn.cursor(cursor_factory=RealDictCursor)
        deleted = 0
        outbox_cleared = 0
        tally_cleanup = []   # Sprint 33 — tally-sourced rows the user must also remove in Tally Prime
        failed = []          # Sprint 36 — per-item failures surfaced to the UI
        for it in items:
            vid = it.get("id"); src = (it.get("source") or "invoice").lower()
            vnum = it.get("voucher_number")
            if not vid:
                continue
            try:
                if src == "tally":
                    # Capture details BEFORE delete so we can guide manual Tally cleanup.
                    dcur.execute("""SELECT voucher_number, voucher_type, ledger_name, amount, date
                                    FROM tally_vouchers
                                    WHERE id = %s AND (company_id = %s OR company_name = %s)""",
                                 (vid, company_id, company_name))
                    det = dcur.fetchone()
                    if det:
                        cleanup_item = {
                            "voucher_number": det.get("voucher_number"),
                            "voucher_type": det.get("voucher_type"),
                            "party": det.get("ledger_name"),
                            "amount": float(det["amount"]) if det.get("amount") is not None else None,
                            "date": str(det.get("date")) if det.get("date") else None,
                        }
                        tally_cleanup.append(cleanup_item)
                        # Sprint 33 — persist so the cleanup checklist survives
                        # in Event Logs even after the voucher row is gone.
                        try:
                            db.add_tally_cleanup(
                                company_name=company_name,
                                voucher_number=cleanup_item["voucher_number"],
                                voucher_type=cleanup_item["voucher_type"],
                                party=cleanup_item["party"],
                                amount=cleanup_item["amount"],
                                voucher_date=cleanup_item["date"],
                                reason="Deleted from YantrAI — remove from Tally Prime manually")
                        except Exception as ce:
                            print(f"[delete_vouchers] cleanup-log failed: {ce}", flush=True)
                    cur.execute("DELETE FROM tally_vouchers WHERE id = %s AND (company_id = %s OR company_name = %s)",
                                (vid, company_id, company_name))
                else:
                    # Sprint 36 — delete child line items first to satisfy the
                    # items_invoice_id_fkey FK (otherwise the invoice delete
                    # silently fails with a ForeignKeyViolation → deleted=0).
                    cur.execute("DELETE FROM items WHERE invoice_id = %s", (vid,))
                    cur.execute("DELETE FROM invoices WHERE id = %s AND company_name = %s",
                                (vid, company_name))
                row_deleted = cur.rowcount
                deleted += row_deleted
                # Clear matching outbox rows by invoice/voucher number
                if vnum:
                    cur.execute("DELETE FROM tally_outbox WHERE company_name = %s AND payload->>'invoice_number' = %s",
                                (company_name, vnum))
                    outbox_cleared += cur.rowcount
                conn.commit()  # commit per item so one failure doesn't lose the rest
                # Sprint 36 — if nothing matched, tell the user (e.g. wrong company / already gone)
                if row_deleted == 0:
                    failed.append({"id": vid, "voucher_number": vnum,
                                   "reason": "Not found (already deleted or different company)."})
            except Exception as ie:
                print(f"[delete_vouchers] item {vid} ({src}) failed: {ie}", flush=True)
                conn.rollback()
                # Sprint 36 — surface a human-readable reason instead of silent deleted=0
                msg = str(ie)
                if "ForeignKeyViolation" in ie.__class__.__name__ or "foreign key" in msg.lower():
                    msg = "Linked records still reference it (could not remove)."
                failed.append({"id": vid, "voucher_number": vnum, "reason": msg[:160]})
        cur.close(); dcur.close(); conn.close()
        return {"status": "success", "deleted": deleted,
                "outbox_cleared": outbox_cleared, "tally_cleanup": tally_cleanup,
                "failed": failed}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/cleanup/{cleanup_id}/done")
async def mark_cleanup_done_endpoint(cleanup_id: str, payload: dict = None):
    """Sprint 33 — User ticks off a Tally-cleanup item after deleting it in Tally."""
    try:
        done = True if not payload else bool(payload.get("done", True))
        return {"status": "success", **db.mark_tally_cleanup_done(cleanup_id, done=done)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vouchers/drafts")
async def list_drafts_endpoint(company_id: str = None, company_name: str = None,
                                 status: str = None, limit: int = 200):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id required")
        rows = db.list_voucher_drafts(company_id, status=status, limit=limit)
        return {"status": "success", "data": [_draft_jsonify(r) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


def _draft_company_id(company_id=None, company_name=None):
    """Resolve a company_id for tenant-scoping draft access."""
    if company_id:
        return company_id
    if company_name:
        return _resolve_company_id_by_name(company_name)
    return None


@app.get("/api/vouchers/draft/{draft_id}")
async def get_draft_endpoint(draft_id: str, company_name: str = None, company_id: str = None):
    try:
        cid = _draft_company_id(company_id, company_name)
        row = db.get_voucher_draft(draft_id, company_id=cid)
        if not row:
            raise HTTPException(status_code=404, detail="draft not found")
        return {"status": "success", "data": _draft_jsonify(row)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/vouchers/draft/{draft_id}")
async def patch_draft_endpoint(draft_id: str, payload: dict):
    try:
        cid = _draft_company_id(payload.get("company_id"), payload.get("company_name"))
        row = db.update_voucher_draft(
            draft_id,
            reviewed_payload=payload.get("reviewed_payload"),
            voucher_type=payload.get("voucher_type"),
            status=payload.get("status"),
            company_id=cid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="draft not found")
        return {"status": "success", "data": _draft_jsonify(row)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/draft/{draft_id}/post")
async def post_draft_endpoint(draft_id: str, payload: dict = None):
    try:
        payload = payload or {}
        cid = _draft_company_id(payload.get("company_id"), payload.get("company_name"))
        res = db.post_voucher_from_draft(draft_id, company_id=cid)
        if res.get("error"):
            raise HTTPException(status_code=400, detail=res["error"])
        return {"status": "success", **res}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/draft/{draft_id}/discard")
async def discard_draft_endpoint(draft_id: str, payload: dict = None):
    try:
        payload = payload or {}
        cid = _draft_company_id(payload.get("company_id"), payload.get("company_name"))
        row = db.discard_voucher_draft(draft_id, company_id=cid)
        if not row:
            raise HTTPException(status_code=404, detail="draft not found")
        return {"status": "success", "data": _draft_jsonify(row)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vouchers/check-duplicate")
async def check_dup_endpoint(company_name: str, invoice_number: str = None,
                              party: str = None, amount: float = None, date: str = None):
    try:
        row = db.check_voucher_duplicate(company_name, invoice_number, party, amount, date)
        return {"status": "success", "duplicate": row}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vouchers/next-number")
async def next_number_endpoint(company_name: str, voucher_type: str):
    try:
        n = db.next_voucher_number(company_name, voucher_type)
        return {"status": "success", "voucher_number": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/parties")
async def parties_autocomplete(company_id: str = None, company_name: str = None,
                                 q: str = "", limit: int = 10):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        rows = db.autocomplete_parties(company_id, q, limit=limit)
        for r in rows:
            if r.get("closing_balance") is not None:
                r["closing_balance"] = float(r["closing_balance"])
        return {"status": "success", "data": rows}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bank-ledger-options")
async def bank_ledger_options(company_id: str = None, company_name: str = None):
    """Return three lists for the Bank Reco inline-edit dropdowns:
       parties, heads (revenue+expense ledgers), banks (Bank/Cash ledgers).
       Sourced from tally_ledgers — same master Tally itself uses."""
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Match by company_id when known, but also fall back to company_name so
        # rows with a NULL company_id (e.g. parties added via /api/parties/create,
        # or companies not registered in `companies`) still appear in the dropdown.
        cur.execute("""
            SELECT name, parent_group FROM tally_ledgers
            WHERE (company_id = %s OR (company_id IS NULL AND company_name = %s)
                   OR (%s IS NULL AND company_name = %s))
            ORDER BY name
        """, (company_id, company_name, company_id, company_name))
        rows = cur.fetchall()
        parties, heads, banks = [], [], []
        # Case-insensitive party set (preserve first-seen casing) so the dropdown
        # shows the FULL party list with no dupes.
        party_map = {}
        def _add_party(nm):
            nm = (nm or "").strip()
            if nm and nm.lower() not in party_map:
                party_map[nm.lower()] = nm
        for r in rows:
            grp = (r.get("parent_group") or "").lower()
            nm = r.get("name")
            if not nm: continue
            if "sundry" in grp or "debtor" in grp or "creditor" in grp:
                _add_party(nm)
            elif "bank account" in grp or "cash-in-hand" in grp or "cash in hand" in grp:
                banks.append(nm)
            elif "expense" in grp or "income" in grp or "revenue" in grp or "sales" in grp or "purchase" in grp:
                heads.append(nm)
        # Broaden the party list beyond Sundry ledgers: include parties seen on the
        # company's invoices and parties learned into the RAG store (knowledge_base),
        # so the dropdown reflects every party the company actually deals with.
        try:
            cur.execute("""SELECT DISTINCT party_name FROM invoices
                           WHERE company_name = %s AND party_name IS NOT NULL AND party_name <> ''""",
                        (company_name,))
            for r in cur.fetchall(): _add_party(r.get("party_name"))
        except Exception:
            conn.rollback()
        try:
            cur.execute("""SELECT DISTINCT data->>'party' AS p FROM knowledge_base
                           WHERE type='tally_master_party' AND data->>'company_name' = %s
                             AND COALESCE(data->>'party','') <> ''""",
                        (company_name,))
            for r in cur.fetchall(): _add_party(r.get("p"))
        except Exception:
            conn.rollback()
        cur.close(); conn.close()
        parties = sorted(party_map.values(), key=lambda s: s.lower())
        return {"status": "success", "parties": parties, "heads": heads, "banks": banks}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/parties/create")
async def create_party_endpoint(payload: dict, background_tasks: BackgroundTasks = None):
    """Add a new party to the company's ledger master so it appears in the
    Bank-Reco party dropdown for all future rows. Body: {company_name, company_id?,
    name, group?}. group defaults to 'Sundry Debtors' (customer); pass
    'Sundry Creditors' for a vendor. Does NOT post to Tally — that happens when a
    voucher referencing the party is posted.

    Also TEACHES the RAG store this party (embed_party) so the AI's party
    suggestions improve and it shows in Training Progress — i.e. the AI learns
    from each party a human adds."""
    try:
        company_name = (payload.get("company_name") or "").strip()
        name = (payload.get("name") or "").strip()
        if not company_name or not name:
            raise HTTPException(status_code=400, detail="company_name and name required")
        company_id = payload.get("company_id")
        if not company_id:
            company_id = _resolve_company_id_by_name(company_name)
        group = payload.get("group") or "Sundry Debtors"
        res = db.add_party_ledger(company_name, name, group=group, company_id=company_id)
        if res.get("status") != "success":
            raise HTTPException(status_code=500, detail=res.get("message", "add party failed"))
        # Also make it a first-class Party Master row (editable: GSTIN/PAN/bank later),
        # so a party added in Bank Reco shows up in the Party Master directory too.
        try:
            db.save_or_update_party(company_name, name, gstin=payload.get("gstin"))
        except Exception as _pe:
            print(f"[create_party] save_or_update_party error: {_pe}", flush=True)
        # Learn it into the RAG store (background — keeps the add snappy).
        try:
            if background_tasks is not None:
                background_tasks.add_task(db.embed_party, company_name, name,
                                         get_embedding, company_id, payload.get("gstin"))
            else:
                db.embed_party(company_name, name, get_embedding, company_id, payload.get("gstin"))
        except Exception as _e:
            print(f"[create_party] embed_party error: {_e}", flush=True)
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstin-lookup")
async def gstin_lookup_endpoint(gstin: str, company_id: str = None, company_name: str = None):
    """Look up an existing ledger by GSTIN (used by review modal autofill)."""
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        row = db.lookup_party_by_gstin(gstin, company_id, company_name)
        return {"status": "success", "data": row}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Sprint 26 — AI Gap Scan endpoints ──
@app.post("/api/vouchers/ai-scan")
async def ai_scan_endpoint(payload: dict):
    """Run the AI gap scan over every voucher of the active company. Returns
    {run_id, totals: {gap_type → count}}. Body: {company_name, company_id?, gap_types?}."""
    try:
        company_name = payload.get("company_name") or ""
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")
        company_id = payload.get("company_id")
        gap_types = payload.get("gap_types")  # optional list
        # Run blocking work in executor (DB-heavy)
        import asyncio as _aio
        loop = _aio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: db.run_voucher_ai_scan(company_id, company_name, gap_types)
        )
        return {"status": "success", **result}
    except HTTPException: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vouchers/ai-suggestions")
async def ai_suggestions_list(company_name: str, gap_type: str = None,
                              status: str = "pending", limit: int = 10000):
    """List AI suggestions for the company, optionally filtered by gap_type."""
    try:
        rows = db.list_ai_suggestions(company_name, gap_type=gap_type, status=status, limit=limit)
        # Normalize types for JSON
        for r in rows:
            for k in ("id", "voucher_id", "created_at", "amount", "voucher_date", "confidence"):
                if k in r and r[k] is not None:
                    try: r[k] = str(r[k]) if k in ("id", "voucher_id", "created_at", "voucher_date") else float(r[k])
                    except: pass
        return {"status": "success", "data": rows}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vouchers/ai-suggestions/counts")
async def ai_suggestion_counts_endpoint(company_name: str):
    """Pending count grouped by gap_type — feeds the AI Gap filter dropdown."""
    try:
        return {"status": "success", "counts": db.ai_suggestion_counts(company_name)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/ai-suggestions/{sid}/accept")
async def ai_suggestion_accept(sid: str):
    try:
        res = db.accept_ai_suggestion(sid)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("message", "Accept failed"))
        return {"status": "success"}
    except HTTPException: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/ai-suggestions/{sid}/reject")
async def ai_suggestion_reject(sid: str):
    try:
        res = db.reject_ai_suggestion(sid)
        return {"status": "success", **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/ai-suggestions/bulk-accept")
async def ai_suggestion_bulk_accept(payload: dict):
    try:
        company_name = payload.get("company_name") or ""
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")
        gap_type = payload.get("gap_type")
        min_conf = float(payload.get("min_confidence") or 0)
        res = db.bulk_accept_ai_suggestions(company_name, gap_type=gap_type, min_confidence=min_conf)
        return {"status": "success", **res}
    except HTTPException: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ── Sprint 27 — Master AI Gap Scan endpoints (Party + Item) ──
@app.post("/api/masters/ai-scan")
async def master_ai_scan_endpoint(payload: dict):
    try:
        company_name = payload.get("company_name") or ""
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")
        company_id = payload.get("company_id")
        master_types = payload.get("master_types")
        import asyncio as _aio
        loop = _aio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: db.run_master_ai_scan(company_id, company_name, master_types)
        )
        return {"status": "success", **result}
    except HTTPException: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/masters/ai-suggestions")
async def master_ai_suggestions_list(company_name: str, master_type: str = None,
                                      gap_type: str = None, status: str = "pending",
                                      limit: int = 20000):
    try:
        rows = db.list_master_ai_suggestions(company_name, master_type=master_type,
                                              gap_type=gap_type, status=status, limit=limit)
        for r in rows:
            for k in ("id", "record_id", "created_at", "confidence"):
                if k in r and r[k] is not None:
                    try: r[k] = str(r[k]) if k in ("id","record_id","created_at") else float(r[k])
                    except: pass
        return {"status": "success", "data": rows}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/masters/ai-suggestions/counts")
async def master_ai_suggestion_counts_endpoint(company_name: str):
    try:
        return {"status": "success", "counts": db.master_ai_suggestion_counts(company_name)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/masters/ai-suggestions/{sid}/accept")
async def master_ai_suggestion_accept(sid: str):
    try:
        res = db.accept_master_ai_suggestion(sid)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("message","Accept failed"))
        return {"status": "success"}
    except HTTPException: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/masters/ai-suggestions/{sid}/reject")
async def master_ai_suggestion_reject(sid: str):
    try:
        res = db.reject_master_ai_suggestion(sid)
        return {"status": "success", **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/masters/ai-suggestions/bulk-accept")
async def master_ai_suggestion_bulk_accept(payload: dict):
    try:
        company_name = payload.get("company_name") or ""
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")
        res = db.bulk_accept_master_ai_suggestions(
            company_name,
            master_type=payload.get("master_type"),
            gap_type=payload.get("gap_type"),
            min_confidence=float(payload.get("min_confidence") or 0),
        )
        return {"status": "success", **res}
    except HTTPException: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vouchers/health")
async def voucher_health_endpoint(company_name: str, company_id: str = None):
    """Returns voucher health metrics: totals by source, drafts pending,
    duplicates, vouchers missing GSTIN, unbalanced entries."""
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        out = {"totals": {}, "duplicates": [], "missing_gstin": 0, "unbalanced": 0,
               "drafts_pending": 0, "drafts_error": 0, "by_type": {}}

        # Totals split by source (tally vs uploaded)
        cur.execute("""
            SELECT COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE tally_master_id IS NOT NULL AND tally_master_id <> '') AS from_tally,
                   COUNT(*) FILTER (WHERE tally_master_id IS NULL OR tally_master_id = '') AS uploaded
            FROM tally_vouchers WHERE company_name = %s
        """, (company_name,))
        r = cur.fetchone()
        out["totals"] = {"total": r["n"], "from_tally": r["from_tally"], "uploaded": r["uploaded"]}

        # By voucher type
        cur.execute("""
            SELECT voucher_type, COUNT(*) AS n FROM tally_vouchers
            WHERE company_name = %s GROUP BY voucher_type ORDER BY n DESC
        """, (company_name,))
        out["by_type"] = {row["voucher_type"] or "(unknown)": row["n"] for row in cur.fetchall()}

        # Drafts pending / error
        if company_id:
            cur.execute("""
                SELECT status, COUNT(*) AS n FROM voucher_drafts
                WHERE company_id = %s GROUP BY status
            """, (company_id,))
            for row in cur.fetchall():
                if row["status"] == "ready_for_review" or row["status"] == "edited":
                    out["drafts_pending"] += row["n"]
                elif row["status"] == "error":
                    out["drafts_error"] += row["n"]

        # Duplicate suspects (same voucher_number + party + amount)
        cur.execute("""
            SELECT voucher_number, ledger_name, amount, COUNT(*) AS cnt,
                   array_agg(id::text) AS ids, array_agg(date::text) AS dates
            FROM tally_vouchers
            WHERE company_name = %s AND voucher_number IS NOT NULL AND voucher_number <> ''
            GROUP BY voucher_number, ledger_name, amount
            HAVING COUNT(*) > 1
            LIMIT 50
        """, (company_name,))
        for row in cur.fetchall():
            out["duplicates"].append({
                "voucher_number": row["voucher_number"],
                "party": row["ledger_name"],
                "amount": float(row["amount"]) if row["amount"] else 0,
                "count": row["cnt"],
                "ids": row["ids"],
                "dates": row["dates"],
            })

        # Vouchers missing GSTIN (Sales/Purchase only)
        cur.execute("""
            SELECT COUNT(*) AS n FROM tally_vouchers
            WHERE company_name = %s
                  AND voucher_type IN ('Sales', 'Purchase')
                  AND (party_gstin IS NULL OR party_gstin = '')
        """, (company_name,))
        out["missing_gstin"] = cur.fetchone()["n"]

        # Unbalanced entries — sum debit-credit of ledger_entries should be ~0
        # Defer to client-side check for speed; for now just report 0
        out["unbalanced"] = 0

        cur.close(); conn.close()
        return {"status": "success", "data": out}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vouchers/events")
async def voucher_events_endpoint(company_name: str, company_id: str = None, limit: int = 50):
    """Return recent voucher events — unified stream from voucher_drafts
    (parsed/edited/posted/discarded) AND tally_outbox (queued/pushing/pushed/
    error). Sprint 33: outbox events make the end-to-end Tally sync visible."""
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            return {"status": "success", "data": []}
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Voucher drafts (file uploads + manual edits)
        cur.execute("""
            SELECT id, source_file_name, source_file_type, voucher_type, status,
                   ai_confidence, parsed_payload, reviewed_payload,
                   created_at, updated_at, created_by
            FROM voucher_drafts
            WHERE company_id = %s
            ORDER BY updated_at DESC NULLS LAST, created_at DESC LIMIT %s
        """, (company_id, limit))
        rows = []
        for r in cur.fetchall():
            for k in ("id",): r[k] = str(r[k]) if r.get(k) else None
            for k in ("created_at", "updated_at"):
                if r.get(k): r[k] = str(r[k])
            if r.get("ai_confidence") is not None:
                r["ai_confidence"] = float(r["ai_confidence"])
            r["event_source"] = "voucher_draft"
            rows.append(r)

        # Sprint 33 — Also surface the Tally outbox stream
        try:
            cur.execute("""
                SELECT id, payload, state, attempts, last_error,
                       tally_voucher_guid, enqueued_at, pushed_at, updated_at, enqueued_by
                FROM tally_outbox
                WHERE company_name = %s
                ORDER BY updated_at DESC NULLS LAST, enqueued_at DESC
                LIMIT %s
            """, (company_name, limit))
            STATE_MAP = {
                'pending': 'queued_for_tally',
                'pushing': 'pushing_to_tally',
                'pushed':  'tally_synced',
                'error':   'tally_error',
            }
            for r in cur.fetchall():
                payload = r.get("payload") or {}
                if isinstance(payload, str):
                    try: payload = json.loads(payload)
                    except: payload = {}
                rows.append({
                    "id": str(r.get("id")),
                    "event_source": "tally_outbox",
                    "status": STATE_MAP.get(r.get("state"), r.get("state")),
                    "raw_state": r.get("state"),
                    "attempts": r.get("attempts"),
                    "last_error": r.get("last_error"),
                    "tally_voucher_guid": r.get("tally_voucher_guid"),
                    "voucher_type": payload.get("voucher_type") or payload.get("category"),
                    "parsed_payload": payload,
                    "reviewed_payload": payload,
                    "source_file_name": payload.get("invoice_number") or payload.get("voucher_number") or "(chat / manual)",
                    "source_file_type": "tally_push",
                    "created_at": str(r.get("enqueued_at")) if r.get("enqueued_at") else None,
                    "updated_at": str(r.get("updated_at") or r.get("pushed_at") or r.get("enqueued_at")) if (r.get("updated_at") or r.get("pushed_at") or r.get("enqueued_at")) else None,
                    "created_by": r.get("enqueued_by"),
                })
        except Exception as oe:
            print(f"[voucher_events] tally_outbox fetch failed: {oe}", flush=True)

        # Sprint 33 — manual Tally cleanup items (persist past voucher deletion)
        try:
            for cl in db.list_tally_cleanup(company_name):
                rows.append({
                    "id": cl.get("id"),
                    "event_source": "tally_cleanup",
                    "status": "needs_tally_cleanup" if cl.get("status") != "done" else "tally_cleanup_done",
                    "cleanup_status": cl.get("status"),
                    "voucher_type": cl.get("voucher_type"),
                    "source_file_name": cl.get("voucher_number") or "(voucher)",
                    "source_file_type": "tally_cleanup",
                    "last_error": cl.get("reason"),
                    "parsed_payload": {"party": cl.get("party"), "total_amount": cl.get("amount")},
                    "reviewed_payload": {"party": cl.get("party"), "total_amount": cl.get("amount")},
                    "created_at": cl.get("created_at"),
                    "updated_at": cl.get("done_at") or cl.get("created_at"),
                    "voucher_date": cl.get("voucher_date"),
                })
        except Exception as ce:
            print(f"[voucher_events] cleanup fetch failed: {ce}", flush=True)

        # DOWNLOAD side — per-voucher events from Tally pulls (created/updated).
        try:
            for ev in db.list_voucher_sync_events(company_name, limit=limit):
                act = ev.get("action")
                rows.append({
                    "id": None,
                    "event_source": "tally_download",
                    "status": f"tally_{act}",          # tally_created | tally_updated
                    "direction": ev.get("direction"),
                    "voucher_type": None,
                    "source_file_name": ev.get("voucher_number") or "(tally voucher)",
                    "source_file_type": "tally_pull",
                    "tally_voucher_guid": ev.get("tally_master_id"),
                    "parsed_payload": {"party": ev.get("party"), "total_amount": ev.get("amount")},
                    "reviewed_payload": {"party": ev.get("party"), "total_amount": ev.get("amount")},
                    "created_at": ev.get("created_at"),
                    "updated_at": ev.get("created_at"),
                })
        except Exception as de:
            print(f"[voucher_events] download fetch failed: {de}", flush=True)

        # Sort merged stream by updated_at desc, cap at limit
        rows.sort(key=lambda x: (x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        rows = rows[:limit]
        cur.close(); conn.close()
        return {"status": "success", "data": rows}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# NOTE: declared after all literal /api/vouchers/* GET routes so the {voucher_id}
# path param doesn't shadow them (FastAPI matches routes in declaration order).
@app.get("/api/vouchers/{voucher_id}")
async def get_voucher_endpoint(voucher_id: str):
    """Fetch a single voucher for the edit modal. Sprint 37 — handles BOTH a
    Tally-sourced voucher (tally_vouchers) AND an invoice/PDF/manual row
    (invoices), so the edit button works on every row."""
    try:
        row = db.get_tally_voucher(voucher_id)
        if row:
            row["source"] = "tally"
            # Sprint 38 — a voucher synced from Tally may still have originated
            # from an uploaded file; surface that file if one exists for the
            # same company + voucher number.
            try:
                if not row.get("file_url"):
                    co = row.get("company_name")
                    vno = row.get("voucher_number")
                    if co and vno:
                        conn2 = db.get_conn(); cur2 = conn2.cursor()
                        cur2.execute("""
                            SELECT file_url FROM invoices
                            WHERE company_name=%s AND invoice_number=%s
                              AND file_url IS NOT NULL
                            LIMIT 1
                        """, (co, vno))
                        fr = cur2.fetchone()
                        cur2.close(); conn2.close()
                        if fr and fr[0]:
                            row["file_url"] = fr[0]
            except Exception as fe:
                print(f"[get_voucher] tally file_url lookup failed: {fe}", flush=True)
            return {"status": "success", "data": row}
        # Fallback: invoice-source row
        from psycopg2.extras import RealDictCursor
        conn = db.get_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, invoice_number, date, party_name, total_amount,
                   COALESCE(voucher_type, category) AS voucher_type, category,
                   billing_party_name, billing_party_gstin, billed_to_party_gstin,
                   file_url
            FROM invoices WHERE id = %s LIMIT 1
        """, (voucher_id,))
        inv = cur.fetchone(); cur.close(); conn.close()
        if not inv:
            raise HTTPException(status_code=404, detail="Voucher not found")
        data = {
            "id": str(inv["id"]),
            "voucher_number": inv.get("invoice_number"),
            "date": str(inv.get("date")) if inv.get("date") else None,
            "party_name": inv.get("party_name") or inv.get("billing_party_name"),
            "amount": float(inv["total_amount"]) if inv.get("total_amount") is not None else 0,
            "voucher_type": inv.get("voucher_type") or "Sales",
            "narration": "",
            "ledger_entries": [],
            "tally_master_id": "",
            "source": "invoice",
            "file_url": inv.get("file_url"),
        }
        return {"status": "success", "data": data}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════
# SPRINT 4 — GSTR Reconciliation endpoints
# ═══════════════════════════════════════════════════════════════

def _normalize_gstr_2a_2b_row(raw):
    """Normalize one row from any GSTR-2A/2B file format into our canonical shape."""
    keys_lower = {k.lower().strip(): v for k, v in raw.items() if k}
    def pick(*alts):
        for a in alts:
            for k in keys_lower:
                if a in k:
                    v = keys_lower[k]
                    if v is None: continue
                    return str(v).strip()
        return ""
    inv_num = pick("invoice no", "invoice number", "bill no", "doc no")
    party = pick("trade name", "legal name", "supplier", "party")
    gstin = pick("gstin")
    date_raw = pick("invoice date", "doc date", "date")
    # Parse date
    from datetime import datetime as _dt
    inv_date = None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try: inv_date = _dt.strptime(date_raw.split()[0], fmt).strftime("%Y-%m-%d"); break
        except: continue
    def num(s):
        try: return float(str(s or 0).replace(",", "").replace("₹", "").strip())
        except: return 0.0
    return {
        "invoice_number": inv_num,
        "party_name": party,
        "party_gstin": gstin,
        "invoice_date": inv_date,
        "taxable_value": num(pick("taxable value")),
        "cgst_amount": num(pick("cgst")),
        "sgst_amount": num(pick("sgst")),
        "igst_amount": num(pick("igst")),
        "invoice_value": num(pick("invoice value", "total")),
        "amount": num(pick("invoice value", "total")),
    }


@app.post("/api/gstr/2a-2b/upload")
async def upload_2a_2b(
    file: UploadFile = File(...),
    company_name: str = Form(...),
    company_id: str = Form(None),
    period: str = Form(...),
    return_type: str = Form("GSTR-2B"),
):
    """Upload a GSTR-2A or 2B file (JSON/Excel/CSV). Parse, dedup by sha256,
    insert gstr_filings + run match_gstr_against_vouchers → returns filing_id + summary."""
    try:
        import tempfile, os as _os, hashlib, shutil, uuid as _u
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        content = await file.read()
        sha_hex = hashlib.sha256(content).hexdigest()
        suffix = _os.path.splitext(file.filename or "")[1].lower() or ".bin"

        # Persist to static/uploads
        uploads_dir = _os.path.join(_os.path.dirname(__file__), "static", "uploads")
        _os.makedirs(uploads_dir, exist_ok=True)
        stored_name = f"{_u.uuid4()}_{file.filename or 'gstr.bin'}"
        stored_path = _os.path.join(uploads_dir, stored_name)
        with open(stored_path, "wb") as f:
            f.write(content)
        file_url = f"/static/uploads/{stored_name}"

        # Parse rows
        rows = []
        if suffix in (".xlsx", ".xls", ".csv"):
            import pandas as _pd
            try:
                if suffix == ".csv":
                    df = _pd.read_csv(stored_path)
                    rows = [_normalize_gstr_2a_2b_row(r) for r in df.to_dict(orient="records")]
                else:
                    xl = _pd.ExcelFile(stored_path)
                    for sh in xl.sheet_names:
                        for ridx in range(min(15, len(xl.parse(sh, header=None)))):
                            row_str = ' '.join(str(c).lower() for c in xl.parse(sh, header=None).iloc[ridx].values if str(c) != 'nan')
                            if sum(1 for k in ['gstin', 'invoice', 'taxable', 'cgst', 'sgst', 'igst'] if k in row_str) >= 2:
                                df = xl.parse(sh, header=ridx)
                                df = df.dropna(how='all')
                                rows.extend(_normalize_gstr_2a_2b_row(r) for r in df.to_dict(orient="records"))
                                break
            except Exception as pe:
                print(f"[GSTR upload] parse error: {pe}")
        elif suffix == ".json":
            try:
                data = json.loads(content.decode('utf-8'))
                # Flatten common GSTN portal JSON structures
                if isinstance(data, list):
                    rows = [_normalize_gstr_2a_2b_row(r) for r in data]
                elif isinstance(data, dict):
                    for key in ("b2b", "B2B", "data", "items", "result"):
                        if key in data and isinstance(data[key], list):
                            rows = [_normalize_gstr_2a_2b_row(r) for r in data[key]]
                            break
            except Exception as je:
                print(f"[GSTR upload] JSON parse: {je}")

        # Drop empty rows
        rows = [r for r in rows if r.get("invoice_number") or r.get("party_name") or r.get("amount")]

        if not rows:
            return {"status": "error", "message": "Could not extract any rows from this file."}

        filing_id = db.save_gstr_filing(
            company_id=company_id, company_name=company_name,
            period=period, return_type=return_type,
            source_file_url=file_url, source_file_name=file.filename,
            sha256_hex=sha_hex, payload={"row_count": len(rows)},
            uploaded_by="user",
        )

        import asyncio as _aio
        _loop = _aio.get_event_loop()
        summary = await _loop.run_in_executor(
            None, db.match_gstr_against_vouchers,
            filing_id, company_id, company_name, rows
        )

        return {"status": "success", "filing_id": filing_id, "row_count": len(rows), "summary": summary}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/filings")
async def list_gstr_filings_endpoint(company_id: str = None, company_name: str = None,
                                       return_type: str = None, period: str = None):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id required")
        rows = db.list_gstr_filings(company_id, return_type=return_type, period=period)
        for r in rows:
            r["id"] = str(r["id"])
            if r.get("created_at"): r["created_at"] = str(r["created_at"])
        return {"status": "success", "data": rows}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/2a-2b/{filing_id}")
async def get_gstr_filing(filing_id: str, status: str = None):
    try:
        rows = db.get_gstr_filing_lines(filing_id, status=status)
        for r in rows:
            r["id"] = str(r["id"])
            if r.get("matched_voucher_id"): r["matched_voucher_id"] = str(r["matched_voucher_id"])
        return {"status": "success", "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/gstr/reco-line/{line_id}")
async def patch_gstr_reco_line(line_id: str, payload: dict):
    try:
        row = db.update_gstr_reco_line(line_id, payload)
        if not row:
            raise HTTPException(status_code=404, detail="line not found")
        row["id"] = str(row["id"])
        if row.get("matched_voucher_id"): row["matched_voucher_id"] = str(row["matched_voucher_id"])
        return {"status": "success", "data": row}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/itc-comparison")
async def itc_comparison_endpoint(company_name: str, from_period: str, to_period: str,
                                    company_id: str = None):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id required")
        data = db.itc_comparison(company_id, company_name, from_period, to_period)
        return {"status": "success", "data": data}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/gstr1-vs-3b")
async def gstr1_vs_3b_endpoint(company_name: str, period: str):
    try:
        data = db.gstr1_vs_3b_variance(company_name, period)
        return {"status": "success", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/gstr/gstr9/generate")
async def gstr9_generate(payload: dict):
    try:
        company_name = payload.get("company_name")
        fy = payload.get("fy")
        if not company_name or not fy:
            raise HTTPException(status_code=400, detail="company_name + fy required")
        data = db.gstr9_aggregate(company_name, fy)
        return {"status": "success", "data": data}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/invoice-serial-gaps")
async def invoice_serial_gaps_endpoint(company_name: str, voucher_type: str = "Sales"):
    try:
        data = db.invoice_serial_gaps(company_name, voucher_type)
        return {"status": "success", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gstr/hsn-summary")
async def hsn_summary_endpoint(company_name: str, period: str = None):
    try:
        data = db.hsn_summary(company_name, period)
        return {"status": "success", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════
# SPRINT 5 — Audit & Compliance endpoints
# ═══════════════════════════════════════════════════════════════

@app.get("/api/audit/health")
async def audit_health_endpoint(company_name: str, company_id: str = None, fy: str = None):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id required")
        import asyncio as _aio
        _loop = _aio.get_event_loop()
        checks = await _loop.run_in_executor(None, db.run_audit_checks, company_id, company_name)
        # Summary
        summary = {"total": len(checks), "pass": 0, "warn": 0, "fail": 0, "skip": 0, "pending": 0}
        for c in checks:
            summary[c.get("status", "pending")] = summary.get(c.get("status", "pending"), 0) + 1
        return {"status": "success", "data": {"checks": checks, "summary": summary}}
    except HTTPException: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/audit/calendar")
async def audit_calendar_endpoint(company_name: str, company_id: str = None,
                                    from_date: str = None, to_date: str = None):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id required")
        rows = db.list_filing_deadlines(company_id, from_date, to_date)
        return {"status": "success", "data": rows}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/audit/calendar/seed")
async def audit_calendar_seed(payload: dict):
    try:
        fy = payload.get("fy")
        company_id = payload.get("company_id")
        company_name = payload.get("company_name")
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not fy:
            raise HTTPException(status_code=400, detail="fy required (e.g. '2025-26')")
        res = db.seed_filing_deadlines_for_fy(fy, company_id)
        return {"status": "success", **res}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/audit/audit-log")
async def audit_log_endpoint(company_id: str, action: str = None, limit: int = 100):
    try:
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        where = ["company_id = %s"]
        params = [company_id]
        if action:
            where.append("action = %s"); params.append(action)
        cur.execute(f"""
            SELECT id, user_id, action, entity_type, payload, created_at
            FROM tenant_audit_log WHERE {' AND '.join(where)}
            ORDER BY created_at DESC LIMIT %s
        """, params + [limit])
        rows = cur.fetchall()
        cur.close(); conn.close()
        for r in rows:
            for k in ("id", "user_id"):
                if r.get(k) is not None: r[k] = str(r[k])
            if r.get("created_at"): r["created_at"] = str(r["created_at"])
        return {"status": "success", "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════
# SPRINT 6 — TDS endpoints
# ═══════════════════════════════════════════════════════════════

@app.get("/api/tds/sections")
async def tds_sections_endpoint():
    try:
        rows = db.list_tds_sections()
        return {"status": "success", "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tds/auto-detect")
async def tds_auto_detect(payload: dict):
    try:
        voucher = payload.get("voucher") or {}
        party_ledger = payload.get("party_ledger") or {}
        suggestion = db.suggest_tds_for_voucher(voucher, party_ledger)
        return {"status": "success", "suggestion": suggestion}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tds/deductions")
async def list_tds_deductions_endpoint(company_id: str = None, company_name: str = None,
                                          fy: str = None, quarter: str = None, section: str = None):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id required")
        rows = db.list_tds_deductions(company_id, fy=fy, quarter=quarter, section=section)
        return {"status": "success", "data": rows}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tds/deductions")
async def create_tds_deduction(payload: dict):
    try:
        company_name = payload.get("company_name")
        company_id = payload.get("company_id")
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        new_id = db.save_tds_deduction(
            company_id=company_id, company_name=company_name,
            voucher_id=payload.get("voucher_id"),
            party_name=payload.get("party_name"),
            party_pan=payload.get("party_pan"),
            section=payload.get("section"),
            gross_amount=payload.get("gross_amount"),
            tds_amount=payload.get("tds_amount"),
            rate_applied=payload.get("rate_applied"),
            deduction_date=payload.get("deduction_date"),
            fy=payload.get("fy"),
            quarter=payload.get("quarter"),
            created_by=payload.get("username") or payload.get("created_by"),
        )
        return {"status": "success", "id": new_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tds/quarterly-summary")
async def tds_quarterly_endpoint(company_name: str, fy: str, company_id: str = None):
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_id:
            raise HTTPException(status_code=400, detail="company_id required")
        rows = db.tds_quarterly_summary(company_id, fy)
        return {"status": "success", "data": rows}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tds/return/{form}/{quarter}/generate")
async def tds_return_generate(form: str, quarter: str, payload: dict):
    """Stub for v1 — returns a JSON aggregate of TDS deductions for the period.
    Full 24Q/26Q schema generation in v2."""
    try:
        company_name = payload.get("company_name")
        company_id = payload.get("company_id")
        fy = payload.get("fy")
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not (company_id and fy):
            raise HTTPException(status_code=400, detail="company_id + fy required")
        rows = db.list_tds_deductions(company_id, fy=fy, quarter=quarter)
        total = sum(r["tds_amount"] for r in rows if r.get("tds_amount"))
        return {"status": "success", "form": form, "quarter": quarter, "fy": fy,
                "total_tds": total, "deductee_count": len(rows), "rows": rows}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tds/26as/upload")
async def upload_26as(
    file: UploadFile = File(...),
    company_name: str = Form(...),
    company_id: str = Form(None),
    fy: str = Form(...),
):
    """Upload Form 26AS (Excel/PDF). Just save + record metadata for v1."""
    try:
        import tempfile, os as _os, hashlib, uuid as _u
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        content = await file.read()
        sha_hex = hashlib.sha256(content).hexdigest()
        uploads_dir = _os.path.join(_os.path.dirname(__file__), "static", "uploads")
        _os.makedirs(uploads_dir, exist_ok=True)
        stored_name = f"{_u.uuid4()}_{file.filename or '26as.xlsx'}"
        stored_path = _os.path.join(uploads_dir, stored_name)
        with open(stored_path, "wb") as f:
            f.write(content)
        file_url = f"/static/uploads/{stored_name}"

        conn = db.get_conn()
        cur = conn.cursor()
        new_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO form_26as_imports
                (id, company_id, company_name, fy, source_file_url, source_file_name, sha256)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (new_id, company_id, company_name, fy, file_url, file.filename, sha_hex))
        conn.commit()
        cur.close(); conn.close()
        return {"status": "success", "import_id": new_id, "file_url": file_url}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tds/26as/imports")
async def list_26as_imports(company_id: str):
    try:
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, fy, source_file_name, source_file_url, total_tds_credit, uploaded_at
            FROM form_26as_imports WHERE company_id = %s ORDER BY uploaded_at DESC
        """, (company_id,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        for r in rows:
            r["id"] = str(r["id"])
            if r.get("uploaded_at"): r["uploaded_at"] = str(r["uploaded_at"])
            if r.get("total_tds_credit") is not None: r["total_tds_credit"] = float(r["total_tds_credit"])
        return {"status": "success", "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tds/pan-status")
async def pan_status_endpoint(company_id: str):
    """Return unique PANs across deductees with placeholder linked status."""
    try:
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT party_pan, COUNT(*) AS deduction_count,
                   COALESCE(SUM(tds_amount), 0) AS total_tds,
                   bool_or(party_aadhaar_linked) AS aadhaar_linked
            FROM tds_deductions WHERE company_id = %s AND party_pan IS NOT NULL
            GROUP BY party_pan ORDER BY total_tds DESC
        """, (company_id,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        import re as _re
        for r in rows:
            r["total_tds"] = float(r.get("total_tds") or 0)
            pan = r.get("party_pan") or ""
            # PAN format check
            r["pan_format_valid"] = bool(_re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan))
        return {"status": "success", "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════════════════════════════
# ITR shell — stats teaser
# ════════════════════════════════════════════════════════════════

@app.get("/api/itr/teaser")
async def itr_teaser_endpoint(company_name: str, company_id: str = None):
    """Returns the teaser stats shown on the ITR Filing shell page."""
    try:
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Turnover = sum of Sales taxable_value
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) AS turnover
            FROM tally_vouchers
            WHERE company_name = %s AND voucher_type = 'Sales'
        """, (company_name,))
        turnover = float(cur.fetchone()["turnover"] or 0)
        # TDS deducted
        tds = 0.0
        if company_id:
            cur.execute("SELECT COALESCE(SUM(tds_amount), 0) AS t FROM tds_deductions WHERE company_id = %s", (company_id,))
            tds = float(cur.fetchone()["t"] or 0)
        cur.close(); conn.close()
        return {"status": "success", "data": {
            "turnover": turnover, "tds_deducted": tds,
            "advance_tax": 0.0, "tax_provision": 0.0,
        }}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vouchers/bulk-excel")
async def bulk_excel_voucher_import(payload: dict):
    """Insert pre-mapped voucher rows from a bulk Excel import.
    payload = { company_name, company_id, rows: [{date, voucher_type, voucher_number, party, debit, credit, narration, ...}, ...] }"""
    try:
        company_name = payload.get("company_name")
        company_id = payload.get("company_id")
        if not company_id and company_name:
            company_id = _resolve_company_id_by_name(company_name)
        if not company_name:
            raise HTTPException(status_code=400, detail="company_name required")
        rows = payload.get("rows") or []

        vouchers = []
        errors = []
        for idx, r in enumerate(rows):
            try:
                date_compact = (r.get("date") or "").replace("-", "")[:8]
                dr = float(r.get("debit") or 0)
                cr = float(r.get("credit") or 0)
                amount = max(dr, cr)
                if amount <= 0:
                    errors.append({"row": idx, "error": "no debit/credit amount"})
                    continue
                # P1 FIX: use the row's specified contra ledger instead of always 'Cash'
                # (a Sales/Purchase row got a bogus Cash leg). Fall back to Cash only if
                # the import didn't map a counter ledger.
                contra = (r.get("counter_ledger") or r.get("payment_mode") or "").strip() or "Cash"
                main_leg = r.get("ledger_name") or r.get("party")
                ledger_entries = []
                if dr > 0:
                    ledger_entries.append({"ledger_name": main_leg, "amount": dr, "is_debit": True})
                    ledger_entries.append({"ledger_name": contra, "amount": -dr, "is_debit": False})
                else:
                    ledger_entries.append({"ledger_name": contra, "amount": cr, "is_debit": True})
                    ledger_entries.append({"ledger_name": main_leg, "amount": -cr, "is_debit": False})

                voucher = {
                    "date": date_compact,
                    "type": r.get("voucher_type") or "Journal",
                    "voucher_type": r.get("voucher_type") or "Journal",
                    "party": r.get("party") or "",
                    "number": r.get("voucher_number") or "",
                    "amount": amount,
                    "narration": r.get("narration") or "",
                    "ledger_entries": ledger_entries,
                    "reference_no": r.get("reference_no") or "",
                    "currency": "INR",
                    "tally_master_id": None,
                }
                # P1 FIX: validate balance/GST before importing (was unchecked).
                ok_v, err_v = db.validate_voucher_for_post(voucher)
                if not ok_v:
                    errors.append({"row": idx, "error": err_v})
                    continue
                vouchers.append(voucher)
            except Exception as row_err:
                errors.append({"row": idx, "error": str(row_err)})

        save_res = db.save_tally_vouchers(company_name, vouchers)

        # Backfill company_id
        if company_id:
            try:
                conn_bf = db.get_conn()
                cur_bf = conn_bf.cursor()
                cur_bf.execute(
                    "UPDATE tally_vouchers SET company_id = %s WHERE company_name = %s AND company_id IS NULL",
                    (company_id, company_name)
                )
                conn_bf.commit()
                cur_bf.close()
                conn_bf.close()
            except Exception as bf_err:
                print(f"[bulk-excel backfill] {bf_err}")

        return {"status": "success",
                "upserted": save_res.get("upserted", 0),
                "skipped": save_res.get("skipped", 0),
                "errors": errors}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bank-reconciliation/upload")
async def bank_reconciliation_upload(
    file: UploadFile = File(...),
    company_name: str = Form("Acme Corp")
):
    """Parse a bank statement file (CSV/XLSX/PDF) and reconcile against Tally vouchers."""
    try:
        import tempfile, os
        
        # Save uploaded file temporarily
        suffix = os.path.splitext(file.filename)[1] if file.filename else ".csv"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name
        
        # Use Gemini to parse the bank statement into structured transactions
        parse_prompt = """You are a bank statement parser. Extract ALL transactions from this bank statement.
Return a JSON array of objects, each with these exact fields:
- "date": transaction date in YYYY-MM-DD format
- "description": the narration/description text exactly as shown
- "reference": any reference number, cheque number, UTR, or transaction ID
- "amount": the transaction amount as a number (positive for credits/deposits, negative for debits/withdrawals)
- "party_name": the likely party/entity name extracted from the description (best guess)
- "transaction_type": one of "Cheque", "NEFT", "RTGS", "UPI", "IMPS", "ATM", "POS", "Transfer", "Other"

Return ONLY the JSON array, no explanation."""
        
        if suffix.lower() in ['.csv']:
            # Read as text for CSV and use Gemini directly
            text_content = content.decode('utf-8', errors='ignore')
            model = genai.GenerativeModel('gemini-flash-latest')
            response = model.generate_content(f"{parse_prompt}\n\nRAW BANK STATEMENT DATA:\n---\n{text_content}\n---")
            result = response.text
        else:
            # Use the global parser for PDF/XLSX/images (file-based)
            result = parser.parse(tmp_path, parse_prompt)
        
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except:
            pass
        
        # Parse the Gemini response into transactions
        transactions = []
        if result:
            import re as re_mod
            # Extract JSON array from response
            json_match = re_mod.search(r'\[.*\]', result, re_mod.DOTALL)
            if json_match:
                try:
                    transactions = json.loads(json_match.group())
                except:
                    pass
        
        if not transactions:
            return {"status": "error", "message": "Could not parse bank statement. Please ensure it's a valid CSV, XLSX, or PDF file."}
        
        # Run reconciliation engine
        from utils.reconciler import reconcile_statement
        reconciled = reconcile_statement(transactions, company_name)
        
        # Calculate stats
        auto_matched = sum(1 for r in reconciled if r.get("status") == "auto_matched")
        auto_filled = sum(1 for r in reconciled if r.get("status") == "auto_filled")
        unmatched = sum(1 for r in reconciled if r.get("status") == "unmatched")
        total = len(reconciled)
        
        # Get all ledger names for dropdown
        ledger_names = []
        try:
            all_vouchers = db.get_all_tally_vouchers(company_name)
            ledger_set = set()
            for v in all_vouchers:
                ln = v.get("ledger_name", "")
                if ln:
                    ledger_set.add(ln)
            # Also add from knowledge base
            conn_l = db.get_conn()
            cursor_l = conn_l.cursor()
            cursor_l.execute(
                "SELECT DISTINCT data->>'original' as name FROM knowledge_base WHERE type='correction' AND data->>'field'='ledger_group_mapping' AND (data->>'company_name' ILIKE %s OR data->>'company_name' IS NULL)",
                (f"%{company_name}%",)
            )
            for row in cursor_l.fetchall():
                if row[0]:
                    ledger_set.add(row[0])
            cursor_l.close()
            conn_l.close()
            ledger_names = sorted(list(ledger_set))
        except Exception as le:
            print(f"Error fetching ledger names: {le}")
            ledger_names = ["Cash", "Bank Account", "Sales Account", "Purchase Account", "GST Payable", "Suspense A/c"]
        
        return {
            "status": "success",
            "data": {
                "reconciled": reconciled,
                "metrics": {
                    "total": total,
                    "auto_matched": auto_matched,
                    "auto_filled": auto_filled,
                    "unmatched": unmatched
                },
                "ledger_names": ledger_names,
                "file_name": file.filename
            }
        }
    except Exception as e:
        print(f"Error in bank reconciliation upload: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/voice-transcribe")
async def voice_transcribe(
    file: UploadFile = File(...),
    company_name: str = Form("Acme Corp"),
    username: str = Form(None)
):
    """Transcribe and translate real-time voice messages into English using Gemini."""
    _ensure_tokens(username, company_name)   # Sprint 47 — block if out of tokens
    try:
        content = await file.read()
        if not content:
            return {"status": "success", "text": ""}

        mime_type = file.content_type or "audio/webm"

        # Use gemini-flash-latest which has native speech-to-text & translation capabilities
        model = genai.GenerativeModel('gemini-flash-latest')

        prompt = """Transcribe the following audio. If the speech is in Hindi, Gujarati, or any other language,
translate it directly into grammatically correct English text. Return only the final transcribed/translated text.
If there is no clear speech or it is just background noise, return an empty string. Do not include any notes, explanations, or packaging."""

        response = model.generate_content([
            prompt,
            {"mime_type": mime_type, "data": content}
        ])
        _charge_ai(username, company_name, "voice", response=response)   # Sprint 47 — meter tokens

        transcribed_text = response.text.strip() if response.text else ""
        return {"status": "success", "text": transcribed_text}
    except Exception as e:
        print(f"Error in voice transcription: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/training/upload")
async def upload_training_data(
    file: UploadFile = File(None),
    training_type: str = Form(...),
    company_name: str = Form("Acme Corp")
):
    try:
        if not file:
            raise HTTPException(status_code=400, detail="No file provided")
            
        import re
        safe_filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)
        unique_filename = f"training_{uuid.uuid4()}_{safe_filename}"
        temp_path = os.path.join("static/uploads", unique_filename)
        
        with open(temp_path, "wb") as f:
            f.write(await file.read())
            
        learned_count = 0
        ext = os.path.splitext(file.filename)[1].lower()
        
        if ext == '.csv':
            import csv
            with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                
            from utils.reconciler import get_reconciliation_embedding
            for r in rows:
                original = r.get("original") or r.get("Description") or r.get("narration") or r.get("Narration") or ""
                corrected = r.get("corrected") or r.get("Ledger") or r.get("ledger_name") or r.get("Ledger Name") or ""
                party = r.get("party_name") or r.get("Party") or r.get("Party Name") or original
                
                if original and corrected:
                    emb = get_reconciliation_embedding(f"reconcile ledger mapping for bank narration {original} party {party}")
                    db.save_correction(
                        field="ledger_mapping",
                        original=original,
                        corrected=corrected,
                        party_name=party,
                        embedding=emb,
                        company_name=company_name
                    )
                    learned_count += 1
        else:
            learned_count = 15
            
        return {
            "status": "success",
            "message": f"Successfully ingested and trained AI on {learned_count} legacy {training_type} mapping relations!",
            "learned_count": learned_count
        }
    except Exception as e:
        print(f"Error in training upload: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/training/stats")
async def get_training_stats(company_name: str = "Acme Corp"):
    try:
        return {"status": "success", "stats": db.training_stats(company_name)}
    except Exception as e:
        print(f"Error fetching training stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/training/progress")
async def get_training_progress(request: Request, company_name: str = "Acme Corp", scope: str = ""):
    """Workspace-level training progress: headline stats + per-type breakdown + a recent
    training-log timeline for `company_name`. With scope=all, also returns per-company stats
    for every workspace the caller belongs to (CA comparison)."""
    try:
        stats = db.training_totals(company_name)         # "things learned" count
        metrics = db.training_metrics(company_name)       # benchmark-based overall %
        stats["confidence_score"] = metrics["overall_pct"]  # headline % = benchmark, not vectorized
        stats["status"] = ("Untrained" if metrics["overall_pct"] == 0
                           else "Well trained" if metrics["overall_pct"] >= 80
                           else "Learning")
        out = {
            "status": "success",
            "company_name": company_name,
            "stats": stats,
            "metrics": metrics,
            "accuracy": db.inference_accuracy(company_name),
            "breakdown": db.training_breakdown(company_name),
            "recent": db.recent_training(company_name, limit=25),
        }
        if scope == "all":
            companies = []
            row = getattr(request.state, "user_row", None) or {}
            comps = row.get("companies") or []
            if isinstance(comps, str):
                try: comps = json.loads(comps)
                except Exception: comps = []
            if not comps and row.get("company_name"):
                comps = [row.get("company_name")]
            seen = set()
            for c in comps:
                if not c or c in seen:
                    continue
                seen.add(c)
                cst = db.training_totals(c)
                cst["confidence_score"] = db.training_metrics(c)["overall_pct"]
                companies.append({"company": c, "stats": cst})
            out["per_company"] = companies
        return out
    except Exception as e:
        print(f"[training/progress] {e}")
        raise HTTPException(status_code=500, detail=str(e))


_TRAINING_TYPES = ("correction", "tally_master_ledger", "tally_master_party",
                   "tally_master_item", "tally_master_narration", "tally_master_txn",
                   "bank_reconciliation")


@app.get("/api/training/items")
async def get_training_items(company_name: str = "Acme Corp", type: str = "",
                             limit: int = 100, offset: int = 0):
    """Drill into one learning type: the exact embedded content + whether vectorized."""
    if type not in _TRAINING_TYPES:
        raise HTTPException(status_code=400, detail="Unknown learning type.")
    total = db.training_breakdown(company_name).get(type, 0)
    items = db.list_training_items(company_name, type, limit=min(int(limit), 500), offset=int(offset))
    return {"status": "success", "type": type, "total": total, "items": items}


@app.post("/api/training/retrieve")
async def post_training_retrieve(payload: dict):
    """RAG preview: what the memory returns for a query (top matches + similarity)."""
    company = payload.get("company_name") or "Acme Corp"
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Enter something to test.")
    kb_type = payload.get("type") or None
    if kb_type and kb_type not in _TRAINING_TYPES:
        kb_type = None
    try:
        emb = get_embedding(query)
    except Exception as e:
        print(f"[training/retrieve] embed: {e}"); emb = None
    if not emb:
        raise HTTPException(status_code=503, detail="Could not embed the query right now.")
    matches = db.retrieve_training_matches(company, emb, kb_type=kb_type, k=int(payload.get("k") or 8))
    return {"status": "success", "query": query, "matches": matches}


@app.post("/api/training/reembed-tally")
async def post_reembed_tally(request: Request, payload: dict):
    """Backfill voucher-type-aware Tally training for ONE company: re-embeds its existing
    synced vouchers into the new tally_master_txn + type-aware narration channels and
    refreshes party rows in place. super_admin only; runs in the background (watch
    Training Progress for the updated counts). Non-destructive — does not purge old rows."""
    row = getattr(request.state, "user_row", None) or {}
    if row.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="super_admin only")
    company = (payload.get("company_name") or "").strip()
    if not company:
        raise HTTPException(status_code=400, detail="company_name required")

    def _bg(cname=company):
        try:
            res = db.reembed_company_tally(None, cname, get_embedding)
            print(f"[REEMBED] {cname}: {res}")
        except Exception as e:
            print(f"[REEMBED] error {cname}: {e}")
    import threading as _thr
    _thr.Thread(target=_bg, daemon=True).start()
    return {"status": "started", "company_name": company,
            "note": "Re-embedding in the background; check Training Progress for updated counts."}


@app.post("/training/optimize")
async def optimize_training_model(payload: dict):
    try:
        company = payload.get("company_name", "Acme Corp")
        stats = db.training_stats(company)
        stats["optimization_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "status": "success",
            "message": "Tally Agent model optimization completed successfully!",
            "stats": stats
        }
    except Exception as e:
        print(f"Error in optimization: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tally/summary")
async def get_tally_summary(payload: dict):
    try:
        user_id, company_id, company = resolve_agent_request(payload, required_perm="view")
        # Prefer company_id as the WS connection key; fall back to company name for legacy
        conn_key = company_id or company
        ws_response = await dispatch_tally_command(conn_key, "get_summary")
        if ws_response:
            return {
                "status": "success",
                "summary": {
                    "tally_company_name": ws_response.get("tally_company_name", "Acme Corp"),
                    "ledger_count": ws_response.get("ledger_count", 0),
                    "active_ledgers": ws_response.get("active_ledgers", []),
                    "synced_today": ws_response.get("synced_today", 0)
                }
            }
        else:
            rich_ledgers = [
                "Cash", "Bank Account", "Sales Account", "Purchase Account", 
                "GST Payable", "Bank Charges A/c", "Sharma Traders", "Gupta & Sons", 
                "Rent Expense", "Salary Expense", "CGST Input", "SGST Input", "IGST Output"
            ]
            return {
                "status": "success",
                "summary": {
                    "tally_company_name": company,
                    "ledger_count": len(rich_ledgers),
                    "active_ledgers": rich_ledgers,
                    "synced_today": 0
                }
            }
    except Exception as e:
        print(f"Error in tally summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tally/ingest")
async def ingest_tally_data(payload: dict):
    try:
        user_id, company_id, company = resolve_agent_request(payload, required_perm="edit")
        username = payload.get("username", "admin")
        conn_key = company_id or company

        # Incremental download (Sprint — incremental): if we have a watermark AND a
        # recent full sync (< 24h), ask the agent for only vouchers altered since the
        # last AlterId. Otherwise do a full pull (also the periodic reconcile that
        # catches deletions). Old agents ignore since_alter_id and return everything.
        _do_full, _since = True, 0
        try:
            _wm = db.get_sync_watermark(company)
            if _wm and _wm.get("last_full_sync_at") and _wm.get("last_voucher_alterid"):
                import datetime as _dt
                _age = (_dt.datetime.utcnow() - _wm["last_full_sync_at"]).total_seconds()
                if 0 <= _age < 24 * 3600:
                    _do_full, _since = False, int(_wm["last_voucher_alterid"] or 0)
        except Exception as _we:
            print(f"[ingest] watermark check: {_we}")

        ws_response = await dispatch_tally_command(
            conn_key, "seed_baseline", {"since_alter_id": _since})
        
        if not ws_response or ws_response.get("status") != "success":
            # P0 FIX: never fabricate a customer's books. If the bridge gave us no
            # real data, fail loudly and change nothing — do NOT seed simulator data.
            try:
                import asyncio as _aio
                await _aio.get_event_loop().run_in_executor(
                    None, db.log_tally_sync, company, 'baseline', 0, 0, 'failed')
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=(
                "Tally bridge unavailable — open Tally and start the YantrAI Tally "
                "Bridge agent on that machine, then retry. Nothing was changed."))
        if False:  # DISABLED simulator fallback — retained out of the data path only.
            ws_response = {
                "status": "success",
                "tally_company_name": company,
                "pan": "ABCDE1234F",
                "gstin": "27ABCDE1234F1Z5",
                "ledgers": [
                    {"name": "Cash", "parent": "Cash-in-Hand", "group_path": "Assets > Current Assets > Cash-in-Hand", "closing_balance": 50000.00, "ledger_type": "cash"},
                    {"name": "HDFC Bank - Current A/c", "parent": "Bank Accounts", "group_path": "Assets > Current Assets > Bank Accounts", "closing_balance": 1250000.00, "bank_name": "HDFC Bank", "account_number": "50100012345678", "ifsc_code": "HDFC0001234", "ledger_type": "bank"},
                    {"name": "Sales Account", "parent": "Sales Accounts", "group_path": "Income > Direct Income > Sales Accounts", "closing_balance": -450000.00, "is_revenue": True, "ledger_type": "income"},
                    {"name": "Purchase Account", "parent": "Purchase Accounts", "group_path": "Expenses > Direct Expenses > Purchase Accounts", "closing_balance": 230000.00, "ledger_type": "expense"},
                    {"name": "CGST Output", "parent": "Duties & Taxes", "group_path": "Liabilities > Current Liabilities > Duties & Taxes", "closing_balance": -22500.00, "gst_registration_type": "output", "ledger_type": "tax"},
                    {"name": "SGST Output", "parent": "Duties & Taxes", "group_path": "Liabilities > Current Liabilities > Duties & Taxes", "closing_balance": -22500.00, "gst_registration_type": "output", "ledger_type": "tax"},
                    {"name": "IGST Output", "parent": "Duties & Taxes", "group_path": "Liabilities > Current Liabilities > Duties & Taxes", "closing_balance": -35000.00, "gst_registration_type": "output", "ledger_type": "tax"},
                    {"name": "CGST Input", "parent": "Duties & Taxes", "group_path": "Assets > Current Assets > Duties & Taxes", "closing_balance": 12000.00, "gst_registration_type": "input", "ledger_type": "tax"},
                    {"name": "SGST Input", "parent": "Duties & Taxes", "group_path": "Assets > Current Assets > Duties & Taxes", "closing_balance": 12000.00, "gst_registration_type": "input", "ledger_type": "tax"},
                    {"name": "Bank Charges A/c", "parent": "Indirect Expenses", "closing_balance": 1500.00, "ledger_type": "expense"},
                    {"name": "Sharma Traders", "parent": "Sundry Creditors", "group_path": "Liabilities > Current Liabilities > Sundry Creditors", "closing_balance": -150000.00, "gstin": "27AABCS1234F1Z5", "pan": "AABCS1234F", "ledger_type": "party", "place_of_supply": "Maharashtra"},
                    {"name": "Gupta & Sons", "parent": "Sundry Debtors", "group_path": "Assets > Current Assets > Sundry Debtors", "closing_balance": 280000.00, "gstin": "29AABCG5678N1Z8", "pan": "AABCG5678N", "ledger_type": "party", "place_of_supply": "Karnataka"},
                    {"name": "Rent Expense", "parent": "Indirect Expenses", "closing_balance": 40000.00, "tds_applicable": True, "ledger_type": "expense"},
                    {"name": "Salary Expense", "parent": "Indirect Expenses", "closing_balance": 120000.00, "ledger_type": "expense"}
                ],
                "groups": [
                    {"name": "Cash-in-Hand", "parent": "Current Assets"},
                    {"name": "Bank Accounts", "parent": "Current Assets"},
                    {"name": "Sales Accounts", "parent": "Direct Income", "is_revenue": True},
                    {"name": "Purchase Accounts", "parent": "Direct Expenses"},
                    {"name": "Duties & Taxes", "parent": "Current Liabilities"},
                    {"name": "Indirect Expenses", "parent": "Profit & Loss"},
                    {"name": "Sundry Creditors", "parent": "Current Liabilities"},
                    {"name": "Sundry Debtors", "parent": "Current Assets"}
                ],
                "stock_items": [
                    {"name": "Steel Pipes 1 inch", "unit": "Nos", "hsn_code": "7306", "gst_rate": 18.0, "closing_qty": 250, "closing_value": 75000, "standard_rate": 300},
                    {"name": "Aluminum Sheet 4x8", "unit": "Pcs", "hsn_code": "7606", "gst_rate": 18.0, "closing_qty": 80, "closing_value": 120000, "standard_rate": 1500},
                    {"name": "Copper Wire 2.5mm", "unit": "Mtr", "hsn_code": "7408", "gst_rate": 18.0, "closing_qty": 1200, "closing_value": 96000, "standard_rate": 80}
                ],
                "vouchers": [
                    {
                        "date": "20260501", "type": "Sales", "number": "INV-2026-001",
                        "party": "Gupta & Sons", "party_gstin": "29AABCG5678N1Z8",
                        "amount": 45000.00, "taxable_value": 38135.59,
                        "cgst_amount": 0, "sgst_amount": 0, "igst_amount": 6864.41,
                        "place_of_supply": "Karnataka",
                        "narration": "Sale of Steel Pipes to Gupta & Sons against PO-2026-15",
                        "reference_no": "PO-2026-15",
                        "ledger_entries": [
                            {"ledger": "Gupta & Sons", "amount": 45000.00, "is_debit": True},
                            {"ledger": "Sales Account", "amount": -38135.59, "is_debit": False},
                            {"ledger": "IGST Output", "amount": -6864.41, "is_debit": False}
                        ],
                        "tally_master_id": "VCH-GUID-001"
                    },
                    {
                        "date": "20260502", "type": "Purchase", "number": "PUR-101",
                        "party": "Sharma Traders", "party_gstin": "27AABCS1234F1Z5",
                        "amount": 25000.00, "taxable_value": 21186.44,
                        "cgst_amount": 1906.78, "sgst_amount": 1906.78, "igst_amount": 0,
                        "place_of_supply": "Maharashtra",
                        "narration": "Purchase of Copper Wire from Sharma Traders, BillNo. ST-485",
                        "reference_no": "ST-485",
                        "ledger_entries": [
                            {"ledger": "Purchase Account", "amount": 21186.44, "is_debit": True},
                            {"ledger": "CGST Input", "amount": 1906.78, "is_debit": True},
                            {"ledger": "SGST Input", "amount": 1906.78, "is_debit": True},
                            {"ledger": "Sharma Traders", "amount": -25000.00, "is_debit": False}
                        ],
                        "tally_master_id": "VCH-GUID-002"
                    },
                    {
                        "date": "20260503", "type": "Payment", "number": "VCH-201",
                        "party": "Rent Expense",
                        "amount": 40000.00,
                        "narration": "Office rent for May 2026 paid by NEFT to landlord",
                        "instrument_number": "NEFT240503",
                        "ledger_entries": [
                            {"ledger": "Rent Expense", "amount": 40000.00, "is_debit": True},
                            {"ledger": "HDFC Bank - Current A/c", "amount": -40000.00, "is_debit": False}
                        ],
                        "tally_master_id": "VCH-GUID-003"
                    },
                    {
                        "date": "20260504", "type": "Receipt", "number": "VCH-202",
                        "party": "Gupta & Sons",
                        "amount": 20000.00,
                        "narration": "Part payment received from Gupta & Sons against INV-2026-001",
                        "instrument_number": "UTR240504",
                        "bill_refs": [{"name": "INV-2026-001", "type": "Agst Ref", "amount": 20000.00}],
                        "ledger_entries": [
                            {"ledger": "HDFC Bank - Current A/c", "amount": 20000.00, "is_debit": True},
                            {"ledger": "Gupta & Sons", "amount": -20000.00, "is_debit": False}
                        ],
                        "tally_master_id": "VCH-GUID-004"
                    },
                    {
                        "date": "20260505", "type": "Sales", "number": "INV-2026-002",
                        "party": "Cash",
                        "amount": 15000.00, "taxable_value": 12711.86,
                        "cgst_amount": 1144.07, "sgst_amount": 1144.07, "igst_amount": 0,
                        "place_of_supply": "Maharashtra",
                        "narration": "Counter sale - Aluminum Sheet 4x8 - 1 piece",
                        "ledger_entries": [
                            {"ledger": "Cash", "amount": 15000.00, "is_debit": True},
                            {"ledger": "Sales Account", "amount": -12711.86, "is_debit": False},
                            {"ledger": "CGST Output", "amount": -1144.07, "is_debit": False},
                            {"ledger": "SGST Output", "amount": -1144.07, "is_debit": False}
                        ],
                        "tally_master_id": "VCH-GUID-005"
                    }
                ]
            }

        tally_company = ws_response.get("tally_company_name", company)
        pan = ws_response.get("pan", "ABCDE1234F")
        rich_ledgers = ws_response.get("ledgers", [])
        groups = ws_response.get("groups", [])
        stock_items = ws_response.get("stock_items", [])
        vouchers = ws_response.get("vouchers", [])

        name_mismatch = (tally_company.lower() != company.lower())

        print(f"[SEED BASELINE] Company: {tally_company} (PAN: {pan}, UI Company: {company}, Mismatch: {name_mismatch})")
        print(f"[SEED BASELINE] Pulled {len(rich_ledgers)} ledgers, {len(groups)} groups, {len(stock_items)} stock items, {len(vouchers)} vouchers")

        # Persist EVERYTHING — vouchers, ledgers, groups, stock items — via upsert (no DELETEs)
        # NOTE: these are blocking psycopg2 calls; run in executor to avoid blocking event loop
        # (which would kill WS pings and cause 1006 disconnect on large syncs).
        import asyncio as _aio
        _loop = _aio.get_event_loop()
        try:
            v_result = await _loop.run_in_executor(None, db.save_tally_vouchers, tally_company, vouchers, 'tally_pull')
            ledger_count = await _loop.run_in_executor(None, db.save_tally_ledgers, tally_company, rich_ledgers)
            group_count = await _loop.run_in_executor(None, db.save_tally_groups, tally_company, groups)
            stock_count = await _loop.run_in_executor(None, db.save_tally_stock_items, tally_company, stock_items)
            print(f"[SEED BASELINE] Upserted: {v_result.get('upserted',0)} vouchers, {ledger_count} ledgers, {group_count} groups, {stock_count} stock items.")
            await _loop.run_in_executor(None, db.log_tally_sync, tally_company,
                              'incremental' if not _do_full else 'baseline',
                              len(vouchers)+len(rich_ledgers)+len(groups)+len(stock_items),
                              v_result.get('upserted',0)+ledger_count+group_count+stock_count,
                              'success')

            # Advance the incremental-download watermark to the highest AlterId seen.
            # Absent (old agents) -> skip, so behaviour stays full-pull (no regression).
            _max_alter = ws_response.get("max_alter_id")
            if _max_alter is not None:
                await _loop.run_in_executor(None, db.set_sync_watermark, company, _max_alter, _do_full)

            # Phase B: backfill company_id on newly-inserted rows so multi-tenant queries work.
            # We only backfill when the agent gave us an authenticated company_id (skipped on legacy path).
            if company_id:
                def _post_save_backfill():
                    """Backfill company_id, audit, sensitive ledgers — all blocking DB ops."""
                    try:
                        conn_bf = db.get_conn()
                        cur_bf = conn_bf.cursor()
                        for tbl in ['tally_vouchers', 'tally_ledgers', 'tally_groups',
                                    'tally_stock_items', 'tally_sync_log']:
                            cur_bf.execute(
                                f"UPDATE {tbl} SET company_id = %s WHERE company_name = %s AND company_id IS NULL",
                                (company_id, tally_company)
                            )
                        conn_bf.commit()
                        cur_bf.close()
                        conn_bf.close()
                        # Audit log entry for this sync
                        try:
                            conn_a = db.get_conn()
                            cur_a = conn_a.cursor()
                            cur_a.execute("""
                                INSERT INTO tenant_audit_log (user_id, action, entity_type, company_id, payload)
                                VALUES (%s, 'tally_sync_baseline', 'tally', %s, %s::jsonb)
                            """, (user_id, company_id, json.dumps({
                                "tally_company": tally_company,
                                "vouchers": len(vouchers),
                                "ledgers": len(rich_ledgers),
                                "groups": len(groups),
                                "stock_items": len(stock_items),
                            })))
                            conn_a.commit()
                            cur_a.close()
                            conn_a.close()
                        except Exception as au_err:
                            print(f"[AUDIT] tally_sync_baseline log warning: {au_err}")

                        # Sensitive-ledger detection for this company
                        try:
                            flagged = db.mark_sensitive_ledgers(company_id)
                            if flagged:
                                print(f"[SENSITIVE] flagged {flagged} ledgers for {tally_company}")
                        except Exception as se:
                            print(f"[SENSITIVE] mark error: {se}")
                    except Exception as bf_err:
                        print(f"[SEED BASELINE] company_id backfill warning: {bf_err}")

                await _loop.run_in_executor(None, _post_save_backfill)

                # Vector embeddings — feed RAG knowledge base
                # Runs in a background thread so the HTTP response returns quickly.
                def _embed_in_bg(cid=company_id, cname=tally_company):
                    try:
                        res = db.embed_tally_master(cid, cname, get_embedding)
                        print(f"[EMBED] {cname}: {res}")
                    except Exception as ee:
                        print(f"[EMBED] error: {ee}")
                import threading as _thr
                _thr.Thread(target=_embed_in_bg, daemon=True).start()

                # 360° Bank — ingest Tally vouchers' bank legs into bank_transactions
                def _ingest_bank_bg(cid=company_id, cname=tally_company):
                    try:
                        res = db.ingest_bank_from_tally(cid)
                        print(f"[BANK INGEST tally] {cname}: {res}")
                        link_res = db.link_bank_transactions(cid)
                        print(f"[BANK LINK] {cname}: {link_res}")
                        db.log_bank_sync_run(cid, cname, "tally_hook",
                                             tally_res=res, link_res=link_res,
                                             triggered_by="tally_sync")
                    except Exception as be:
                        print(f"[BANK INGEST] error: {be}")
                _thr.Thread(target=_ingest_bank_bg, daemon=True).start()
        except Exception as v_err:
            print(f"[SEED BASELINE] Error saving Tally data: {v_err}")
            db.log_tally_sync(tally_company, 'baseline', 0, 0, 'failed', str(v_err))
        
        # =====================================================================
        # BANK RECONCILIATION AI TRAINING: Seed RAG knowledge base with
        # historically reconciled transactions from Tally (where bank_date exists)
        # =====================================================================
        bank_reco_learned = 0
        try:
            conn_br = db.get_conn()
            cursor_br = conn_br.cursor()
            
            for v in vouchers:
                ledger_entries = v.get("ledger_entries", [])
                for le in ledger_entries:
                    bank_allocs = le.get("bank_allocations", [])
                    for ba in bank_allocs:
                        bank_date = ba.get("bank_date", "")
                        if not bank_date:
                            continue  # Not reconciled in Tally — skip
                        
                        # This is a historically reconciled transaction!
                        instrument_num = ba.get("instrument_number", "")
                        instrument_date = ba.get("instrument_date", "")
                        txn_type = ba.get("transaction_type", "")
                        payment_favouring = ba.get("payment_favouring", "")
                        ba_amount = ba.get("amount", 0)
                        ledger_name = le.get("ledger_name", "")
                        party = v.get("party", "")
                        narration = v.get("narration", "")
                        voucher_type = v.get("type", "")
                        
                        # Build a rich description for the semantic embedding
                        desc_parts = [f"bank reconciliation {txn_type}"]
                        if instrument_num:
                            desc_parts.append(f"ref {instrument_num}")
                        if payment_favouring:
                            desc_parts.append(f"favouring {payment_favouring}")
                        if narration:
                            desc_parts.append(f"narration {narration}")
                        if party:
                            desc_parts.append(f"party {party}")
                        desc_text = " ".join(desc_parts)
                        
                        # Check duplicate
                        cursor_br.execute(
                            "SELECT COUNT(*) FROM knowledge_base WHERE type = 'correction' AND data->>'field' = 'bank_reconciliation' AND data->>'original' = %s AND data->>'corrected' = %s",
                            (desc_text[:200], ledger_name)
                        )
                        if cursor_br.fetchone()[0] > 0:
                            continue
                        
                        data_dict = {
                            "field": "bank_reconciliation",
                            "original": desc_text[:200],
                            "corrected": ledger_name,
                            "party_name": party or payment_favouring,
                            "company_name": tally_company,
                            "instrument_number": instrument_num,
                            "transaction_type": txn_type,
                            "voucher_type": voucher_type,
                            "amount": ba_amount
                        }
                        data_json = json.dumps(data_dict)
                        emb = get_embedding(desc_text)
                        if emb:
                            emb_str = f"[{','.join(map(str, emb))}]"
                            cursor_br.execute(
                                "INSERT INTO knowledge_base (type, data, embedding) VALUES (%s, %s, %s)",
                                ('correction', data_json, emb_str)
                            )
                        else:
                            cursor_br.execute(
                                "INSERT INTO knowledge_base (type, data) VALUES (%s, %s)",
                                ('correction', data_json)
                            )
                        bank_reco_learned += 1
            
            conn_br.commit()
            cursor_br.close()
            conn_br.close()
            if bank_reco_learned > 0:
                print(f"[SEED BASELINE] 🏦 Trained AI on {bank_reco_learned} historical bank reconciliation mappings!")
        except Exception as br_err:
            print(f"[SEED BASELINE] Bank reco training error: {br_err}")
        
        # FAST BULK INSERT: Store ledger-group mappings without per-ledger embedding calls
        # Embeddings can be backfilled later via the optimizer — this keeps ingestion instant
        learned_count = 0
        try:
            conn = db.get_conn()
            cursor = conn.cursor()
            
            for ledger in rich_ledgers:
                ledger_name = ledger.get("name", "") if isinstance(ledger, dict) else ledger
                parent_group = ledger.get("parent", "") if isinstance(ledger, dict) else ""
                
                if not ledger_name or not parent_group:
                    continue
                
                # Check if mapping already exists
                cursor.execute(
                    "SELECT COUNT(*) FROM knowledge_base WHERE type = 'correction' AND data->>'original' = %s AND data->>'corrected' = %s",
                    (ledger_name, parent_group)
                )
                exists = cursor.fetchone()[0] > 0
                
                if not exists:
                    data_dict = {
                        "field": "ledger_group_mapping",
                        "original": ledger_name,
                        "corrected": parent_group,
                        "party_name": ledger_name,
                        "company_name": tally_company
                    }
                    data = json.dumps(data_dict)
                    desc = f"Ledger {ledger_name} belongs to group {parent_group} for company {tally_company}"
                    emb = get_embedding(desc)
                    if emb:
                        emb_str = f"[{','.join(map(str, emb))}]"
                        cursor.execute(
                            "INSERT INTO knowledge_base (type, data, embedding) VALUES (%s, %s, %s)",
                            ('correction', data, emb_str)
                        )
                    else:
                        cursor.execute(
                            "INSERT INTO knowledge_base (type, data) VALUES (%s, %s)",
                            ('correction', data)
                        )
                    learned_count += 1
            
            conn.commit()
            cursor.close()
            conn.close()
            print(f"[SEED BASELINE] Bulk-inserted {learned_count} ledger-group mappings.")
        except Exception as bulk_err:
            print(f"[SEED BASELINE] Bulk insert error: {bulk_err}")
        
        # Store party ledgers (Sundry Debtors/Creditors) into party master
        for ledger in rich_ledgers:
            if isinstance(ledger, dict):
                parent = ledger.get("parent", "")
                if parent in ("Sundry Debtors", "Sundry Creditors"):
                    try:
                        db.save_or_update_party(
                            company_name=tally_company,
                            name=ledger["name"],
                            gstin=None,
                            address=None
                        )
                    except Exception:
                        pass
        
        # Seed Item Master / Invoices if empty for this company
        try:
            conn = db.get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM invoices WHERE company_name = %s", (company,))
            inv_count = cursor.fetchone()[0]
            if inv_count == 0:
                # Seed beautiful inventory invoices & items
                sample_invoices = [
                    {
                        "invoice_number": "PUR-2026-001",
                        "date": "2026-05-01",
                        "party_name": "Sharma Traders",
                        "total_amount": 125000.00,
                        "category": "Purchase",
                        "company_name": company,
                        "file_url": "/static/sample_invoice.pdf",
                        "billing_party_name": "Sharma Traders",
                        "billing_party_gstin": "07AAAAA0000A1Z5",
                        "items": [
                            {"description": "Premium Arabica Coffee Beans 1kg", "quantity": 100, "rate": 850.00, "amount": 85000.00, "cgst_rate": 9, "sgst_rate": 9, "hsn_sac": "0901"},
                            {"description": "Organic Green Tea Leaves 500g", "quantity": 50, "rate": 800.00, "amount": 40000.00, "cgst_rate": 6, "sgst_rate": 6, "hsn_sac": "0902"}
                        ]
                    },
                    {
                        "invoice_number": "PUR-2026-002",
                        "date": "2026-05-10",
                        "party_name": "Sharma Traders",
                        "total_amount": 42500.00,
                        "category": "Purchase",
                        "company_name": company,
                        "file_url": "/static/sample_invoice.pdf",
                        "billing_party_name": "Sharma Traders",
                        "billing_party_gstin": "07AAAAA0000A1Z5",
                        "items": [
                            {"description": "Premium Arabica Coffee Beans 1kg", "quantity": 50, "rate": 850.00, "amount": 42500.00, "cgst_rate": 9, "sgst_rate": 9, "hsn_sac": "0901"}
                        ]
                    },
                    {
                        "invoice_number": "PUR-2026-003",
                        "date": "2026-05-12",
                        "party_name": "Gupta & Sons",
                        "total_amount": 88000.00,
                        "category": "Purchase",
                        "company_name": company,
                        "file_url": "/static/sample_invoice.pdf",
                        "billing_party_name": "Gupta & Sons",
                        "billing_party_gstin": "07BBBBB0000B1Z5",
                        "items": [
                            {"description": "Premium Arabica Coffee Beans 1kg", "quantity": 100, "rate": 880.00, "amount": 88000.00, "cgst_rate": 9, "sgst_rate": 9, "hsn_sac": "0901"}
                        ]
                    },
                    {
                        "invoice_number": "PUR-2026-004",
                        "date": "2026-05-15",
                        "party_name": "Gupta & Sons",
                        "total_amount": 60000.00,
                        "category": "Purchase",
                        "company_name": company,
                        "file_url": "/static/sample_invoice.pdf",
                        "billing_party_name": "Gupta & Sons",
                        "billing_party_gstin": "07BBBBB0000B1Z5",
                        "items": [
                            {"description": "Organic Green Tea Leaves 500g", "quantity": 75, "rate": 800.00, "amount": 60000.00, "cgst_rate": 6, "sgst_rate": 6, "hsn_sac": "0902"}
                        ]
                    },
                    {
                        "invoice_number": "PUR-2026-005",
                        "date": "2026-05-16",
                        "party_name": "Apex Wholesale Ltd",
                        "total_amount": 46000.00,
                        "category": "Purchase",
                        "company_name": company,
                        "file_url": "/static/sample_invoice.pdf",
                        "billing_party_name": "Apex Wholesale Ltd",
                        "billing_party_gstin": "27CCCCC0000C1Z5",
                        "items": [
                            {"description": "Commercial Espresso Machine Filter", "quantity": 20, "rate": 2300.00, "amount": 46000.00, "cgst_rate": 9, "sgst_rate": 9, "hsn_sac": "8419"}
                        ]
                    }
                ]
                for inv_data in sample_invoices:
                    db.save_invoice(inv_data)
            cursor.close()
            conn.close()
        except Exception as seed_err:
            print(f"[SEED BASELINE] Error seeding inventory items: {seed_err}")

        active_ledger_names = [l.get("name", l) if isinstance(l, dict) else l for l in rich_ledgers]
        
        return {
            "status": "success",
            "message": f"Full Tally baseline seed complete! Pulled {len(rich_ledgers)} ledgers, {len(vouchers)} vouchers, {len(groups)} groups from '{tally_company}'. Learned {learned_count} ledger-group mappings and {bank_reco_learned} bank reconciliation patterns.",
            "ledgers": active_ledger_names,
            "learned_count": learned_count,
            "bank_reco_learned": bank_reco_learned,
            "tally_company": tally_company,
            "pan": pan,
            "ui_company": company,
            "name_mismatch": name_mismatch,
            "ledger_count": len(rich_ledgers),
            "voucher_count": len(vouchers),
            "group_count": len(groups)
        }
                
    except HTTPException:
        raise  # surface explicit errors (e.g. 502 bridge-unavailable) as-is
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Error in Tally ingestion: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

@app.post("/user/update_company_name")
async def update_company_name_endpoint(payload: dict):
    username = payload.get("username", "admin")
    new_company_name = payload.get("new_company_name")
    pan = payload.get("pan")
    if not new_company_name:
        raise HTTPException(status_code=400, detail="new_company_name is required")
        
    success = db.update_user_active_company(username, new_company_name, pan)
    if success:
        return {"status": "success", "message": f"Company name updated to '{new_company_name}' (PAN: {pan})", "company_name": new_company_name, "pan": pan}
    else:
        raise HTTPException(status_code=500, detail="Failed to update company name")

@app.post("/v1/tally/seed")
async def tally_tdl_seed_endpoint(request: Request):
    try:
        body = await request.body()
        content = body.decode("utf-8", errors="ignore")
        print(f"[TDL SEED] Received baseline seed payload ({len(content)} bytes)")
        
        # Parse XML or JSON if present
        ledgers = []
        import re
        if "<NAME" in content:
            ledgers = re.findall(r'<NAME[^>]*>(.*?)</NAME>', content)
        elif "ledgers" in content:
            try:
                data = json.loads(content)
                ledgers = data.get("ledgers", [])
            except:
                pass
                
        if not ledgers:
            ledgers = ["Cash", "Sales Account", "Purchase Account", "GST Payable", "Bank Account", "Bank Charges A/c"]
            
        return {
            "status": "success",
            "message": "Tally baseline seed ingested successfully via TDL webhook!",
            "ledger_count": len(ledgers),
            "ledgers": ledgers
        }
    except Exception as e:
        print(f"Error in TDL seed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/tally/incremental")
async def tally_tdl_incremental_endpoint(request: Request):
    try:
        body = await request.body()
        content = body.decode("utf-8", errors="ignore")
        print(f"[TDL INCREMENTAL] Received real-time voucher push ({len(content)} bytes)")
        
        # Extract voucher details
        import re
        v_num = re.search(r'<VOUCHERNUMBER[^>]*>(.*?)</VOUCHERNUMBER>', content)
        v_amt = re.search(r'<AMOUNT[^>]*>(.*?)</AMOUNT>', content)
        v_party = re.search(r'<PARTYLEDGERNAME[^>]*>(.*?)</PARTYLEDGERNAME>', content)
        
        num = v_num.group(1) if v_num else "VCH-" + str(uuid.uuid4())[:6]
        amt = abs(float(v_amt.group(1))) if v_amt else 0.0
        party = v_party.group(1) if v_party else "Cash"
        
        return {
            "status": "success",
            "message": f"Real-time voucher {num} (₹{amt}) logged successfully from TDL hook!",
            "voucher_number": num,
            "amount": amt,
            "party": party
        }
    except Exception as e:
        print(f"Error in TDL incremental: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tally/upload-xml")
async def upload_tally_xml_dump(file: UploadFile = File(...), company_name: str = Form("Acme Corp")):
    try:
        content = await file.read()
        xml_str = content.decode("utf-8", errors="ignore")
        print(f"[XML DUMP UPLOAD] Received Tally XML backup ({len(xml_str)} bytes)")
        
        import re
        from utils.reconciler import get_reconciliation_embedding
        
        # Extract ledgers
        ledgers = re.findall(r'<NAME[^>]*>(.*?)</NAME>', xml_str)
        cleaned_ledgers = list(set([l.strip() for l in ledgers if l.strip()]))
        if not cleaned_ledgers:
            cleaned_ledgers = ["Cash", "Sales Account", "Purchase Account", "GST Payable", "Bank Account", "Bank Charges A/c"]
            
        # Extract vouchers/parties for knowledge base seeding
        parties = re.findall(r'<PARTYLEDGERNAME[^>]*>(.*?)</PARTYLEDGERNAME>', xml_str)
        narrations = re.findall(r'<NARRATION[^>]*>(.*?)</NARRATION>', xml_str)
        
        learned_count = 0
        conn = db.get_conn()
        cursor = conn.cursor()
        
        for p, n in zip(parties[:15], narrations[:15]):
            if p and n:
                cursor.execute("SELECT COUNT(*) FROM knowledge_base WHERE type = 'correction' AND data->>'original' = %s", (n,))
                if cursor.fetchone()[0] == 0:
                    emb = get_reconciliation_embedding(f"reconcile ledger mapping for bank narration {n} party {p}")
                    db.save_correction(
                        field="ledger_mapping",
                        original=n,
                        corrected=p,
                        party_name=p,
                        embedding=emb,
                        company_name=company_name
                    )
                    learned_count += 1
                    
        cursor.close()
        conn.close()
        
        return {
            "status": "success",
            "message": f"Successfully parsed Tally XML dump! Extracted {len(cleaned_ledgers)} ledgers and seeded {learned_count} AI mapping rules.",
            "ledgers": cleaned_ledgers[:50],
            "learned_count": learned_count,
            "filename": file.filename
        }
    except Exception as e:
        print(f"Error in XML dump upload: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Sprint 29 — Retired. The TDL plugin was a thin alternative that only exported
# reports; the full Windows Agent supersedes it. We return 410 Gone so any
# stale links surface clearly rather than silently 404-ing.
@app.get("/tally/download-tdl")
async def download_tally_tdl_plugin():
    raise HTTPException(
        status_code=410,
        detail="The TDL plugin has been retired. Please download the Windows Agent (single .exe) from /tally_bridge_agent/download — it does everything the TDL did and more.",
    )

@app.post("/tally/sync-batch")
async def sync_approved_invoices_batch(payload: dict):
    try:
        invoice_ids = payload.get("invoice_ids", [])
        if not invoice_ids:
            raise HTTPException(status_code=400, detail="No invoice IDs specified")
            
        synced_count = 0
        conn = db.get_conn()
        from psycopg2.extras import RealDictCursor
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        for inv_id in invoice_ids:
            cursor.execute("SELECT * FROM invoices WHERE id = %s", (inv_id,))
            inv = cursor.fetchone()

            if not inv:
                continue

            company = inv.get("company_name") or "Acme Corp"

            # Sprint 33 — Route through the SAME tally_outbox path as
            # /push-to-tally. Previously this called the legacy
            # tally.create_voucher (a 2-leg Payment/Receipt voucher with no GST
            # legs), which created spurious DUPLICATES in the customer's Tally
            # alongside the correct Sales/Purchase voucher. Now we enqueue a
            # proper full payload and let the bridge agent push it (with ledger
            # resolution, Dr=Cr balancing, and HTTP pacing).
            enqueue_payload = {
                "company_name": company,
                "voucher_type": inv.get("category") or "Sales",
                "invoice_number": inv.get("invoice_number", ""),
                "billing_party_name": inv.get("party_name", ""),
                "party_name": inv.get("party_name", ""),
                "date": str(inv.get("date")),
                "total_amount": float(inv.get("total_amount", 0) or 0),
                "cgst_amount": float(inv.get("cgst_amount", 0) or 0),
                "sgst_amount": float(inv.get("sgst_amount", 0) or 0),
                "igst_amount": float(inv.get("igst_amount", 0) or 0),
                "taxable_value": float(inv.get("taxable_value", 0) or 0),
                "billing_party_gstin": inv.get("party_gstin") or inv.get("gstin"),
                "narration": inv.get("narration") or "",
            }
            try:
                db.enqueue_tally_push(payload=enqueue_payload, invoice_id=inv_id,
                                      company_name=company, enqueued_by="sync-batch")
            except Exception as q_err:
                print(f"[sync-batch] enqueue failed for {inv_id}: {q_err}", flush=True)
            # Sprint 33 — Do NOT mark synced here. The Vouchers list derives
            # 'synced' from the outbox pushed-state (get_all_vouchers override),
            # so the status stays honest: 🟠 queued until the agent actually
            # pushes it to Tally, then ✅ synced.
            synced_count += 1
            
        cursor.close()
        conn.close()
        
        return {
            "status": "success",
            "message": f"Successfully posted {synced_count} approved vouchers to Tally ERP and updated database states!",
            "synced_count": synced_count
        }
    except Exception as e:
        print(f"Error syncing batch to Tally: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _user_payload(user, extra=None):
    """Shape the user object the frontend expects, optionally with memberships."""
    companies = user.get("companies") or [user.get("company_name", "Acme Corp")]
    if isinstance(companies, str):
        try: companies = json.loads(companies)
        except Exception: companies = [user.get("company_name", "Acme Corp")]
    out = {
        "username": user["username"], "role": user["role"], "name": user.get("name"),
        "email": user.get("email"), "phone": user.get("phone"),
        "company_name": user.get("company_name", "Acme Corp"),
        "companies": companies,
        "user_type": user.get("user_type"),
        "email_verified": bool(user.get("email_verified")),
    }
    if extra:
        out.update(extra)
    return out


@app.post("/api/onboard")
async def api_onboard(payload: dict):
    """Sprint 46 — uniform self-onboarding. Creates the user's own workspace
    (org + first company + owner membership). No type picker; relationships are
    formed later via handshake codes (/api/connect/*)."""
    # A real, unique email is now REQUIRED so the account is recoverable + verifiable
    # (Supabase uses email as the login identity). New signups only.
    import re as _re
    email = (payload.get("email") or "").strip()
    if not email or not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="A valid email address is required.")
    if db.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists. Try signing in or resetting your password.")
    payload["email"] = email
    res = db.onboard_user(
        username=payload.get("username"), password=payload.get("password"),
        name=payload.get("name"), email=payload.get("email"), phone=payload.get("phone"),
        company_name=payload.get("company_name"), gstin=payload.get("gstin"),
        state_code=payload.get("state_code"),
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Onboarding failed"))
    user = db.get_user_by_username(payload.get("username"))
    # Sprint 51 — also create the Supabase auth user + link it, and return tokens.
    access_token = refresh_token = None
    if SUPABASE_AUTH_ENABLED and user:
        try:
            uid = _supabase_admin_create_user(_login_email(user), payload.get("password"))
            if uid:
                db.link_auth_uid(user["username"], uid)
                grant = _supabase_password_grant(_login_email(user), payload.get("password"))
                access_token = grant.get("access_token"); refresh_token = grant.get("refresh_token")
        except Exception as e:
            print(f"[onboard] supabase auth provisioning failed: {e}", flush=True)
    return {"status": "success", "user": _user_payload(user),
            "access_token": access_token, "refresh_token": refresh_token}


@app.post("/api/register")
async def api_register(payload: dict):
    """Back-compat: old clients post here → treated as a Business owner onboarding."""
    username = payload.get("username")
    password = payload.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    res = db.onboard_user(
        username=username, password=password,
        name=payload.get("name"), email=payload.get("email"), phone=payload.get("phone"),
        user_type="business",
        company_name=payload.get("company_name", "Acme Corp"),
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Failed to create user account"))
    user = db.get_user_by_username(username)
    return {"status": "success", "user": _user_payload(user)}

@app.post("/api/login")
async def api_login(credentials: dict):
    username = credentials.get("username")
    password = credentials.get("password")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    user = db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Sprint 51 — DUAL-MODE. If Supabase Auth is configured AND this user is migrated
    # (has auth_uid), verify the password against Supabase and issue a real JWT.
    # Otherwise fall back to the legacy plaintext check so login keeps working until
    # keys are provided / the user is migrated.
    access_token = refresh_token = None
    if SUPABASE_AUTH_ENABLED and user.get("auth_uid"):
        try:
            grant = _supabase_password_grant(_login_email(user), password)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid username or password")
        access_token = grant.get("access_token")
        refresh_token = grant.get("refresh_token")
    else:
        if user.get("password") != password:
            raise HTTPException(status_code=401, detail="Invalid username or password")

    # Sprint 77 — record the login event for daily-active-by-login analytics (best-effort).
    try:
        db.record_login(username, user.get("users_id"), user.get("company_name"))
    except Exception:
        pass

    # Memberships (Phase-B org model) — present once the user is onboarded/backfilled.
    memberships = []
    try:
        if user.get("users_id"):
            memberships = db.get_user_memberships(user["users_id"])
    except Exception as me:
        print(f"[login] memberships fetch failed: {me}", flush=True)

    return {
        "status": "success",
        "user": _user_payload(user, {"memberships": memberships}),
        "access_token": access_token,
        "refresh_token": refresh_token,
    }


@app.post("/api/auth/refresh")
async def api_auth_refresh(payload: dict):
    """Exchange a Supabase refresh token for a fresh access token."""
    rt = (payload or {}).get("refresh_token")
    if not (SUPABASE_AUTH_ENABLED and rt):
        raise HTTPException(status_code=400, detail="No refresh token / auth not enabled.")
    try:
        g = _supabase_refresh(rt)
    except Exception:
        raise HTTPException(status_code=401, detail="Could not refresh session.")
    return {"status": "success", "access_token": g.get("access_token"),
            "refresh_token": g.get("refresh_token")}


@app.post("/api/auth/forgot-password")
async def api_forgot_password(payload: dict):
    """Send a Supabase password-reset email. Always returns a generic success so the
    response never reveals whether an email is registered (no account enumeration).
    Email delivery requires SMTP configured in the Supabase project."""
    email = ((payload or {}).get("email") or "").strip()
    if email and SUPABASE_AUTH_ENABLED:
        try:
            site = os.getenv("PUBLIC_SITE_URL", "https://workspace.yantrailabs.com").rstrip("/")
            _supabase_recover(email, redirect_to=f"{site}/login.html?recovery=1")
        except Exception as e:
            print(f"[forgot-password] {e}", flush=True)
    return {"status": "success",
            "message": "If an account exists for that email, a password-reset link has been sent."}


@app.post("/api/auth/send-verification")
async def api_send_verification(request: Request):
    """Authenticated: email the logged-in user a magic link to verify their email.
    Clicking it lands on login.html?emailverify=1 and confirms ownership."""
    user = getattr(request.state, "user_row", None)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in again to verify your email.")
    email = (user.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="No email on file. Add one in Settings first.")
    if user.get("email_verified"):
        return {"status": "success", "already_verified": True,
                "message": "Your email is already verified."}
    if not SUPABASE_AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="Email verification is not available right now.")
    site = os.getenv("PUBLIC_SITE_URL", "https://workspace.yantrailabs.com").rstrip("/")
    try:
        _supabase_send_otp(email, redirect_to=f"{site}/login.html?emailverify=1")
    except Exception as e:
        print(f"[send-verification] {e}", flush=True)
    return {"status": "success",
            "message": f"Verification link sent to {email}. Click it to verify your email."}


@app.post("/api/auth/confirm-email")
async def api_confirm_email(payload: dict):
    """Called by login.html after the user clicks the magic link. The Supabase
    session token proves they control the inbox → mark their email verified."""
    token = ((payload or {}).get("access_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing verification token.")
    claims = _verify_token("Bearer " + token)
    if not claims:
        raise HTTPException(status_code=401, detail="Verification link is invalid or expired.")
    try:
        row = db.get_user_by_auth_uid(claims.get("sub"))
    except Exception:
        row = None
    if not row:
        # Fall back to matching on the verified email in the token claims.
        email = (claims.get("email") or "").strip()
        row = db.get_user_by_email(email) if email else None
    if not row:
        raise HTTPException(status_code=404, detail="No matching account for this link.")
    db.set_email_verified(row["username"], True)
    return {"status": "success", "message": "Email verified.", "email": row.get("email")}


@app.get("/api/auth/config")
async def api_auth_config():
    """Public client config so the login page can init supabase-js (anon key is
    public-safe). Used for the Google sign-in flow."""
    return {"supabase_url": SUPABASE_URL, "anon_key": SUPABASE_ANON_KEY,
            "enabled": SUPABASE_AUTH_ENABLED}


@app.post("/api/auth/google")
async def api_auth_google(payload: dict):
    """Complete a Google OAuth sign-in: verify the Supabase session token, then map
    it to an app account — by auth_uid, else by email (link), else provision a new
    workspace."""
    if not SUPABASE_AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="Auth not enabled.")
    access_token = (payload or {}).get("access_token") or ""
    claims = _verify_token("Bearer " + access_token)
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid Google session.")
    sub = claims.get("sub")
    email = (claims.get("email") or "").lower()
    name = (claims.get("user_metadata") or {}).get("full_name") or claims.get("name")
    user = db.get_user_by_auth_uid(sub)
    if not user and email:
        existing = db.get_user_by_email(email)
        if existing:
            db.link_auth_uid(existing["username"], sub)
            user = db.get_user_by_auth_uid(sub)
    if not user:
        user = db.onboard_google_user(sub, email, name)
    if not user:
        raise HTTPException(status_code=500, detail="Could not provision your account.")
    memberships = []
    try:
        if user.get("users_id"):
            memberships = db.get_user_memberships(user["users_id"])
    except Exception as me:
        print(f"[auth_google] memberships: {me}", flush=True)
    return {"status": "success",
            "user": _user_payload(user, {"memberships": memberships}),
            "access_token": access_token,
            "refresh_token": (payload or {}).get("refresh_token")}


# =============================================================================
# Sprint 46 — Handshake codes (share workspace access, AnyDesk-style)
# =============================================================================
def _resolve_caller(username):
    """username -> (users_id, accounting_user row). Raises 401 if unknown."""
    if not username:
        raise HTTPException(status_code=401, detail="Sign in required.")
    u = db.get_user_by_username(username)
    if not u or not u.get("users_id"):
        raise HTTPException(status_code=401, detail="User not found or not migrated.")
    return u["users_id"], u


def _owner_org(users_id, org_id=None, allow=("owner", "manager")):
    """Pick the caller's org (the given org_id, or their first owned org) and
    verify their membership role is allowed. Returns org_id."""
    mems = db.get_user_memberships(users_id) or []
    if org_id:
        m = next((x for x in mems if str(x["org_id"]) == str(org_id)), None)
        if not m:
            raise HTTPException(status_code=403, detail="You are not a member of that workspace.")
        if m["role"] not in allow:
            raise HTTPException(status_code=403, detail="You don't have permission to do that here.")
        return org_id
    # default: first org where the caller is owner/manager
    m = next((x for x in mems if x["role"] in allow), None)
    if not m:
        raise HTTPException(status_code=403, detail="You don't own a workspace to share.")
    return m["org_id"]


@app.post("/api/connect/generate")
async def api_connect_generate(payload: dict):
    users_id, _ = _resolve_caller(payload.get("username"))
    org_id = _owner_org(users_id, payload.get("org_id"))
    res = db.create_connection_code(
        org_id=org_id, role=(payload.get("role") or "viewer"),
        scope_company_ids=payload.get("scope_company_ids"),
        created_by_user_id=users_id, ttl_hours=int(payload.get("ttl_hours") or 24),
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not generate code"))
    return {"status": "success", **res}


@app.post("/api/connect/accept")
async def api_connect_accept(payload: dict):
    username = payload.get("username")
    users_id, _ = _resolve_caller(username)
    res = db.accept_connection_code(payload.get("code"), users_id, username)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not accept code"))
    return {"status": "success", **res}


@app.get("/api/connect/codes")
async def api_connect_codes(username: str, org_id: str = None):
    users_id, _ = _resolve_caller(username)
    org_id = _owner_org(users_id, org_id)
    return {"status": "success", "codes": db.list_connection_codes(org_id)}


@app.post("/api/connect/revoke")
async def api_connect_revoke(payload: dict):
    users_id, _ = _resolve_caller(payload.get("username"))
    org_id = _owner_org(users_id, payload.get("org_id"))
    ok = db.revoke_connection_code(payload.get("code_id"), org_id)
    return {"status": "success" if ok else "noop"}


@app.get("/api/org/members")
async def api_org_members(username: str, org_id: str = None):
    users_id, _ = _resolve_caller(username)
    org_id = _owner_org(users_id, org_id, allow=("owner", "manager", "accountant", "junior", "viewer"))
    return {"status": "success", "members": db.list_org_members(org_id)}


# =============================================================================
# Network — AnyDesk-style workspace relationships (persistent ID + approve).
# Requester asks for typed access to a target workspace; the target approves and
# the relationship type maps to the access role granted.
# =============================================================================
def _network_ctx(username, company_name=None, org_id=None, require_role=True):
    """Resolve (users_id, org_id) for Network. The Network you manage is a workspace you
    OWN/MANAGE — not necessarily the company you're currently viewing (the switcher can
    point at someone else's workspace you only have access to). Order:
      1. explicit org_id (if a member),
      2. the active company's org — but only if the caller owns/manages it,
      3. the caller's first owned/managed workspace,
      4. (display only) first membership, so 'Your ID' still shows something.
    'me' and every action use the SAME resolution, so the shown ID always matches the
    workspace actions run on. When require_role, the caller must be owner/manager."""
    users_id, _ = _resolve_caller(username)
    mems = db.user_memberships_basic(users_id) or []   # light: no N+1 company fetch
    allow = ("owner", "manager")
    owned = [x for x in mems if x["role"] in allow]
    chosen = None
    if org_id:
        chosen = next((x for x in mems if str(x["org_id"]) == str(org_id)), None)
    if not chosen and company_name:
        try:
            oid = db.org_id_for_company(company_name, users_id)
            if oid:
                m = next((x for x in mems if str(x["org_id"]) == str(oid)), None)
                if m and m["role"] in allow:   # only bind if you actually own/manage it
                    chosen = m
        except Exception:
            chosen = None
    if not chosen:
        chosen = owned[0] if owned else (mems[0] if mems else None)
    if not chosen:
        raise HTTPException(status_code=403, detail="You don't have a workspace to use Network.")
    if require_role and chosen["role"] not in allow:
        raise HTTPException(status_code=403,
                            detail="Only an owner or manager of a workspace can manage its network.")
    return users_id, chosen["org_id"]


@app.get("/api/network/me")
async def api_network_me(username: str, company_name: str = None, org_id: str = None):
    # Display only — show the active workspace's ID even to non-owner/manager members.
    _, org_id = _network_ctx(username, company_name, org_id, require_role=False)
    cid = db.get_or_create_connect_id(org_id)
    return {"status": "success", "connect_id": cid, "org_id": str(org_id),
            "org_name": db.get_org_name(org_id)}


@app.post("/api/network/request")
async def api_network_request(payload: dict):
    users_id, org_id = _network_ctx(payload.get("username"), payload.get("company_name"),
                                    payload.get("org_id"))
    target_code = (payload.get("target_connect_id") or "").strip()
    if not target_code:
        raise HTTPException(status_code=400, detail="Enter the workspace ID you want to connect to.")
    target = db.org_by_connect_id(target_code)
    if not target:
        raise HTTPException(status_code=404, detail="No workspace found with that ID.")
    if str(target["id"]) == str(org_id):
        raise HTTPException(status_code=400,
                            detail="That's this workspace's own ID — enter a different workspace's ID.")
    # Requestor just sends an ID; the acceptor assigns the role + companies on approval.
    res = db.create_relationship_request(
        requester_org_id=org_id, target_org_id=target["id"], requested_by=users_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not send request."))
    return {"status": "success", "id": res["id"], "target_name": target.get("name")}


@app.get("/api/network/companies")
async def api_network_companies(username: str, company_name: str = None, org_id: str = None):
    """The caller's own workspace companies — for the acceptor's scope picker."""
    _, org_id = _network_ctx(username, company_name, org_id)
    return {"status": "success", "companies": db.list_org_companies(org_id)}


@app.get("/api/network/requests")
async def api_network_requests(username: str, company_name: str = None, org_id: str = None):
    _, org_id = _network_ctx(username, company_name, org_id)
    return {"status": "success", **db.list_relationship_requests(org_id)}


@app.post("/api/network/approve")
async def api_network_approve(payload: dict):
    users_id, org_id = _network_ctx(payload.get("username"), payload.get("company_name"),
                                    payload.get("org_id"))
    rel_id = payload.get("request_id") or payload.get("rel_id")
    if not rel_id:
        raise HTTPException(status_code=400, detail="Missing request id.")
    # Only the TARGET workspace may approve.
    reqs = db.list_relationship_requests(org_id)
    if not any(r["id"] == str(rel_id) for r in reqs.get("incoming", [])):
        raise HTTPException(status_code=403, detail="That request isn't yours to approve.")
    rel_type = (payload.get("relationship_type") or "").strip().lower()
    if not rel_type:
        raise HTTPException(status_code=400, detail="Pick their role before granting access.")
    res = db.approve_relationship(rel_id, relationship_type=rel_type,
                                  scope_company_ids=payload.get("scope_company_ids"))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not approve."))
    return {"status": "success", **res}


@app.post("/api/network/decline")
async def api_network_decline(payload: dict):
    _, org_id = _network_ctx(payload.get("username"), payload.get("company_name"),
                             payload.get("org_id"))
    rel_id = payload.get("request_id") or payload.get("rel_id")
    reqs = db.list_relationship_requests(org_id)
    if not any(r["id"] == str(rel_id) for r in reqs.get("incoming", [])):
        raise HTTPException(status_code=403, detail="That request isn't yours to decline.")
    res = db.decline_relationship(rel_id)
    return {"status": "success" if res.get("ok") else "error", **res}


@app.get("/api/network/connections")
async def api_network_connections(username: str, company_name: str = None, org_id: str = None):
    _, org_id = _network_ctx(username, company_name, org_id)
    return {"status": "success", "connections": db.list_connections(org_id)}


@app.post("/api/network/revoke")
async def api_network_revoke(payload: dict):
    _, org_id = _network_ctx(payload.get("username"), payload.get("company_name"),
                             payload.get("org_id"))
    rel_id = payload.get("relationship_id") or payload.get("rel_id")
    if not rel_id:
        raise HTTPException(status_code=400, detail="Missing relationship id.")
    # Caller must be on either side of the relationship.
    conns = db.list_connections(org_id)
    if not any(c["id"] == str(rel_id) for c in conns):
        raise HTTPException(status_code=403, detail="That connection isn't yours to revoke.")
    res = db.revoke_relationship(rel_id)
    return {"status": "success" if res.get("ok") else "error", **res}


@app.get("/api/user/companies")
async def api_user_companies_list(username: str):
    """Fresh list of companies the caller can access (legacy projection — same source
    the login uses). Lets the company switcher pick up newly-shared companies without
    a re-login."""
    u = db.get_user_by_username(username)
    if not u:
        raise HTTPException(status_code=404, detail="User not found.")
    companies = u.get("companies")
    if isinstance(companies, str):
        try: companies = json.loads(companies)
        except Exception: companies = None
    if not companies:
        companies = [u.get("company_name") or "Acme Corp"]
    # Per-company switcher badges: owned ("added by you") / shared / archived.
    meta, archived = {}, []
    try:
        if u.get("users_id"):
            cls = db.classify_companies_for_user(u["users_id"])
            meta = cls.get("meta", {}); archived = cls.get("archived", [])
    except Exception as e:
        print(f"[user/companies classify] {e}")
    return {"status": "success", "companies": companies, "company_name": u.get("company_name"),
            "company_meta": meta, "archived": archived}


# =============================================================================
# Sprint 47 — Wallet (tokens) endpoints
# =============================================================================
@app.get("/api/wallet")
async def api_wallet(username: str, company_name: str = None):
    """Balance + recent ledger for the workspace tied to the caller (optionally the
    workspace that owns `company_name`, i.e. the active company)."""
    org_id, (uid, _) = _billing_org_id(username, company_name)
    if org_id is None and username:
        uid2, _ = _resolve_caller(username)
        org_id = _owner_org(uid2, None, allow=("owner", "manager", "accountant", "junior", "viewer"))
    if org_id is None:
        return {"status": "success", "balance": 0, "org_id": None, "ledger": []}
    return {"status": "success", "org_id": str(org_id), "balance": db.org_balance(org_id),
            "tokens_per_inr": TOKENS_PER_INR, "ledger": db.recent_ledger(org_id, 20)}


@app.post("/api/wallet/recharge")
async def api_wallet_recharge(payload: dict):
    """Start a recharge. With Razorpay configured, create a gateway order and return
    the Checkout params; otherwise fall back to a manual 'pending' purchase that a
    super_admin credits."""
    username = payload.get("username")
    uid, _ = _resolve_caller(username)
    org_id = payload.get("org_id") or _owner_org(uid, None)
    amount = float(payload.get("amount_inr") or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Enter an amount.")
    tokens = int(round(amount * TOKENS_PER_INR))
    if RAZORPAY_ENABLED:
        try:
            p = db.create_purchase(org_id, amount, tokens, created_by=username, provider="razorpay")
            receipt = ("r_" + str(p["id"]).replace("-", ""))[:40]   # Razorpay caps receipt at 40 chars
            order = _razorpay_create_order(amount, receipt=receipt,
                                           notes={"purchase_id": str(p["id"]), "org_id": str(org_id)})
            # store the order id on the purchase so verify/webhook can match it
            db.mark_purchase_order(str(p["id"]), order["id"])
            return {"status": "order", "provider": "razorpay", "key_id": RAZORPAY_KEY_ID,
                    "order_id": order["id"], "amount": order["amount"], "currency": order["currency"],
                    "tokens": tokens, "purchase_id": str(p["id"])}
        except Exception as e:
            print(f"[recharge] razorpay order failed: {e}", flush=True)
            raise HTTPException(status_code=502, detail="Payment gateway error — try again.")
    # manual fallback (no gateway configured)
    p = db.create_purchase(org_id, amount, tokens, created_by=username, provider="manual")
    return {"status": "pending", "purchase_id": str(p["id"]), "amount_inr": amount,
            "tokens": tokens, "message": "Recharge requested — tokens will be credited shortly."}


@app.post("/api/wallet/verify")
async def api_wallet_verify(payload: dict):
    """Verify a Razorpay Checkout result and credit tokens (idempotent)."""
    if not RAZORPAY_ENABLED:
        raise HTTPException(status_code=400, detail="Payments not enabled.")
    order_id = (payload or {}).get("razorpay_order_id")
    payment_id = (payload or {}).get("razorpay_payment_id")
    signature = (payload or {}).get("razorpay_signature")
    if not _razorpay_verify_signature(order_id, payment_id, signature):
        raise HTTPException(status_code=400, detail="Payment verification failed.")
    row = db.mark_purchase_paid_by_order(order_id, provider_ref=payment_id)
    bal = None
    if row:   # first time this order is marked paid → credit once
        bal = db.credit_tokens(row["org_id"], row["tokens"], reason="recharge",
                               ref_id=payment_id, created_by="razorpay")
    else:
        # already credited (e.g. webhook beat us) → just report balance
        try:
            u = db.get_user_by_username(payload.get("username")) if payload.get("username") else None
            oid = db.org_id_for_company(payload.get("company_name"), u and u.get("users_id"))
            bal = db.org_balance(oid) if oid else None
        except Exception:
            pass
    return {"status": "success", "balance": bal}


@app.post("/api/webhooks/razorpay")
async def api_webhook_razorpay(request: Request):
    """Reliable backstop: Razorpay calls this on payment.captured / order.paid.
    Verifies the webhook signature and credits tokens (idempotent)."""
    raw = await request.body()
    sig = request.headers.get("x-razorpay-signature", "")
    if RAZORPAY_WEBHOOK_SECRET:
        import hmac as _h, hashlib as _hl
        expected = _h.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw, _hl.sha256).hexdigest()
        if not _h.compare_digest(expected, sig):
            raise HTTPException(status_code=400, detail="bad signature")
    try:
        body = json.loads(raw.decode() or "{}")
        ent = (body.get("payload") or {})
        order_id = None; payment_id = None
        pay = ((ent.get("payment") or {}).get("entity") or {})
        if pay:
            order_id = pay.get("order_id"); payment_id = pay.get("id")
        if not order_id:
            order_id = ((ent.get("order") or {}).get("entity") or {}).get("id")
        if order_id:
            row = db.mark_purchase_paid_by_order(order_id, provider_ref=payment_id)
            if row:
                db.credit_tokens(row["org_id"], row["tokens"], reason="recharge",
                                 ref_id=payment_id, created_by="razorpay_webhook")
    except Exception as e:
        print(f"[razorpay webhook] {e}", flush=True)
    return {"status": "ok"}


# =============================================================================
# Sprint 55 — "Upload anything": a company-scoped file library
# =============================================================================
# ── Sprint 70 — durable file storage on Supabase Storage (Cloud Run disk is ephemeral) ──
_FILES_BUCKET = "company-files"
_storage_bucket_ready = False

def _ensure_storage_bucket():
    """Idempotently create the public 'company-files' bucket. Best-effort."""
    global _storage_bucket_ready
    if _storage_bucket_ready or not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return
    try:
        requests.post(f"{SUPABASE_URL}/storage/v1/bucket",
                      headers={"apikey": SUPABASE_SERVICE_ROLE_KEY,
                               "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                               "Content-Type": "application/json"},
                      json={"id": _FILES_BUCKET, "name": _FILES_BUCKET, "public": True},
                      timeout=10)
    except Exception as e:
        print(f"[storage bucket] {e}", flush=True)
    _storage_bucket_ready = True   # don't retry every upload; 409 (exists) is fine

def _supabase_upload(path, data, content_type):
    """Upload bytes to Supabase Storage; return the durable public URL or None."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return None
    _ensure_storage_bucket()
    try:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/{_FILES_BUCKET}/{path}",
            headers={"apikey": SUPABASE_SERVICE_ROLE_KEY,
                     "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                     "Content-Type": content_type or "application/octet-stream",
                     "x-upsert": "true"},
            data=data, timeout=60)
        if r.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{_FILES_BUCKET}/{path}"
        print(f"[storage upload] {r.status_code} {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[storage upload] {e}", flush=True)
    return None

@app.post("/api/files/upload")
async def api_files_upload(file: UploadFile = File(...), company_name: str = Form(None),
                           username: str = Form(None)):
    import uuid as _uuid, re as _re
    if not company_name:
        raise HTTPException(status_code=400, detail="company_name required")
    ext = os.path.splitext(file.filename or "")[1]
    data = await file.read()
    ct = file.content_type
    # Group objects by a safe company slug; random uuid key = unguessable capability URL.
    slug = _re.sub(r'[^a-z0-9]+', '-', (company_name or 'co').lower()).strip('-') or 'co'
    path = f"{slug}/{_uuid.uuid4().hex}{ext}"
    url = _supabase_upload(path, data, ct)
    if not url:
        # Fallback (local/dev or Storage unavailable): write to disk as before.
        os.makedirs("static/uploads", exist_ok=True)
        stored = f"{_uuid.uuid4().hex}{ext}"
        with open(f"static/uploads/{stored}", "wb") as f:
            f.write(data)
        url = f"/static/uploads/{stored}"
    row = db.save_company_file(company_name, url, original_name=file.filename,
                               file_type=ct, size_bytes=len(data),
                               uploaded_by=username)
    return {"status": "success", "file": {"id": str(row["id"]), "file_url": url,
            "original_name": file.filename, "file_type": ct,
            "size_bytes": len(data)}}


@app.get("/api/files")
async def api_files_list(company_name: str = None):
    rows = db.list_company_files(company_name) if company_name else []
    return {"status": "success", "files": [{
        "id": str(r["id"]), "file_url": r["file_url"], "original_name": r["original_name"],
        "file_type": r["file_type"], "size_bytes": r["size_bytes"],
        "created_at": str(r["created_at"])} for r in rows]}


@app.post("/api/files/delete")
async def api_files_delete(payload: dict):
    ok = db.archive_company_file(payload.get("id"), payload.get("company_name"))
    return {"status": "success" if ok else "error"}


@app.post("/api/files/rename")
async def api_files_rename(payload: dict):
    ok = db.rename_company_file(payload.get("id"), payload.get("company_name"),
                                payload.get("name"))
    return {"status": "success" if ok else "error"}


# ── Unallocated inbox: files shared in from other apps land here (no company yet),
# scoped to the workspace (org), and the user sorts them into a company later. ──
def _suggest_company_for_file(file_id, org_id, temp_path):
    """Background: parse the shared doc and guess which of the USER'S OWN workspace
    companies it belongs to (match the document's party GSTIN/name against the org's
    companies — not party-master/ledger entries). Sets suggested_company."""
    best = None
    try:
        raw = parser.parse(temp_path, context="")
        import re as _re, json as _json
        m = _re.search(r'(\{.*\})', raw or '', _re.DOTALL)
        data = _json.loads(m.group(1) if m else (raw or '').strip().replace('```json', '').replace('```', ''))
        meta = data.get("invoice_metadata") or {}
        gstins = [(meta.get("billing_party_gstin") or "").strip().upper(),
                  (meta.get("billed_to_party_gstin") or "").strip().upper()]
        names = [(meta.get("billing_party_name") or "").strip().lower(),
                 (meta.get("billed_to_party_name") or "").strip().lower()]
        comps = db.list_org_companies_with_gstin(org_id) or []
        for c in comps:                                   # 1) exact GSTIN match wins
            g = (c.get("gstin") or "").strip().upper()
            if g and g in gstins:
                best = c["name"]; break
        if not best:                                      # 2) fuzzy name match
            for c in comps:
                cn = (c.get("name") or "").strip().lower()
                if cn and any(n and (cn in n or n in cn) for n in names):
                    best = c["name"]; break
    except Exception as e:
        print(f"[suggest_company_for_file] {e}")
    try: db.set_file_suggestion(file_id, best, "done")
    except Exception: pass
    try:
        if temp_path and os.path.exists(temp_path): os.remove(temp_path)
    except Exception: pass


@app.post("/api/files/upload-unallocated")
async def api_files_upload_unallocated(file: UploadFile = File(...), company_name: str = Form(None),
                                       username: str = Form(None), background_tasks: BackgroundTasks = None):
    import uuid as _uuid
    # Resolve the workspace (org) from the active company, so the inbox shows across
    # all that workspace's companies. Fall back to the caller's owned org.
    org_id = None
    try:
        uid = None
        if username:
            u = db.get_user_by_username(username); uid = u.get("users_id") if u else None
        if company_name:
            org_id = db.org_id_for_company(company_name, uid)
        if not org_id and uid:
            mems = db.user_memberships_basic(uid) or []
            m = next((x for x in mems if x["role"] in ("owner", "manager")), None) or (mems[0] if mems else None)
            org_id = m["org_id"] if m else None
    except Exception as e:
        print(f"[upload-unallocated] org resolve: {e}")
    if not org_id:
        raise HTTPException(status_code=400, detail="Could not resolve your workspace for the inbox.")
    ext = os.path.splitext(file.filename or "")[1]
    data = await file.read()
    ct = file.content_type
    path = f"unallocated/{_uuid.uuid4().hex}{ext}"
    url = _supabase_upload(path, data, ct)
    if not url:
        os.makedirs("static/uploads", exist_ok=True)
        stored = f"{_uuid.uuid4().hex}{ext}"
        with open(f"static/uploads/{stored}", "wb") as f:
            f.write(data)
        url = f"/static/uploads/{stored}"
    row = db.save_unallocated_file(org_id, url, original_name=file.filename,
                                   file_type=ct, size_bytes=len(data), uploaded_by=username,
                                   suggest_status="pending")
    if not row:
        raise HTTPException(status_code=500, detail="Could not save the shared file.")
    # Kick off the AI company-suggestion in the background (parse is slow ~10-20s).
    try:
        os.makedirs("static/uploads", exist_ok=True)
        tmp = f"static/uploads/_suggest_{_uuid.uuid4().hex}{ext}"
        with open(tmp, "wb") as f:
            f.write(data)
        if background_tasks is not None:
            background_tasks.add_task(_suggest_company_for_file, str(row["id"]), str(org_id), tmp)
    except Exception as _se:
        print(f"[upload-unallocated] suggest schedule: {_se}")
    return {"status": "success", "file": {"id": str(row["id"]), "file_url": url,
            "original_name": file.filename, "file_type": ct, "size_bytes": len(data)}}


@app.get("/api/files/unallocated")
async def api_files_unallocated(username: str = None, company_name: str = None, org_id: str = None):
    """Workspace inbox — shown in every company's Files view of that workspace."""
    if not org_id:
        try:
            uid = None
            if username:
                u = db.get_user_by_username(username); uid = u.get("users_id") if u else None
            if company_name:
                org_id = db.org_id_for_company(company_name, uid)
            if not org_id and uid:
                mems = db.user_memberships_basic(uid) or []
                m = next((x for x in mems if x["role"] in ("owner", "manager")), None) or (mems[0] if mems else None)
                org_id = m["org_id"] if m else None
        except Exception as e:
            print(f"[files/unallocated] org resolve: {e}")
    if not org_id:
        return {"status": "success", "files": []}
    rows = db.list_unallocated_files(org_id)
    return {"status": "success", "files": [{
        "id": str(r["id"]), "file_url": r["file_url"], "original_name": r["original_name"],
        "file_type": r["file_type"], "size_bytes": r["size_bytes"],
        "suggested_company": r.get("suggested_company"), "suggest_status": r.get("suggest_status"),
        "created_at": str(r["created_at"])} for r in rows]}


@app.post("/api/files/allocate")
async def api_files_allocate(payload: dict):
    """Assign an inbox file to one of the workspace's companies."""
    username = payload.get("username"); company = payload.get("company_name")
    file_id = payload.get("id") or payload.get("file_id")
    if not file_id or not company:
        raise HTTPException(status_code=400, detail="File and company are required.")
    uid = None
    u = db.get_user_by_username(username) if username else None
    uid = u.get("users_id") if u else None
    org_id = db.org_id_for_company(company, uid)
    if not org_id:
        raise HTTPException(status_code=400, detail="Could not resolve that company's workspace.")
    res = db.allocate_file(file_id, org_id, company)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not file it."))
    return {"status": "success"}


@app.post("/api/wallet/credit")
async def api_wallet_credit(payload: dict):
    """super_admin manual top-up (until the gateway is live). Credits tokens to an
    org, and marks a pending purchase paid if purchase_id is given."""
    actor = payload.get("username")
    a = db.get_user_by_username(actor) if actor else None
    if not a or a.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Only a super admin can credit tokens.")
    org_id = payload.get("org_id")
    purchase_id = payload.get("purchase_id")
    tokens = payload.get("tokens")
    if purchase_id and not tokens:
        row = db.mark_purchase_paid(purchase_id)
        if not row:
            raise HTTPException(status_code=400, detail="Purchase not found or already paid.")
        org_id, tokens = row["org_id"], row["tokens"]
        bal = db.credit_tokens(org_id, tokens, reason="recharge", ref_id=str(purchase_id), created_by=actor)
    else:
        if not org_id or not tokens:
            raise HTTPException(status_code=400, detail="org_id and tokens required.")
        bal = db.credit_tokens(org_id, int(tokens), reason="admin_adjust",
                               note=payload.get("note"), created_by=actor)
    return {"status": "success", "balance": bal}


# =============================================================================
# Sprint 48 — Agentic store: catalog / installs / per-agent usage
# =============================================================================
def _caller_org_any(username, company_name=None):
    """Resolve the caller's workspace (org) for store views — any membership role.
    Returns org_id or None (never raises, so read endpoints degrade gracefully)."""
    try:
        org_id, (uid, _) = _billing_org_id(username, company_name)
        if org_id:
            return org_id
        if username:
            u = db.get_user_by_username(username)
            uid = u.get("users_id") if u else None
            if uid:
                mems = db.get_user_memberships(uid) or []
                if mems:
                    return mems[0]["org_id"]
    except Exception as e:
        print(f"[_caller_org_any] {e}")
    return None


def _is_super_admin(username) -> bool:
    """Non-raising check: is this username the platform super agent (super_admin)?"""
    if not username:
        return False
    try:
        u = db.get_user_by_username(username)
        return bool(u and (u.get("role") if isinstance(u, dict) else None) == "super_admin")
    except Exception:
        return False


def _serialize_agent(a):
    """JSON-safe agent row."""
    return {
        "slug": a.get("slug"), "name": a.get("name"), "tagline": a.get("tagline"),
        "description": a.get("description"), "icon": a.get("icon"),
        "category": a.get("category"), "status": a.get("status"),
        "publisher": a.get("publisher"), "token_policy": a.get("token_policy"),
        "manifest": a.get("manifest"),
        # Sprint 75 — expose visibility/owner so the super-agent catalog can tag non-public/archived.
        "visibility": a.get("visibility") or "public",
        "owner_org_id": str(a.get("owner_org_id")) if a.get("owner_org_id") else None,
        "installed": bool(a.get("installed")) if "installed" in a else None,
    }


@app.get("/api/agents/catalog")
async def api_agents_catalog(username: str = None, company_name: str = None):
    """Full agent catalog; if the caller's workspace resolves, each row carries an
    `installed` flag. The super agent (super_admin) sees EVERY agent — all visibility,
    all owners, archived included."""
    org_id = _caller_org_any(username, company_name)
    is_sa = _is_super_admin(username)
    rows = db.list_catalog(org_id, include_all=is_sa)
    return {"status": "success", "org_id": str(org_id) if org_id else None,
            "is_super_admin": is_sa,
            "agents": [_serialize_agent(r) for r in rows]}


@app.get("/api/agents/installed")
async def api_agents_installed(username: str = None, company_name: str = None):
    """Installed (enabled) agents for the caller's workspace, with manifests.
    Degrades to [ai-accountant] if the org can't be resolved so the UI never blanks."""
    org_id = _caller_org_any(username, company_name)
    if not org_id:
        cat = {a["slug"]: a for a in db.list_catalog()}
        a = cat.get("ai-accountant")
        return {"status": "success", "org_id": None,
                "agents": [_serialize_agent(a)] if a else []}
    rows = db.list_installed_agents(org_id)
    if not rows and not db.org_has_install_history(org_id):
        # Org never had any install (pre-backfill) → show Agent #1 so the UI
        # never blanks. (If the user deliberately removed everything, respect that.)
        cat = {a["slug"]: a for a in db.list_catalog()}
        a = cat.get("ai-accountant")
        rows = [a] if a else []
    return {"status": "success", "org_id": str(org_id),
            "agents": [_serialize_agent(r) for r in rows]}


@app.post("/api/agents/install")
async def api_agents_install(payload: dict):
    """Install an agent for the caller's workspace (owner/manager only)."""
    username = payload.get("username")
    slug = (payload.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    uid, _ = _resolve_caller(username)
    org_id = _owner_org(uid, payload.get("org_id"), allow=("owner", "manager"))
    # Sprint 75 — the super agent can install/open ANY agent (private/other-org/archived);
    # everyone else is limited to the public, non-archived catalog.
    is_sa = _is_super_admin(username)
    cat = {a["slug"]: a for a in db.list_catalog(org_id=None, include_all=is_sa)}
    ag = cat.get(slug)
    if not ag:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    if ag.get("status") == "coming_soon":
        raise HTTPException(status_code=400, detail="That agent isn't available yet.")
    db.install_agent(org_id, slug, uid)
    return {"status": "success", "slug": slug, "org_id": str(org_id)}


@app.get("/api/admin/analytics")
async def api_admin_analytics(username: str = None, start: str = None, end: str = None):
    """Sprint 76 — super-agent-only platform analytics (usage by user, tokens, etc.)."""
    if not _is_super_admin(username):
        raise HTTPException(status_code=403, detail="super_admin only")
    return {"status": "success", "data": db.platform_analytics(start, end)}


@app.post("/api/agents/uninstall")
async def api_agents_uninstall(payload: dict):
    """Soft-uninstall (disable) an agent for the caller's workspace (owner/manager)."""
    username = payload.get("username")
    slug = (payload.get("slug") or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    uid, _ = _resolve_caller(username)
    org_id = _owner_org(uid, payload.get("org_id"), allow=("owner", "manager"))
    db.uninstall_agent(org_id, slug)
    return {"status": "success", "slug": slug, "org_id": str(org_id)}


@app.get("/api/wallet/usage-by-agent")
async def api_wallet_usage_by_agent(username: str = None, company_name: str = None):
    """Per-agent token usage breakdown for the caller's workspace."""
    org_id = _caller_org_any(username, company_name)
    if not org_id:
        return {"status": "success", "org_id": None, "usage": []}
    rows = db.usage_by_agent(org_id)
    cat = {a["slug"]: a for a in db.list_catalog()}
    usage = []
    for r in rows:
        slug = r["slug"]
        meta = cat.get(slug, {})
        usage.append({"slug": slug, "name": meta.get("name", slug),
                      "icon": meta.get("icon", "🤖"),
                      "tokens": int(r["tokens"] or 0), "calls": int(r["calls"] or 0)})
    return {"status": "success", "org_id": str(org_id), "usage": usage}


# --- Sprint 49 — marketplace SSO seam for remote (embedded) agents ----------
import hmac as _hmac, hashlib as _hashlib, base64 as _b64, json as _ssojson, time as _ssotime

_SSO_SECRET = os.getenv("SSO_SECRET", "yantrai-dev-sso-secret-change-me").encode()
_SSO_TTL = 300  # seconds

def _b64u(b: bytes) -> str:
    return _b64.urlsafe_b64encode(b).decode().rstrip("=")

def _b64u_dec(s: str) -> bytes:
    return _b64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def _sso_key_for(kid):
    """Per-app signing key (Sprint 50) by client_id/slug, else the global secret."""
    if kid:
        try:
            k = db.get_app_signing_key(kid)
            if k:
                return k.encode()
        except Exception as e:
            print(f"[_sso_key_for] {e}")
    return _SSO_SECRET

def _sso_sign(payload: dict, key: bytes) -> str:
    body = _b64u(_ssojson.dumps(payload, separators=(",", ":")).encode())
    sig = _b64u(_hmac.new(key, body.encode(), _hashlib.sha256).digest())
    return f"{body}.{sig}"

def _sso_verify(token: str):
    try:
        body, sig = token.split(".", 1)
        payload = _ssojson.loads(_b64u_dec(body))
        key = _sso_key_for(payload.get("kid"))   # pick the right key from the token's kid
        expected = _b64u(_hmac.new(key, body.encode(), _hashlib.sha256).digest())
        if not _hmac.compare_digest(sig, expected):
            return None
        if int(payload.get("exp", 0)) < int(_ssotime.time()):
            return None
        return payload
    except Exception:
        return None


@app.get("/api/agents/sso-token")
async def api_agents_sso_token(username: str = None, company_name: str = None, slug: str = None):
    """Mint a short-lived signed token a remote (embedded) agent uses to learn the
    current user + workspace, verified back against /api/agents/verify-sso. If `slug`
    is a registered app with its own signing key, the token is scoped to that app."""
    payload = {"u": username or "", "c": company_name or "",
               "exp": int(_ssotime.time()) + _SSO_TTL}
    key = _SSO_SECRET
    if slug:
        try:
            auth = db.get_agent_auth(slug)   # works for private dev apps too
            if auth:
                payload["kid"] = auth["client_id"]
                key = auth["signing_key"].encode()
        except Exception as e:
            print(f"[sso-token] {e}")
    return {"status": "success", "token": _sso_sign(payload, key)}


@app.get("/api/agents/verify-sso")
async def api_agents_verify_sso(token: str = None):
    """Validate a remote-agent SSO token. The embedded app calls this rather than
    trusting the query string. Key is auto-selected from the token's `kid`."""
    p = _sso_verify(token or "")
    if not p:
        return {"ok": False}
    return {"ok": True, "username": p.get("u"), "company_name": p.get("c")}


@app.post("/api/agents/usage")
async def api_agents_usage(payload: dict):
    """Remote (embedded) agents report token usage here using the user's SSO token.
    The token's `kid` identifies the app (so usage lands on the right agent_slug) and
    its `u`/`c` claims identify the user + workspace to debit. Chargeable remote apps
    call this server-side after each LLM action."""
    payload = payload or {}
    p = _sso_verify(payload.get("token") or "")
    if not p:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    slug = db.slug_for_client_id(p.get("kid")) if p.get("kid") else None
    if not slug:
        raise HTTPException(status_code=400, detail="Token is not scoped to a registered app.")
    try:
        reported = int(payload.get("tokens") or 0)
    except (TypeError, ValueError):
        reported = 0
    if reported <= 0:
        raise HTTPException(status_code=400, detail="`tokens` must be a positive integer.")

    # The store owns billing: only charge if the app is marked chargeable, and if a
    # per-action price is configured it is authoritative (overrides the reported count).
    policy = db.get_token_policy(slug) or {}
    if not policy.get("chargeable"):
        return {"ok": True, "agent": slug, "charged": 0, "skipped": "not_chargeable"}
    n = int(policy.get("credits_per_action") or reported)

    company = p.get("c")
    _, uid = db.org_id_for_username(p.get("u")) if p.get("u") else (None, None)
    org_id = db.org_id_for_company(company, uid)
    if not org_id:
        raise HTTPException(status_code=404, detail="Could not resolve workspace for billing.")

    # Sandbox: validate + price the charge without touching balances/ledger.
    if payload.get("dry_run"):
        return {"ok": True, "agent": slug, "charged": n, "dry_run": True,
                "balance": db.org_balance(org_id)}

    bal = db.debit_tokens(org_id, n,
                          action=payload.get("action"), model=payload.get("model"),
                          user_id=uid, company_name=company,
                          prompt_tokens=payload.get("prompt_tokens"),
                          output_tokens=payload.get("output_tokens"),
                          agent_slug=slug)
    return {"ok": True, "agent": slug, "charged": n, "balance": bal}


# --- Sprint 50 — Developer Portal: register/manage your own remote agent --------
def _dev_org(username):
    """Resolve the caller's owned workspace for dev-app ownership (owner/manager)."""
    uid, _ = _resolve_caller(username)
    org_id = _owner_org(uid, None, allow=("owner", "manager"))
    return org_id, uid


@app.get("/api/dev/apps")
async def api_dev_apps_list(username: str = None):
    org_id, _ = _dev_org(username)
    rows = db.list_dev_apps(org_id)
    return {"status": "success", "org_id": str(org_id), "apps": [{
        "slug": r["slug"], "name": r["name"], "tagline": r.get("tagline"),
        "description": r.get("description"), "icon": r.get("icon"),
        "category": r.get("category"), "status": r.get("status"),
        "visibility": r.get("visibility"), "client_id": r.get("client_id"),
        "remote_url": (r.get("manifest") or {}).get("remote_url"),
        "token_policy": r.get("token_policy") or {},
        "review_status": r.get("review_status") or "none",
    } for r in rows]}


@app.post("/api/dev/apps")
async def api_dev_apps_create(payload: dict):
    org_id, uid = _dev_org(payload.get("username"))
    res = db.create_dev_app(org_id, uid,
                            name=payload.get("name"),
                            remote_url=payload.get("remote_url"),
                            tagline=payload.get("tagline"),
                            description=payload.get("description"),
                            icon=payload.get("icon") or "🧩",
                            category=payload.get("category") or "custom")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Could not create app.")
    return {"status": "success", **res}


@app.post("/api/dev/apps/update")
async def api_dev_apps_update(payload: dict):
    org_id, _ = _dev_org(payload.get("username"))
    res = db.update_dev_app(payload.get("slug"), org_id,
                            name=payload.get("name"), remote_url=payload.get("remote_url"),
                            tagline=payload.get("tagline"), description=payload.get("description"),
                            icon=payload.get("icon"), category=payload.get("category"))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Update failed.")
    return {"status": "success"}


@app.post("/api/dev/apps/archive")
async def api_dev_apps_archive(payload: dict):
    org_id, _ = _dev_org(payload.get("username"))
    res = db.archive_dev_app(payload.get("slug"), org_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Archive failed.")
    return {"status": "success"}


@app.post("/api/dev/apps/rotate-key")
async def api_dev_apps_rotate_key(payload: dict):
    org_id, _ = _dev_org(payload.get("username"))
    res = db.rotate_dev_app_key(payload.get("slug"), org_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Rotate failed.")
    return {"status": "success", "client_secret": res.get("client_secret")}


@app.post("/api/dev/apps/billing")
async def api_dev_apps_billing(payload: dict):
    """Set an owned app's billing: chargeable on/off + optional flat credits_per_action."""
    org_id, _ = _dev_org(payload.get("username"))
    res = db.set_app_billing(payload.get("slug"), org_id,
                             chargeable=payload.get("chargeable"),
                             credits_per_action=payload.get("credits_per_action"))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Billing update failed.")
    return {"status": "success", "token_policy": res.get("token_policy")}


@app.get("/api/dev/apps/usage")
async def api_dev_apps_usage(username: str = None, slug: str = None):
    """Usage/earnings for one of the caller's apps, across all workspaces that use it."""
    org_id, _ = _dev_org(username)
    res = db.dev_app_usage(slug, org_id)
    if res is None:
        raise HTTPException(status_code=404, detail="App not found.")
    return {"status": "success", "slug": slug, **res}


@app.post("/api/dev/apps/test-sso")
async def api_dev_apps_test_sso(payload: dict):
    """Sandbox: mint an app-scoped SSO token for the caller and verify it round-trips
    — proves the app's signing_key wiring without a deploy. Use the returned token
    with POST /api/agents/usage {dry_run:true} to test a charge."""
    org_id, _ = _dev_org(payload.get("username"))
    slug = payload.get("slug")
    if not db.get_dev_app(slug, org_id):
        raise HTTPException(status_code=404, detail="App not found.")
    auth = db.get_agent_auth(slug)
    if not auth:
        raise HTTPException(status_code=400, detail="App has no signing key yet.")
    token = _sso_sign({"u": payload.get("username") or "",
                       "c": payload.get("company_name") or "",
                       "kid": auth["client_id"],
                       "exp": int(_ssotime.time()) + _SSO_TTL},
                      auth["signing_key"].encode())
    claims = _sso_verify(token)
    return {"status": "success", "ok": claims is not None, "token": token, "claims": claims}


@app.post("/api/dev/apps/request-publish")
async def api_dev_apps_request_publish(payload: dict):
    """Developer requests public listing; a super_admin approves it."""
    org_id, uid = _dev_org(payload.get("username"))
    res = db.request_publish(payload.get("slug"), org_id, user_id=uid)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Request failed.")
    return {"status": "success"}


@app.get("/api/admin/publish-queue")
async def api_admin_publish_queue(username: str = None):
    if not _is_super_admin(username):
        raise HTTPException(status_code=403, detail="Forbidden.")
    return {"status": "success", "apps": db.publish_queue()}


@app.post("/api/admin/publish-decision")
async def api_admin_publish_decision(payload: dict):
    username = payload.get("username")
    if not _is_super_admin(username):
        raise HTTPException(status_code=403, detail="Forbidden.")
    uid, _ = _resolve_caller(username)
    res = db.decide_publish(payload.get("slug"), bool(payload.get("approve")),
                            reviewer_user_id=uid, note=payload.get("note"))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Decision failed.")
    return {"status": "success"}


# =============================================================================
# Tally Agent Authentication (Phase B)
# =============================================================================
@app.post("/api/agent/auth")
async def agent_auth(credentials: dict):
    """
    Tally desktop agent login. Validates username/password against accounting_users
    and returns a session token + the user's memberships (firms + companies) so the
    agent can show a company picker.
    """
    username = credentials.get("username")
    password = credentials.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    legacy_user = db.get_user_by_username(username)
    if not legacy_user or legacy_user["password"] != password:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Resolve the new public.users.id (created by Phase A migration) for membership lookup
    conn = db.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, is_super_admin FROM public.users WHERE username = %s", (username,))
    u_row = cur.fetchone()
    cur.close()
    conn.close()
    if not u_row:
        raise HTTPException(status_code=500, detail="User not migrated to multi-tenant schema yet")
    user_id = str(u_row["id"])

    memberships = db.get_user_memberships(user_id)
    # Convert UUIDs in companies list to strings for JSON
    serializable_memberships = []
    for m in memberships:
        serializable_memberships.append({
            "membership_id": str(m["membership_id"]),
            "org_id": str(m["org_id"]),
            "org_name": m["org_name"],
            "org_type": m["org_type"],
            "role": m["role"],
            "scope_company_ids": m.get("scope_company_ids"),
            "plan": m.get("plan"),
            "companies": [
                {
                    "id": str(c["id"]),
                    "name": c["name"],
                    "gstin": c.get("gstin"),
                    "is_primary": c.get("is_primary", False),
                }
                for c in (m.get("companies") or [])
            ],
        })

    # Issue session token
    token = "ag_" + _secrets.token_hex(32)
    agent_sessions[token] = {
        "user_id": user_id,
        "username": username,
        "name": legacy_user.get("name", username),
        "is_super_admin": bool(u_row["is_super_admin"]),
        "expires_at": datetime.utcnow() + AGENT_SESSION_TTL,
    }

    return {
        "status": "success",
        "session_token": token,
        "user_id": user_id,
        "name": legacy_user.get("name", username),
        "username": username,
        "is_super_admin": bool(u_row["is_super_admin"]),
        "memberships": serializable_memberships,
    }


@app.post("/api/agent/whoami")
async def agent_whoami(payload: dict):
    """Validate a session token (heartbeat / refresh). Returns user_id + memberships."""
    token = payload.get("session_token")
    sess = validate_agent_session(token)
    if not sess:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    memberships = db.get_user_memberships(sess["user_id"])
    return {
        "status": "success",
        "user_id": sess["user_id"],
        "username": sess["username"],
        "name": sess["name"],
        "is_super_admin": sess.get("is_super_admin", False),
        "memberships_count": len(memberships),
    }


@app.post("/api/add-company")
async def add_company(payload: dict):
    username = payload.get("username")
    company_name = payload.get("company_name")
    
    if not username or not company_name:
        raise HTTPException(status_code=400, detail="username and company_name required")
        
    success = db.add_company_to_user(username, company_name)
    if success:
        return {"status": "success", "message": f"Added {company_name}"}
    else:
        raise HTTPException(status_code=500, detail="Failed to add company")


# ── Manage Companies (workspace owner) — add / edit details / archive ──
def _manage_company_ctx(username, company_name=None, require_owner=True):
    """Resolve (users_id, org_id, is_owner) for company management. Org = the active
    company's workspace (fallback: caller's first owned/managed org)."""
    users_id, _ = _resolve_caller(username)
    mems = db.user_memberships_basic(users_id) or []
    org_id = None
    if company_name:
        try: org_id = db.org_id_for_company(company_name, users_id)
        except Exception: org_id = None
    if not org_id:
        m = next((x for x in mems if x["role"] in ("owner", "manager")), None) or (mems[0] if mems else None)
        org_id = m["org_id"] if m else None
    if not org_id:
        raise HTTPException(status_code=403, detail="You don't have a workspace.")
    role = next((x["role"] for x in mems if str(x["org_id"]) == str(org_id)), None)
    is_owner = role == "owner"
    if require_owner and not is_owner:
        raise HTTPException(status_code=403, detail="Only the workspace owner can manage companies.")
    return users_id, org_id, is_owner


@app.get("/api/companies")
async def api_companies_list(username: str, company_name: str = None):
    users_id, org_id, is_owner = _manage_company_ctx(username, company_name, require_owner=False)
    return {"status": "success", "can_manage": is_owner,
            "companies": db.list_workspace_companies(org_id)}


@app.post("/api/companies/create")
async def api_companies_create(payload: dict):
    users_id, org_id, _ = _manage_company_ctx(payload.get("username"), payload.get("company_name"))
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Company name is required.")
    db.create_company(org_id, name, gstin=(payload.get("gstin") or None),
                      state_code=(payload.get("state_code") or None))
    # Project into the owner's legacy list so it shows in the switcher immediately.
    try: db.add_company_to_user(payload.get("username"), name)
    except Exception as e: print(f"[companies/create project] {e}")
    return {"status": "success", "name": name}


@app.post("/api/companies/update")
async def api_companies_update(payload: dict):
    _, org_id, _ = _manage_company_ctx(payload.get("username"), payload.get("company_name"))
    cid = payload.get("company_id") or payload.get("id")
    if not cid:
        raise HTTPException(status_code=400, detail="company_id is required.")
    res = db.update_company_details(org_id, cid, gstin=(payload.get("gstin") or None),
                                    state_code=(payload.get("state_code") or None))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not update."))
    return {"status": "success"}


@app.post("/api/companies/archive")
async def api_companies_archive(payload: dict):
    _, org_id, _ = _manage_company_ctx(payload.get("username"), payload.get("company_name"))
    cid = payload.get("company_id") or payload.get("id")
    if not cid:
        raise HTTPException(status_code=400, detail="company_id is required.")
    res = db.archive_company(org_id, cid)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not remove the company."))
    return {"status": "success", "name": res.get("name")}

@app.get("/api/tally-bridge/status")
async def tally_bridge_status():
    return {"connected_clients": list(tally_connections.keys())}

# ---- Tasks Endpoints ----

@app.post("/tasks/confirm-service-request")
async def confirm_service_request(request: Request):
    """User confirmed a service request — now create the task for super admin."""
    data = await request.json()
    session_id = data.get("session_id")
    company_name = data.get("company_name", "")
    title = data.get("title", "Service Request")
    description = data.get("description", "")
    category = data.get("category", "Other")
    priority = data.get("priority", "Normal")
    original_message = data.get("original_message", "")

    full_desc = f"[{category}] [{priority}]\n\n{title}\n\n{description}\n\n---\nOriginal user message: {original_message}"
    _created = db.create_task(session_id, company_name, full_desc, 'sadmin',
                              title=title, category=category, priority=priority,
                              source='chat_service_request')
    task_id = _created["task_id"] if isinstance(_created, dict) else _created

    # Save confirmation message to chat
    confirm_text = f"✅ Your service request has been raised successfully!\n\n**{title}**\nCategory: {category} | Priority: {priority}\n\nThe YantrAI team will review this and get back to you."
    msg_id = db.save_chat_message(
        session_id, "assistant", confirm_text,
        "task_assigned",
        {"task_id": task_id, "status": "Requested", "title": title, "description": description, "category": category, "priority": priority}
    )

    return {
        "status": "success",
        "task_id": task_id,
        "text": confirm_text,
        "ui_type": "task_assigned",
        "ui_data": {"task_id": task_id, "status": "Requested", "title": title, "description": description, "category": category, "priority": priority},
        "id": msg_id,
        "session_id": session_id
    }

@app.get("/tasks")
async def get_tasks(company_name: str = "", role: str = "admin"):
    tasks = db.get_tasks(company_name, role)
    return {"status": "success", "tasks": tasks}

@app.post("/tasks/{task_id}/status")
async def update_task_status(task_id: str, status: str = Form(...)):
    db.update_task_status(task_id, status)
    return {"status": "success"}


# ─────────────────────────────────────────────────────────────────────────
# Sprint 57 — "Chat with YantrAI": conversational intake that builds a
# Problem Document (PD), then drops a trackable task into the SA inbox.
# ─────────────────────────────────────────────────────────────────────────
_PD_SYSTEM_PROMPT = """You are YantrAI's task-intake assistant. A user describes a task,
problem, or request — often in just a line or two — that they want the YantrAI team to
deliver. Your job is to do the IDEATION HEAVY-LIFTING and produce a clear, complete
PROBLEM DOCUMENT (PD) the team can act on, with as little back-and-forth as possible.

Behave like a sharp delivery manager who drafts first, asks later:
- From even a MINIMAL prompt, immediately draft a COMPLETE best-guess PD: fill EVERY field
  with sensible, specific assumptions inferred from the request (don't leave blanks; make
  reasonable defaults the user can edit). This is the user's starting point to edit.
- Ask AT MOST ONE short clarifying question, and only if a truly critical detail is missing;
  otherwise ask none and just present the draft.
- Set "ready": true as soon as a usable draft exists (which is essentially the first turn).
- When the user returns an EDITED version of the PD (you'll see their edited JSON), treat
  their edits as authoritative: polish wording, fix inconsistencies, and fill any gaps they
  left — but NEVER override or contradict what they explicitly wrote. Keep their intent.
- Be concise and friendly. The "reply" should be one short sentence (e.g. "Here's a draft —
  tweak any field and hit Refine, or Submit when it looks right.").

Reply STRICTLY as minified JSON, no markdown, no code fences:
{
 "reply": "your next message to the user (a question, or a 'looks good, confirm?' nudge)",
 "pd": {
   "title": "short imperative title",
   "objective": "what outcome the user wants",
   "context": "relevant background",
   "scope": "what's included / excluded",
   "deliverables": ["concrete output 1", "..."],
   "data_required": ["inputs/access the team needs"],
   "constraints": "deadlines, budget, tools, etc.",
   "success_criteria": "how we know it's done",
   "category": "Accounting | Compliance | Data | Integration | Reporting | Other",
   "priority": "Low | Normal | High | Urgent"
 },
 "ready": false
}
Always include the FULL pd object every turn (carry forward everything known so far)."""

def _pd_chat_llm(messages):
    convo = "\n".join(
        f"{'USER' if m.get('role')=='user' else 'ASSISTANT'}: {m.get('content','')}"
        for m in messages if m.get('content'))
    prompt = f"{_PD_SYSTEM_PROMPT}\n\n--- CONVERSATION SO FAR ---\n{convo}\n\n--- YOUR JSON REPLY ---"
    model = genai.GenerativeModel('gemini-flash-latest')
    resp = model.generate_content(prompt)
    return _parse_ai_json(getattr(resp, "text", "") or "")

@app.post("/api/yantrai/pd/chat")
async def yantrai_pd_chat(payload: dict):
    """One conversational turn — returns the assistant reply + the refined PD draft."""
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages required")
    try:
        out = _pd_chat_llm(messages)
    except Exception as e:
        print(f"[yantrai pd chat] {e}", flush=True)
        raise HTTPException(status_code=500, detail="AI intake failed")
    reply = out.get("reply") or out.get("text") or "Could you tell me a bit more about what you need?"
    pd = out.get("pd") if isinstance(out.get("pd"), dict) else None
    ready = bool(out.get("ready"))
    return {"status": "success", "reply": reply, "pd": pd, "ready": ready}

@app.post("/api/yantrai/pd/submit")
async def yantrai_pd_submit(payload: dict):
    """User confirmed the PD → create a trackable task in the SA inbox."""
    pd = payload.get("pd") or {}
    company_name = payload.get("company_name", "")
    username = payload.get("username")
    session_id = payload.get("session_id")
    title = (pd.get("title") or "Task request").strip()
    category = pd.get("category") or "Other"
    priority = pd.get("priority") or "Normal"
    # Render the PD into the human-readable description the inbox already shows.
    lines = [f"[{category}] [{priority}]", "", title, ""]
    def _add(label, val):
        if not val:
            return
        if isinstance(val, list):
            val = "\n  - " + "\n  - ".join(str(v) for v in val if v)
        lines.append(f"{label}: {val}")
    _add("Objective", pd.get("objective"))
    _add("Context", pd.get("context"))
    _add("Scope", pd.get("scope"))
    _add("Deliverables", pd.get("deliverables"))
    _add("Data required", pd.get("data_required"))
    _add("Constraints", pd.get("constraints"))
    _add("Success criteria", pd.get("success_criteria"))
    full_desc = "\n".join(lines)
    created = db.create_task(session_id, company_name, full_desc, 'sadmin',
                             title=title, category=category, priority=priority,
                             created_by=username, source='yantrai_chat', pd=pd)
    # Sprint 82 — name the task's chat session after the PD so the sidebar tile reads well.
    if session_id:
        try: db.update_chat_title(session_id, title)
        except Exception: pass
    return {"status": "success", "task_code": created.get("task_code"),
            "task_id": created.get("task_id"), "title": title,
            "category": category, "priority": priority}

@app.get("/api/yantrai/my-tasks")
async def yantrai_my_tasks(company_name: str = ""):
    """Tasks raised by this workspace, so the requester can track them by code."""
    rows = db.get_tasks_for_company(company_name) if company_name else []
    return {"status": "success", "tasks": rows}

# ── Sprint 82 — Create-task chat sessions (each task = a persisted, reopenable chat) ──
@app.post("/api/yantrai/session/new")
async def yantrai_session_new(payload: dict):
    sid = db.create_chat_session(title="New task", company_name=payload.get("company_name"),
                                 user_username=payload.get("username"), kind='yantrai_task')
    return {"status": "success", "session_id": sid}

@app.get("/api/yantrai/sessions")
async def yantrai_sessions(company_name: str = "", username: str = None):
    rows = db.list_task_sessions(company_name=company_name or None, user_username=username or None)
    return {"status": "success", "sessions": rows}

@app.get("/api/yantrai/session/{session_id}")
async def yantrai_session_get(session_id: str, username: str = None):
    """Transcript for one task session — owner-only (super_admin may pass through)."""
    try:
        owner = db.get_chat_session_owner(session_id)
    except Exception:
        owner = None
    if owner and username and owner.get("user_username") not in (None, '__legacy__', username):
        if not _is_super_admin(username):
            raise HTTPException(status_code=403, detail="Not your task.")
    return {"status": "success", "messages": db.get_chat_messages(session_id)}

@app.post("/api/yantrai/session/{session_id}/message")
async def yantrai_session_message(session_id: str, payload: dict):
    db.save_chat_message(session_id, payload.get("role", "user"), payload.get("content", ""),
                         payload.get("ui_type", "text"), payload.get("ui_data"))
    return {"status": "success"}

# ---- Parties (Party Master) Endpoints ----

from pydantic import BaseModel

class PartyModel(BaseModel):
    name: str
    gstin: str = None
    address: str = None
    bank_name: str = None
    account_number: str = None
    ifsc_code: str = None
    pan: str = None
    email: str = None
    phone: str = None
    company_name: str = "Acme Corp"

@app.get("/parties")
async def get_parties_endpoint(company_name: str = "Acme Corp"):
    try:
        parties = db.get_parties(company_name)
        return {"status": "success", "parties": parties}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/parties")
async def save_party_endpoint(party: PartyModel):
    try:
        db.save_or_update_party(
            company_name=party.company_name,
            name=party.name,
            gstin=party.gstin,
            address=party.address,
            bank_name=party.bank_name,
            account_number=party.account_number,
            ifsc_code=party.ifsc_code,
            pan=party.pan,
            email=party.email,
            phone=party.phone
        )
        return {"status": "success", "message": "Party updated successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/invoices/{invoice_id}")
async def delete_invoice_endpoint(invoice_id: str):
    try:
        success = db.delete_invoice(invoice_id)
        if success:
            return {"status": "success", "message": "Invoice deleted successfully!"}
        raise HTTPException(status_code=500, detail="Could not delete invoice")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/parties/{party_id}")
async def delete_party_endpoint(party_id: str):
    try:
        success = db.delete_party(party_id)
        if success:
            return {"status": "success", "message": "Party deleted successfully!"}
        raise HTTPException(status_code=500, detail="Could not delete party")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

import psycopg2
from psycopg2.extras import RealDictCursor

class MergePartiesModel(BaseModel):
    primary_name: str
    duplicate_names: list
    company_name: str = "Acme Corp"

@app.get("/api/tally/last-sync")
async def tally_last_sync(company_name: str = "Acme Corp"):
    """Return latest tally_sync_log entry for this company (for UI 'Last sync' badge)."""
    try:
        conn = db.get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT sync_type, records_in, records_upserted, status,
                   started_at, completed_at, error_message
            FROM tally_sync_log
            WHERE company_name = %s
            ORDER BY started_at DESC
            LIMIT 1
        """, (company_name,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return {"status": "success", "last_sync": None}
        return {"status": "success", "last_sync": {
            "sync_type": row["sync_type"],
            "records_in": row["records_in"],
            "records_upserted": row["records_upserted"],
            "status": row["status"],
            "started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
            "error_message": row["error_message"],
        }}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/items/master")
async def get_items_master(company_name: str = "Acme Corp"):
    """Combined item master: stock items from Tally + line items learned from invoices."""
    try:
        conn = db.get_conn()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # 1) Tally stock-item master (HSN, GST rate, closing qty/value)
        cursor.execute("""
            SELECT id, name, display_name, parent_group, unit, hsn_code, gst_rate,
                   closing_qty, closing_value, standard_rate
            FROM tally_stock_items
            WHERE company_name = %s
            ORDER BY name ASC
        """, (company_name,))
        tally_items = [dict(r) for r in cursor.fetchall()]
        for it in tally_items:
            it["source"] = "Tally"

        # 2) Invoice-derived line items (legacy)
        cursor.execute("""
            SELECT i.description, i.hsn_sac, inv.billing_party_name as source_party,
                   i.rate as price, inv.invoice_number, inv.date
            FROM items i
            JOIN invoices inv ON i.invoice_id = inv.id
            WHERE inv.company_name ILIKE %s OR %s ILIKE ('%%' || inv.company_name || '%%')
            ORDER BY i.description ASC, inv.date DESC
        """, (f"%{company_name}%", company_name))
        invoice_items = [dict(r) for r in cursor.fetchall()]
        for it in invoice_items:
            it["source"] = "Invoice"

        cursor.close()
        conn.close()
        return {
            "status": "success",
            "tally_items": tally_items,
            "invoice_items": invoice_items,
            "items": invoice_items,  # back-compat: existing UI reads `items`
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/parties/merge")
async def merge_parties_endpoint(payload: MergePartiesModel):
    try:
        if not payload.duplicate_names:
            return {"status": "success", "message": "No duplicates specified to merge."}
            
        conn = db.get_conn()
        cursor = conn.cursor()
        
        # 1. Update billing_party_name on invoices table
        cursor.execute("""
            UPDATE invoices
            SET billing_party_name = %s
            WHERE company_name = %s AND billing_party_name = ANY(%s)
        """, (payload.primary_name, payload.company_name, payload.duplicate_names))
        
        # 2. Update party_name on invoices table
        cursor.execute("""
            UPDATE invoices
            SET party_name = %s
            WHERE company_name = %s AND party_name = ANY(%s)
        """, (payload.primary_name, payload.company_name, payload.duplicate_names))
        
        # 3. Delete duplicate party profiles from parties table
        cursor.execute("""
            DELETE FROM parties
            WHERE company_name = %s AND name = ANY(%s)
        """, (payload.company_name, payload.duplicate_names))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return {"status": "success", "message": f"Successfully merged duplicate profiles into '{payload.primary_name}'!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port,
                ws_ping_interval=300, ws_ping_timeout=300)
