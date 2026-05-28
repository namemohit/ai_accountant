import os
from dotenv import load_dotenv
load_dotenv()
import db
import json
from datetime import datetime
from google import generativeai as genai

# Setup key
if os.getenv("GEMINI_API_KEY"):
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# ── Confidence cutoffs for party suggestions (tunable) ──────────────────────
# Cosine similarity (1 - distance) of the best party candidate must clear
# PARTY_MIN_SIM for the AI to suggest a party at all — below it we leave the
# party BLANK and flag the row "Needs Review" rather than blindly proposing the
# nearest party (which produced "Test Vendor on every row" when only one party
# was embedded). At/above PARTY_AUTOFILL_SIM the row is marked "AI Ready".
# Start balanced; calibrate from the match % shown on each row.
PARTY_MIN_SIM = float(os.getenv("RECON_PARTY_MIN_SIM", "0.75"))
PARTY_AUTOFILL_SIM = float(os.getenv("RECON_PARTY_AUTOFILL_SIM", "0.70"))
# Once a party is known, its accounting head is filled DETERMINISTICALLY from the
# party's own voucher history (most-frequent counter-head). Use that head only when
# its frequency share clears HEAD_MIN_CONF; otherwise fall back to the embedding
# candidate / Sales / Suspense default.
HEAD_MIN_CONF = float(os.getenv("RECON_HEAD_MIN_CONF", "0.5"))

def get_reconciliation_embedding(text):
    try:
        result = genai.embed_content(
            model="models/gemini-embedding-2",
            content=text,
            task_type="retrieval_document"
        )
        return result['embedding']
    except Exception as e:
        print(f"Error generating reconciler embedding: {e}")
        return None

def _classify_ledger_role(parent_group, name):
    """Lightweight heuristic: bucket each ledger into one of:
    bank, cash, expense, revenue, asset, liability, party_creditor, party_debtor, tax, other
    Used to constrain Gemini's pick.
    """
    pg = (parent_group or "").lower()
    n = (name or "").lower()
    if any(k in pg for k in ["bank account"]):
        return "bank"
    if "cash" in pg or n == "cash":
        return "cash"
    if any(k in pg for k in ["sundry creditor", "creditor"]):
        return "party_creditor"
    if any(k in pg for k in ["sundry debtor", "debtor"]):
        return "party_debtor"
    if any(k in pg for k in ["sales", "direct income", "indirect income", "income"]):
        return "revenue"
    if any(k in pg for k in ["purchase", "direct expense", "indirect expense", "expense"]):
        return "expense"
    if any(k in pg for k in ["duties & taxes", "duties and taxes", "gst", "tds", "tax"]):
        return "tax"
    if any(k in pg for k in ["current asset", "fixed asset", "investments", "loans & advances", "loans and advances"]):
        return "asset"
    if any(k in pg for k in ["current liabilit", "loan", "secured", "unsecured", "capital"]):
        return "liability"
    return "other"


def _pick_default_bank(bank_ledgers, cash_ledgers, file_hint=""):
    """Pick the most relevant bank ledger based on the bank statement's file/source hint.
    e.g., 'JMK_ICICI_Apr-26.xlsx' → match a ledger containing 'ICICI'.
    """
    hint = (file_hint or "").lower()
    if hint and bank_ledgers:
        # token-overlap scoring
        hint_tokens = set(t for t in hint.replace('_', ' ').replace('-', ' ').replace('.', ' ').split() if len(t) > 2)
        best = None
        best_score = 0
        for lg in bank_ledgers:
            lg_tokens = set(t for t in lg.lower().replace('-', ' ').split() if len(t) > 2)
            score = len(hint_tokens & lg_tokens)
            if score > best_score:
                best_score = score
                best = lg
        if best: return best
    return bank_ledgers[0] if bank_ledgers else (cash_ledgers[0] if cash_ledgers else "Bank Account")


def ai_reconcile_statement(transactions, company_name, company_id=None, progress_cb=None, file_hint=""):
    """Sprint 2 — AI-driven bank reconciliation using vector embeddings + Gemini.

    For each bank txn, finds:
      - suggested_party  (from tally_master_party embeddings + ledger master)
      - suggested_expense_head  (revenue if amount > 0, expense if < 0)
      - suggested_bank_ledger  (matched against tally_master_ledger 'bank' or 'cash' roles)
      - voucher_type  (Receipt if credit, Payment if debit)
      - confidence  (0..1, from vector distance + Gemini)
      - rationale  (short text)
    """
    import db as _db
    tally_vouchers = _db.get_unreconciled_tally_vouchers(company_name)

    # Get all ledgers for this company → use to constrain Gemini's choices
    ledgers = _db.get_ledger_master_for_company(company_id=company_id, company_name=company_name)

    # Pre-classify by role
    bank_ledgers = []
    cash_ledgers = []
    expense_ledgers = []
    revenue_ledgers = []
    party_ledgers = []
    tax_ledgers = []
    for L in ledgers:
        role = _classify_ledger_role(L.get("parent_group"), L.get("name"))
        if role == "bank":
            bank_ledgers.append(L["name"])
        elif role == "cash":
            cash_ledgers.append(L["name"])
        elif role == "expense":
            expense_ledgers.append(L["name"])
        elif role == "revenue":
            revenue_ledgers.append(L["name"])
        elif role in ("party_creditor", "party_debtor"):
            party_ledgers.append(L["name"])
        elif role == "tax":
            tax_ledgers.append(L["name"])

    default_bank = _pick_default_bank(bank_ledgers, cash_ledgers, file_hint=file_hint)
    print(f"[AI RECON] Default bank ledger picked: '{default_bank}'  (file_hint='{file_hint}', bank_ledgers={bank_ledgers[:5]})", flush=True)

    results = []
    total = len(transactions)
    # Real, workspace-scoped count of learned embeddings the vector search will look
    # at — surfaced in the progress UI so it's clear matching uses THIS company's data.
    try:
        embed_count = _db.count_company_tally_embeddings(company_name)
    except Exception:
        embed_count = 0
    print(f"[AI RECON] Starting reconciliation of {total} transactions for {company_name} "
          f"({embed_count} learned embeddings)", flush=True)
    if progress_cb: progress_cb({"phase": "starting", "total": total, "done": 0, "embed_count": embed_count})

    # Deterministic party-name match: if a known party's name appears in the bank
    # narration, that beats the embedding (which is unreliable for boilerplate
    # "NEFT <party> INV/<yr>/<seq>" lines that all look ~72% alike). Build the
    # known-party set once: ledger-master parties + parties the user has taught.
    import re as _re_n
    def _normp(s):
        return _re_n.sub(r'[^A-Z0-9]', '', (s or '').upper())
    _known_parties = set(party_ledgers)
    try:
        _known_parties |= set(_db.get_learned_party_names(company_name))
    except Exception:
        pass
    party_norm = [(p, _normp(p)) for p in _known_parties]
    party_norm = [(p, pn) for (p, pn) in party_norm if len(pn) >= 4]

    # Precompute every party's modal counter-head once (deterministic head fill).
    try:
        party_heads = _db.get_company_party_heads(company_name)
    except Exception as e:
        print(f"[AI RECON] party-head precompute failed: {e}", flush=True)
        party_heads = {}

    # Phase 1 + 2: deterministic + vector retrieval (fast — no Gemini)
    needs_ai = []  # transactions that need Gemini reasoning

    for i, tx in enumerate(transactions):
        tx_date = None
        if tx.get("date"):
            try: tx_date = datetime.strptime(tx["date"], "%Y-%m-%d").date()
            except Exception:
                try: tx_date = datetime.strptime(tx["date"], "%d/%m/%Y").date()
                except Exception: pass

        tx_amount = float(tx.get("amount", 0) or 0)
        tx_desc = (tx.get("description") or "").strip()
        tx_ref = (tx.get("reference") or "").strip()
        is_credit = tx_amount > 0
        voucher_type = "Receipt" if is_credit else "Payment"

        # Phase 1: deterministic
        best_match = None
        for v in tally_vouchers:
            if v.get("reconciled"): continue
            v_amt = float(v.get("amount") or 0)
            if abs(v_amt - abs(tx_amount)) > 0.01: continue
            v_date = v.get("date")
            if isinstance(v_date, str):
                try: v_date = datetime.strptime(v_date, "%Y-%m-%d").date()
                except Exception: v_date = None
            date_diff = abs((tx_date - v_date).days) if tx_date and v_date else 999
            if date_diff > 7:
                continue
            v_inst = (v.get("instrument_number") or "").strip().lower()
            ref_hit = bool(tx_ref) and bool(v_inst) and (tx_ref.lower() in v_inst or v_inst in tx_ref.lower())
            v_ledger_lower = (v.get("ledger_name") or "").strip().lower()
            # Token-based ledger match — require at least one non-trivial token (>3 chars) to overlap
            tokens_desc = set(w for w in tx_desc.lower().replace('/', ' ').replace('.', ' ').split() if len(w) > 3)
            tokens_ledger = set(w for w in v_ledger_lower.replace('/', ' ').replace('.', ' ').split() if len(w) > 3)
            ledger_hit = bool(tokens_desc & tokens_ledger)
            # Require BOTH amount-exact AND (ref overlap OR token overlap). Drop the
            # "amount + ±3 days" lone fallback — it caused false positives in the
            # ICICI test where every line matched the wrong voucher.
            if ref_hit or ledger_hit:
                best_match = v
                break

        if best_match:
            tally_vouchers.remove(best_match)
            results.append({
                "bank_transaction": tx, "status": "auto_matched",
                "suggested_party": best_match.get("ledger_name"),
                "suggested_expense_head": None, "suggested_bank_ledger": default_bank,
                "voucher_type": voucher_type, "confidence": 1.0,
                "tally_voucher_id": best_match.get("id"),
                "voucher_number": best_match.get("voucher_number"),
                "rationale": f"Matched voucher {best_match.get('voucher_number')}",
                "candidate_parties": [], "candidate_heads": [],
                "candidate_bank_ledgers": bank_ledgers + cash_ledgers,
                "candidate_revenue": revenue_ledgers, "candidate_expense": expense_ledgers,
                "all_party_ledgers": party_ledgers,
            })
            if progress_cb: progress_cb({"phase": "phase1", "done": i+1, "total": total, "embed_count": embed_count})
            continue

        # Phase 2: vector retrieval
        query_text = f"Bank transaction: {tx_desc}. Reference: {tx_ref}. Amount: {tx_amount}."
        emb = get_reconciliation_embedding(query_text)
        line_dir = "inflow" if is_credit else "outflow"

        candidate_parties = []
        candidate_heads = []
        candidate_narrations = []
        if emb:
            party_hits = _db.semantic_search_tally(emb, company_name, ['tally_master_party'], limit=5)
            for h in party_hits:
                d = h["data"] or {}
                candidate_parties.append({"name": d.get("party") or d.get("name"), "distance": h["distance"]})
            ledger_hits = _db.semantic_search_tally(emb, company_name, ['tally_master_ledger'], limit=12)
            for h in ledger_hits:
                d = h["data"] or {}
                role = _classify_ledger_role(d.get("parent_group"), d.get("name"))
                wanted = "revenue" if is_credit else "expense"
                if role == wanted:
                    candidate_heads.append({"name": d.get("name"), "distance": h["distance"]})
            narr_hits = _db.semantic_search_tally(emb, company_name, ['tally_master_narration'], limit=3)
            for h in narr_hits:
                d = h["data"] or {}
                candidate_narrations.append({"narration": d.get("narration"), "party": d.get("party")})
            # Type-aware txn channel: merge its parties into candidate_parties (dedupe
            # by name, keep best distance; nudge hits whose direction matches this line).
            txn_hits = _db.semantic_search_tally(emb, company_name, ['tally_master_txn'], limit=5)
            best = {cp["name"]: cp["distance"] for cp in candidate_parties if cp["name"]}
            for h in txn_hits:
                d = h["data"] or {}
                nm = d.get("party")
                if not nm:
                    continue
                dist = h["distance"] - (0.03 if d.get("direction") == line_dir else 0.0)
                if nm not in best or dist < best[nm]:
                    best[nm] = dist
            if best:
                candidate_parties = sorted(
                    ({"name": n, "distance": dd} for n, dd in best.items()),
                    key=lambda x: x["distance"])

        # Deterministic party-name match BEATS the embedding: if a known party's
        # name appears in the narration, use it (the embedding is unreliable for
        # boilerplate "NEFT <party> INV/<yr>/<seq>" lines that all look ~72% alike,
        # so it collapses onto one party). Else fall back to the embedding floor.
        ndesc = _normp(tx_desc + ' ' + tx_ref)
        name_hits = [(p, pn) for (p, pn) in party_norm if pn in ndesc]
        name_party = max(name_hits, key=lambda x: len(x[1]))[0] if name_hits else None

        if name_party:
            party_sim = 0.97
            suggested_party = name_party
            _party_rationale = f"Party name '{name_party}' found in the narration"
        else:
            party_sim = max(0.0, min(1.0, 1.0 - candidate_parties[0]["distance"])) if candidate_parties else 0.0
            suggested_party = candidate_parties[0]["name"] if (candidate_parties and party_sim >= PARTY_MIN_SIM) else ""
            _party_rationale = ("Vector retrieval (pending AI review)" if party_sim >= PARTY_MIN_SIM
                                else f"No confident party match (best {round(party_sim*100)}%) — needs review")
        party_ok = party_sim >= PARTY_MIN_SIM

        # Head: once the party is known, prefer the DETERMINISTIC head from that
        # party's voucher history (modal counter-head) when confident; else fall back
        # to the embedding candidate, then Sales/Suspense. Head confidence is tracked
        # separately from the row's party-match confidence.
        head_conf = 0.0
        head_candidates = []
        head_info = None
        if suggested_party:
            ph = party_heads.get(suggested_party) or {}
            head_info = ph.get(line_dir) or ph.get("any")
        if head_info and head_info.get("head") and head_info.get("confidence", 0) >= HEAD_MIN_CONF:
            suggested_head = head_info["head"]
            head_conf = head_info["confidence"]
            head_candidates = head_info.get("candidates", [])
        else:
            suggested_head = candidate_heads[0]["name"] if candidate_heads else ("Sales Account" if is_credit else "Suspense A/c")

        item = {
            "bank_transaction": tx, "status": "unmatched",
            "suggested_party": suggested_party, "suggested_expense_head": suggested_head,
            "suggested_bank_ledger": default_bank, "voucher_type": voucher_type,
            # confidence now reflects the PARTY-match similarity (what we gate on +
            # show as "match %"); 0 when no confident party was found.
            "confidence": round(party_sim, 3),
            "head_confidence": round(head_conf, 3),
            "head_candidates": head_candidates,
            "tally_voucher_id": None, "voucher_number": None,
            "rationale": _party_rationale,
            "candidate_parties": [p["name"] for p in candidate_parties[:5]],
            "candidate_heads": [h["name"] for h in candidate_heads[:8]],
            "candidate_narrations": candidate_narrations,
            "candidate_bank_ledgers": bank_ledgers + cash_ledgers,
            "candidate_revenue": revenue_ledgers, "candidate_expense": expense_ledgers,
            "all_party_ledgers": party_ledgers,
            "_is_credit": is_credit,
        }

        # Strong party match → AI Ready, skip Gemini. Otherwise let Gemini reason
        # (it may surface a party from the narration, or return "" = needs review).
        if party_ok and party_sim >= PARTY_AUTOFILL_SIM:
            item["status"] = "auto_filled"
            if not name_party:  # keep the "name found in narration" rationale for name-matches
                item["rationale"] = f"High-confidence party match ({round(party_sim*100)}%)"
        else:
            needs_ai.append((len(results), item))  # store position + item

        results.append(item)
        if progress_cb: progress_cb({"phase": "phase2", "done": i+1, "total": total, "embed_count": embed_count})

    print(f"[AI RECON] Phase 1+2 done. {len([r for r in results if r['status']=='auto_matched'])} matched, {len([r for r in results if r['status']=='auto_filled'])} high-confidence, {len(needs_ai)} need Gemini", flush=True)

    # Phase 3: ONE batched Gemini call for everything that needs AI reasoning
    if needs_ai:
        if progress_cb: progress_cb({"phase": "gemini_start", "needs_ai": len(needs_ai), "total": total})
        # Chunk into groups of 25 to keep prompt size manageable
        CHUNK = 25
        for chunk_start in range(0, len(needs_ai), CHUNK):
            chunk = needs_ai[chunk_start:chunk_start + CHUNK]
            print(f"[AI RECON] Gemini batch {chunk_start//CHUNK + 1}: {len(chunk)} lines", flush=True)

            lines_block = []
            for idx, (_, item) in enumerate(chunk):
                tx = item["bank_transaction"]
                lines_block.append({
                    "line_idx": idx,
                    "date": tx.get("date"),
                    "description": tx.get("description", "")[:200],
                    "reference": tx.get("reference", "")[:60],
                    "amount": float(tx.get("amount") or 0),
                    "type": "CREDIT" if item["_is_credit"] else "DEBIT",
                    "candidate_parties": item["candidate_parties"][:5],
                    "candidate_heads": item["candidate_heads"][:8],
                })

            prompt = f"""You are reconciling bank statement lines for {company_name}. For EACH line, pick the BEST party + ledger head ONLY from its candidate lists OR the fallback lists below. Do NOT invent new names.

ALL PARTY LEDGERS (fallback): {json.dumps(party_ledgers[:80], ensure_ascii=False)}
ALL REVENUE HEADS (fallback for credits): {json.dumps(revenue_ledgers[:50], ensure_ascii=False)}
ALL EXPENSE HEADS (fallback for debits): {json.dumps(expense_ledgers[:50], ensure_ascii=False)}

LINES TO RECONCILE:
{json.dumps(lines_block, ensure_ascii=False, indent=1)}

Return ONLY a JSON array, one object per line in the same order, each with:
  "line_idx": <number, matches the input>
  "party": <party ledger name or "">
  "head": <expense/revenue head name>
  "confidence": <0..1>
  "rationale": <one short sentence>

Return the array only, no markdown fences, no commentary.
"""
            try:
                model = genai.GenerativeModel('gemini-flash-latest')
                response = model.generate_content(prompt)
                text = response.text or ""
                import re as _re
                # Find first [...] JSON array
                m = _re.search(r'\[.*\]', text, _re.DOTALL)
                if m:
                    ai_array = json.loads(m.group())
                    for entry in ai_array:
                        li = entry.get("line_idx")
                        if li is None or li >= len(chunk): continue
                        results_pos, item = chunk[li]
                        gconf = None
                        if entry.get("confidence") is not None:
                            gconf = round(float(entry["confidence"]), 3)
                            item["confidence"] = gconf
                        if entry.get("head"): item["suggested_expense_head"] = entry["head"]
                        gparty = (entry.get("party") or "").strip()
                        # Apply the same party floor to Gemini's self-reported
                        # confidence: below PARTY_MIN_SIM we don't trust the party →
                        # leave it blank for human review rather than guess.
                        if gparty and (gconf is None or gconf >= PARTY_MIN_SIM):
                            item["suggested_party"] = gparty
                            if gconf is not None and gconf >= PARTY_AUTOFILL_SIM:
                                item["status"] = "auto_filled"
                            if entry.get("rationale"): item["rationale"] = entry["rationale"]
                            # Gemini may have picked a different party — prefer that
                            # party's deterministic historical head over Gemini's guess.
                            _ldir = "inflow" if item.get("_is_credit") else "outflow"
                            _ph = party_heads.get(gparty) or {}
                            _hi = _ph.get(_ldir) or _ph.get("any")
                            if _hi and _hi.get("head") and _hi.get("confidence", 0) >= HEAD_MIN_CONF:
                                item["suggested_expense_head"] = _hi["head"]
                                item["head_confidence"] = round(_hi["confidence"], 3)
                                item["head_candidates"] = _hi.get("candidates", [])
                        else:
                            item["suggested_party"] = ""
                            item["status"] = "unmatched"
                            item["rationale"] = ("No confident party match"
                                + (f" (best {round(gconf*100)}%)" if gconf is not None else "")
                                + " — needs review")
                        results[results_pos] = item
                print(f"[AI RECON] Gemini batch {chunk_start//CHUNK + 1} processed {len(ai_array) if m else 0} suggestions", flush=True)
            except Exception as e:
                print(f"[AI RECON] Gemini batch error: {e}", flush=True)

            if progress_cb: progress_cb({"phase": "gemini_progress",
                                         "done": min(chunk_start + CHUNK, len(needs_ai)),
                                         "needs_ai": len(needs_ai), "total": total})

    # Clean up internal field before returning
    for r in results:
        r.pop("_is_credit", None)

    print(f"[AI RECON] Complete. Returning {len(results)} reconciled rows.", flush=True)
    if progress_cb: progress_cb({"phase": "done", "total": total})
    return results


def reconcile_statement(transactions, company_name):
    """
    reconcile_statement takes a list of bank transactions:
    [
      {"date": "2026-05-01", "description": "LUXEDECO VENTURES DEP", "reference": "CHQ12345", "amount": 8320.0},
      ...
    ]
    and reconciles them against tally_vouchers and past learnings.
    """
    tally_vouchers = db.get_unreconciled_tally_vouchers(company_name)
    reconciled_list = []

    for tx in transactions:
        tx_date = None
        if tx.get("date"):
            try:
                tx_date = datetime.strptime(tx.get("date"), "%Y-%m-%d").date()
            except:
                try:
                    tx_date = datetime.strptime(tx.get("date"), "%d/%m/%Y").date()
                except:
                    pass
                    
        tx_amount = float(tx.get("amount", 0))
        tx_ref = str(tx.get("reference", "")).strip().lower()
        tx_desc = str(tx.get("description", "")).strip().lower()

        match_found = False
        best_match = None

        # Phase 1: Try to match deterministic tally_vouchers
        for v in tally_vouchers:
            if v.get("reconciled"):
                continue
            
            # Check absolute amount matching
            v_amount = float(v.get("amount", 0))
            if abs(v_amount - abs(tx_amount)) > 0.01:
                continue
            
            # Match factors:
            # 1. Date closeness
            v_date = None
            if v.get("date"):
                if isinstance(v.get("date"), str):
                    try:
                        v_date = datetime.strptime(v.get("date"), "%Y-%m-%d").date()
                    except:
                        pass
                elif hasattr(v.get("date"), "date"):
                    v_date = v.get("date").date()
                else:
                    v_date = v.get("date")
                    
            date_diff = abs((tx_date - v_date).days) if tx_date and v_date else 999
            
            # 2. Ref no matching
            v_inst = str(v.get("instrument_number", "")).strip().lower()
            ref_match = (tx_ref and v_inst and (tx_ref in v_inst or v_inst in tx_ref))
            
            # 3. Description matching
            v_ledger = str(v.get("ledger_name", "")).strip().lower()
            desc_match = (v_ledger in tx_desc or tx_desc in v_ledger)

            # Match criteria
            if (date_diff <= 7 and (ref_match or desc_match)) or (date_diff <= 3):
                best_match = v
                match_found = True
                break

        if match_found and best_match:
            tally_vouchers.remove(best_match)
            reconciled_list.append({
                "bank_transaction": tx,
                "status": "auto_matched",
                "suggested_ledger": best_match.get("ledger_name"),
                "tally_voucher_id": best_match.get("id"),
                "voucher_number": best_match.get("voucher_number"),
                "score": 1.0
            })
            continue

        # Phase 2: RAG / Semantic Matching on Past Corrections
        query_text = f"reconcile ledger mapping for bank narration {tx.get('description')} reference {tx.get('reference')}"
        emb = get_reconciliation_embedding(query_text)
        
        suggested_ledger = "Suspense A/c"
        status = "unmatched"
        
        if emb:
            relevant = db.get_relevant_corrections(emb, limit=3)
            if relevant:
                ledger_votes = {}
                for r in relevant:
                    r_dict = r if isinstance(r, dict) else json.loads(r)
                    l_name = r_dict.get("corrected") or r_dict.get("party_name")
                    if l_name:
                        ledger_votes[l_name] = ledger_votes.get(l_name, 0) + 1
                
                if ledger_votes:
                    suggested_ledger = max(ledger_votes, key=ledger_votes.get)
                    status = "auto_filled"

        reconciled_list.append({
            "bank_transaction": tx,
            "status": status,
            "suggested_ledger": suggested_ledger,
            "tally_voucher_id": None,
            "voucher_number": None,
            "score": 0.5 if status == "auto_filled" else 0.0
        })

    return reconciled_list
