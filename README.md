# Congressional Trading Digest

Emails you a summary of newly-filed U.S. House and Senate financial disclosures
(Periodic Transaction Reports) every morning around 7:00 AM Central, on a free
GitHub Actions schedule.

**Important context before you rely on this:** members of Congress have 30–45
days after a trade to disclose it (STOCK Act). So this digest tells you what was
*filed* in the last few days, not what was *traded* today — there is no way to
make this real-time from the official source. A once-a-day check is genuinely as
fresh as this data gets.

---

## Start here if it just failed

Run the built-in diagnostic. It checks every credential and endpoint
independently and tells you which one is broken, instead of failing with a bare
traceback and exit code 1:

**Actions → "Daily Congressional Disclosure Digest" → Run workflow → mode: `doctor`**

Or locally:

```bash
python scripts/fetch_and_email.py --doctor
```

Sample output:

```
[PASS] RESEND_API_KEY            set (36 chars, re_a...9x)
[WARN] FMP /house-latest         HTTP 403: Exclusive Endpoint...
       -> Your FMP plan does not include /house-latest. The digest
          will fall back to the free House Clerk feed.
[PASS] House Clerk fallback feed HTTP 200, 56,843 bytes
[INFO] Send decision right now: SKIP -- 22:04 CT is outside the 07:00-10:00 CT window
```

The three failures that produce an identical "exit code 1" in the old version:

| Symptom in `--doctor` | Cause | Fix |
|---|---|---|
| `FMP /house-latest HTTP 401` | Key wrong, or has stray whitespace/quotes | Re-paste the `FMP_API_KEY` secret |
| `FMP /house-latest HTTP 402/403` / "Exclusive Endpoint" | Congressional data is not on your FMP plan | Nothing to fix — the digest falls back to the Clerk and parses the filing PDFs for the same detail. You only lose the Senate. |
| `Resend ... testing emails` | Unverified sender can only deliver to your own Resend account address | Set `RECIPIENT_EMAIL` to that address, or verify a domain and set `FROM_EMAIL` |

---

## How it works

1. **Primary source — [Financial Modeling Prep](https://site.financialmodelingprep.com)** (`house-latest`, `senate-latest`).
   Rich data: ticker, transaction type, amount bracket, trade date, filing date.
   Requires an API key, and congressional data is plan-gated on some FMP tiers.
2. **Fallback — the [U.S. House Clerk](https://disclosures-clerk.house.gov).**
   Free, no key, authoritative. The Clerk's index carries only *who* filed and
   *when*, so the digest downloads each filing's PDF and parses the individual
   trades out of it: ticker, asset, purchase/sale, trade date, amount bracket.
   Roughly 90% of filings are electronic and parse cleanly; the remainder are
   scanned images, and those keep a filing-level row linking to the PDF.

If FMP fails for any reason, the run logs exactly why and falls back to the
Clerk. **A bad key or a plan change costs you Senate coverage, not the digest.**
The email carries a banner saying which source it used, and columns that would be
entirely empty are dropped rather than rendered as a wall of em-dashes.

Then it filters to the last `LOOKBACK_DAYS`, drops anything already emailed,
sorts by estimated size, and sends via [Resend](https://resend.com).

## One-time setup

### 1. API keys

- **FMP** (optional) — sign up at [financialmodelingprep.com](https://site.financialmodelingprep.com).
  Skip it and you still get the Clerk-based digest.
- **Resend** (required to send mail) — sign up at [resend.com](https://resend.com)
  (free tier: 3,000 emails/month) **using the address you want the digest sent to**.

  > Without a verified domain, Resend sends from `onboarding@resend.dev` but only
  > *to* the address on your own account. That is fine for personal use — just
  > sign up with your inbox address. For a custom sender, verify a domain in
  > Resend and set the `FROM_EMAIL` secret.

### 2. Add secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Required | Notes |
|---|---|---|
| `RESEND_API_KEY` | yes | |
| `RECIPIENT_EMAIL` | yes | Your inbox |
| `FMP_API_KEY` | no | Omit to run on the free Clerk feed |
| `FROM_EMAIL` | no | Defaults to `onboarding@resend.dev` |

Tuning knobs go under the **Variables** tab (not Secrets), all optional:
`LOOKBACK_DAYS` (3), `INCLUDE_SENATE` (true), `MIN_AMOUNT` (0),
`SEND_WHEN_EMPTY` (false), `ENRICH_PDFS` (true), `MAX_PDFS` (60).

### 3. Test before waiting for 7 AM

**Actions → Run workflow**, choosing a mode:

| Mode | What it does |
|---|---|
| `doctor` | Checks every credential and endpoint. Sends nothing. **Start here.** |
| `dry-run` | Builds the real digest and uploads it as a downloadable HTML artifact. Sends nothing. |
| `send` | Builds and actually emails it, ignoring the time-of-day check. |
| `debug` | Dumps raw API records to the log so you can verify FMP's field names. |

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in; .env is gitignored
python scripts/fetch_and_email.py --doctor
python scripts/fetch_and_email.py --force --dry-run   # writes digest-preview.html
python -m unittest discover -s tests -v
```

## Scheduling, DST, and duplicate sends

GitHub cron runs in UTC and knows nothing about Daylight Saving, so the workflow
fires at **both** plausible UTC offsets (12:00 and 13:00) and the script decides
which firing is real by checking the actual time in `America/Chicago`.

`state/seen.json` is committed back to the repo after each send and records
both the last send date and the IDs of filings already emailed. That single file
does three jobs:

- **No duplicate emails** — the second cron firing sees today's date already
  recorded and exits quietly.
- **Survives a late cron** — GitHub's scheduler is routinely 10–45 minutes late.
  Because the already-sent guard exists, the send window can be a generous
  07:00–10:00 CT instead of a ±20-minute sliver that a late run would miss
  entirely.
- **No repeated filings** — a filing near the lookback boundary is never shown
  twice across consecutive days.

If `state/seen.json` has never been committed, the script falls back to a strict
`hour == 7` check, since without the guard both cron firings would otherwise send.

## Files

| File | Purpose |
|---|---|
| `scripts/fetch_and_email.py` | Fetch, filter, dedupe, render, send. Also `--doctor`. |
| `tests/test_digest.py` | Unit tests for the parsing, dedupe, escaping, and scheduling logic |
| `.github/workflows/daily-digest.yml` | The schedule and the manual test modes |
| `.github/workflows/tests.yml` | Runs the unit tests on push/PR |
| `state/seen.json` | Committed state: last send date + already-emailed filing IDs |
| `.env.example` | Template for local testing |

## Known limitations

- **Scanned filings carry no trade detail.** Around one filing in ten is a
  scanned image rather than an electronic submission, so there is no text to
  parse. Those rows still appear, with a link to the PDF. Reading them would
  need OCR, which is out of scope.
- **Senate data comes only from FMP.** The Senate's own portal has no comparable
  bulk feed, so without a working FMP key the digest is House-only.
- **FMP field names are best-effort.** They are not fully documented and vary by
  plan; `normalize_fmp()` checks the plausible variants. Use `debug` mode if a column
  looks blank.
- **Amounts are brackets, not exact figures.** Members are only required to
  disclose ranges. "Est. value" in the email sums bracket midpoints and is an
  estimate, nothing more.

## Natural next steps

- Filter to a watchlist of tickers or members.
- Cross-reference tickers against pre-market movers for a 7 AM read.
- OCR the ~10% of filings that are scanned images.
