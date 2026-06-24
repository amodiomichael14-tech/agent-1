#!/usr/bin/env python3
"""Cold email agent.

Reads a list of recipient email addresses, writes a custom cold email for each
one with Claude, and sends it from your Gmail account.

The wording is driven entirely by prompt.md — edit that file any time to change
the pitch, tone, or call to action. No code changes needed.

Safety first:
  * Runs in DRY-RUN mode by default (it writes + previews the emails but sends
    nothing). Add --send to actually deliver.
  * Skips anyone already in sent_log.csv, so re-runs never double-send.
  * Pauses between messages to stay on Gmail's good side.

Usage:
    python send_emails.py            # dry run — preview only
    python send_emails.py --send     # actually send
    python send_emails.py --send --limit 5 --yes
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from dotenv import load_dotenv

import anthropic

ROOT = Path(__file__).resolve().parent
SENT_LOG = ROOT / "sent_log.csv"

DEFAULT_MODEL = "claude-opus-4-8"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Free / personal mail providers — we don't try to guess a company from these.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "mail.com", "msn.com", "pm.me", "fastmail.com",
}

# Structured-output schema: the model must return exactly this shape.
EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Config & input loading
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    """Read settings from .env / environment and validate the required ones."""
    load_dotenv(ROOT / ".env")

    cfg = {
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", "").strip(),
        "gmail_address": os.getenv("GMAIL_ADDRESS", "").strip(),
        "gmail_app_password": os.getenv("GMAIL_APP_PASSWORD", "").strip(),
        "sender_name": os.getenv("SENDER_NAME", "").strip(),
        "model": os.getenv("MODEL", "").strip() or DEFAULT_MODEL,
        "footer": os.getenv("EMAIL_FOOTER", "").strip(),
    }

    try:
        cfg["delay"] = float(os.getenv("SEND_DELAY_SECONDS", "8"))
    except ValueError:
        cfg["delay"] = 8.0

    missing = [
        name
        for name, key in (
            ("ANTHROPIC_API_KEY", "anthropic_api_key"),
            ("GMAIL_ADDRESS", "gmail_address"),
            ("GMAIL_APP_PASSWORD", "gmail_app_password"),
        )
        if not cfg[key]
    ]
    if missing:
        sys.exit(
            "Missing required setting(s): "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in (see README.md)."
        )
    return cfg


def parse_recipients(path: Path) -> list[str]:
    """One email per line. Blank lines and '#' comments are ignored. Deduped."""
    if not path.exists():
        sys.exit(f"Recipients file not found: {path}")

    seen: set[str] = set()
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        addr = line.lower()
        if not EMAIL_RE.match(addr):
            print(f"  ! skipping invalid address: {line}")
            continue
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def recipient_hints(email: str) -> tuple[str, str]:
    """Best-effort name and company guesses from the address alone.

    e.g. "john.smith@acme.com" -> ("John Smith", "Acme"). These are only hints;
    the prompt tells the model they may be wrong.
    """
    local, _, domain = email.partition("@")
    domain = domain.lower()

    tokens = [re.sub(r"\d+", "", t) for t in re.split(r"[._\-+]", local)]
    tokens = [t for t in tokens if t.isalpha() and len(t) > 1]
    name = " ".join(t.capitalize() for t in tokens[:2])

    company = ""
    if domain and domain not in GENERIC_DOMAINS:
        labels = domain.split(".")
        if len(labels) >= 2:
            company = labels[-2].replace("-", " ").title()
    return name, company


def load_sent() -> set[str]:
    """Addresses we've already emailed, so we never send twice."""
    if not SENT_LOG.exists():
        return set()
    sent: set[str] = set()
    with SENT_LOG.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            addr = (row.get("email") or "").strip().lower()
            if addr:
                sent.add(addr)
    return sent


def record_sent(email: str, subject: str) -> None:
    """Append one row to sent_log.csv (writing a header the first time)."""
    new_file = not SENT_LOG.exists()
    with SENT_LOG.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["sent_at", "email", "subject"])
        writer.writerow(
            [datetime.now(timezone.utc).isoformat(timespec="seconds"), email, subject]
        )


# --------------------------------------------------------------------------- #
# Email generation (Claude) + sending (Gmail SMTP)
# --------------------------------------------------------------------------- #
def build_system_prompt(prompt_text: str, sender_name: str) -> str:
    """Combine the user's editable instructions with fixed output rules."""
    signature = sender_name or "the sender"
    return (
        prompt_text.strip()
        + "\n\n---\n"
        + "You are writing ONE cold outreach email based on the instructions "
        + "above.\n"
        + f"- The sender is {signature}. Sign off as {signature}.\n"
        + "- Do NOT invent phone numbers, links, prices, calendar links, or any "
        + "contact details that were not given to you.\n"
        + "- Plain text only — no markdown, no HTML.\n"
        + "- Keep it short: a few short paragraphs at most.\n"
        + "- The recipient details may be incomplete or guessed. If the name "
        + 'guess looks unreliable, open with a neutral greeting like "Hi there".'
    )


def generate_email(
    client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    email: str,
    name_hint: str,
    company_hint: str,
) -> tuple[str, str]:
    """Ask Claude for a subject + body. Returns (subject, body)."""
    user_msg = (
        "Write the cold email for this recipient.\n\n"
        f"Recipient email: {email}\n"
        f"Best guess at their name: {name_hint or 'unknown'}\n"
        f"Best guess at their company: {company_hint or 'unknown'}"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
        output_config={"format": {"type": "json_schema", "schema": EMAIL_SCHEMA}},
    )

    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)  # output_config guarantees valid JSON in this schema
    subject = data["subject"].strip()
    body = data["body"].strip()
    if not subject or not body:
        raise ValueError("model returned an empty subject or body")
    return subject, body


def send_email(cfg: dict, to_addr: str, subject: str, body: str) -> None:
    """Send one message via Gmail SMTP over SSL."""
    msg = EmailMessage()
    if cfg["sender_name"]:
        msg["From"] = formataddr((cfg["sender_name"], cfg["gmail_address"]))
    else:
        msg["From"] = cfg["gmail_address"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(cfg["gmail_address"], cfg["gmail_app_password"])
        smtp.send_message(msg)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write and send custom cold emails with Claude + Gmail."
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send the emails (default is a dry run that only previews).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt when sending.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N recipients (0 = all).",
    )
    parser.add_argument(
        "--recipients",
        type=Path,
        default=ROOT / "recipients.txt",
        help="Path to the recipients file (default: recipients.txt).",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=ROOT / "prompt.md",
        help="Path to the prompt file (default: prompt.md).",
    )
    args = parser.parse_args()

    cfg = load_config()

    if not args.prompt.exists():
        sys.exit(f"Prompt file not found: {args.prompt}")
    prompt_text = args.prompt.read_text(encoding="utf-8").strip()
    if not prompt_text:
        sys.exit(f"Prompt file is empty: {args.prompt}")

    recipients = parse_recipients(args.recipients)
    already = load_sent()
    pending = [e for e in recipients if e not in already]
    skipped = len(recipients) - len(pending)
    if args.limit > 0:
        pending = pending[: args.limit]

    mode = "SENDING" if args.send else "DRY RUN (no emails sent)"
    print("=" * 60)
    print(f"  Cold email agent — {mode}")
    print(f"  Model:      {cfg['model']}")
    print(f"  From:       {cfg['sender_name'] or cfg['gmail_address']} "
          f"<{cfg['gmail_address']}>")
    print(f"  Recipients: {len(pending)} to process"
          + (f" ({skipped} already emailed, skipped)" if skipped else ""))
    print("=" * 60)

    if not pending:
        print("Nothing to do. Add addresses to recipients.txt or clear sent_log.csv.")
        return

    if args.send and not args.yes:
        if not sys.stdin.isatty():
            sys.exit("Refusing to send in non-interactive mode without --yes.")
        answer = input(f"Send {len(pending)} email(s)? Type 'yes' to confirm: ")
        if answer.strip().lower() != "yes":
            print("Cancelled.")
            return

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    system_prompt = build_system_prompt(prompt_text, cfg["sender_name"])

    sent = failed = 0
    for i, email in enumerate(pending, 1):
        name_hint, company_hint = recipient_hints(email)
        print(f"\n[{i}/{len(pending)}] {email}")

        try:
            subject, body = generate_email(
                client, cfg["model"], system_prompt, email, name_hint, company_hint
            )
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            failed += 1
            print(f"  ! could not generate email: {exc}")
            continue

        if cfg["footer"]:
            body = f"{body}\n\n{cfg['footer']}"

        print(f"  Subject: {subject}")
        print("  " + "\n  ".join(body.splitlines()))

        if not args.send:
            continue

        try:
            send_email(cfg, email, subject, body)
            record_sent(email, subject)
            sent += 1
            print("  -> sent")
        except smtplib.SMTPAuthenticationError:
            sys.exit(
                "\nGmail login failed. Use a Gmail *App Password* (not your normal "
                "password) and make sure 2-Step Verification is on. See README.md."
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ! send failed: {exc}")
            continue

        if i < len(pending):
            time.sleep(cfg["delay"])

    print("\n" + "-" * 60)
    if args.send:
        print(f"Done. Sent {sent}, failed {failed}.")
    else:
        print(f"Dry run complete. Previewed {len(pending)}, "
              f"{failed} failed to generate.")
        print("Re-run with --send to deliver them.")


if __name__ == "__main__":
    main()
