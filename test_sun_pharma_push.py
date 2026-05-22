"""Sprint 32 — End-to-end smoke test of the Sprint 31 push pipeline.

Walks the entire web → Tally pipeline using mock_tally as a Tally Prime
stand-in, exactly the way a real user click in the UI would:

  1. Start mock_tally on :9000 (agent_dump/mock_tally.py)
  2. POST /api/tally/heartbeat — sidebar flips 🟢
  3. POST /push-to-tally with the Sun Pharma payload — enqueues to tally_outbox
  4. Run the agent's outbox_poll_loop briefly — claims, pushes, acks
  5. Verify final outbox state + /api/tally/status reflect 'pushed'
"""
import os, sys, time, json, urllib.request, urllib.parse, subprocess, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'tally_agent'))
sys.path.insert(0, os.path.dirname(__file__))

SERVER = 'http://localhost:8000'
TALLY  = 'http://localhost:9000'
COMPANY = 'Jai Mata Kalka Enterprises'


def _post(url, body):
    req = urllib.request.Request(
        url, method='POST',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60.0) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


def _get(url):
    with urllib.request.urlopen(url, timeout=15.0) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


def main():
    print("=" * 70)
    print("Sprint 32 — Sun Pharma push end-to-end test")
    print("=" * 70)

    # 1. Tally on :9000 — use real Tally Prime if it's already listening,
    # otherwise start the mock as a stand-in.
    print(f"\n[1] Probing :9000 …")
    real_tally = False
    try:
        # Tally only answers POST; a minimal POST returns a small XML envelope.
        probe_req = urllib.request.Request(
            TALLY, method='POST', data=b'<ENVELOPE/>',
            headers={'Content-Type': 'text/xml'})
        with urllib.request.urlopen(probe_req, timeout=2.0) as r:
            body = r.read(300).decode('utf-8', errors='replace')
        if 'RESPONSE' in body.upper() or 'ENVELOPE' in body.upper():
            real_tally = True
            print("  ✓ real Tally Prime detected on :9000 — skipping mock")
    except Exception as e:
        print(f"  (probe: {e})")

    mock_proc = None
    if not real_tally:
        mock_path = os.path.join(os.path.dirname(__file__), 'agent_dump', 'mock_tally.py')
        print(f"  → starting mock_tally on :9000 …")
        mock_proc = subprocess.Popen(
            [sys.executable, mock_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        time.sleep(2.5)
        try:
            urllib.request.urlopen(TALLY, timeout=2.0)
            print("  ✓ mock_tally listening")
        except urllib.error.HTTPError:
            print("  ✓ mock_tally listening (405 on GET is fine)")
        except Exception as e:
            print(f"  ⚠️  mock_tally not responding: {e}")

    # 2. Heartbeat
    print("\n[2] Sending heartbeat …")
    hb = _post(f'{SERVER}/api/tally/heartbeat',
               {'company_name': COMPANY, 'agent_version': '0.2.0-test'})
    print(f"  → {hb}")

    # 3. Status before
    print("\n[3] Status BEFORE push:")
    st = _get(f'{SERVER}/api/tally/status?company_name={urllib.parse.quote(COMPANY)}')
    print(f"  agent_online={st.get('agent_online')}  "
          f"pending={st.get('pending')}  pushed={st.get('pushed')}  error={st.get('error')}")

    # 4. Enqueue the Sun Pharma push exactly as the web UI does
    print("\n[4] POST /push-to-tally for SUN PHARMA ₹2,87,396 …")
    payload = {
        'company_name': COMPANY,
        'voucher_type': 'Sales',
        'invoice_number': 'JMK/2026-27/047-TEST',
        'billing_party_name': 'SUN PHARMACEUTICAL INDUSTRIES LTD',
        'party_name':          'SUN PHARMACEUTICAL INDUSTRIES LTD',
        'date': '2026-05-21',
        'total_amount': 287396.00,
        'cgst_amount': 6842.76, 'sgst_amount': 6842.76,
        'taxable_value': 273710.40,
        'narration': 'Briquettes biomass sawdust — Sprint 32 e2e test',
    }
    resp = _post(f'{SERVER}/push-to-tally', payload)
    print(f"  → {resp}")

    # Snapshot which outbox row is ours so we can assert later
    import db
    conn=db.get_conn(); cur=conn.cursor()
    cur.execute("""SELECT id, state, enqueued_at FROM tally_outbox
                   WHERE company_name=%s AND payload->>'invoice_number'=%s
                   ORDER BY enqueued_at DESC LIMIT 1""",
                (COMPANY, 'JMK/2026-27/047-TEST'))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        print("  ❌ enqueue didn't create a tally_outbox row — aborting")
        if mock_proc: mock_proc.terminate()
        return
    outbox_id = str(row[0])
    print(f"  ✓ outbox row {outbox_id[:12]}… state={row[1]} enqueued_at={row[2]}")

    # 5. Run the agent's outbox poller briefly
    print("\n[5] Running outbox_poll_loop for 45s …")
    from tally_bridge_agent import outbox_poll_loop, heartbeat_loop
    stop = threading.Event()
    th_hb = threading.Thread(target=heartbeat_loop,
                              args=(SERVER, COMPANY, stop), daemon=True)
    th_poll = threading.Thread(target=outbox_poll_loop,
                                args=(SERVER, TALLY, COMPANY, stop,
                                      lambda m: print(f"  [agent] {m}")),
                                daemon=True)
    th_hb.start(); th_poll.start()
    time.sleep(45)
    stop.set()
    time.sleep(0.5)

    # 6. Stop mock + verify final state
    print("\n[6] Final state of the outbox row:")
    import db
    conn=db.get_conn(); cur=conn.cursor()
    cur.execute("""SELECT state, attempts, last_error, tally_voucher_guid, pushed_at
                   FROM tally_outbox WHERE id = %s""", (outbox_id,))
    final = cur.fetchone()
    cur.close(); conn.close()
    if final:
        state, attempts, last_error, guid, pushed_at = final
        print(f"  state          = {state}")
        print(f"  attempts       = {attempts}")
        print(f"  tally_guid     = {guid}")
        print(f"  pushed_at      = {pushed_at}")
        print(f"  last_error     = {last_error}")

    print("\n[7] Final status summary:")
    st2 = _get(f'{SERVER}/api/tally/status?company_name={urllib.parse.quote(COMPANY)}')
    print(f"  pending={st2.get('pending')}  pushed={st2.get('pushed')}  "
          f"error={st2.get('error')}  agent_online={st2.get('agent_online')}")

    # Cleanup mock (only if we started one)
    if mock_proc:
        try: mock_proc.terminate(); mock_proc.wait(timeout=5)
        except Exception: mock_proc.kill()
        out, _ = mock_proc.communicate(timeout=2) if mock_proc.poll() is None else (b'', b'')
    else:
        out = b''
    txt = (out or b'').decode('utf-8', errors='replace')
    if 'SUN PHARMACEUTICAL' in txt or 'VOUCHERNUMBER' in txt:
        # Show a snippet
        idx = max(txt.find('RECEIVED'), 0)
        print("\n[8] What mock Tally received (first 800 chars after header):")
        print(txt[idx:idx+1400])
    print("\n" + "=" * 70)
    print("Test complete.")
    print("=" * 70)


if __name__ == '__main__':
    main()
