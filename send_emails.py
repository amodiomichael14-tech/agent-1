#!/usr/bin/env python3
"""Cold email agent — review-drafts-first, Gmail delivery.

You give it a CSV of leads (email + optional name / company / notes). It writes
a personalized cold email for each one with Claude. You review the drafts,
approve the ones you like, and it sends only those from your Gmail account.

Three steps:

  1. GENERATE  ->  python send_emails.py
        Writes one draft per lead into drafts/, each marked "Approved: no".

  2. REVIEW    ->  open the files in drafts/, read/edit them, and change
        "Approved: no" to "Approved: yes" on the ones you want sent.

  3. SEND      ->  python send_emails.py --send
        Sends ONLY the approved drafts via Gmail, logs them, and moves each
        sent draft into drafts/sent/.

The wording is driven entirely by prompt.md — edit it any time.

Other commands:
    python send_emails.py --status        # counts: pending / approved / sent
    python send_emails.py --limit 5        # only generate the first 5
    python send_emails.py --regenerate     # re-draft even if a draft exists
"""

from __future__ import annotations

import argparse
import csv
import io
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
DRAFTS_DIR = ROOT / "drafts"
SENT_DIR = DRAFTS_DIR / "sent"
SENT_LOG = ROOT / "sent_log.csv"

DEFAULT_MODEL = "claude-opus-4-8"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "mail.com", "msn.com", "pm.me", "fastmail.com",
}

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}

DRAFT_HELP = (
    "# Review this email. To approve it for sending, change "
    '"Approved: no" to "Approved: yes".\n'
    "# You can freely edit the Subject line and the body below the --- line.\n"
)


# --------------------------------------------------------------------------- #
# Config & input
# --------------------------------------------------------------------------- #
def load_config() -> dict:
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
    return cfg


def require(cfg: dict, keys: dict[str, str]) -> None:
    """Exit if any required setting is missing. keys = {ENV_NAME: cfg_key}."""
    missing = [env for env, k in keys.items() if not cfg[k]]
    if missing:
        sys.exit(
            "Missing required setting(s): "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in (see README.md)."
        )


def load_leads(path: Path) -> list[dict]:
    """Read leads from CSV. Only 'email' is required; name/company/notes optional.

    Accepts a header row with any of: email, name, company, notes (case-
    insensitive). If there's no 'email' header, each row's first cell is treated
    as the email. Rows whose email is blank, invalid, or starts with '#' are
    skipped. Duplicates are removed.
    """
    if not path.exists():
        sys.exit(f"Leads file not found: {path}")

    rows = [
        r for r in csv.reader(io.StringIO(path.read_text(encoding="utf-8")))
        if r and any(c.strip() for c in r)
    ]
    if not rows:
        return []

    header = [c.strip().lower() for c in rows[0]]
    has_header = "email" in header

    leads: list[dict] = []
    seen: set[str] = set()

    def add(email: str, name: str = "", company: str = "", notes: str = "") -> None:
        email = email.strip().lower()
        if not email or email.startswith("#"):
            return
        if not EMAIL_RE.match(email):
            print(f"  ! skipping invalid email: {email}")
            return
        if email in seen:
            return
        seen.add(email)
        leads.append({
            "email": email,
            "name": name.strip(),
            "company": company.strip(),
            "notes": notes.strip(),
        })

    if has_header:
        idx = {c: i for i, c in enumerate(header)}

        def cell(row: list[str], col: str) -> str:
            i = idx.get(col)
            return row[i] if (i is not None and i < len(row)) else ""

        for row in rows[1:]:
            add(cell(row, "email"), cell(row, "name"),
                cell(row, "company"), cell(row, "notes"))
    else:
        for row in rows:
            add(row[0])

    return leads


def recipient_hints(email: str) -> tuple[str, str]:
    """Best-effort name + company guess from the address alone (hints only)."""
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


def draft_filename(email: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "_", email.lower()) + ".txt"


def load_sent() -> set[str]:
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
    new_file = not SENT_LOG.exists()
    with SENT_LOG.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["sent_at", "email", "subject"])
        writer.writerow(
            [datetime.now(timezone.utc).isoformat(timespec="seconds"), email, subject]
        )


# --------------------------------------------------------------------------- #
# Drafts (write / read)
# --------------------------------------------------------------------------- #
def write_draft(email: str, subject: str, body: str) -> Path:
    DRAFTS_DIR.mkdir(exist_ok=True)
    path = DRAFTS_DIR / draft_filename(email)
    path.write_text(
        f"{DRAFT_HELP}To: {email}\nSubject: {subject}\nApproved: no\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def read_draft(path: Path) -> dict | None:
    """Parse a draft file into {to, subject, approved, body}. None if malformed."""
    header, sep, body = path.read_text(encoding="utf-8").partition("\n---\n")
    if not sep:
        print(f"  ! skipping malformed draft (no '---' separator): {path.name}")
        return None

    fields = {"to": "", "subject": "", "approved": "no"}
    for line in header.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        if key in fields:
            fields[key] = val.strip()

    to = fields["to"].lower()
    if not EMAIL_RE.match(to):
        print(f"  ! skipping draft with bad/missing To: {path.name}")
        return None

    return {
        "to": to,
        "subject": fields["subject"].strip(),
        "approved": fields["approved"].strip().lower() in {"yes", "y", "true", "1"},
        "body": body.strip("\n"),
    }


def existing_draft_addresses() -> set[str]:
    """Addresses that already have a draft (pending or already sent)."""
    addrs: set[str] = set()
    for d in (DRAFTS_DIR, SENT_DIR):
        if d.exists():
            for f in d.glob("*.txt"):
                parsed = read_draft(f)
                if parsed:
                    addrs.add(parsed["to"])
    return addrs


# --------------------------------------------------------------------------- #
# Claude generation
# --------------------------------------------------------------------------- #
def build_system_prompt(prompt_text: str, sender_name: str) -> str:
    signature = sender_name or "the sender"
    return (
        prompt_text.strip()
        + "\n\n---\n"
        + "You are writing ONE cold outreach email based on the instructions "
        + "above.\n"
        + f"- The sender is {signature}. Sign off as {signature}.\n"
        + "- Do NOT invent phone numbers, links, prices, calendar links, or any "
        + "contact details that were not given to you.\n"
        + "- If notes about the lead are provided, use the relevant details to "
        + "personalize naturally — don't quote them verbatim or list them back.\n"
        + "- Plain text only — no markdown, no HTML.\n"
        + "- Keep it short: a few short paragraphs at most.\n"
        + "- If the recipient's name is unknown or only a guess that looks "
        + 'unreliable, open with a neutral greeting like "Hi there".'
    )


def generate_email(
    client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    lead: dict,
) -> tuple[str, str]:
    email = lead["email"]
    hint_name, hint_company = recipient_hints(email)

    name = lead.get("name", "").strip()
    if name:
        name_line = name
    elif hint_name:
        name_line = f"(unknown — best guess from the email: {hint_name})"
    else:
        name_line = "(unknown)"

    company = lead.get("company", "").strip()
    if company:
        company_line = company
    elif hint_company:
        company_line = f"(unknown — best guess from the email: {hint_company})"
    else:
        company_line = "(unknown)"

    notes = lead.get("notes", "").strip()

    user_msg = (
        "Write the cold email for this recipient.\n\n"
        f"Recipient email: {email}\n"
        f"Name: {name_line}\n"
        f"Company: {company_line}\n"
        f"Notes about this lead: {notes or '(none)'}"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
        output_config={"format": {"type": "json_schema", "schema": EMAIL_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)
    subject = data["subject"].strip()
    body = data["body"].strip()
    if not subject or not body:
        raise ValueError("model returned an empty subject or body")
    return subject, body


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #
def make_gmail_sender(cfg: dict):
    def _send(to_addr: str, subject: str, body: str) -> None:
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

    return _send


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def do_generate(cfg: dict, args: argparse.Namespace) -> None:
    require(cfg, {"ANTHROPIC_API_KEY": "anthropic_api_key"})

    if not args.prompt.exists():
        sys.exit(f"Prompt file not found: {args.prompt}")
    prompt_text = args.prompt.read_text(encoding="utf-8").strip()
    if not prompt_text:
        sys.exit(f"Prompt file is empty: {args.prompt}")

    leads = load_leads(args.leads)
    sent = load_sent()
    drafted = set() if args.regenerate else existing_draft_addresses()

    todo = [ld for ld in leads if ld["email"] not in sent and ld["email"] not in drafted]
    skipped = len(leads) - len(todo)
    if args.limit > 0:
        todo = todo[: args.limit]

    print("=" * 60)
    print("  Cold email agent — GENERATING DRAFTS")
    print(f"  Model:    {cfg['model']}")
    print(f"  To draft: {len(todo)}"
          + (f"  ({skipped} already sent or drafted, skipped)" if skipped else ""))
    print("=" * 60)
    if not todo:
        print("\nNothing new to draft. Edit leads.csv, or use --regenerate.")
        return

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    system_prompt = build_system_prompt(prompt_text, cfg["sender_name"])

    written = failed = 0
    for i, lead in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] {lead['email']}")
        try:
            subject, body = generate_email(client, cfg["model"], system_prompt, lead)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ! could not generate: {exc}")
            continue
        if cfg["footer"]:
            body = f"{body}\n\n{cfg['footer']}"
        path = write_draft(lead["email"], subject, body)
        written += 1
        print(f"  Subject: {subject}")
        print(f"  -> drafted: drafts/{path.name}")

    print("\n" + "-" * 60)
    print(f"Drafted {written}" + (f", {failed} failed" if failed else "") + ".")
    print("\nNext: open the drafts/ folder, review each email, and change")
    print('      "Approved: no" -> "Approved: yes" on the ones you want sent.')
    print("Then: python send_emails.py --send")


def do_send(cfg: dict, args: argparse.Namespace) -> None:
    require(cfg, {
        "GMAIL_ADDRESS": "gmail_address",
        "GMAIL_APP_PASSWORD": "gmail_app_password",
    })

    if not DRAFTS_DIR.exists():
        sys.exit("No drafts/ folder yet. Run `python send_emails.py` first.")

    sent_already = load_sent()
    approved: list[tuple[Path, dict]] = []
    pending = 0
    for f in sorted(DRAFTS_DIR.glob("*.txt")):
        parsed = read_draft(f)
        if not parsed or parsed["to"] in sent_already:
            continue
        if parsed["approved"]:
            approved.append((f, parsed))
        else:
            pending += 1

    if args.limit > 0:
        approved = approved[: args.limit]

    print("=" * 60)
    print("  Cold email agent — SENDING APPROVED DRAFTS (Gmail)")
    print(f"  From:     {cfg['sender_name'] or cfg['gmail_address']} "
          f"<{cfg['gmail_address']}>")
    print(f"  Approved: {len(approved)} to send"
          + (f"   ({pending} still marked 'Approved: no')" if pending else ""))
    print("=" * 60)
    if not approved:
        print("\nNo approved drafts. In the drafts/ folder, change")
        print('"Approved: no" -> "Approved: yes" on the emails you want to send.')
        return

    send = make_gmail_sender(cfg)
    SENT_DIR.mkdir(parents=True, exist_ok=True)

    sent = failed = 0
    for i, (path, draft) in enumerate(approved, 1):
        print(f"\n[{i}/{len(approved)}] {draft['to']}  —  {draft['subject']}")
        try:
            send(draft["to"], draft["subject"], draft["body"])
        except smtplib.SMTPAuthenticationError:
            sys.exit(
                "\nGmail login failed. Use a Gmail *App Password* (not your normal "
                "password) and make sure 2-Step Verification is on. See README.md."
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ! send failed: {exc}")
            continue
        record_sent(draft["to"], draft["subject"])
        path.rename(SENT_DIR / path.name)
        sent += 1
        print("  -> sent")
        if i < len(approved):
            time.sleep(cfg["delay"])

    print("\n" + "-" * 60)
    print(f"Done. Sent {sent}" + (f", failed {failed}" if failed else "") + ".")


def do_status(cfg: dict) -> None:
    pending = approved = 0
    if DRAFTS_DIR.exists():
        for f in DRAFTS_DIR.glob("*.txt"):
            parsed = read_draft(f)
            if not parsed:
                continue
            if parsed["approved"]:
                approved += 1
            else:
                pending += 1
    sent_count = len(load_sent())
    print("Cold email agent — status")
    print(f"  Drafts awaiting approval : {pending}")
    print(f"  Approved, ready to send  : {approved}")
    print(f"  Already sent             : {sent_count}")
    if approved:
        print("\nRun: python send_emails.py --send")
    elif pending:
        print('\nApprove drafts by changing "Approved: no" -> "Approved: yes".')


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Draft personalized cold emails with Claude, review, then send via Gmail."
    )
    parser.add_argument("--send", action="store_true",
                        help="Send the approved drafts (default action is to generate drafts).")
    parser.add_argument("--status", action="store_true",
                        help="Show counts of pending / approved / sent.")
    parser.add_argument("--regenerate", action="store_true",
                        help="When generating, re-draft even if a draft already exists.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N (0 = all).")
    parser.add_argument("--leads", type=Path, default=ROOT / "leads.csv",
                        help="Path to the leads CSV (default: leads.csv).")
    parser.add_argument("--prompt", type=Path, default=ROOT / "prompt.md",
                        help="Path to the prompt file (default: prompt.md).")
    args = parser.parse_args()

    cfg = load_config()
    if args.status:
        do_status(cfg)
    elif args.send:
        do_send(cfg, args)
    else:
        do_generate(cfg, args)


if __name__ == "__main__":
    main()
