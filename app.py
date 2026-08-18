from flask import Flask, request, jsonify
import json
import requests

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online"
    })

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json(silent=True)

    print("\n========================")
    print("NEW REQUEST RECEIVED")
    print("========================")

    print(json.dumps(data, indent=4))

    # Get the utility bill URL
    bill_url = data.get("utility_bill_url")

    if bill_url:
        try:
            response = requests.get(bill_url, timeout=30)

            print("\n========================")
            print("UTILITY BILL DOWNLOAD")
            print("========================")
            print("Status code:", response.status_code)
            print("File size:", len(response.content), "bytes")
            print("Content type:", response.headers.get("Content-Type"))

        except Exception as e:
            print("Error downloading utility bill:", str(e))

    else:
        print("No utility bill URL received.")

    return jsonify({
        "status": "success"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
