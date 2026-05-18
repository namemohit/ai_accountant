import os
from typing import Dict, Any
import base64
from google import generativeai as genai

class InvoiceParser:
    def __init__(self, api_key: str):
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel('gemini-flash-latest')

    def parse(self, image_path: str, context: str = "") -> str:
        ext = os.path.splitext(image_path)[1].lower()
        
        # If it is a CSV bank statement, read as plain text to ensure bulletproof parsing
        if ext == '.csv':
            try:
                with open(image_path, "r", encoding="utf-8", errors="ignore") as f:
                    csv_text = f.read()
                
                prompt_with_csv = f"""
                Understand and parse the following document.
                CONTEXT: {context}
                
                RAW CSV DATA:
                ---
                {csv_text}
                ---
                """
                response = self.model.generate_content(prompt_with_csv)
                return response.text
            except Exception as e:
                print(f"Error reading CSV directly: {e}")
                
        with open(image_path, "rb") as f:
            image_data = f.read()
            
        mime_map = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.webp': 'image/webp',
            '.pdf': 'application/pdf',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.xls': 'application/vnd.ms-excel'
        }
        mime_type = mime_map.get(ext, 'image/jpeg')
        
        prompt = f"""
        Analyze this invoice image and extract structured data.
        CONTEXT: {context}
        
        EXTRACTION RULES:
        1. BILLING PARTY DETAILS: Look at the top seller header/supplier company name. Extract address, email, phone, PAN, and any Bank Details listed (Bank Name, Account Number, IFSC Code) if present on the document.
        2. BILLED TO PARTY DETAILS: Look at 'Billing Details', 'Billed To', 'Buyer', 'Name', or 'Customer'. Extract address, email, phone, and PAN.
        3. INVOICE NO: Look for 'Invoice Number', 'Bill No', or 'Ref No'.
        4. TOTAL: The final 'Total' or 'Payable' amount. Ensure it matches the sum of subtotal + taxes.
        5. ITEMS: Extract EVERY single row from the description table. For each item, capture:
           - Description
           - Quantity
           - Unit Price/Rate
           - Discount (%) or Amount (if present, else 0)
           - CGST (%) (specific percentage if mentioned for this line item, else 0)
           - SGST (%) (specific percentage if mentioned for this line item, else 0)
           - HSN/SAC Code (if present on the invoice rows, else empty string)
           - Line Item Total Amount
        
        JSON FORMAT REQUIRED:
        {{
            "invoice_number": "string",
            "date": "YYYYMMDD",
            "party_name": "string",
            "billing_party": {{
                "name": "string",
                "gstin": "string",
                "address": "string",
                "bank_name": "string",
                "account_number": "string",
                "ifsc_code": "string",
                "pan": "string",
                "email": "string",
                "phone": "string"
            }},
            "billed_to_party": {{
                "name": "string",
                "gstin": "string",
                "address": "string",
                "bank_name": "string",
                "account_number": "string",
                "ifsc_code": "string",
                "pan": "string",
                "email": "string",
                "phone": "string"
            }},
            "total_amount": number,
            "discount_amount": number,
            "gst_amount": number,
            "cgst": number,
            "sgst": number,
            "igst": number,
            "items": [
                {{
                    "description": "string",
                    "quantity": number,
                    "rate": number,
                    "discount": number,
                    "cgst_rate": number,
                    "sgst_rate": number,
                    "hsn_sac": "string",
                    "amount": number
                }}
            ],
            "category": "Sales|Purchase|Expenses"
        }}
        
        If a value is missing, return null or 0. Return ONLY the JSON.
        """
        
        response = self.model.generate_content([
            prompt,
            {"mime_type": mime_type, "data": image_data}
        ])
        
        # In a real app, we'd add JSON parsing and validation here
        return response.text
