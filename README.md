# Cold Email Agent

Give it a list of email addresses. It writes a **custom** cold email for each
one with Claude. You **review the drafts**, approve the ones you like, and it
sends only those — through **SendGrid**.

Nothing is ever sent without your explicit approval.

---

## How it works (3 steps)

```
1. GENERATE   python send_emails.py
              → writes one draft per recipient into drafts/ (each "Approved: no")

2. REVIEW     open the files in drafts/, read them, edit if you want, and change
              "Approved: no" → "Approved: yes" on the ones you want to send

3. SEND       python send_emails.py --send
              → sends ONLY the approved drafts, then moves them to drafts/sent/
```

Check progress any time with `python send_emails.py --status`.

---

## What you need

1. **Python 3.9+**
2. An **Anthropic API key** — https://console.anthropic.com/ → API Keys
3. A **SendGrid account** with an API key and a verified sender (free tier
   sends ~100 emails/day)

---

## Setup (one time)

```bash
pip install -r requirements.txt
cp .env.example .env
#   then open .env and fill in your keys (see below)
```

### SendGrid setup

1. Create a free account: https://signup.sendgrid.com/
2. **Verify a sender or domain** — Settings → *Sender Authentication*. SendGrid
   will not deliver mail "from" an address you haven't verified. Verifying a
   whole domain gives the best deliverability; a single verified sender is the
   quickest start.
3. Create an API key — Settings → *API Keys* → give it **Mail Send** access.
4. Put the key in `.env` as `SENDGRID_API_KEY`, and put your verified address
   in `FROM_EMAIL`.

---

## Use it

**1. Edit your pitch** — open `prompt.md` and describe who you are, what you're
offering, and the tone you want. This is the part you'll tweak most. Change it
any time; the next run uses whatever's there.

**2. Add your list** — one email address per line in `recipients.txt`.

**3. Generate drafts:**

```bash
python send_emails.py
```

This creates `drafts/<address>.txt`, one per recipient. Each looks like:

```
# Review this email. To approve it for sending, change "Approved: no" to "Approved: yes".
# You can freely edit the Subject line and the body below the --- line.
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
| `python send_emails.py` | Generate drafts for new recipients |
| `python send_emails.py --status` | Show pending / approved / sent counts |
| `python send_emails.py --send` | Send the approved drafts |
| `python send_emails.py --limit 5` | Only generate the first 5 (handy for testing) |
| `python send_emails.py --regenerate` | Re-draft even if a draft already exists |

---

## Good to know

- **No double-sends.** Sent addresses are recorded in `sent_log.csv` and skipped
  on future runs. Generating again won't overwrite drafts you've edited (use
  `--regenerate` if you want a fresh draft).
- **Personalization from email-only.** From `jane.doe@acme.com` it infers a
  likely name ("Jane Doe") and company ("Acme") as *hints* for Claude, which
  falls back to a neutral greeting when a guess looks off. The more detail you
  put in `prompt.md`, the better every email reads.
- **Cost.** Each draft is one short Claude call. `claude-opus-4-8` (default) is
  the most capable; for big lists set `MODEL=claude-haiku-4-5` in `.env` to cut
  cost a lot.

---

## Please send responsibly

Cold outreach is legal in many places **with** a few basics, and good practice
everywhere: email people who plausibly want to hear from you, say who you are,
honor opt-outs and replies immediately, and include a real mailing address plus
an unsubscribe line (set `EMAIL_FOOTER` in `.env` to add one to every message).
Rules like CAN-SPAM (US), CASL (Canada), and GDPR/PECR (EU/UK) may apply — check
what's required for your audience.
