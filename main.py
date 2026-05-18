import os
from providers.tally import TallyProvider
from utils.parser import InvoiceParser
import json

def main():
    # Configuration
    TALLY_URL = os.getenv("TALLY_URL", "http://localhost:9000")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    
    if not GEMINI_API_KEY:
        print("Please set GEMINI_API_KEY environment variable.")
        return

    # Initialize components
    tally = TallyProvider()
    parser = InvoiceParser(api_key=GEMINI_API_KEY)

    print("--- AI Accounting Agent ---")
    image_path = input("Enter path to invoice image (or 'test.jpg'): ") or "test.jpg"
    
    if not os.path.exists(image_path):
        print(f"File {image_path} not found.")
        return

    print("Parsing invoice...")
    parsed_data_raw = parser.parse(image_path)
    print("Extracted Data:", parsed_data_raw)
    
    try:
        # Basic cleanup of the LLM response to get valid JSON
        json_str = parsed_data_raw.strip().replace('```json', '').replace('```', '')
        data = json.loads(json_str)
        
        # Prepare for Tally
        voucher_data = {
            "type": "Purchase" if data.get("category") != "Sales" else "Sales",
            "date": data.get("date", "20240101"),
            "number": data.get("invoice_number"),
            "party": data.get("party_name"),
            "amount": data.get("total_amount"),
            "cash_bank_ledger": "Cash"
        }
        
        print(f"Creating voucher in Tally for {voucher_data['party']} - ₹{voucher_data['amount']}...")
        response = tally.create_voucher(voucher_data)
        print("Tally Response:", response)
        
    except Exception as e:
        print(f"Error processing: {e}")

if __name__ == "__main__":
    main()
