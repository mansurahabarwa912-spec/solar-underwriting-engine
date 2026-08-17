from flask import Flask, request, jsonify
import json

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

    return jsonify({
        "status": "success"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)