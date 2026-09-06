"""OpenPing placeholder — full app arriving next commit."""
from flask import Flask, jsonify
app = Flask(__name__)
@app.get("/health")
def health():
    return jsonify({"status": "ok", "smtp_configured": False, "llm_configured": False})
