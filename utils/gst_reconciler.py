import csv
import io
from typing import List, Dict, Any

def reconcile_gstr(file_content: bytes, report_type: str, tally_vouchers: List[Dict[str, Any]]):
    """
    Parses a GSTR CSV file and reconciles it against Tally vouchers.
    
    Expected CSV columns (approximate):
    - Invoice Number / Document Number
    - Invoice Value / Total Amount
    - Invoice Date / Document Date
    - Trade Name / Party Name
    - GSTIN
    """
    
    # Read CSV
    decoded_content = file_content.decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(decoded_content))
    
    gstr_records = []
    for row in reader:
        # Normalize keys by lowercasing and stripping
        normalized_row = {k.strip().lower(): v for k, v in row.items() if k}
        
        # Extract key fields with fallbacks
        inv_no = normalized_row.get('invoice number', normalized_row.get('document number', ''))
        
        # Parse Amount
        amount_str = normalized_row.get('invoice value', normalized_row.get('total amount', '0'))
        try:
            amount = float(amount_str.replace(',', '').strip())
        except ValueError:
            amount = 0.0
            
        party_name = normalized_row.get('trade name', normalized_row.get('party name', ''))
        gstin = normalized_row.get('gstin of supplier', normalized_row.get('gstin/uin of recipient', ''))
        date_str = normalized_row.get('invoice date', normalized_row.get('document date', ''))
        
        if inv_no:
            gstr_records.append({
                'invoice_number': inv_no,
                'amount': amount,
                'party_name': party_name,
                'gstin': gstin,
                'date': date_str,
                'source_row': row
            })
            
    # Convert Tally vouchers into a lookup dictionary for fast matching
    tally_lookup = {}
    for tv in tally_vouchers:
        v_num = tv.get('voucher_number', '')
        if v_num:
            tally_lookup[v_num.lower()] = tv

    discrepancies = []
    matched_count = 0
    missing_in_tally = 0
    mismatched_amount = 0
    
    matched_tally_ids = set()

    # Reconcile GSTR against Tally
    for g_rec in gstr_records:
        inv_lower = g_rec['invoice_number'].lower()
        t_rec = tally_lookup.get(inv_lower)
        
        if t_rec:
            matched_tally_ids.add(t_rec['id'])
            
            t_amount = float(t_rec.get('amount', 0.0))
            g_amount = g_rec['amount']
            
            # Check for amount mismatch (allowing 1 rupee tolerance for rounding)
            if abs(t_amount - g_amount) > 1.0:
                mismatched_amount += 1
                discrepancies.append({
                    'status': 'Mismatched Amount',
                    'invoice_number': g_rec['invoice_number'],
                    'party_name': g_rec['party_name'],
                    'gstr_amount': g_amount,
                    'tally_amount': t_amount,
                    'variance': g_amount - t_amount
                })
            else:
                matched_count += 1
                discrepancies.append({
                    'status': 'Matched',
                    'invoice_number': g_rec['invoice_number'],
                    'party_name': g_rec['party_name'],
                    'gstr_amount': g_amount,
                    'tally_amount': t_amount,
                    'variance': 0.0
                })
        else:
            missing_in_tally += 1
            discrepancies.append({
                'status': 'Missing in Tally',
                'invoice_number': g_rec['invoice_number'],
                'party_name': g_rec['party_name'],
                'gstr_amount': g_rec['amount'],
                'tally_amount': None,
                'variance': g_rec['amount']
            })
            
    # Find Tally vouchers missing in GSTR
    missing_in_gstr = 0
    for tv in tally_vouchers:
        if tv['id'] not in matched_tally_ids:
            missing_in_gstr += 1
            discrepancies.append({
                'status': 'Missing in GSTR',
                'invoice_number': tv.get('voucher_number', 'N/A'),
                'party_name': tv.get('ledger_name', 'Unknown'),
                'gstr_amount': None,
                'tally_amount': tv.get('amount', 0.0),
                'variance': -tv.get('amount', 0.0)
            })

    total_gstr = len(gstr_records)
    total_tally = len(tally_vouchers)
    
    return {
        'metrics': {
            'total_gstr': total_gstr,
            'total_tally': total_tally,
            'matched': matched_count,
            'mismatched_amount': mismatched_amount,
            'missing_in_tally': missing_in_tally,
            'missing_in_gstr': missing_in_gstr,
            'match_percentage': round((matched_count / max(total_gstr, 1)) * 100, 1)
        },
        'discrepancies': discrepancies
    }
