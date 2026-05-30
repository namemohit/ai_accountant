"""One-shot backfill: link existing tally_vouchers rows to their YantrAI
invoice twin by retro-stamping `yantrai_uid`. Sprint 46 — for legacy
workspaces (JMK + every workspace whose Tally history was synced before
Sprint 35-39 introduced the `[YAI:uid]` narration round-trip marker).

Why: Sprint 45's Reco column reads tally_vouchers.yantrai_uid to detect
whether a YantrAI invoice has landed in Tally. For invoices that were
pushed BEFORE the marker existed, the link is missing and Reco shows
the misleading "⊘ Not in Tally" for everything. This script walks the
unlinked tally_vouchers rows for one company and links the unambiguous
ones, so Reco starts reflecting reality.

Matching (per invoice, priority order):
  Tier 1 — exact voucher_number match → unique row → link
  Tier 2 — same party (substring, case-ins) + same date + |Δamount|≤₹0.50
           → unique row → link
  Tier 3 — outbox.tally_voucher_guid matches tally_vouchers.tally_master_id

Safety:
  - Dry-run by default. Prints decisions, writes nothing.
  - `--apply` flag to mutate. All UPDATEs in one transaction.
  - Per-company scoping (required `--company`).
  - Idempotent — WHERE yantrai_uid IS NULL skips already-linked rows.
  - Audit trail in tally_cleanup_log (kind='backfill_yantrai_uid').

Usage:
  python _backfill_yantrai_uid.py --company "Jai Mata Kalka Enterprises"
  python _backfill_yantrai_uid.py --company "Jai Mata Kalka Enterprises" --apply
  python _backfill_yantrai_uid.py --company "Jai Mata Kalka Enterprises" --apply --verbose
"""

import argparse
import sys
from datetime import date

# Force UTF-8 stdout so emoji + ₹ render on Windows cp1252 terminals.
try:
    sys.stdout.reconfigure(encoding='utf-8')   # type: ignore[attr-defined]
except Exception:
    pass

import db


def _normalize_date(value):
    """Accept the various date formats invoices.date / tally_vouchers.date
    may carry (YYYYMMDD text, YYYY-MM-DD text, date obj) and return a
    Python date for cross-table compares. None on failure."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _f(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def find_candidates(cur, company_name, inv_id, inv_number, inv_party, inv_date, inv_amount):
    """Return (tier, candidates, why) for one invoice. `candidates` is a list
    of (tv_id, tv_amount, tv_ledger_name) tuples. tier=0 means no candidates."""
    inv_amount = _f(inv_amount)
    norm_date = _normalize_date(inv_date)

    # Tier 1 — exact voucher_number match (unlinked rows only).
    cur.execute("""SELECT id, amount, ledger_name FROM tally_vouchers
                   WHERE company_name = %s
                     AND voucher_number = %s
                     AND yantrai_uid IS NULL""",
                (company_name, inv_number))
    t1 = cur.fetchall()
    if t1:
        if len(t1) == 1:
            return 1, t1, "exact voucher_number match"
        # Multiple — narrow by amount.
        narrowed = [r for r in t1 if abs(_f(r[1]) - inv_amount) <= 0.01]
        if len(narrowed) == 1:
            return 1, narrowed, "voucher_number + amount tiebreak"
        # Genuinely ambiguous — record + skip.
        return 1, t1, f"ambiguous voucher_number ({len(t1)} candidates)"

    # Tier 2 — party + date + amount signature.
    p_low = (inv_party or "").lower().strip()
    if p_low and norm_date:
        # Keep the LIKE selective: the first 24 chars of the party name are
        # almost always enough to disambiguate; using the full string risks
        # matching nothing when Tally trims trailing whitespace differently.
        like_pattern = f"%{p_low[:24]}%"
        cur.execute("""SELECT id, amount, ledger_name FROM tally_vouchers
                       WHERE company_name = %s
                         AND yantrai_uid IS NULL
                         AND date = %s
                         AND ABS(amount - %s) <= 0.50
                         AND (LOWER(ledger_name) LIKE %s
                              OR %s LIKE '%%' || LOWER(ledger_name) || '%%')""",
                    (company_name, norm_date, inv_amount, like_pattern, p_low))
        t2 = cur.fetchall()
        if t2:
            if len(t2) == 1:
                return 2, t2, "party + date + amount signature match"
            return 2, t2, f"ambiguous signature ({len(t2)} candidates)"

    # Tier 3 — outbox-recorded GUID, matched against tally_vouchers.tally_master_id.
    cur.execute("""SELECT MAX(tally_voucher_guid) FROM tally_outbox
                   WHERE company_name = %s
                     AND payload->>'invoice_number' = %s
                     AND state = 'pushed'
                     AND tally_voucher_guid IS NOT NULL""",
                (company_name, inv_number))
    guid = (cur.fetchone() or [None])[0]
    if guid and len(guid) > 4 and "-" in guid:    # skip the synthetic reconcile sentinels (raw LASTVCHID ints)
        cur.execute("""SELECT id, amount, ledger_name FROM tally_vouchers
                       WHERE company_name = %s
                         AND tally_master_id = %s
                         AND yantrai_uid IS NULL""",
                    (company_name, guid))
        t3 = cur.fetchall()
        if len(t3) == 1:
            return 3, t3, f"outbox.tally_voucher_guid → tally_master_id"

    return 0, [], "no candidates"


def main():
    ap = argparse.ArgumentParser(description="Retro-link tally_vouchers to YantrAI invoices.")
    ap.add_argument("--company", required=True, help="Target workspace company_name.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the yantrai_uid links + audit rows. Without this flag the script only prints decisions.")
    ap.add_argument("--verbose", action="store_true",
                    help="Per-invoice diagnostic detail.")
    args = ap.parse_args()

    co = args.company
    print(f"Backfill yantrai_uid for company: {co}")

    conn = db.pget()
    cur = conn.cursor()

    # Inventory.
    cur.execute("SELECT COUNT(*) FROM invoices WHERE company_name = %s", (co,))
    inv_total = cur.fetchone()[0]
    cur.execute("""SELECT COUNT(*) FROM tally_vouchers
                   WHERE company_name = %s AND yantrai_uid IS NULL""", (co,))
    tv_unlinked = cur.fetchone()[0]
    cur.execute("""SELECT COUNT(*) FROM tally_vouchers
                   WHERE company_name = %s AND yantrai_uid IS NOT NULL""", (co,))
    tv_already = cur.fetchone()[0]
    print(f"  invoices: {inv_total}")
    print(f"  tally_vouchers: {tv_unlinked} unlinked + {tv_already} already linked")
    print()

    if inv_total == 0:
        print("No invoices in this workspace — nothing to do.")
        cur.close(); db.pput(conn)
        return

    # Walk invoices in created_at order — newest first matches the UI.
    cur.execute("""SELECT id, invoice_number, party_name, date, total_amount
                   FROM invoices WHERE company_name = %s
                   ORDER BY created_at DESC""", (co,))
    invoices = cur.fetchall()

    linked = []          # list of (tier, why, inv_row, tv_row)
    ambiguous = []       # list of (tier, why, inv_row, [tv_rows])
    unmatched = []       # list of inv_row

    print("Walking invoices…")
    for inv_id, num, party, dt, amt in invoices:
        # Idempotence guard: if ANY tally_vouchers row in this company already
        # carries this invoice's id as yantrai_uid, the invoice is linked and
        # the matcher must NOT search for additional candidates — otherwise a
        # second run would attach a Tier 2 false positive to a row that's
        # already cleanly matched by Tier 1. Caught when re-running the script
        # on JMK after the first apply.
        cur.execute("""SELECT 1 FROM tally_vouchers
                       WHERE company_name = %s AND yantrai_uid = %s LIMIT 1""",
                    (co, str(inv_id)))
        if cur.fetchone():
            if args.verbose:
                amt_str = f"₹{_f(amt):>14,.2f}"
                party_short = (party or "")[:30]
                print(f"  {num:24} {party_short:30} {amt_str}  → already linked, skipping")
            continue
        tier, cands, why = find_candidates(cur, co, inv_id, num, party, dt, amt)
        amt_str = f"₹{_f(amt):>14,.2f}"
        party_short = (party or "")[:30]
        if tier == 0:
            unmatched.append((inv_id, num, party, dt, amt))
            print(f"  {num:24} {party_short:30} {amt_str}  → no candidates")
        elif len(cands) == 1:
            linked.append((tier, why, (inv_id, num, party, dt, amt), cands[0]))
            tv_id, tv_amt, tv_ledger = cands[0]
            extra = ""
            if abs(_f(amt) - _f(tv_amt)) > 0.01:
                extra = f"  ⚠️ amount drift: tally ₹{_f(tv_amt):,.2f}"
            print(f"  {num:24} {party_short:30} {amt_str}  → Tier {tier} ({why}) tv.id={tv_id}{extra}")
        else:
            ambiguous.append((tier, why, (inv_id, num, party, dt, amt), cands))
            print(f"  {num:24} {party_short:30} {amt_str}  → AMBIGUOUS Tier {tier} ({why})")
            if args.verbose:
                for tv_id, tv_amt, tv_ledger in cands[:5]:
                    print(f"        cand tv.id={tv_id}  ₹{_f(tv_amt):,.2f}  {(tv_ledger or '')[:50]}")

    # Summary.
    by_tier = {1: 0, 2: 0, 3: 0}
    for t, _, _, _ in linked:
        by_tier[t] = by_tier.get(t, 0) + 1
    print()
    print("Summary:")
    print(f"  {len(linked)} candidate link(s) ready  (Tier 1: {by_tier.get(1,0)}  Tier 2: {by_tier.get(2,0)}  Tier 3: {by_tier.get(3,0)})")
    print(f"  {len(unmatched)} unmatched")
    print(f"  {len(ambiguous)} ambiguous (need a manual call — skipped)")

    if not args.apply:
        print()
        print("DRY-RUN only — re-run with --apply to write the links.")
        cur.close(); db.pput(conn)
        return

    # Apply phase — single transaction wrapping all UPDATEs + audit rows.
    print()
    print(f"APPLYING {len(linked)} link(s)…")
    if not linked:
        print("Nothing to apply.")
        cur.close(); db.pput(conn)
        return

    try:
        for tier, why, inv_row, tv_row in linked:
            inv_id, inv_num, inv_party, inv_dt, inv_amt = inv_row
            tv_id, tv_amt, tv_ledger = tv_row
            cur.execute("""UPDATE tally_vouchers
                           SET yantrai_uid = %s,
                               origin = 'yantrai'
                           WHERE id = %s AND yantrai_uid IS NULL""",
                        (str(inv_id), tv_id))
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"Concurrent modification on tally_vouchers id={tv_id} "
                    f"(rowcount={cur.rowcount}). Aborting transaction.")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"FAILED — transaction rolled back: {e}")
        cur.close(); db.pput(conn)
        sys.exit(1)
    print(f"  wrote {len(linked)} UPDATEs (committed).")

    # Audit rows — best-effort (failures here don't undo the links).
    audit_ok = 0
    for tier, why, inv_row, tv_row in linked:
        inv_id, inv_num, inv_party, inv_dt, inv_amt = inv_row
        tv_id, tv_amt, _ = tv_row
        try:
            db.add_tally_cleanup(
                company_name=co,
                voucher_number=inv_num,
                voucher_type=None,
                party=inv_party,
                amount=_f(inv_amt),
                voucher_date=str(inv_dt) if inv_dt else None,
                reason=f"Sprint 46 backfill — linked tally_vouchers.id={tv_id} via Tier {tier} ({why}); invoice.id={inv_id}",
                kind="backfill_yantrai_uid")
            audit_ok += 1
        except Exception as e:
            print(f"  audit insert failed for {inv_num}: {e}")
    print(f"  wrote {audit_ok}/{len(linked)} audit rows in tally_cleanup_log.")

    # Post-apply Reco-flag preview — re-import db's _derive_reco_flag and
    # show what the Vouchers page will read now.
    print()
    print("Post-apply Reco distribution preview:")
    cur.execute("""SELECT i.id, i.total_amount, i.cgst_amount, i.sgst_amount, i.igst_amount,
                          tv.cnt, tv.amt, tv.disc, tv.cgst, tv.sgst, tv.igst
                   FROM invoices i
                   LEFT JOIN LATERAL (
                     SELECT COUNT(*) AS cnt, MAX(amount) AS amt, MAX(discarded_at) AS disc,
                            MAX(cgst_amount) AS cgst, MAX(sgst_amount) AS sgst, MAX(igst_amount) AS igst
                     FROM tally_vouchers
                     WHERE yantrai_uid = i.id::text AND company_name = i.company_name
                   ) tv ON TRUE
                   WHERE i.company_name = %s""", (co,))
    from collections import Counter
    counter = Counter()
    for row in cur.fetchall():
        iid, amt, cgst, sgst, igst, cnt, tv_amt, tv_disc, tv_cgst, tv_sgst, tv_igst = row
        synth = {
            'source': 'invoice',
            'amount': amt, 'cgst_amount': cgst, 'sgst_amount': sgst, 'igst_amount': igst,
            'tv_count': cnt or 0, 'tv_amount': tv_amt, 'tv_discarded_at': tv_disc,
            'tv_cgst_amount': tv_cgst, 'tv_sgst_amount': tv_sgst, 'tv_igst_amount': tv_igst,
        }
        counter[db._derive_reco_flag(synth)] += 1
    for k, v in counter.most_common():
        print(f"  {k:24} {v}")

    cur.close()
    db.pput(conn)


if __name__ == "__main__":
    main()
