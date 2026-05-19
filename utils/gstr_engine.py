"""
GSTR-1 and GSTR-3B Filing Assistant
═══════════════════════════════════════════════════════════════════════════
Pulls Tally vouchers (sales, purchases) for a given month, classifies them
per GST rules, and produces:
  • GSTR-1 filing-ready data (B2B, B2C-S, B2C-L, HSN, exports, CDNR)
  • GSTR-3B summary (3.1 outward, 4 ITC, 6.1 tax payable)
  • Validation issues (missing GSTINs, invalid POS, rate inconsistencies)
  • GSTN-compliant JSON for offline tool upload

This module operates on the canonical voucher data ingested from Tally —
it doesn't talk to Tally directly. Everything flows through `db.py`.
"""

from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime
from collections import defaultdict
import db


# ─── Helpers ───────────────────────────────────────────────────────────────

# State code → name (subset; can be extended)
STATE_CODE = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "25": "Daman & Diu",
    "26": "Dadra & Nagar Haveli", "27": "Maharashtra", "28": "Andhra Pradesh (Old)",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}
STATE_NAME_TO_CODE = {v.lower(): k for k, v in STATE_CODE.items()}


def gstin_to_state_code(gstin: str) -> Optional[str]:
    if not gstin or len(gstin) < 2:
        return None
    return gstin[:2]


def state_name_to_code(name: str) -> Optional[str]:
    if not name:
        return None
    return STATE_NAME_TO_CODE.get(name.lower().strip())


def is_interstate(supplier_gstin: str, pos_state_code: str) -> bool:
    """Inter-state if supplier state code != place-of-supply state code."""
    s = gstin_to_state_code(supplier_gstin)
    if not s or not pos_state_code:
        return False
    return s != pos_state_code


def is_valid_gstin(g: str) -> bool:
    """Quick structural check — 15 chars: 2 state + 10 PAN + 1 entity + Z + 1 checksum."""
    if not g or len(g) != 15:
        return False
    if not g[:2].isdigit():
        return False
    return True


def parse_month_year(month_str: str) -> Tuple[int, int]:
    """Accepts 'May 2026', '05/2026', '2026-05', '052026', or '052026'."""
    s = (month_str or "").strip()
    formats = ['%b %Y', '%B %Y', '%m/%Y', '%Y-%m', '%m-%Y', '%m%Y']
    for fmt in formats:
        try:
            d = datetime.strptime(s, fmt)
            return d.month, d.year
        except ValueError:
            continue
    # GSTN format mmYYYY
    if len(s) == 6 and s.isdigit():
        return int(s[:2]), int(s[2:])
    today = datetime.now()
    return today.month, today.year


# ─── Voucher fetchers ──────────────────────────────────────────────────────

def get_vouchers_for_period(company_name: str, month: int, year: int,
                             voucher_types: List[str]) -> List[Dict]:
    """Fetch tally_vouchers for the given month/year filtered by type."""
    conn = db.get_conn()
    from psycopg2.extras import RealDictCursor
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT * FROM tally_vouchers
             WHERE company_name = %s
               AND voucher_type = ANY(%s)
               AND EXTRACT(MONTH FROM date) = %s
               AND EXTRACT(YEAR FROM date) = %s
             ORDER BY date, voucher_number
        """, (company_name, voucher_types, month, year))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def get_supplier_gstin(company_name: str) -> Optional[str]:
    """Get the filer's own GSTIN from their party / settings."""
    conn = db.get_conn()
    from psycopg2.extras import RealDictCursor
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Look for it in tally_ledgers where the company itself appears as an entity
        cursor.execute("""
            SELECT gstin FROM tally_ledgers
             WHERE company_name = %s AND gstin IS NOT NULL
             LIMIT 1
        """, (company_name,))
        row = cursor.fetchone()
        return row['gstin'] if row else None
    finally:
        cursor.close()
        conn.close()


# ─── GSTR-1 Computation ────────────────────────────────────────────────────

def compute_gstr1(company_name: str, month: int, year: int,
                   supplier_gstin: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns structure resembling GSTN's offline tool JSON:
      • gstin (filer)
      • fp (filing period, format 'mmYYYY')
      • b2b: list of inv to B2B parties
      • b2cl: list of B2C invoices > ₹2.5L inter-state
      • b2cs: aggregated B2C-Small (consumer, ≤ ₹2.5L or intra-state)
      • hsn: HSN summary
      • cdnr: credit/debit notes (registered)
      • exp: exports (zero-rated)
      • nil: nil/exempt/non-GST outward supplies
      • validation_issues: list of problems
    """
    if not supplier_gstin:
        supplier_gstin = get_supplier_gstin(company_name) or ""
    supplier_state = gstin_to_state_code(supplier_gstin) if supplier_gstin else None

    # Pull all outward vouchers — Sales + Credit/Debit notes
    sales = get_vouchers_for_period(company_name, month, year, ['Sales'])
    credit_notes = get_vouchers_for_period(company_name, month, year, ['Credit Note'])
    debit_notes = get_vouchers_for_period(company_name, month, year, ['Debit Note'])

    b2b = defaultdict(lambda: {"ctin": "", "inv": []})
    b2cl: List[Dict] = []
    b2cs_agg = defaultdict(lambda: {
        "sply_ty": "INTRA", "pos": supplier_state, "typ": "OE",
        "txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0
    })
    hsn_summary = defaultdict(lambda: {
        "hsn_sc": "", "desc": "", "uqc": "", "qty": 0.0,
        "txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0
    })
    cdnr: List[Dict] = []
    nil_supplies = {"nil_amt": 0.0, "expt_amt": 0.0, "ngsup_amt": 0.0}

    issues: List[Dict] = []

    for v in sales:
        gstin = v.get("party_gstin") or ""
        party = v.get("ledger_name") or "Cash"
        invoice_no = v.get("voucher_number") or ""
        invoice_date = v.get("date")
        taxable = float(v.get("taxable_value") or 0)
        cgst = float(v.get("cgst_amount") or 0)
        sgst = float(v.get("sgst_amount") or 0)
        igst = float(v.get("igst_amount") or 0)
        total = float(v.get("amount") or 0)
        pos_state_name = v.get("place_of_supply") or ""
        pos_state_code = state_name_to_code(pos_state_name) or supplier_state

        # If taxable is zero, derive from total (assume 18% standard if no breakup)
        if taxable == 0 and total > 0 and (cgst == 0 and sgst == 0 and igst == 0):
            issues.append({
                "voucher": invoice_no, "date": str(invoice_date),
                "severity": "warning",
                "message": "No tax breakup found — manual GST classification needed."
            })

        # Determine GST rate
        rate = 0.0
        if taxable > 0:
            tax_total = cgst + sgst + igst
            rate = round(tax_total / taxable * 100, 2) if tax_total > 0 else 0.0

        # Classify
        if is_valid_gstin(gstin):
            # B2B
            inv = {
                "inum": invoice_no,
                "idt": str(invoice_date),
                "val": round(total, 2),
                "pos": pos_state_code or "",
                "rchrg": "N",
                "inv_typ": "R",
                "itms": [{
                    "num": 1,
                    "itm_det": {
                        "txval": round(taxable, 2),
                        "rt": rate,
                        "iamt": round(igst, 2),
                        "camt": round(cgst, 2),
                        "samt": round(sgst, 2),
                        "csamt": 0.0
                    }
                }]
            }
            b2b[gstin]["ctin"] = gstin
            b2b[gstin]["inv"].append(inv)
        else:
            # B2C
            inter_state = (pos_state_code and supplier_state and pos_state_code != supplier_state)
            if inter_state and total > 250000:
                # B2CL
                b2cl.append({
                    "pos": pos_state_code,
                    "inv": [{
                        "inum": invoice_no,
                        "idt": str(invoice_date),
                        "val": round(total, 2),
                        "itms": [{
                            "num": 1,
                            "itm_det": {
                                "txval": round(taxable, 2),
                                "rt": rate,
                                "iamt": round(igst, 2),
                                "csamt": 0.0
                            }
                        }]
                    }]
                })
            else:
                # B2CS
                k = (pos_state_code or supplier_state or "", rate, "INTER" if inter_state else "INTRA")
                bucket = b2cs_agg[k]
                bucket["sply_ty"] = k[2]
                bucket["pos"] = k[0]
                bucket["txval"] += taxable
                bucket["iamt"] += igst
                bucket["camt"] += cgst
                bucket["samt"] += sgst

        # HSN summary aggregate (any voucher contributes if it has line items)
        # If line_items aren't recorded at voucher level, we approximate one row
        # per voucher type. A real-world version would use the item-level data.
        hsn_key = "9999"  # fallback HSN
        hsn_summary[hsn_key]["hsn_sc"] = hsn_key
        hsn_summary[hsn_key]["desc"] = "Misc"
        hsn_summary[hsn_key]["txval"] += taxable
        hsn_summary[hsn_key]["iamt"] += igst
        hsn_summary[hsn_key]["camt"] += cgst
        hsn_summary[hsn_key]["samt"] += sgst

        # Validations
        if total > 0 and not gstin and not pos_state_code:
            issues.append({
                "voucher": invoice_no, "date": str(invoice_date),
                "severity": "error",
                "message": "Missing Place of Supply on a B2C invoice."
            })
        if gstin and not is_valid_gstin(gstin):
            issues.append({
                "voucher": invoice_no, "date": str(invoice_date),
                "severity": "error",
                "message": f"Invalid GSTIN format: {gstin}"
            })
        if supplier_state and pos_state_code:
            inter = supplier_state != pos_state_code
            if inter and (cgst > 0 or sgst > 0):
                issues.append({
                    "voucher": invoice_no, "date": str(invoice_date),
                    "severity": "error",
                    "message": "Inter-state sale charged CGST/SGST instead of IGST."
                })
            if (not inter) and igst > 0:
                issues.append({
                    "voucher": invoice_no, "date": str(invoice_date),
                    "severity": "error",
                    "message": "Intra-state sale charged IGST instead of CGST+SGST."
                })

    # Credit notes → cdnr
    for cn in credit_notes + debit_notes:
        gstin = cn.get("party_gstin") or ""
        if not is_valid_gstin(gstin):
            continue  # only registered CDNR here
        cdnr.append({
            "ctin": gstin,
            "nt": [{
                "ntty": "C" if cn.get("voucher_type") == "Credit Note" else "D",
                "nt_num": cn.get("voucher_number"),
                "nt_dt": str(cn.get("date")),
                "val": round(float(cn.get("amount") or 0), 2),
                "itms": [{
                    "num": 1,
                    "itm_det": {
                        "txval": round(float(cn.get("taxable_value") or 0), 2),
                        "iamt": round(float(cn.get("igst_amount") or 0), 2),
                        "camt": round(float(cn.get("cgst_amount") or 0), 2),
                        "samt": round(float(cn.get("sgst_amount") or 0), 2)
                    }
                }]
            }]
        })

    fp = f"{month:02d}{year}"
    totals = {
        "total_invoices": len(sales),
        "total_outward_value": sum(float(v.get("amount") or 0) for v in sales),
        "total_taxable": sum(float(v.get("taxable_value") or 0) for v in sales),
        "total_cgst": sum(float(v.get("cgst_amount") or 0) for v in sales),
        "total_sgst": sum(float(v.get("sgst_amount") or 0) for v in sales),
        "total_igst": sum(float(v.get("igst_amount") or 0) for v in sales),
        "b2b_count": sum(len(v["inv"]) for v in b2b.values()),
        "b2cl_count": sum(len(b["inv"]) for b in b2cl),
        "b2cs_buckets": len(b2cs_agg),
        "credit_notes_count": len(credit_notes),
        "debit_notes_count": len(debit_notes),
    }

    return {
        "gstin": supplier_gstin,
        "fp": fp,
        "filing_period": f"{month:02d}/{year}",
        "b2b": list(b2b.values()),
        "b2cl": b2cl,
        "b2cs": list(b2cs_agg.values()),
        "hsn": {"data": list(hsn_summary.values())},
        "cdnr": cdnr,
        "nil": nil_supplies,
        "totals": totals,
        "validation_issues": issues,
    }


# ─── GSTR-3B Computation ───────────────────────────────────────────────────

def compute_gstr3b(company_name: str, month: int, year: int,
                    supplier_gstin: Optional[str] = None) -> Dict[str, Any]:
    """
    GSTR-3B summary tables:
      • Table 3.1: Outward + RCM supplies
      • Table 4: ITC available + reversed + net
      • Table 6.1: Tax payable (with cash + credit utilization)
    """
    if not supplier_gstin:
        supplier_gstin = get_supplier_gstin(company_name) or ""

    sales = get_vouchers_for_period(company_name, month, year, ['Sales'])
    purchases = get_vouchers_for_period(company_name, month, year, ['Purchase'])

    # Table 3.1: Outward supplies
    table_31 = {
        "a_outward_taxable_other_than_zero": {
            "txval": sum(float(v.get("taxable_value") or 0) for v in sales),
            "iamt": sum(float(v.get("igst_amount") or 0) for v in sales),
            "camt": sum(float(v.get("cgst_amount") or 0) for v in sales),
            "samt": sum(float(v.get("sgst_amount") or 0) for v in sales),
            "csamt": 0.0
        },
        "b_outward_zero_rated": {"txval": 0.0, "iamt": 0.0, "csamt": 0.0},
        "c_other_outward_nil_exempt_non_gst": {"nil_amt": 0.0, "expt_amt": 0.0, "ngsup_amt": 0.0},
        "d_inward_supplies_liable_rcm": {"txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0},
        "e_non_gst_outward": {"txval": 0.0},
    }

    # Table 4: ITC
    purchase_cgst = sum(float(v.get("cgst_amount") or 0) for v in purchases)
    purchase_sgst = sum(float(v.get("sgst_amount") or 0) for v in purchases)
    purchase_igst = sum(float(v.get("igst_amount") or 0) for v in purchases)

    table_4 = {
        "a_itc_available": {
            "1_import_goods": {"iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "2_import_services": {"iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "3_inward_rcm": {"iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "4_inward_isd": {"iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "5_all_other_itc": {
                "iamt": purchase_igst,
                "camt": purchase_cgst,
                "samt": purchase_sgst,
                "csamt": 0
            }
        },
        "b_itc_reversed": {
            "1_rule_42_43": {"iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "2_others": {"iamt": 0, "camt": 0, "samt": 0, "csamt": 0}
        },
        "c_net_itc_available": {
            "iamt": purchase_igst,
            "camt": purchase_cgst,
            "samt": purchase_sgst,
            "csamt": 0
        },
        "d_ineligible_itc": {
            "1_rule_42_43": {"iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "2_others": {"iamt": 0, "camt": 0, "samt": 0, "csamt": 0}
        }
    }

    # Table 6.1: Tax payable (output - ITC = cash)
    output_cgst = table_31["a_outward_taxable_other_than_zero"]["camt"]
    output_sgst = table_31["a_outward_taxable_other_than_zero"]["samt"]
    output_igst = table_31["a_outward_taxable_other_than_zero"]["iamt"]

    net_cgst = max(0, output_cgst - purchase_cgst)
    net_sgst = max(0, output_sgst - purchase_sgst)
    net_igst = max(0, output_igst - purchase_igst)

    table_61 = {
        "iamt_total": round(output_igst, 2),
        "camt_total": round(output_cgst, 2),
        "samt_total": round(output_sgst, 2),
        "iamt_itc": round(purchase_igst, 2),
        "camt_itc": round(purchase_cgst, 2),
        "samt_itc": round(purchase_sgst, 2),
        "iamt_cash": round(net_igst, 2),
        "camt_cash": round(net_cgst, 2),
        "samt_cash": round(net_sgst, 2),
        "total_tax_payable_cash": round(net_cgst + net_sgst + net_igst, 2),
    }

    summary = {
        "outward_taxable_value": round(table_31["a_outward_taxable_other_than_zero"]["txval"], 2),
        "output_tax_total": round(output_cgst + output_sgst + output_igst, 2),
        "itc_available_total": round(purchase_cgst + purchase_sgst + purchase_igst, 2),
        "net_tax_payable_cash": table_61["total_tax_payable_cash"],
        "sales_invoices": len(sales),
        "purchase_invoices": len(purchases),
    }

    return {
        "gstin": supplier_gstin,
        "fp": f"{month:02d}{year}",
        "filing_period": f"{month:02d}/{year}",
        "table_31_outward_supplies": table_31,
        "table_4_itc": table_4,
        "table_61_tax_payable": table_61,
        "summary": summary,
    }


# ─── Filing JSON Export (GSTN offline tool compatible format) ──────────────

def gstr1_to_gstn_json(gstr1: Dict) -> Dict:
    """Trim our internal structure to the format the GSTN offline tool expects."""
    return {
        "gstin": gstr1.get("gstin"),
        "fp": gstr1.get("fp"),
        "version": "GST3.0.4",
        "hash": "hash",
        "b2b": gstr1.get("b2b") or [],
        "b2cl": gstr1.get("b2cl") or [],
        "b2cs": gstr1.get("b2cs") or [],
        "cdnr": gstr1.get("cdnr") or [],
        "hsn": gstr1.get("hsn") or {"data": []},
        "nil": gstr1.get("nil") or {},
    }


def gstr3b_to_gstn_json(gstr3b: Dict) -> Dict:
    return {
        "gstin": gstr3b.get("gstin"),
        "ret_period": gstr3b.get("fp"),
        "sup_details": {
            "osup_det": gstr3b["table_31_outward_supplies"]["a_outward_taxable_other_than_zero"],
            "osup_zero": gstr3b["table_31_outward_supplies"]["b_outward_zero_rated"],
            "osup_nil_exmp": gstr3b["table_31_outward_supplies"]["c_other_outward_nil_exempt_non_gst"],
            "isup_rev": gstr3b["table_31_outward_supplies"]["d_inward_supplies_liable_rcm"],
            "osup_nongst": gstr3b["table_31_outward_supplies"]["e_non_gst_outward"]
        },
        "itc_elg": gstr3b["table_4_itc"],
        "tx_pmt_amt": gstr3b["table_61_tax_payable"],
    }
