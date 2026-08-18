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

    data = request.get_json(silent=True)

    print("\n========================")
    print("NEW REQUEST RECEIVED")
    print("========================")

    print(json.dumps(data, indent=4))

    bill_url = data.get("customData", {}).get("utility_bill_url")

    if not bill_url:
        print("No utility bill URL received.")

        return jsonify({"status": "success"})

    try:

        response = requests.get(bill_url, timeout=30)

        print("\n========================")
        print("UTILITY BILL DOWNLOAD")
        print("========================")
        print("Status code:", response.status_code)

        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False
        ) as temp_file:

            temp_file.write(response.content)

            pdf_path = temp_file.name

        uploaded_file = client.files.create(
            file=open(pdf_path, "rb"),
            purpose="user_data"
        )

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
Extract the following fields from this utility bill.

Return ONLY valid JSON.

{
    "utility_provider": "",
    "annual_kwh_usage": "",
    "peak_demand_kw": "",
    "billing_period": ""
}
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

    except Exception as e:

        print("ERROR:", str(e))

    return jsonify({"status": "success"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
