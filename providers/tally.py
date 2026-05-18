import requests
from typing import Dict, Any, List
from core.provider import AccountingProvider

class TallyProvider(AccountingProvider):
    def __init__(self, host: str = "http://localhost", port: int = 9000):
        self.url = f"{host}:{port}"

    def _send_xml(self, xml_payload: str) -> str:
        try:
            response = requests.post(self.url, data=xml_payload, headers={'Content-Type': 'text/xml'})
            return response.text
        except Exception as e:
            return f"Error connecting to Tally: {str(e)}"

    def create_voucher(self, data: Dict[str, Any]) -> str:
        # Simple Tally XML for a payment/receipt voucher
        # This is a template, in a real app we'd use a robust XML builder
        xml = f"""
        <ENVELOPE>
            <HEADER>
                <TALLYREQUEST>Import Data</TALLYREQUEST>
            </HEADER>
            <BODY>
                <IMPORTDATA>
                    <REQUESTDESC>
                        <REPORTNAME>Vouchers</REPORTNAME>
                    </REQUESTDESC>
                    <REQUESTDATA>
                        <TALLYMESSAGE xmlns:UDF="TallyUDF">
                            <VOUCHER VCHTYPE="{data.get('type', 'Payment')}" ACTION="Create">
                                <DATE>{data.get('date')}</DATE>
                                <VOUCHERNUMBER>{data.get('number', '')}</VOUCHERNUMBER>
                                <PARTYLEDGERNAME>{data.get('party')}</PARTYLEDGERNAME>
                                <ALLLEDGERENTRIES.LIST>
                                    <LEDGERNAME>{data.get('party')}</LEDGERNAME>
                                    <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
                                    <AMOUNT>-{data.get('amount')}</AMOUNT>
                                </ALLLEDGERENTRIES.LIST>
                                <ALLLEDGERENTRIES.LIST>
                                    <LEDGERNAME>{data.get('cash_bank_ledger', 'Cash')}</LEDGERNAME>
                                    <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
                                    <AMOUNT>{data.get('amount')}</AMOUNT>
                                </ALLLEDGERENTRIES.LIST>
                            </VOUCHER>
                        </TALLYMESSAGE>
                    </REQUESTDATA>
                </IMPORTDATA>
            </BODY>
        </ENVELOPE>
        """
        return self._send_xml(xml)

    def get_ledgers(self) -> List[str]:
        # Implementation for fetching ledgers via XML
        return ["Cash", "Sales", "Purchase", "GST Input"]

    def get_balance(self, ledger_name: str) -> float:
        # Implementation for fetching balance
        return 0.0
