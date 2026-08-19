from flask import Flask, request, jsonify
from openai import OpenAI
import os
import json
import requests
import tempfile

app = Flask(__name__)

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY")
)


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "online"})


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json(silent=True) or {}

    print("\n========================")
    print("NEW REQUEST RECEIVED")
    print("========================")

    print(json.dumps(data, indent=4))

    # Get custom data from GHL
    custom_data = data.get("customData", {})

    # GHL sends the uploaded bill under "utility_bill"
    bill_data = custom_data.get("utility_bill")

    print("\n========================")
    print("UTILITY BILL DATA")
    print("========================")
    print(bill_data)

    # Handle different formats GHL may send
    bill_url = None

    if isinstance(bill_data, str):
        bill_url = bill_data

    elif isinstance(bill_data, list) and len(bill_data) > 0:

        first_bill = bill_data[0]

        if isinstance(first_bill, str):
            bill_url = first_bill

        elif isinstance(first_bill, dict):
            bill_url = (
                first_bill.get("url")
                or first_bill.get("fileUrl")
                or first_bill.get("file_url")
                or first_bill.get("downloadUrl")
            )

    elif isinstance(bill_data, dict):
        bill_url = (
            bill_data.get("url")
            or bill_data.get("fileUrl")
            or bill_data.get("file_url")
            or bill_data.get("downloadUrl")
        )

    if not bill_url:

        print("\n========================")
        print("NO UTILITY BILL URL FOUND")
        print("========================")

        return jsonify({
            "status": "success",
            "message": "No utility bill URL received"
        })


    print("\n========================")
    print("UTILITY BILL URL FOUND")
    print("========================")
    print(bill_url)


    try:

        # Download utility bill
        response = requests.get(
            bill_url,
            timeout=30
        )

        print("\n========================")
        print("UTILITY BILL DOWNLOAD")
        print("========================")

        print("Status code:", response.status_code)
        print("File size:", len(response.content), "bytes")
        print("Content type:", response.headers.get("Content-Type"))


        if response.status_code != 200:
            return jsonify({
                "status": "error",
                "message": "Could not download utility bill"
            }), 400


        # Save PDF temporarily
        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False
        ) as temp_file:

            temp_file.write(response.content)

            pdf_path = temp_file.name


        # Upload PDF to OpenAI
        uploaded_file = client.files.create(
            file=open(pdf_path, "rb"),
            purpose="user_data"
        )


        print("\n========================")
        print("FILE UPLOADED TO OPENAI")
        print("========================")

        print("File ID:", uploaded_file.id)


        # Ask AI to extract information
        result = client.responses.create(
            model="gpt-4.1",

            input=[
                {
                    "role": "user",

                    "content": [

                        {
                            "type": "input_file",
                            "file_id": uploaded_file.id
                        },

                        {
                            "type": "input_text",

                            "text": """
Read this utility bill carefully.

Extract these fields:

1. Utility provider name
2. Annual electricity usage in kWh
3. Peak demand in kW
4. Billing period

Return ONLY valid JSON in exactly this format:

{
    "utility_provider": "",
    "annual_kwh_usage": "",
    "peak_demand_kw": "",
    "billing_period": ""
}

If a value cannot be found, return an empty string.
"""
                        }

                    ]
                }
            ]
        )


        print("\n========================")
        print("AI EXTRACTION")
        print("========================")

        print(result.output_text)
        contact_id = custom_data.get("contact_id")

        print("\n========================")
        print("CONTACT ID")
        print("========================")
        print(contact_id)
        print("\n========================")
        print("GHL ENVIRONMENT")
        print("========================")
        print("API key found:", bool(os.environ.get("GHL_API_KEY")))
        print("Location ID found:", bool(os.environ.get("GHL_LOCATION_ID")))


        # Try to convert AI response to JSON
        try:

            extracted_data = json.loads(
                result.output_text
            )

        except Exception:

            extracted_data = {
                "raw_ai_response": result.output_text
            }


        print("\n========================")
        print("EXTRACTED DATA")
        print("========================")

        print(
            json.dumps(
                extracted_data,
                indent=4
            )
        )


        return jsonify({
            "status": "success",
            "utility_bill_url": bill_url,
            "extracted_data": extracted_data
        })


    except Exception as e:

        print("\n========================")
        print("ERROR")
        print("========================")

        print(str(e))

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
    
