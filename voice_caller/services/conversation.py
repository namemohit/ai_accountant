"""Qualify + book conversation engine.

Stateless: given the campaign script, lead context, and transcript so far,
produces the agent's next line and detects when the call has reached a
terminal outcome.

The brain (LLM) is instructed to append exactly one machine-readable marker
when the call should end:

    [[OUTCOME status=<booked|interested|not_interested|callback>
              interest=<low|medium|high> callback=<when>]]

We strip the marker from what the caller hears (it's never synthesized) and
use it to set the call's outcome + write back the CRM status on the lead.
"""
import re

_OUTCOME_RE = re.compile(r"\[\[OUTCOME([^\]]*)\]\]", re.IGNORECASE)
_KV_RE = re.compile(r"(\w+)\s*=\s*([^=]+?)(?=\s+\w+\s*=|$)")

SYSTEM_TEMPLATE = """You are {agent_name}, a warm, polite Indian voice agent calling on behalf of {company}.
You are speaking on a live phone call, so keep every reply short (1-2 sentences), natural, and conversational. Never sound like you are reading a form.

Goal of this call: {goal}

About the business you are calling:
- Name: {business_name}
- Why they are a good fit: {why_fit}

Your pitch / script guidance:
{script}

Rules:
- Open by greeting them and confirming you are speaking to the right person.
- Qualify their interest naturally; respect a "no" gracefully and end politely.
- If they are interested, propose a specific time and BOOK a follow-up call/meeting.
- Stay in {language}. Do not invent facts about pricing you were not given.
- When the call should end, append on its own at the very end EXACTLY one marker:
  [[OUTCOME status=<booked|interested|not_interested|callback> interest=<low|medium|high> callback=<time or none>]]
  Only add the marker when ending the call. Do not mention the marker out loud."""


def build_system_prompt(campaign: dict, call: dict, agent_name: str = "Asha") -> str:
    return SYSTEM_TEMPLATE.format(
        agent_name=agent_name,
        company=campaign.get("company_name") or campaign.get("company") or "our company",
        goal=campaign.get("goal") or "qualify the lead and book a follow-up call",
        business_name=call.get("business_name") or call.get("name") or "the business",
        why_fit=call.get("why_fit") or "identified as a strong potential customer",
        script=campaign.get("script") or "Introduce who we are and the value we offer.",
        language=campaign.get("language") or "English (India)",
    )


def _messages(system, transcript):
    msgs = [{"role": "system", "content": system}]
    for t in transcript:
        role = "assistant" if t.get("speaker") == "agent" else "user"
        msgs.append({"role": role, "content": t.get("text", "")})
    return msgs


def parse_outcome(text):
    """Extract and strip [[OUTCOME ...]]. Returns (clean_text, outcome|None)."""
    m = _OUTCOME_RE.search(text)
    clean = _OUTCOME_RE.sub("", text).strip()
    if not m:
        return clean, None
    fields = {k.strip().lower(): v.strip() for k, v in _KV_RE.findall(m.group(1))}
    status = (fields.get("status") or "unknown").lower()
    callback = fields.get("callback")
    if callback and callback.lower() in ("none", "na", "n/a", ""):
        callback = None
    return clean, {"status": status,
                   "interest": (fields.get("interest") or "").lower() or None,
                   "callback": callback}


def next_turn(brain, campaign, call, transcript, caller_said):
    """Advance the dialogue by one agent turn.

    `caller_said` is None on the opening turn. Returns:
      {reply, transcript, ended, outcome}.
    """
    system = build_system_prompt(campaign, call)
    if caller_said:
        transcript = transcript + [{"speaker": "caller", "text": caller_said}]
    raw = brain.chat(_messages(system, transcript))
    reply, outcome = parse_outcome(raw)
    transcript = transcript + [{"speaker": "agent", "text": reply}]
    return {"reply": reply, "transcript": transcript,
            "ended": outcome is not None, "outcome": outcome}
