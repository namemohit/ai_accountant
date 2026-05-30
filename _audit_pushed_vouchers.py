"""
Sprint 53.1 — read-only correction worksheet for vouchers YantrAI PUSHED to Tally
with an inverted debit/credit AMOUNT sign (the v0.19.x agent bug in
_build_voucher_xml: Dr legs emitted positive, Cr legs negative — so Sales landed
in Tally's CREDIT column, party credited instead of debited).

SCOPE = the authoritative push list: tally_outbox rows with state='pushed'.
Native vouchers entered directly in Tally are correct and are NOT touched. Every
outbox-pushed voucher went through the buggy builder, so each is uniformly
inverted; the correction is to negate every leg (Dr<->Cr).

STRICTLY READ-ONLY. Writes nothing. Tally's XML API can't reliably alter/delete a
pushed voucher (Sprint 44), so the user re-enters these correctly in Tally Prime.

Usage:
    python _audit_pushed_vouchers.py --company "Jai Mata Kalka Enterprises"
    python _audit_pushed_vouchers.py --company "Jai Mata Kalka Enterprises" --csv out.csv
"""
import argparse, json, csv
import db


def _legs(le_text):
    try:
        legs = json.loads(le_text) if le_text else []
    except Exception:
        legs = []
    out = []
    for L in legs:
        nm = (L.get("ledger") or L.get("ledger_name") or "").strip()
        try:
            amt = round(float(L.get("amount") or 0), 2)
        except (TypeError, ValueError):
            amt = 0.0
        if nm:
            out.append((nm, amt))
    return out


def _looks_inverted(vtype, party, legs):
    """For a Sales voucher, the customer/party leg should be NEGATIVE (Dr). If it's
    positive, the voucher is still inverted (not yet corrected)."""
    vt = (vtype or "").strip().lower()
    p = (party or "").strip().lower()
    for nm, amt in legs:
        if nm.strip().lower() == p:
            if vt in ("sales", "sale"):
                return amt > 0
            if vt in ("purchase",):
                return amt < 0
            return None  # Payment/Receipt party leg semantics vary; can't assert
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()
    CO = args.company

    conn = db.get_conn()
    cur = conn.cursor()
    # Authoritative: what YantrAI actually pushed.
    cur.execute("""
        SELECT DISTINCT COALESCE(payload->>'invoice_number', payload->>'voucher_number')
        FROM tally_outbox WHERE company_name = %s AND state = 'pushed'
    """, (CO,))
    pushed_nums = sorted({r[0] for r in cur.fetchall() if r[0]})

    report = []
    for vn in pushed_nums:
        cur.execute("""SELECT voucher_type, date, ledger_name, amount, ledger_entries::text
                       FROM tally_vouchers WHERE company_name=%s AND voucher_number=%s
                       ORDER BY (discarded_at IS NULL) DESC LIMIT 1""", (CO, vn))
        r = cur.fetchone()
        if not r:
            report.append((vn, None, None, None, None, [], None))
            continue
        vt, vdate, party, amt, le = r
        legs = _legs(le)
        report.append((vn, vt, str(vdate), party, amt, legs, _looks_inverted(vt, party, legs)))
    cur.close(); conn.close()

    print(f"\n=== Dr/Cr correction worksheet — {CO} ===")
    print(f"YantrAI pushed {len(pushed_nums)} voucher(s) (tally_outbox state='pushed').")
    print("All were posted by the v0.19.x builder with INVERTED Dr/Cr. Re-enter each")
    print("in Tally Prime using the CORRECTED legs below (then delete the wrong one).\n")

    inv = 0
    for vn, vt, vdate, party, amt, legs, looks_inv in report:
        print("-" * 72)
        if vt is None:
            print(f"{vn}  — not found in tally_vouchers (pull may be stale; re-import to confirm)")
            continue
        tag = "" if looks_inv is None else ("  [still inverted]" if looks_inv else "  [already looks correct]")
        if looks_inv:
            inv += 1
        print(f"{vn}  [{vt}]  {vdate}  party={party}  total~{amt}{tag}")
        print("  AS POSTED (WRONG):")
        for nm, a in legs:
            print(f"      {('Cr' if a>0 else 'Dr')}  {nm[:34]:34s} {abs(a):>14,.2f}")
        print("  CORRECTED (re-enter as):")
        for nm, a in legs:
            ca = -a
            print(f"      {('Cr' if ca>0 else 'Dr')}  {nm[:34]:34s} {abs(ca):>14,.2f}")

    print("\n" + "=" * 72)
    print(f"{len(pushed_nums)} pushed; {inv} still inverted (Sales party on the wrong side).")
    print("NOTE: pushed Sales used ledger 'Sales Account' (vs the firm's native 'Sale')")
    print("and Output CGST/SGST or IGST Tax — verify ledger names while re-entering.")
    print("Also clean the leaked duplicate 'JMK/2026-27/047-TEST' (Phase 2).")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["voucher_number", "type", "date", "party", "leg_ledger",
                        "as_posted", "as_posted_amt", "corrected", "corrected_amt"])
            for vn, vt, vdate, party, amt, legs, _ in report:
                for nm, a in legs:
                    w.writerow([vn, vt, vdate, party, nm,
                                "Cr" if a > 0 else "Dr", f"{abs(a):.2f}",
                                "Cr" if -a > 0 else "Dr", f"{abs(a):.2f}"])
        print(f"CSV written: {args.csv}")


if __name__ == "__main__":
    main()
