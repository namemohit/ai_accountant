import csv
import io
from typing import List, Dict, Any

def reconcile_revenue(file_content: bytes, gateway_type: str, tally_vouchers: List[Dict[str, Any]]):
    """
    Parses a Payment Gateway/Settlement CSV (Razorpay, Stripe, UPI, POS)
    and reconciles it against Tally sales vouchers.
    """
    # Decode content robustly
    try:
        decoded_content = file_content.decode('utf-8-sig')
    except UnicodeDecodeError:
        decoded_content = file_content.decode('latin1')
        
    reader = csv.DictReader(io.StringIO(decoded_content))
    
    gateway_records = []
    for row in reader:
        # Normalize keys by lowercasing and stripping
        normalized_row = {k.strip().lower(): v for k, v in row.items() if k}
        
        # Extract Order/Invoice/Payment ID
        order_id = normalized_row.get('order id', normalized_row.get('invoice number', normalized_row.get('payment id', normalized_row.get('transaction id', normalized_row.get('id', normalized_row.get('receipt', ''))))))
        
        # Parse Gross Amount
        amount_str = normalized_row.get('amount', normalized_row.get('gross', normalized_row.get('settlement amount', normalized_row.get('total', normalized_row.get('credit', '0')))))
        try:
            amount = float(amount_str.replace(',', '').strip())
        except ValueError:
            amount = 0.0
            
        # Parse Fee
        fee_str = normalized_row.get('fee', normalized_row.get('fees', normalized_row.get('deduction', normalized_row.get('tax', normalized_row.get('charges', '0')))))
        try:
            fee = float(fee_str.replace(',', '').strip())
        except ValueError:
            fee = 0.0
            
        customer_name = normalized_row.get('customer name', normalized_row.get('customer', normalized_row.get('party', normalized_row.get('name', normalized_row.get('email', normalized_row.get('payer', 'Customer'))))))
        date_str = normalized_row.get('date', normalized_row.get('created at', normalized_row.get('settled at', normalized_row.get('time', ''))))
        
        if order_id or amount > 0:
            if not order_id:
                order_id = f"TXN-{len(gateway_records)+1}"
            gateway_records.append({
                'order_id': order_id,
                'gateway_gross': amount,
                'gateway_fee': fee,
                'customer_name': customer_name,
                'date': date_str,
                'source_row': row
            })
            
    # Convert Tally vouchers into a lookup dictionary for fast matching
    # Match by voucher number or invoice number
    tally_lookup = {}
    for tv in tally_vouchers:
        # We only care about Sales vouchers or positive revenue vouchers
        v_num = tv.get('voucher_number', tv.get('invoice_number', ''))
        if v_num:
            tally_lookup[v_num.lower()] = tv

    discrepancies = []
    matched_count = 0
    missing_in_tally = 0
    mismatched_amount = 0
    total_fees = 0.0
    
    matched_tally_ids = set()

    # Reconcile Gateway records against Tally
    for g_rec in gateway_records:
        ord_lower = g_rec['order_id'].lower()
        t_rec = tally_lookup.get(ord_lower)
        
        total_fees += g_rec['gateway_fee']
        g_gross = g_rec['gateway_gross']
        g_net = g_gross - g_rec['gateway_fee']
        
        if t_rec:
            matched_tally_ids.add(t_rec.get('id'))
            
            t_amount = float(t_rec.get('amount', 0.0))
            
            # Check for amount mismatch against gross or net
            if abs(t_amount - g_gross) <= 1.0:
                if g_rec['gateway_fee'] > 0:
                    matched_count += 1
                    discrepancies.append({
                        'status': 'Fee Deduction',
                        'order_id': g_rec['order_id'],
                        'customer_name': g_rec['customer_name'],
                        'gateway_gross': g_gross,
                        'gateway_fee': g_rec['gateway_fee'],
                        'tally_amount': t_amount,
                        'variance': g_rec['gateway_fee']
                    })
                else:
                    matched_count += 1
                    discrepancies.append({
                        'status': 'Matched',
                        'order_id': g_rec['order_id'],
                        'customer_name': g_rec['customer_name'],
                        'gateway_gross': g_gross,
                        'gateway_fee': g_rec['gateway_fee'],
                        'tally_amount': t_amount,
                        'variance': 0.0
                    })
            elif abs(t_amount - g_net) <= 1.0:
                matched_count += 1
                discrepancies.append({
                    'status': 'Matched (Net)',
                    'order_id': g_rec['order_id'],
                    'customer_name': g_rec['customer_name'],
                    'gateway_gross': g_gross,
                    'gateway_fee': g_rec['gateway_fee'],
                    'tally_amount': t_amount,
                    'variance': 0.0
                })
            else:
                mismatched_amount += 1
                discrepancies.append({
                    'status': 'Mismatched Amount',
                    'order_id': g_rec['order_id'],
                    'customer_name': g_rec['customer_name'],
                    'gateway_gross': g_gross,
                    'gateway_fee': g_rec['gateway_fee'],
                    'tally_amount': t_amount,
                    'variance': g_gross - t_amount
                })
        else:
            missing_in_tally += 1
            discrepancies.append({
                'status': 'Unrecorded in Tally',
                'order_id': g_rec['order_id'],
                'customer_name': g_rec['customer_name'],
                'gateway_gross': g_gross,
                'gateway_fee': g_rec['gateway_fee'],
                'tally_amount': None,
                'variance': g_gross
            })
            
    # Find Tally sales vouchers missing in Gateway
    missing_in_gateway = 0
    for tv in tally_vouchers:
        # Check if it's a sales voucher
        v_type = tv.get('voucher_type', tv.get('type', ''))
        if v_type.lower() == 'sales' and tv.get('id') not in matched_tally_ids:
            missing_in_gateway += 1
            discrepancies.append({
                'status': 'Missing in Gateway',
                'order_id': tv.get('voucher_number', tv.get('invoice_number', 'N/A')),
                'customer_name': tv.get('ledger_name', tv.get('party_name', 'Unknown')),
                'gateway_gross': None,
                'gateway_fee': None,
                'tally_amount': tv.get('amount', 0.0),
                'variance': -tv.get('amount', 0.0)
            })

    total_records = len(gateway_records)
    total_tally = len(tally_vouchers)
    
    return {
        'metrics': {
            'total_records': total_records,
            'total_tally': total_tally,
            'matched': matched_count,
            'mismatched_amount': mismatched_amount,
            'missing_in_tally': missing_in_tally,
            'missing_in_gateway': missing_in_gateway,
            'total_fees': total_fees,
            'match_percentage': round((matched_count / max(total_records, 1)) * 100, 1)
        },
        'discrepancies': discrepancies
    }
