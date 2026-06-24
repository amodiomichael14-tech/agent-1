# Cold Email Agent

Give it a CSV of leads (email + optional name, company, and notes). It writes a
**personalized** cold email for each one with Claude. You **review the drafts**,
approve the ones you like, and it sends only those from your **Gmail** account.

Nothing is ever sent without your explicit approval.

---

## How it works (3 steps)

```
1. GENERATE   python send_emails.py
              → writes one draft per lead into drafts/ (each "Approved: no")

2. REVIEW     open the files in drafts/, read them, edit if you want, and change
              "Approved: no" → "Approved: yes" on the ones you want to send

3. SEND       python send_emails.py --send
              → sends ONLY the approved drafts via Gmail, then moves them
                to drafts/sent/
```

Check progress any time with `python send_emails.py --status`.

---

## What you need

1. **Python 3.9+**
2. An **Anthropic API key** — https://console.anthropic.com/ → API Keys
3. A **Gmail App Password** (see below)

---

## Setup (one time)

```bash
pip install -r requirements.txt
cp .env.example .env
#   then open .env and add your API key, Gmail address, and App Password
```

### Getting a Gmail App Password

A normal Gmail password won't work for sending via a script — Google requires an
"App Password". About 2 minutes:

1. Turn on **2-Step Verification**: https://myaccount.google.com/security
2. Go to **App Passwords**: https://myaccount.google.com/apppasswords
3. Create one (name it e.g. `cold-emailer`) and copy the 16-character code.
4. Paste it into `.env` as `GMAIL_APP_PASSWORD` (the spaces are fine).

---

## Your leads file (`leads.csv`)

A CSV with a header row. **Only `email` is required** — `name`, `company`, and
`notes` are optional, and the more you fill in, the more personalized each email
gets. The `notes` field is the secret weapon: Claude weaves it in naturally.

```csv
email,name,company,notes
jane.doe@acme.com,Jane Doe,Acme Co,Runs the online store; site is slow on mobile
mike@brightlabs.io,Mike,Bright Labs,Met briefly at the Denver meetup
owner@corner-bakery.com,,Corner Bakery,No website yet; found them on Google Maps
```

Rows whose email starts with `#` are ignored, so you can keep notes-to-self in
the file. If you only have addresses, a one-column list works too — Claude will
infer a likely name/company from each address as a fallback.

---

## Use it

**1. Edit your pitch** — open `prompt.md` and describe who you are, what you're
offering, and the tone you want. Change it any time; the next run uses it.

**2. Add your leads** to `leads.csv`.

**3. Generate drafts:**

```bash
python send_emails.py
```

This creates `drafts/<address>.txt`, one per lead. Each looks like:

```
To: jane.doe@acme.com
Subject: A quick idea for Acme's website
Approved: no
---
Hi Jane,

...the email...

— Your Name
```

**4. Review** — read each draft. Edit the subject or body however you like.
Change `Approved: no` to `Approved: yes` for every email you want to send.

**5. Send the approved ones:**

```bash
python send_emails.py --send
```

Only drafts marked `Approved: yes` go out. Each sent email is logged to
`sent_log.csv` and its draft is moved to `drafts/sent/`.

### Commands

| Command | What it does |
| --- | --- |
| `python send_emails.py` | Generate drafts for new leads |
| `python send_emails.py --status` | Show pending / approved / sent counts |
| `python send_emails.py --send` | Send the approved drafts |
| `python send_emails.py --limit 5` | Only generate the first 5 (handy for testing) |
| `python send_emails.py --regenerate` | Re-draft even if a draft already exists |

---

## Good to know

- **No double-sends.** Sent addresses are recorded in `sent_log.csv` and skipped
  on future runs. Generating again won't overwrite drafts you've edited (use
  `--regenerate` for a fresh draft).
- **Cost.** Each draft is one short Claude call. `claude-opus-4-8` (default) is
  the most capable; for big lists set `MODEL=claude-haiku-4-5` in `.env` to cut
  cost a lot.
- **Sending limits.** Gmail caps daily sends (~500/day for free Gmail, ~2,000 for
  Workspace). For large lists, send in batches with `--limit`.

---

## Please send responsibly

Cold outreach is legal in many places **with** a few basics, and good practice
everywhere: email people who plausibly want to hear from you, say who you are,
honor opt-outs and replies immediately, and include a real mailing address plus
an unsubscribe line (set `EMAIL_FOOTER` in `.env` to add one to every message).
Rules like CAN-SPAM (US), CASL (Canada), and GDPR/PECR (EU/UK) may apply — check
what's required for your audience.
