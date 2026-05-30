"""Upload an invoice PDF via the chat endpoint AND then click "Confirm & Sync".

Replays the exact two-step user flow:
  1. drag PDF into chat   ->  POST /chat            (Gemini parses, returns ui_data)
  2. click "Confirm & Sync" ->  POST /push-to-tally  (server queues to tally_outbox)

After (2) the headless / Windows agent's outbox poll picks up the row and pushes
to Tally Prime. The voucher then shows up in Tally Day Book.

Usage:
    python -X utf8 _chat_upload_invoice.py <pdf_path> [<pdf_path> ...]
"""
import json
import os
import sys
import time
import uuid
import urllib.request

SERVER = os.environ.get("YAI_SERVER", "http://localhost:8000")
COMPANY = os.environ.get("YAI_COMPANY", "Agent")
USERNAME = os.environ.get("YAI_USERNAME", "agent")


def multipart_encode(fields, files):
    boundary = "----yai-test-" + uuid.uuid4().hex
    body = []
    for k, v in fields.items():
        body.append(f"--{boundary}".encode())
        body.append(f'Content-Disposition: form-data; name="{k}"'.encode())
        body.append(b"")
        body.append(str(v).encode("utf-8"))
    for k, (fname, fbytes, ctype) in files.items():
        body.append(f"--{boundary}".encode())
        body.append(
            f'Content-Disposition: form-data; name="{k}"; filename="{fname}"'.encode()
        )
        body.append(f"Content-Type: {ctype}".encode())
        body.append(b"")
        body.append(fbytes)
    body.append(f"--{boundary}--".encode())
    body.append(b"")
    return b"\r\n".join(body), boundary


def post_chat_upload(pdf_path, session_id=None):
    """STEP 1 — Upload PDF to /chat (same as the chat picker)."""
    fname = os.path.basename(pdf_path)
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    fields = {
        "message": "",   # empty msg with a file attachment, like a drag-drop
        "company_name": COMPANY,
        "username": USERNAME,
    }
    if session_id:
        fields["session_id"] = session_id
    files = {"file": (fname, pdf_bytes, "application/pdf")}
    body, boundary = multipart_encode(fields, files)
    req = urllib.request.Request(
        f"{SERVER}/chat", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    print(f"  STEP 1 — POST /chat  {fname}  ({len(pdf_bytes):,} bytes)")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode("utf-8"))
    print(f"           HTTP {r.status}  in {time.time()-t0:.1f}s  "
          f"ui_type={data.get('ui_type')!r}")
    return data


def post_confirm_sync(chat_resp, file_url=None):
    """STEP 2 — click 'Confirm & Sync' = POST /push-to-tally with structured payload.

    Builds the payload identically to confirmTableData() in index.html:10508 —
    pulls invoice_metadata + rows from ui_data, computes the totals.
    """
    ui = chat_resp.get("ui_data") or {}
    meta = ui.get("invoice_metadata") or {}
    headers = ui.get("headers") or []
    rows = ui.get("rows") or []

    # Build items[] from rows by header-name matching (same logic as the UI).
    def _idx(needle_list):
        for i, h in enumerate(headers):
            hl = h.lower()
            for n in needle_list:
                if n in hl:
                    return i
        return -1

    desc_i = _idx(["description", "item"])
    qty_i = _idx(["qty", "quantity"])
    rate_i = _idx(["rate", "price"])
    total_i = _idx(["total", "amount"])
    disc_i = _idx(["discount"])
    cgst_i = _idx(["cgst"])
    sgst_i = _idx(["sgst"])
    hsn_i = _idx(["hsn", "sac"])

    def _num(s):
        if s is None:
            return 0.0
        try:
            return float(str(s).replace(",", "").replace("Rs.", "").replace("₹", "").strip() or 0)
        except Exception:
            return 0.0

    items = []
    for row in rows:
        items.append({
            "description": row[desc_i] if desc_i >= 0 else "Item",
            "quantity":    _num(row[qty_i]) if qty_i >= 0 else 1,
            "rate":        _num(row[rate_i]) if rate_i >= 0 else 0,
            "amount":      _num(row[total_i]) if total_i >= 0 else 0,
            "discount":    _num(row[disc_i]) if disc_i >= 0 else 0,
            "cgst_rate":   _num(row[cgst_i]) if cgst_i >= 0 else 0,
            "sgst_rate":   _num(row[sgst_i]) if sgst_i >= 0 else 0,
            "hsn_sac":     row[hsn_i] if hsn_i >= 0 else "",
        })

    # Date: chat returns YYYY-MM-DD; UI wants YYYYMMDD (no dashes).
    raw_date = (meta.get("date") or "").replace("-", "")
    if not raw_date:
        raw_date = time.strftime("%Y%m%d")

    # Sprint 44.1 — voucher_type drop fix. Gemini sometimes returns
    # "Purchase Invoice" / "Tax Invoice" / "Purchase" etc.; normalize to the
    # short form Tally expects. category is the fallback because Gemini
    # sometimes labels the kind there instead of voucher_type.
    raw_vt = (meta.get("voucher_type") or meta.get("category") or "Sales").strip()
    _vt_low = raw_vt.lower()
    if "purchase" in _vt_low:
        voucher_type = "Purchase"
    elif "receipt" in _vt_low:
        voucher_type = "Receipt"
    elif "payment" in _vt_low:
        voucher_type = "Payment"
    elif "contra" in _vt_low:
        voucher_type = "Contra"
    elif "journal" in _vt_low:
        voucher_type = "Journal"
    elif "debit" in _vt_low and "note" in _vt_low:
        voucher_type = "Debit Note"
    elif "credit" in _vt_low and "note" in _vt_low:
        voucher_type = "Credit Note"
    else:
        voucher_type = "Sales"
    taxable = _num(meta.get("taxable_value"))
    cgst = _num(meta.get("cgst_amount"))
    sgst = _num(meta.get("sgst_amount"))
    igst = _num(meta.get("igst_amount"))
    gross = taxable + cgst + sgst + igst or _num(meta.get("invoice_total"))

    payload = {
        "party_name":              meta.get("billed_to_party_name") or meta.get("billing_party_name") or "Chat Sync Client",
        "billing_party_name":      meta.get("billing_party_name"),
        "billing_party_gstin":     meta.get("billing_party_gstin"),
        "billed_to_party_gstin":   meta.get("billed_to_party_gstin"),
        "invoice_number":          meta.get("invoice_number") or f"CHAT-{int(time.time())%1000000}",
        "date":                    raw_date,
        "total_amount":            gross,
        "taxable_value":           taxable,
        "cgst_amount":             cgst,
        "sgst_amount":             sgst,
        "igst_amount":             igst,
        "category":                meta.get("category") or "Sales",
        "voucher_type":            voucher_type,
        "counter_ledger":          meta.get("counter_ledger") or "",
        "payment_mode":            meta.get("payment_mode") or "",
        "items":                   items,
        "company_name":            COMPANY,
        "file_url":                file_url or chat_resp.get("file_url") or "",
        "message_id":              chat_resp.get("id") or "",
    }

    print(f"  STEP 2 — POST /push-to-tally  {payload['voucher_type']}  "
          f"{payload['invoice_number']}  Rs.{payload['total_amount']:,.2f}")
    req = urllib.request.Request(
        f"{SERVER}/push-to-tally",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
        print(f"           HTTP {r.status}  in {time.time()-t0:.1f}s  "
              f"status={d.get('status')!r}")
        for k in ("warn", "voucher_id", "invoice_id", "outbox_id", "message"):
            if k in d:
                print(f"             {k}: {d[k]}")
        return d
    except urllib.error.HTTPError as he:
        body = he.read().decode("utf-8", errors="replace")
        print(f"           HTTP {he.code}: {body[:400]}")
        raise


def process_one(pdf_path, session_id=None):
    print(f"\n{'-' * 70}")
    print(f"  {os.path.basename(pdf_path)}")
    print(f"{'-' * 70}")
    chat_resp = post_chat_upload(pdf_path, session_id=session_id)
    if chat_resp.get("ui_type") != "table":
        print(f"  ! chat did not produce a voucher table — got ui_type="
              f"{chat_resp.get('ui_type')!r}; skipping push")
        return chat_resp, None
    push_resp = post_confirm_sync(chat_resp)
    return chat_resp, push_resp


def main():
    if len(sys.argv) < 2:
        print("usage: python _chat_upload_invoice.py <pdf_path> [<pdf_path> ...]")
        sys.exit(1)

    session_id = None  # reuse across uploads, same as a real chat session
    for pdf in sys.argv[1:]:
        chat_resp, _ = process_one(pdf, session_id=session_id)
        session_id = chat_resp.get("session_id") or session_id
        time.sleep(2)


if __name__ == "__main__":
    main()
