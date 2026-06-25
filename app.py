#!/usr/bin/env python3
"""Web UI for the cold email agent.

Run locally:
    pip install -r requirements.txt
    python app.py
    # then open http://localhost:5000

Each visitor enters their own Anthropic API key and Gmail App Password in the
Settings panel. Those secrets are held server-side in memory for the browser
session only — never written to disk and never sent back to the page. If you set
keys in a local .env file, they pre-fill as defaults for you (the operator).

Reuses all the core logic from send_emails.py (the command-line version).
"""

from __future__ import annotations

import os
import secrets
import smtplib
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session

import anthropic

import send_emails as core  # reuse the CLI's core logic

ROOT = Path(__file__).resolve().parent

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Per-session settings (including secrets) kept in server memory only.
_STORE: dict[str, dict] = {}

MODELS = [
    {"id": "claude-opus-4-8", "label": "Opus 4.8 — best quality"},
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6 — balanced"},
    {"id": "claude-haiku-4-5", "label": "Haiku 4.5 — fastest & cheapest"},
]


def _sid() -> str:
    if "sid" not in session:
        session["sid"] = secrets.token_hex(16)
        session.permanent = False
    return session["sid"]


def _settings() -> dict:
    sid = _sid()
    if sid not in _STORE:
        # Pre-fill from .env if present so a single operator can configure once;
        # other visitors start blank and enter their own credentials.
        core.load_dotenv(ROOT / ".env")
        _STORE[sid] = {
            "anthropic_key": os.getenv("ANTHROPIC_API_KEY", "").strip(),
            "gmail_address": os.getenv("GMAIL_ADDRESS", "").strip(),
            "gmail_app_password": os.getenv("GMAIL_APP_PASSWORD", "").strip(),
            "sender_name": os.getenv("SENDER_NAME", "").strip(),
            "footer": os.getenv("EMAIL_FOOTER", "").strip(),
            "model": os.getenv("MODEL", "").strip() or core.DEFAULT_MODEL,
        }
    return _STORE[sid]


def default_prompt() -> str:
    p = ROOT / "prompt.md"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _public_settings(s: dict) -> dict:
    """Settings safe to expose to the browser (no secret values)."""
    return {
        "sender_name": s["sender_name"],
        "footer": s["footer"],
        "model": s["model"],
        "gmail_address": s["gmail_address"],
        "has_anthropic": bool(s["anthropic_key"]),
        "has_gmail": bool(s["gmail_address"] and s["gmail_app_password"]),
    }


@app.get("/")
def index():
    s = _settings()
    init = _public_settings(s)
    init["default_prompt"] = default_prompt()
    init["models"] = MODELS
    return render_template("index.html", init=init)


@app.get("/api/settings")
def get_settings():
    return jsonify(_public_settings(_settings()))


@app.post("/api/settings")
def save_settings():
    s = _settings()
    data = request.get_json(force=True, silent=True) or {}
    for key in ("sender_name", "footer", "model"):
        if key in data:
            s[key] = (data.get(key) or "").strip()
    # Only overwrite secrets when a non-empty value is supplied, so a blank field
    # means "keep what I had".
    if data.get("anthropic_key"):
        s["anthropic_key"] = data["anthropic_key"].strip()
    if data.get("gmail_address") is not None:
        s["gmail_address"] = (data.get("gmail_address") or "").strip()
    if data.get("gmail_app_password"):
        s["gmail_app_password"] = data["gmail_app_password"].strip()
    if not s["model"]:
        s["model"] = core.DEFAULT_MODEL
    return jsonify(_public_settings(s))


@app.post("/api/generate")
def api_generate():
    s = _settings()
    if not s["anthropic_key"]:
        return jsonify({"error": "Add your Anthropic API key in Settings first."}), 400

    data = request.get_json(force=True, silent=True) or {}
    prompt_text = (data.get("prompt") or "").strip()
    if not prompt_text:
        return jsonify({"error": "Write your pitch in the Compose box first."}), 400

    leads, seen = [], set()
    for ld in data.get("leads") or []:
        email = (ld.get("email") or "").strip().lower()
        if not email or not core.EMAIL_RE.match(email) or email in seen:
            continue
        seen.add(email)
        leads.append({
            "email": email,
            "name": (ld.get("name") or "").strip(),
            "company": (ld.get("company") or "").strip(),
            "notes": (ld.get("notes") or "").strip(),
        })
    if not leads:
        return jsonify({"error": "Add at least one valid email address."}), 400

    client = anthropic.Anthropic(api_key=s["anthropic_key"])
    system_prompt = core.build_system_prompt(prompt_text, s["sender_name"])
    footer = s["footer"]

    drafts, errors = [], []
    for ld in leads:
        try:
            subject, body = core.generate_email(client, s["model"], system_prompt, ld)
        except Exception as exc:  # noqa: BLE001
            errors.append({"email": ld["email"], "error": str(exc)})
            continue
        if footer:
            body = f"{body}\n\n{footer}"
        drafts.append({
            "email": ld["email"],
            "name": ld["name"],
            "company": ld["company"],
            "subject": subject,
            "body": body,
        })
    return jsonify({"drafts": drafts, "errors": errors})


@app.post("/api/send")
def api_send():
    s = _settings()
    if not (s["gmail_address"] and s["gmail_app_password"]):
        return jsonify({"error": "Add your Gmail address and App Password in Settings first."}), 400

    drafts = (request.get_json(force=True, silent=True) or {}).get("drafts") or []
    if not drafts:
        return jsonify({"error": "No approved emails to send."}), 400

    sender = core.make_gmail_sender({
        "sender_name": s["sender_name"],
        "gmail_address": s["gmail_address"],
        "gmail_app_password": s["gmail_app_password"],
    })

    results = []
    for d in drafts:
        to = (d.get("email") or "").strip().lower()
        subject = (d.get("subject") or "").strip()
        body = (d.get("body") or "").strip()
        if not core.EMAIL_RE.match(to) or not subject or not body:
            results.append({"email": to, "ok": False, "error": "missing or invalid fields"})
            continue
        try:
            sender(to, subject, body)
        except smtplib.SMTPAuthenticationError:
            return jsonify({
                "error": "Gmail login failed. Use a Gmail App Password (not your "
                         "normal password) with 2-Step Verification turned on."
            }), 400
        except Exception as exc:  # noqa: BLE001
            results.append({"email": to, "ok": False, "error": str(exc)})
            continue
        results.append({"email": to, "ok": True})

    return jsonify({"results": results, "sent": sum(1 for r in results if r["ok"])})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"\n  Cold Email Studio running →  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
