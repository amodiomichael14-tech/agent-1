# Cold Email Agent

Give it a list of email addresses. It writes a **custom** cold email for each
one with Claude and sends it from your Gmail account. You control the wording by
editing one plain-text file (`prompt.md`) — change it any time, no code needed.

It's safe by default: it **previews** everything and sends nothing until you add
`--send`, and it never emails the same person twice.

---

## What you need

1. **Python 3.9+**
2. An **Anthropic API key** — https://console.anthropic.com/ → API Keys
3. A **Gmail App Password** (see below)

---

## Setup (one time)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your config file and fill it in
cp .env.example .env
#    then open .env and add your API key, Gmail address, and App Password
```

### Getting a Gmail App Password

A normal Gmail password won't work for sending via script — Google requires an
"App Password". It takes about 2 minutes:

1. Turn on **2-Step Verification**: https://myaccount.google.com/security
2. Go to **App Passwords**: https://myaccount.google.com/apppasswords
3. Create one (name it e.g. `cold-emailer`) and copy the 16-character code.
4. Paste it into `.env` as `GMAIL_APP_PASSWORD` (the spaces are fine).

---

## Use it

**1. Edit your pitch** — open `prompt.md` and describe who you are, what you're
offering, and the tone you want. This is the part you'll tweak most.

**2. Add your list** — put one email address per line in `recipients.txt`.

**3. Dry run** (writes + previews the emails, sends nothing):

```bash
python send_emails.py
```

Read the previews. Happy? Then…

**4. Send for real:**

```bash
python send_emails.py --send
```

That's it. It works through the list on its own, writing a fresh email for each
person and sending it.

### Options

| Command | What it does |
| --- | --- |
| `python send_emails.py` | Dry run — preview only (default) |
| `python send_emails.py --send` | Actually send |
| `python send_emails.py --limit 5` | Only do the first 5 (great for testing) |
| `python send_emails.py --send --yes` | Send without the confirmation prompt |
| `python send_emails.py --send --limit 3` | Send to just the first 3 |

---

## How it personalizes with only an email address

From an address like `jane.doe@acme.com` it infers a likely name ("Jane Doe")
and company ("Acme") and hands those to Claude as *hints*. The prompt tells the
model they might be wrong, so it falls back to a neutral greeting when a guess
looks off. The more you put in `prompt.md`, the better every email reads.

## It won't double-send

Every successful send is recorded in `sent_log.csv`. Run the script again later
and it skips anyone already emailed — so you can keep adding new addresses to
`recipients.txt` and just re-run. (To intentionally re-email someone, remove
their row from `sent_log.csv`.)

## Cost

Each email is one short Claude call. `claude-opus-4-8` (the default) is the most
capable; for high-volume runs set `MODEL=claude-haiku-4-5` (or
`claude-sonnet-4-6`) in `.env` to cut cost significantly.

## Sending limits

Gmail caps daily sends (~500/day for free Gmail, ~2,000 for Workspace). For
large lists, send in batches across days with `--limit`.

---

## Please send responsibly

Cold outreach is legal in many places **with** a few basics, and good practice
everywhere: email people who plausibly want to hear from you, tell them who you
are, honor opt-outs and replies immediately, and include a real mailing address
plus an unsubscribe line (set `EMAIL_FOOTER` in `.env` to add one to every
message automatically). Rules like CAN-SPAM (US), CASL (Canada), and GDPR/PECR
(EU/UK) may apply to you — check what's required for your audience.
