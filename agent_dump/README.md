# agent_dump/

Archived Tally-related scripts that are **superseded by the main Bridge Agent**
(`tally_bridge_agent.py` + `dist/tally_bridge_agent.exe` in the repo root).

Nothing in here is wired to the UI or imported by `server.py`. Kept for
forensic / reference value only — feel free to delete if you don't need
them anymore.

## What lives here

| File | What it was | Why it's archived |
|------|------------|-------------------|
| `yantrai_sync.tdl` | A 92-line Tally Definition Language plugin that added a menu item inside Tally. | Only exported a basic ledger report. No HTTP push, no two-way sync. The Bridge Agent does everything this did and a lot more. |
| `headless_agent_demo.py` | A demo headless variant of the bridge agent used during Sprint 1 verification. | One-off testing helper. Imports from `tally_bridge_agent.py` so the real agent stays the source of truth. |
| `direct_sync.py` | CLI utility for direct Tally → DB sync, bypassing WS. | One-off forensic tool. Useful pattern but not part of the production flow. |
| `drive_sync.py` | A "driver" that scripted the full agent auth + WS + ingest flow end-to-end without the GUI. | Sprint 1 test rig. Superseded by the agent's own ingest path. |
| `mock_tally.py` | A tiny Flask server pretending to be Tally on port 9000, used to exercise the agent without a real Tally installation. | Local dev helper. Not needed in production. |
| `tally_bridge_agent.OLD_2026-05-19.exe` | Stale PyInstaller build (17.5 MB) from 2026-05-19 01:24. | Predated the latest `.py` edits; was being served unintentionally as the download. Sprint 30 rebuilt a fresh `.exe` from the current source. Kept here for forensic comparison. |
| `build_old_2026-05-19/` | PyInstaller intermediates (`*.toc`, `*.pyz`) from the stale build. | Same vintage as the OLD .exe. Kept so we can reproduce the exact stale build if ever needed. |

## What is now the canonical Tally agent

Everything is under `tally_agent/` in the repo root:

- `tally_agent/tally_bridge_agent.py` — the **only** production source. ~1,200 lines: native GUI, WS auth, Tally fetch+push, ingest of ledgers/vouchers/items. Successfully ingested JMK's 2,841 vouchers + 1,604 bank-leg entries end-to-end.
- `tally_agent/tally_bridge_agent.spec` — PyInstaller spec to rebuild the `.exe`.
- `tally_agent/dist/tally_bridge_agent.exe` — the compiled Windows binary served via `/tally_bridge_agent/download`. Rebuilt from current source on every Sprint that edits the `.py`.

## How to rebuild after touching `tally_bridge_agent.py`

```bash
pyinstaller tally_agent/tally_bridge_agent.spec \
            --workpath tally_agent/build \
            --distpath tally_agent/dist \
            --noconfirm
```

The server picks up the new `.exe` on its next download request — no restart needed.

If you want to re-introduce any of the archived files, just `git mv` them back.
