"""Voice Caller — an in-platform L2 agent.

Architecture mirrors Lead Gen (`providers/leads/`): pluggable provider
abstractions + in-process services. No FastAPI sub-app, no separate DB —
schema and routes live in the platform's `db.py` / `server.py`.
"""
