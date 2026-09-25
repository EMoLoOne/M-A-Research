# Consumer M&A Daily Digest

Every morning at **7:00 AM Pacific**, this repo emails **molo@berkeley.edu** a digest of consumer
M&A deals announced in the last 24 hours. Deals are grouped by sector, with the most significant
first. Each deal shows the acquirer, the target, the deal size if disclosed, the sector, and a
one-line description.

It also learns from your feedback. Replies to the email are folded into a running notes file
([`preferences/notes.md`](preferences/notes.md)). That file is read before each digest and
steers sector weighting, filtering, source trust and formatting.

## How a run works

```
GitHub Actions cron (UTC, two slots to cover DST)
  └─ python -m digest run
       1. Guard: only send once per local day, at/after ~7:00 AM America/Los_Angeles
       2. Feedback: read new email replies (IMAP) + feedback/inbox.md
          → Claude updates the profile in preferences/notes.md and logs what changed
       3. Research: one Claude + web search request per sector group, run in parallel.
          The number of searches per sector scales with that sector's weight in the profile,
          and a weight of 0 skips the sector.
       4. Curate: Claude merges and dedupes the results, drops anything outside the window or
          already sent, and scores each deal's significance to you using the profile
       5. Render the HTML email (grouped by sector, ranked) and send it over SMTP
       6. Archive the digest (state/digests/DATE.json) so feedback like "D3 -" can be traced
          to a deal, then commit notes + state back to the repo
```

Sector groups (editable in `config.yaml`): Restaurants & Foodservice, Food & Beverage,
Retail & E-commerce, Consumer Health & Beauty, Pets, and Home, Leisure & Consumer Services.
The last group is the catch-all for other consumer-facing categories.

## Giving feedback

Reply to any digest with:

| You write | Effect |
|---|---|
| `D3 +`, `D7 -`, `D2 ++` | Rates specific deals. The system generalizes modestly from each rating, based on the deal's sector, size, type and source |
| `more pet and vet deals`, `skip deals under $50M` | Explicit rules, applied as you state them |
| `trust Restaurant Business more`, `ignore aggregator sites` | Changes how much each source is trusted |
| `max 5 per sector`, `hide sources`, `show rumors` | Formatting changes |
| `Profile: pets should be 1.0, not 2.0` | Corrects a preference that has drifted |

Each deal also has **▲ More like this / ▼ Less like this** links, which open a pre-filled reply.
If you'd rather not use email, write in [`feedback/inbox.md`](feedback/inbox.md) or edit
`preferences/notes.md` directly.

Only replies from `FEEDBACK_SENDERS` (default: the recipient address) are accepted.

**Preference profile check-in:** every Monday (and on the first run), the email ends with a
"What I've learned" section. It shows the current profile and a summary of recent shifts, so you
can correct anything that has drifted. `python -m digest profile` prints the same thing on demand.

## Setup

1. **Add repository secrets** (Settings → Secrets and variables → Actions):

   | Secret | Required | Notes |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | yes | Claude API key with web search enabled for the org |
   | `SMTP_USERNAME` | yes | Mailbox that sends the digest, e.g. a Gmail address |
   | `SMTP_PASSWORD` | yes | For Gmail, an [app password](https://myaccount.google.com/apppasswords) (needs 2-Step Verification) |
   | `SMTP_HOST` / `SMTP_PORT` | no | Default `smtp.gmail.com` / `465` (port 587 uses STARTTLS) |
   | `SMTP_FROM` | no | Defaults to `SMTP_USERNAME` |
   | `IMAP_HOST` / `IMAP_USERNAME` / `IMAP_PASSWORD` | no | Feedback mailbox. Defaults to `imap.gmail.com` and the SMTP credentials |
   | `FEEDBACK_SENDERS` | no | Comma-separated addresses allowed to give feedback. Default: the recipient |

   The digest's `Reply-To` is the IMAP mailbox, so your replies land where the next run reads them.
   The simplest setup is a dedicated Gmail account (with IMAP enabled) that sends to
   molo@berkeley.edu.

2. **Test it:** Actions → *Daily consumer M&A digest* → *Run workflow*. Tick `dry_run` to get the
   HTML as a downloadable artifact without sending it, or leave `force` checked to send now.

3. The schedule runs automatically from then on. Scheduled workflows only run on the repo's
   **default branch**, so make sure this code is on it.

### Changing the recipient, time or time zone

Edit `config.yaml` (`recipient`, `timezone`, `send_time`) **and** the two `cron:` lines in
`.github/workflows/daily-digest.yml`. The two cron slots should fall about 15–45 minutes before
`send_time` in standard time and in daylight time. For example, for 7:00 AM Eastern use
`41 10 * * *` and `41 11 * * *`.

GitHub can start scheduled jobs 5–30 minutes late, and a run takes about 5–10 minutes. Expect the
email between about 6:50 and 7:30 AM.

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...            # plus SMTP_* / IMAP_* to send and read feedback
python -m digest run --force --dry-run  # writes out/digest.html, sends nothing
python -m digest run --force            # send now
python -m digest feedback               # only process new feedback
python -m digest profile                # show the current profile + what's been learned
python -m digest preview tests/fixtures/sample_digest.json --with-profile  # offline render
python -m pytest
```

## Cost and tuning

Each run makes 6 research requests on `claude-sonnet-5` (up to 8 web searches each by default,
scaled by sector weight), plus 1–3 curation/feedback requests on `claude-opus-5`. Change either in
`config.yaml` under `models`; lower `searches_per_group` to cut cost further. Opus/Fable requests
enable Anthropic's server-side refusal fallback (`fallbacks: "default"`), so a request that a safety
classifier declines is retried on a fallback model rather than dropped.

## Files

| Path | Purpose |
|---|---|
| `digest/` | The pipeline: `research.py`, `curate.py`, `render.py`, `learn.py`, `mailer.py`, `main.py` |
| `config.yaml` | Recipient, schedule, models, sector groups |
| `preferences/notes.md` | Your preference profile + feedback log (the system's memory) |
| `feedback/inbox.md` | Optional manual feedback, cleared after it is processed |
| `state/` | Sent deals (for dedupe), archived digests, run bookkeeping |
