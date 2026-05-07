# Gmail Triage Agent

An intelligent Gmail agent that classifies your emails, notifies you about important messages, and automatically archives promotional/spam emails. Powered by a hybrid rule engine + Claude AI classifier.

---

## How It Works

Each email is processed through a two-stage pipeline:

1. **Rule engine** — fast keyword/domain scoring (zero cost, runs first)
2. **Claude AI** — called only when rules are indecisive (with prompt caching for cost efficiency)

Each email is classified as:
- **Important** → desktop notification sent immediately, labeled `Auto/Important-Agent`
- **Promotional** → automatically archived (never deleted), labeled `Auto/Archived-Promo`
- **Normal** → left in inbox, labeled `Auto/Processed-Agent` or `Auto/Needs-Review-Agent`

Every action is logged to `gmail_agent_audit.jsonl` and stored in `gmail_agent.db`.

---

## Setup: Step 1 — Google Cloud Console

You need a Google Cloud project with the Gmail API enabled. This is a one-time setup.

### 1.1 Create a Google Cloud project

1. Go to [https://console.cloud.google.com/](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project**
3. Enter a name like `gmail-triage-agent` and click **Create**

### 1.2 Enable the Gmail API

1. In your project, go to **APIs & Services** → **Library**
2. Search for **Gmail API**
3. Click on it and click **Enable**

### 1.3 Configure the OAuth consent screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Choose **External** and click **Create**
3. Fill in:
   - App name: `Gmail Triage Agent`
   - User support email: your Gmail address
   - Developer contact email: your Gmail address
4. Click **Save and Continue** through the remaining steps
5. On the **Test users** page, click **Add Users** and add: `your email id`
6. Click **Save and Continue**

### 1.4 Create OAuth credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Name: `gmail-agent-desktop`
5. Click **Create**
6. Click **Download JSON** on the confirmation dialog
7. Rename the downloaded file to `credentials.json`
8. Move it into the `gmail_agent/` folder (same folder as `main.py`)

---

## Setup: Step 2 — Python Environment

```powershell
# Navigate to the gmail_agent folder
cd "/path/to/gmail_agent"

# Create a virtual environment
python -m venv .venv

# Activate it
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Setup: Step 3 — Configuration

```powershell
# Copy the example config
copy .env.example .env
```

Open `.env` in any text editor and fill in:

```ini
# Required: your Anthropic API key
ANTHROPIC_API_KEY=sk-ant-your-key-here

# Optional: add your university/important domains so they are always Important
WHITELIST_DOMAINS=uchicago.edu,illinois.edu

# Keep DRY_RUN=true until you have tested and are satisfied
DRY_RUN=true
```

Get your Anthropic API key at: https://console.anthropic.com/

---

## Running the Agent

### First run — test in dry-run mode (RECOMMENDED)

```powershell
cd "/path/to/gmail_agent"
.venv\Scripts\activate
python main.py --dry-run --once --verbose
```

This will:
1. Open a browser window asking you to authorize Gmail access (one time only)
2. Fetch your recent unread emails
3. Classify each one and print what it **would** do — without touching your Gmail

### Continuous dry-run

```powershell
python main.py --dry-run --verbose
```

Polls your inbox every 60 seconds (configurable via `POLL_INTERVAL_SECONDS`). No Gmail changes.
(later change POLL_INTERVAL = 86400  // check once a day)
### Enable live mode

Once you are satisfied with the dry-run results:

1. Open `.env` and change `DRY_RUN=true` → `DRY_RUN=false`
2. Run:

```powershell
python main.py
```

### Other commands

```powershell
# One cycle only (for cron jobs or scheduled tasks)
python main.py --once

# Verbose output (see all signals and reasons)
python main.py --verbose

# Restore a wrongly archived email back to inbox
python main.py --restore <message_id>
```

To find the `message_id` of an archived email, check `gmail_agent_audit.jsonl` or `gmail_agent.db`.

---

## Output Format (Dry-Run Example)

```
============================================================
  RUNNING IN DRY-RUN MODE
  No changes will be made to your Gmail account.
============================================================

[10:30:01] --- Poll started | run=abc12345 | mode=DRY-RUN ---
[10:30:02] Important     conf=0.92  rules=+0.40 → LLM:0.91  → NOTIFIED  |  prof@university.edu  |  Project deadline update

==============================================================
  Important Email (HIGH)
==============================================================
  From: prof@university.edu
  Subject: Project deadline update
  Why: Email from professor about upcoming project deadline
  Action: submit
==============================================================

[10:30:03] Promotional   conf=0.91  rules=-0.82               → ARCHIVED  |  deals@store.com  |  50% off today only!
  DRY RUN: Would archive (Promotional) | from=deals@store.com | subject='50% off today only!' | conf=0.91

[10:30:04] Normal        conf=0.55  rules=+0.12 → LLM:0.61  → LABELED_NORMAL  |  news@app.com  |  Your weekly summary
[10:30:05] --- Poll complete | run=abc12345 | processed=3 | errors=0 ---
```

---

## Gmail Labels

The agent creates these labels automatically in your Gmail:

| Label | Meaning |
|-------|---------|
| `Auto/Important-Agent` | Flagged as important by the agent |
| `Auto/Archived-Promo` | Archived as promotional |
| `Auto/Needs-Review-Agent` | Uncertain — review manually |
| `Auto/Processed-Agent` | Seen and processed by the agent |

---

## Configuration Reference

All settings go in `.env`:

| Setting | Default | Description |
|---------|---------|-------------|
| `DRY_RUN` | `true` | Set to `false` to enable real Gmail changes |
| `POLL_INTERVAL_SECONDS` | `60` | How often to check for new emails (seconds) |
| `MAX_EMAILS_PER_RUN` | `50` | Max emails to process per poll cycle |
| `IMPORTANT_CONFIDENCE_THRESHOLD` | `0.75` | Notify if confidence ≥ this |
| `SPAM_CONFIDENCE_THRESHOLD` | `0.85` | Archive if confidence ≥ this |
| `WHITELIST_DOMAINS` | (empty) | Comma-separated domains always classified Important |
| `WHITELIST_SENDERS` | (empty) | Comma-separated email addresses always Important |
| `BLACKLIST_DOMAINS` | (empty) | Comma-separated domains always classified Promotional |
| `DAILY_DIGEST_ENABLED` | `true` | Send a daily summary notification |
| `DAILY_DIGEST_HOUR` | `18` | Hour (0–23 UTC) to send the digest |
| `TELEGRAM_BOT_TOKEN` | (empty) | Optional: Telegram bot token for phone alerts |
| `TELEGRAM_CHAT_ID` | (empty) | Optional: your Telegram chat ID |

---

## Optional: Telegram Notifications

To receive alerts on your phone:

1. Open Telegram, search for `@BotFather`
2. Send `/newbot` and follow the prompts to create a bot
3. Copy the bot token and set `TELEGRAM_BOT_TOKEN=...` in `.env`
4. Start a chat with your new bot, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
5. Find your `chat.id` and set `TELEGRAM_CHAT_ID=...` in `.env`

---

## Audit Log

Every processed email is logged to `gmail_agent_audit.jsonl`. Each line is a JSON object:

```json
{
  "timestamp": "2026-04-26T10:30:00Z",
  "event": "email_classified",
  "from": "professor@university.edu",
  "subject": "Project deadline update",
  "rule_score": 0.62,
  "used_llm": true,
  "llm_confidence": 0.91,
  "cache_hit": true,
  "final_category": "Important",
  "final_confidence": 0.92,
  "reason": "Email from professor about an upcoming deadline.",
  "action_taken": "notified",
  "archived": false,
  "dry_run": true
}
```

You can query the SQLite database `gmail_agent.db` for full records.

---

## Safety Guarantees

- **Emails are never deleted** — archiving only removes from INBOX, still in All Mail
- **Replies are never sent** automatically
- **High-risk senders** (`.edu`, `.gov`, banks) require 90%+ confidence to archive
- **Uncertain emails** are left in inbox with a review label
- **Dry-run mode** makes no changes to Gmail — always test first

---

## Troubleshooting

**`credentials.json not found`**
→ Download it from Google Cloud Console (see Step 1.4) and place it in the `gmail_agent/` folder.

**`ANTHROPIC_API_KEY is not set`**
→ Add your key to `.env`: `ANTHROPIC_API_KEY=sk-ant-...`

**Browser doesn't open for OAuth**
→ Run `python main.py --once` from a terminal (not from an IDE) so the local server can start.

**Desktop notifications not working**
→ Install plyer: `pip install plyer`. On Windows, notifications appear in the Action Center.

**Too many emails being marked as Important**
→ Lower `IMPORTANT_CONFIDENCE_THRESHOLD` to `0.80` in `.env`.

**Promotional emails not being archived**
→ Check you have `DRY_RUN=false` in `.env`. Also verify `SPAM_CONFIDENCE_THRESHOLD` — default 0.85 is conservative.
