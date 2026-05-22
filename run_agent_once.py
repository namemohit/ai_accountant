"""Sprint 32 — Run the bridge agent's outbox poll loop once, long enough to
handle pre-flight ledger creation + voucher push against real Tally."""
import os, sys, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'tally_agent'))
sys.path.insert(0, os.path.dirname(__file__))
from tally_bridge_agent import outbox_poll_loop, heartbeat_loop

SERVER = 'http://localhost:8000'
TALLY  = 'http://localhost:9000'
COMPANY = 'Jai Mata Kalka Enterprises'

stop = threading.Event()
th_hb = threading.Thread(target=heartbeat_loop, args=(SERVER, COMPANY, stop), daemon=True)
th_poll = threading.Thread(target=outbox_poll_loop,
                            args=(SERVER, TALLY, COMPANY, stop,
                                  lambda m: print(f"[agent] {m}", flush=True)),
                            daemon=True)
th_hb.start(); th_poll.start()
print("Agent running for 180s …", flush=True)
time.sleep(180)
stop.set()
time.sleep(1)
print("Agent stopped.", flush=True)
