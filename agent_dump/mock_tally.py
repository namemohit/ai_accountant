from flask import Flask, request
import xml.etree.ElementTree as ET

app = Flask(__name__)

@app.route('/', methods=['POST'])
def mock_tally():
    xml_data = request.data.decode('utf-8')
    print("\n" + "="*50)
    print("RECEIVED XML FROM AGENT:")
    print("="*50)
    print(xml_data)
    print("="*50 + "\n")
    
    # Return a standard Tally success response
    success_response = """
    <ENVELOPE>
        <HEADER>
            <VERSION>1</VERSION>
            <STATUS>1</STATUS>
        </HEADER>
        <BODY>
            <DATA>
                <IMPORTRESULT>
                    <CREATED>1</CREATED>
                    <ALTERED>0</ALTERED>
                    <DELETED>0</DELETED>
                    <ERRORS>0</ERRORS>
                </IMPORTRESULT>
            </DATA>
        </BODY>
    </ENVELOPE>
    """
    return success_response, 200, {'Content-Type': 'text/xml'}

if __name__ == '__main__':
    print("Tally Mock Server running on http://localhost:9000")
    print("Ready to receive vouchers...")
    app.run(port=9000)
