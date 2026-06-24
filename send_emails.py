#!/usr/bin/env python3
"""Cold email agent — review-drafts-first workflow.

Two steps, so you always approve before anything goes out:

  1. GENERATE  ->  python send_emails.py
        Writes a custom cold email for each recipient into the drafts/ folder,
        one file per person, each marked "Approved: no".

  2. REVIEW    ->  open the files in drafts/, read them, edit if you like,
        and change "Approved: no" to "Approved: yes" on the ones you want sent.

  3. SEND      ->  python send_emails.py --send
        Sends ONLY the approved drafts (via SendGrid), logs them, and moves
        each sent draft into drafts/sent/.

The wording is driven entirely by prompt.md — edit it any time.

Other commands:
    python send_emails.py --status        # counts: pending / approved / sent
    python send_emails.py --limit 5        # only generate the first 5
    python send_emails.py --regenerate     # re-draft even if a draft exists
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
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
        "sendgrid_api_key": os.getenv("SENDGRID_API_KEY", "").strip(),
        "from_email": os.getenv("FROM_EMAIL", "").strip(),
        "sender_name": os.getenv("SENDER_NAME", "").strip(),
        "model": os.getenv("MODEL", "").strip() or DEFAULT_MODEL,
        "footer": os.getenv("EMAIL_FOOTER", "").strip(),
    }
    try:
        cfg["delay"] = float(os.getenv("SEND_DELAY_SECONDS", "2"))
    except ValueError:
        cfg["delay"] = 2.0
    return cfg


def require(cfg: dict, keys: dict[str, str]) -> None:
    """Exit if any required setting is missing. keys = {env_name: cfg_key}."""
    missing = [env for env, k in keys.items() if not cfg[k]]
    if missing:
        sys.exit(
            "Missing required setting(s): "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in (see README.md)."
        )


def parse_recipients(path: Path) -> list[str]:
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
    text = path.read_text(encoding="utf-8")
    if "\n---\n" not in text and not text.rstrip().endswith("---"):
        # Be lenient: locate the first standalone '---' line.
        pass
    header, sep, body = text.partition("\n---\n")
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
    data = json.loads(text)
    subject = data["subject"].strip()
    body = data["body"].strip()
    if not subject or not body:
        raise ValueError("model returned an empty subject or body")
    return subject, body


# --------------------------------------------------------------------------- #
# SendGrid
# --------------------------------------------------------------------------- #
def make_sendgrid_sender(cfg: dict):
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import From, Mail
    except ImportError:
        sys.exit("SendGrid not installed. Run: pip install -r requirements.txt")

    client = SendGridAPIClient(cfg["sendgrid_api_key"])
    from_obj = From(cfg["from_email"], cfg["sender_name"] or None)

    def _send(to_addr: str, subject: str, body: str) -> None:
        message = Mail(
            from_email=from_obj,
            to_emails=to_addr,
            subject=subject,
            plain_text_content=body,
        )
        resp = client.send(message)
        if resp.status_code >= 400:
            raise RuntimeError(f"SendGrid returned HTTP {resp.status_code}")

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

    recipients = parse_recipients(args.recipients)
    sent = load_sent()
    drafted = set() if args.regenerate else existing_draft_addresses()

    todo = [e for e in recipients if e not in sent and e not in drafted]
    skipped = len(recipients) - len(todo)
    if args.limit > 0:
        todo = todo[: args.limit]

    print("=" * 60)
    print("  Cold email agent — GENERATING DRAFTS")
    print(f"  Model:    {cfg['model']}")
    print(f"  To draft: {len(todo)}"
          + (f"  ({skipped} already sent or drafted, skipped)" if skipped else ""))
    print("=" * 60)
    if not todo:
        print("\nNothing new to draft. Edit recipients.txt, or use --regenerate.")
        return

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    system_prompt = build_system_prompt(prompt_text, cfg["sender_name"])

    written = failed = 0
    for i, email in enumerate(todo, 1):
        name_hint, company_hint = recipient_hints(email)
        print(f"\n[{i}/{len(todo)}] {email}")
        try:
            subject, body = generate_email(
                client, cfg["model"], system_prompt, email, name_hint, company_hint
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ! could not generate: {exc}")
            continue
        if cfg["footer"]:
            body = f"{body}\n\n{cfg['footer']}"
        path = write_draft(email, subject, body)
        written += 1
        print(f"  Subject: {subject}")
        print(f"  -> drafted: drafts/{path.name}")

    print("\n" + "-" * 60)
    print(f"Drafted {written}" + (f", {failed} failed" if failed else "") + ".")
    print("\nNext: open the drafts/ folder, review each email, and change")
    print('      "Approved: no" -> "Approved: yes" on the ones you want sent.')
    print("Then: python send_emails.py --send")


def do_send(cfg: dict, args: argparse.Namespace) -> None:
    require(cfg, {"SENDGRID_API_KEY": "sendgrid_api_key", "FROM_EMAIL": "from_email"})

    if not DRAFTS_DIR.exists():
        sys.exit("No drafts/ folder yet. Run `python send_emails.py` first.")

    sent_already = load_sent()
    approved: list[tuple[Path, dict]] = []
    pending = 0
    for f in sorted(DRAFTS_DIR.glob("*.txt")):
        parsed = read_draft(f)
        if not parsed:
            continue
        if parsed["to"] in sent_already:
            continue
        if parsed["approved"]:
            approved.append((f, parsed))
        else:
            pending += 1

    if args.limit > 0:
        approved = approved[: args.limit]

    print("=" * 60)
    print("  Cold email agent — SENDING APPROVED DRAFTS (SendGrid)")
    print(f"  From:     {cfg['sender_name'] or cfg['from_email']} "
          f"<{cfg['from_email']}>")
    print(f"  Approved: {len(approved)} to send"
          + (f"   ({pending} still marked 'Approved: no')" if pending else ""))
    print("=" * 60)
    if not approved:
        print("\nNo approved drafts. In the drafts/ folder, change")
        print('"Approved: no" -> "Approved: yes" on the emails you want to send.')
        return

    send = make_sendgrid_sender(cfg)
    SENT_DIR.mkdir(parents=True, exist_ok=True)

    sent = failed = 0
    for i, (path, draft) in enumerate(approved, 1):
        print(f"\n[{i}/{len(approved)}] {draft['to']}  —  {draft['subject']}")
        try:
            send(draft["to"], draft["subject"], draft["body"])
        except Exception as exc:  # noqa: BLE001
            failed += 1
            msg = str(exc)
            hint = ""
            if "401" in msg:
                hint = "  (check SENDGRID_API_KEY)"
            elif "403" in msg:
                hint = "  (is FROM_EMAIL a verified SendGrid sender?)"
            print(f"  ! send failed: {exc}{hint}")
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
        description="Draft custom cold emails with Claude, review, then send via SendGrid."
    )
    parser.add_argument("--send", action="store_true",
                        help="Send the approved drafts (default action is to generate drafts).")
    parser.add_argument("--status", action="store_true",
                        help="Show counts of pending / approved / sent.")
    parser.add_argument("--regenerate", action="store_true",
                        help="When generating, re-draft even if a draft already exists.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N (0 = all).")
    parser.add_argument("--recipients", type=Path, default=ROOT / "recipients.txt",
                        help="Path to the recipients file (default: recipients.txt).")
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
