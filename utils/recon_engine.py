"""
Universal Reconciliation Engine
═══════════════════════════════════════════════════════════════════════════
ONE engine. Any industry. Driven by JSON templates.

A reconciliation = match a Master source (your truth) against N External
sources (third-party reports), then verify deductions/adjustments per a
formula schema defined in the template.

This module does NOT know about hotels, restaurants, e-commerce, etc.
It only knows: parse file → map columns → match records → compute variances.

Inputs:
  • Template (JSON)  — what fields to extract, how to match, what to verify
  • Master file      — your internal source of truth
  • External file(s) — third-party reports to verify against
  • Config (JSON)    — runtime overrides (e.g., contractual commission %)

Outputs:
  • Normalized records (canonical schema)
  • Matches with computed variances
  • Aggregated metrics per external source
"""

import csv
import io
import json
import re
import os
from difflib import SequenceMatcher
from typing import List, Dict, Any, Tuple, Optional


# ═══════════════════════════════════════════════════════════════════════════
# FILE PARSING — Accept CSV, XLSX, XLS
# ═══════════════════════════════════════════════════════════════════════════

def parse_file_to_rows(file_content: bytes, file_name: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """Returns (headers, rows). Rows are list of dicts keyed by header."""
    ext = (file_name or '').lower().split('.')[-1]

    if ext in ('xlsx', 'xls'):
        try:
            import openpyxl  # type: ignore
            wb = openpyxl.load_workbook(io.BytesIO(file_content), data_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers = [str(h).strip() if h is not None else '' for h in next(rows_iter)]
            rows = []
            for r in rows_iter:
                if all(v is None or v == '' for v in r):
                    continue
                rows.append({headers[i]: ('' if v is None else str(v)) for i, v in enumerate(r) if i < len(headers)})
            return headers, rows
        except Exception as e:
            print(f"[recon_engine] XLSX parse failed, falling back to CSV: {e}")

    # CSV (also fallback)
    try:
        text = file_content.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = file_content.decode('latin1', errors='ignore')

    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for row in reader:
        rows.append({k.strip() if k else '': (v or '').strip() for k, v in row.items() if k})
    return headers, rows


# ═══════════════════════════════════════════════════════════════════════════
# COLUMN MAPPING — Template-driven, AI fallback
# ═══════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

def detect_columns_from_template(headers: List[str], schema: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    schema: { canonical_field: [alias_1, alias_2, ...] }
    Returns { canonical_field: matched_header_or_None }
    """
    normalized = {h: _norm(h) for h in headers}
    mapping: Dict[str, Optional[str]] = {}

    for canonical_field, aliases in schema.items():
        if not isinstance(aliases, list):
            aliases = [aliases]
        norm_aliases = [_norm(a) for a in aliases]
        match = None

        # Exact normalized match first
        for h, nh in normalized.items():
            if nh in norm_aliases:
                match = h
                break

        # Partial / substring match
        if not match:
            for h, nh in normalized.items():
                for na in norm_aliases:
                    if na and (na in nh or nh in na):
                        match = h
                        break
                if match:
                    break

        mapping[canonical_field] = match

    return mapping

def detect_columns_with_ai(headers: List[str], sample_rows: List[Dict],
                            schema: Dict[str, Any], source_name: str) -> Dict[str, Optional[str]]:
    """
    Fallback: when template aliases don't match, ask Gemini to map columns.
    schema canonical_field → list of aliases. We send the headers + first few rows
    and ask Gemini which header corresponds to each canonical field.
    """
    try:
        from utils.parser import genai_configure  # reuse the configured client
        import google.generativeai as genai

        canonical_fields = list(schema.keys())
        sample = sample_rows[:5] if sample_rows else []

        prompt = f"""You are a data column mapper. Given a CSV from "{source_name}", map its headers to canonical fields.

Headers: {headers}

Sample rows: {json.dumps(sample, default=str)[:2000]}

Canonical fields to map: {canonical_fields}

Hint about what each canonical field means:
{json.dumps({k: v[:3] if isinstance(v, list) else v for k, v in schema.items()}, indent=2)}

Return ONLY a JSON object like:
{{"booking_id": "Booking Ref", "guest_name": "Customer Name", "gross_amount": "Total"}}

If a canonical field has no matching header, set its value to null. Return JSON ONLY, no markdown.
"""
        model = genai.GenerativeModel('gemini-flash-latest')
        resp = model.generate_content(prompt)
        text = resp.text.strip()
        text = re.sub(r'^```(?:json)?', '', text).rstrip('`').strip()
        result = json.loads(text)
        # Sanitize: only return keys we asked for
        return {k: result.get(k) for k in canonical_fields}
    except Exception as e:
        print(f"[recon_engine] AI column mapping failed for {source_name}: {e}")
        return {k: None for k in schema.keys()}


def map_row(row: Dict[str, str], column_mapping: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Apply column_mapping to a single row → returns canonical dict."""
    out = {}
    for canonical_field, header in column_mapping.items():
        if header and header in row:
            out[canonical_field] = _coerce_value(canonical_field, row[header])
        else:
            out[canonical_field] = None
    return out


def _coerce_value(field_name: str, raw_value: str) -> Any:
    """Convert string values to numbers/dates based on field naming heuristic."""
    if raw_value is None:
        return None
    v = str(raw_value).strip()
    if not v:
        return None

    lower = field_name.lower()
    # Numeric fields
    if any(tok in lower for tok in ('amount', 'gross', 'fee', 'commission', 'tax',
                                     'gst', 'tds', 'payout', 'net', 'price',
                                     'total', 'rate', 'value', 'cost', 'discount')):
        cleaned = re.sub(r'[₹,\s]', '', v)
        cleaned = cleaned.replace('Rs.', '').replace('Rs', '').replace('INR', '')
        try:
            return float(cleaned)
        except ValueError:
            return None
    return v


# ═══════════════════════════════════════════════════════════════════════════
# MATCHING — Cascade through strategies in priority order
# ═══════════════════════════════════════════════════════════════════════════

def _string_sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def _date_close(a, b, tolerance_days: int = 1) -> bool:
    """Lenient date comparison — accepts strings or date-likes."""
    if not a or not b:
        return False
    a_str = str(a).strip()[:10]
    b_str = str(b).strip()[:10]
    if a_str == b_str:
        return True
    try:
        from datetime import datetime
        formats = ['%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%m/%d/%Y', '%d-%b-%Y']
        da = db = None
        for f in formats:
            try: da = datetime.strptime(a_str, f); break
            except: pass
        for f in formats:
            try: db = datetime.strptime(b_str, f); break
            except: pass
        if da and db:
            return abs((da - db).days) <= tolerance_days
    except Exception:
        pass
    return False

def match_records(master_records: List[Dict], external_records: List[Dict],
                  matching_rules: List[Dict]) -> List[Dict]:
    """
    master_records / external_records: list of dicts each with
      { 'id': uuid, 'matching_key': str, 'canonical_data': {...} }

    matching_rules: list ordered by priority, each entry like:
      { "strategy": "exact_id", "field": "booking_id", "priority": 1 }
      { "strategy": "fuzzy_text+date", "fields": ["guest_name", "check_in"],
        "name_similarity": 0.85, "date_tolerance": 1, "priority": 2 }
      { "strategy": "amount+date", "fields": ["gross_amount", "check_in"],
        "amount_tolerance": 1.0, "date_tolerance": 2, "priority": 3 }

    Returns list of match dicts:
      { 'master_record_id', 'external_record_id', 'match_type', 'match_score' }
    """
    matches = []
    matched_master = set()
    matched_external = set()

    rules_sorted = sorted(matching_rules, key=lambda r: r.get('priority', 99))

    for rule in rules_sorted:
        strategy = rule.get('strategy')

        if strategy == 'exact_id':
            field = rule.get('field', 'matching_key')
            ext_index = {}
            for er in external_records:
                if er['id'] in matched_external:
                    continue
                key = er.get('matching_key') or (er['canonical_data'].get(field) or '')
                key = str(key).lower().strip()
                if key:
                    ext_index.setdefault(key, []).append(er)

            for mr in master_records:
                if mr['id'] in matched_master:
                    continue
                key = mr.get('matching_key') or (mr['canonical_data'].get(field) or '')
                key = str(key).lower().strip()
                if not key:
                    continue
                candidates = ext_index.get(key, [])
                for er in candidates:
                    if er['id'] in matched_external:
                        continue
                    matches.append({
                        'master_record_id': mr['id'],
                        'external_record_id': er['id'],
                        'match_type': 'exact_id',
                        'match_score': 1.0,
                    })
                    matched_master.add(mr['id'])
                    matched_external.add(er['id'])
                    break

        elif strategy == 'fuzzy_text+date':
            fields = rule.get('fields', ['name', 'date'])
            name_field = fields[0] if len(fields) > 0 else 'name'
            date_field = fields[1] if len(fields) > 1 else 'date'
            sim_threshold = rule.get('name_similarity', 0.85)
            date_tol = rule.get('date_tolerance', 1)

            for mr in master_records:
                if mr['id'] in matched_master:
                    continue
                m_name = str(mr['canonical_data'].get(name_field) or '')
                m_date = mr['canonical_data'].get(date_field)
                best = None
                best_score = 0.0
                for er in external_records:
                    if er['id'] in matched_external:
                        continue
                    e_name = str(er['canonical_data'].get(name_field) or '')
                    e_date = er['canonical_data'].get(date_field)
                    sim = _string_sim(m_name, e_name)
                    if sim >= sim_threshold and _date_close(m_date, e_date, date_tol):
                        if sim > best_score:
                            best_score = sim
                            best = er
                if best:
                    matches.append({
                        'master_record_id': mr['id'],
                        'external_record_id': best['id'],
                        'match_type': 'fuzzy_text+date',
                        'match_score': round(best_score, 3),
                    })
                    matched_master.add(mr['id'])
                    matched_external.add(best['id'])

        elif strategy == 'amount+date':
            fields = rule.get('fields', ['amount', 'date'])
            amount_field = fields[0]
            date_field = fields[1] if len(fields) > 1 else 'date'
            amount_tol = rule.get('amount_tolerance', 1.0)
            date_tol = rule.get('date_tolerance', 2)

            for mr in master_records:
                if mr['id'] in matched_master:
                    continue
                m_amt = mr['canonical_data'].get(amount_field)
                m_date = mr['canonical_data'].get(date_field)
                if m_amt is None:
                    continue
                for er in external_records:
                    if er['id'] in matched_external:
                        continue
                    e_amt = er['canonical_data'].get(amount_field)
                    e_date = er['canonical_data'].get(date_field)
                    if e_amt is None:
                        continue
                    if abs(float(m_amt) - float(e_amt)) <= amount_tol and _date_close(m_date, e_date, date_tol):
                        matches.append({
                            'master_record_id': mr['id'],
                            'external_record_id': er['id'],
                            'match_type': 'amount+date',
                            'match_score': 0.75,
                        })
                        matched_master.add(mr['id'])
                        matched_external.add(er['id'])
                        break

    return matches


# ═══════════════════════════════════════════════════════════════════════════
# VARIANCE COMPUTATION — Safe formula evaluator
# ═══════════════════════════════════════════════════════════════════════════

def compute_variances(master_data: Dict, external_data: Dict,
                       variance_formulas: List[Dict], config: Dict) -> Dict[str, float]:
    """
    variance_formulas: list of dicts:
      { "name": "commission_variance",
        "formula": "external.commission - (external.gross_amount * config.commission_rate / 100)" }

    Evaluates safely against namespaces:
      • master.<field>
      • external.<field>
      • config.<key>

    Only +, -, *, /, parentheses, and numbers/identifiers are allowed.
    """
    results = {}
    namespace = {
        'master': _NS(master_data or {}),
        'external': _NS(external_data or {}),
        'config': _NS(config or {}),
        'abs': abs, 'min': min, 'max': max, 'round': round,
    }

    for f in variance_formulas:
        name = f.get('name')
        expr = f.get('formula', '')
        if not name or not expr:
            continue
        if not _is_safe_expr(expr):
            results[name] = None
            continue
        try:
            val = eval(expr, {"__builtins__": {}}, namespace)
            if isinstance(val, (int, float)):
                results[name] = round(float(val), 2)
            else:
                results[name] = val
        except Exception as e:
            results[name] = None
    return results

class _NS:
    """Dotted-attribute access for dicts; missing keys return 0 (numeric-safe)."""
    def __init__(self, d):
        self._d = d or {}
    def __getattr__(self, k):
        v = self._d.get(k)
        if v is None:
            return 0
        if isinstance(v, (int, float)):
            return v
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0

def _is_safe_expr(expr: str) -> bool:
    # Allow identifiers, dots, digits, math ops, parens, decimal points, spaces
    return bool(re.match(r'^[\w\.\s\+\-\*\/\(\)\,\d\.]+$', expr))


# ═══════════════════════════════════════════════════════════════════════════
# HIGH-LEVEL ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════

def parse_and_normalize(file_content: bytes, file_name: str, schema: Dict[str, Any],
                        source_name: str, use_ai_fallback: bool = True
                        ) -> Tuple[List[Dict], Dict[str, Optional[str]]]:
    """
    1. Parse the file
    2. Detect column mapping (template aliases → AI fallback)
    3. Normalize each row into canonical_data

    Returns: (canonical_records, column_mapping)
      canonical_records: [{'matching_key': '...', 'canonical_data': {...}, 'raw_data': {...}}, ...]
    """
    headers, rows = parse_file_to_rows(file_content, file_name)
    if not rows:
        return [], {}

    mapping = detect_columns_from_template(headers, schema)

    # If primary key not found, try AI fallback
    primary_key_field = list(schema.keys())[0] if schema else None
    if use_ai_fallback and primary_key_field and not mapping.get(primary_key_field):
        ai_mapping = detect_columns_with_ai(headers, rows, schema, source_name)
        for k, v in ai_mapping.items():
            if v and not mapping.get(k):
                mapping[k] = v

    # The primary key in the schema (first field) is what we use for matching
    canonical_records = []
    for row in rows:
        cdata = map_row(row, mapping)
        pk = cdata.get(primary_key_field) if primary_key_field else None
        canonical_records.append({
            'matching_key': str(pk).strip() if pk else '',
            'canonical_data': cdata,
            'raw_data': row,
        })

    return canonical_records, mapping


def reconcile(master_records: List[Dict], external_records: List[Dict],
              template: Dict, config: Dict) -> Tuple[List[Dict], Dict]:
    """
    Top-level reconcile call. Returns (matches_with_variances, metrics).
    Each match dict includes computed variances per template formulas.
    """
    matching_rules = template.get('matching_rules', [])
    variance_formulas = template.get('variance_formulas', [])

    pairs = match_records(master_records, external_records, matching_rules)

    master_index = {r['id']: r for r in master_records}
    external_index = {r['id']: r for r in external_records}

    enriched = []
    for p in pairs:
        m = master_index.get(p['master_record_id'])
        e = external_index.get(p['external_record_id'])
        if not m or not e:
            continue
        variances = compute_variances(
            m.get('canonical_data', {}),
            e.get('canonical_data', {}),
            variance_formulas,
            config,
        )
        enriched.append({**p, 'variances': variances})

    matched_master_ids = {p['master_record_id'] for p in pairs}
    matched_external_ids = {p['external_record_id'] for p in pairs}
    unmatched_master = [r for r in master_records if r['id'] not in matched_master_ids]
    unmatched_external = [r for r in external_records if r['id'] not in matched_external_ids]

    metrics = {
        'total_master': len(master_records),
        'total_external': len(external_records),
        'matched': len(pairs),
        'unmatched_master': len(unmatched_master),
        'unmatched_external': len(unmatched_external),
        'match_rate': round(len(pairs) / max(len(master_records), 1) * 100, 1),
    }

    return enriched, metrics
