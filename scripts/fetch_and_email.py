#!/usr/bin/env python3
"""
Daily Congressional Trading Disclosure Digest
------------------------------------------------
Pulls newly-FILED U.S. House (and optionally Senate) financial disclosures --
Periodic Transaction Reports -- and emails a summary via Resend.

Data sources, in order of preference:

  1. Financial Modeling Prep (FMP). Rich: ticker, transaction type, amount
     bracket, trade date, filing date. Requires an API key, and the
     congressional endpoints are gated by plan on some FMP tiers.
  2. The U.S. House Clerk's own disclosure index (fallback). Free, no key,
     authoritative. Thinner: it tells you WHO filed and WHEN, plus a link to
     the filing PDF, but not the individual trades -- those only exist inside
     the PDF, often as a scan.

If FMP fails for any reason the script says exactly why and falls back to the
Clerk feed, so a bad key or a plan change degrades the digest instead of
killing it. Run with `--doctor` to test every dependency independently.

Scheduling: the GitHub Actions cron runs in UTC and does not know about US
Daylight Saving Time, so the workflow fires at both plausible UTC offsets and
this script decides which firing is the real one by checking the actual
current time in America/Chicago. A small committed state file records the last
date a digest was sent, which both prevents a double-send and lets a
cron-delayed run still go out (GitHub's scheduler is routinely late).

Environment variables -- see .env.example and the README.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # very old urllib3
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "state" / "seen.json"

CENTRAL = ZoneInfo("America/Chicago")

FMP_BASE = "https://financialmodelingprep.com/stable"
RESEND_URL = "https://api.resend.com/emails"
CLERK_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
CLERK_PTR_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

USER_AGENT = "congress-disclosures-digest/2.0 (+https://github.com/)"

# How long a filing stays in the "already emailed you this" set. Comfortably
# longer than any sane LOOKBACK_DAYS, short enough that the state file stays
# small forever.
SEEN_RETENTION_DAYS = 120


def env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log(f"warning: {name}={raw!r} is not an integer, using {default}")
        return default


def log(msg: str) -> None:
    """Timestamped, unbuffered -- Actions logs interleave badly otherwise."""
    stamp = datetime.now(CENTRAL).strftime("%H:%M:%S")
    print(f"[{stamp} CT] {msg}", flush=True)


def load_dotenv() -> None:
    """Load a local .env if present. No dependency, and a no-op in CI where
    the variables come from Actions secrets instead."""
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("\"", "'") and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        else:
            # Strip a trailing ` # comment`, but only when the # is preceded by
            # whitespace -- otherwise a value like "pass#word" gets truncated.
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        # Real environment always wins over the file.
        os.environ.setdefault(key, value)
    log(f"Loaded local overrides from {path.name}")


load_dotenv()

FMP_API_KEY = env_str("FMP_API_KEY")
RESEND_API_KEY = env_str("RESEND_API_KEY")
RECIPIENT_EMAIL = env_str("RECIPIENT_EMAIL")
FROM_EMAIL = env_str("FROM_EMAIL", "onboarding@resend.dev")

LOOKBACK_DAYS = env_int("LOOKBACK_DAYS", 3)
INCLUDE_SENATE = env_bool("INCLUDE_SENATE", True)
MIN_AMOUNT = env_int("MIN_AMOUNT", 0)
HIGHLIGHT_AMOUNT = env_int("HIGHLIGHT_AMOUNT", 50_000)
SEND_WHEN_EMPTY = env_bool("SEND_WHEN_EMPTY", False)

SEND_HOUR_CT = env_int("SEND_HOUR_CT", 7)
# How many hours past SEND_HOUR_CT a late run is still allowed to send.
# GitHub's scheduler is frequently 10-45 minutes late and occasionally worse.
LATE_GRACE_HOURS = env_int("LATE_GRACE_HOURS", 3)


class SourceError(Exception):
    """A data source failed in a way we can explain to the user."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def make_session() -> requests.Session:
    """Retry transient failures. Without this a single blip in FMP or Resend
    fails the whole day's digest, which is how a daily job quietly rots."""
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


SESSION = make_session()


def redact(text: str) -> str:
    """Never let an API key reach the Actions log, even inside an error body."""
    for secret in (FMP_API_KEY, RESEND_API_KEY):
        if secret and len(secret) > 6:
            text = text.replace(secret, secret[:4] + "..." + secret[-2:])
    # Belt and braces: requests embeds the full request URL in connection
    # errors, so scrub any apikey= parameter regardless of which key it holds.
    return re.sub(r"(apikey=)[^&\s\)\"']+", r"\1<redacted>", text)


# --------------------------------------------------------------------------
# State: what we have already emailed, and when we last sent
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_sent_date": None, "seen": {}, "_existed": False}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"warning: could not read state file ({exc}); starting fresh")
        return {"last_sent_date": None, "seen": {}, "_existed": False}
    data.setdefault("last_sent_date", None)
    data.setdefault("seen", {})
    data["_existed"] = True
    return data


def save_state(state: dict) -> None:
    cutoff = (datetime.now(CENTRAL) - timedelta(days=SEEN_RETENTION_DAYS)).date().isoformat()
    seen = {k: v for k, v in state.get("seen", {}).items() if v >= cutoff}
    payload = {
        "last_sent_date": state.get("last_sent_date"),
        "updated_at": datetime.now(CENTRAL).isoformat(timespec="seconds"),
        "seen": dict(sorted(seen.items())),
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log(f"State saved: {len(seen)} filing id(s) remembered.")


def should_send(state: dict, force: bool, now_ct: datetime | None = None) -> tuple[bool, str]:
    """Decide whether this particular firing is the real 7 AM one.

    `now_ct` is injectable so the DST and late-cron cases can be tested."""
    if force:
        return True, "forced"

    now_ct = now_ct or datetime.now(CENTRAL)
    today = now_ct.date().isoformat()

    if state.get("last_sent_date") == today:
        return False, f"a digest was already sent today ({today})"

    if state.get("_existed"):
        # State is being persisted between runs, so the last_sent_date guard
        # above will stop a second send. That lets us accept a wide window and
        # tolerate a late cron.
        low, high = SEND_HOUR_CT, SEND_HOUR_CT + LATE_GRACE_HOURS
        ok = low <= now_ct.hour < high
        return ok, (
            f"{now_ct:%H:%M} CT is inside the {low:02d}:00-{high:02d}:00 CT window"
            if ok
            else f"{now_ct:%H:%M} CT is outside the {low:02d}:00-{high:02d}:00 CT send window"
        )

    # No state file has ever been committed, so we cannot rely on the
    # already-sent guard. Fall back to the strict single-hour check, which is
    # what keeps the two daily cron firings from both sending.
    ok = now_ct.hour == SEND_HOUR_CT
    return ok, (
        f"{now_ct:%H:%M} CT matches the {SEND_HOUR_CT:02d}:00 hour (no state file yet, strict mode)"
        if ok
        else f"{now_ct:%H:%M} CT is not the {SEND_HOUR_CT:02d}:00 hour (no state file yet, strict mode)"
    )


# --------------------------------------------------------------------------
# Source 1: Financial Modeling Prep
# --------------------------------------------------------------------------


def _fmp_error(resp: requests.Response, endpoint: str) -> SourceError:
    body = redact(resp.text[:400])
    lowered = body.lower()
    if resp.status_code == 401:
        hint = ("FMP rejected the key itself. Check that the FMP_API_KEY secret holds the "
                "whole key with no stray whitespace or quotes.")
    elif resp.status_code == 403 or "exclusive endpoint" in lowered or "not available under your" in lowered:
        hint = (f"Your FMP plan does not include /{endpoint}. Congressional disclosure data is a "
                "paid add-on on some FMP tiers. The digest will fall back to the free House Clerk feed.")
    elif resp.status_code == 429:
        hint = "FMP rate limit hit (the free tier is a few hundred calls/day)."
    else:
        hint = "Unexpected FMP response."
    return SourceError(f"FMP /{endpoint} returned HTTP {resp.status_code}: {body}", hint)


def fetch_fmp(endpoint: str, max_pages: int = 4, page_size: int = 100) -> list[dict]:
    """Page through an FMP 'latest disclosures' endpoint."""
    if not FMP_API_KEY:
        raise SourceError("FMP_API_KEY is not set.",
                          "Add it under Settings -> Secrets and variables -> Actions.")
    records: list[dict] = []
    for page in range(max_pages):
        resp = SESSION.get(
            f"{FMP_BASE}/{endpoint}",
            params={"page": page, "limit": page_size, "apikey": FMP_API_KEY},
            timeout=30,
        )
        if resp.status_code >= 300:
            raise _fmp_error(resp, endpoint)
        try:
            batch = resp.json()
        except ValueError:
            raise SourceError(
                f"FMP /{endpoint} returned non-JSON: {redact(resp.text[:200])}",
                "This usually means an HTML error or maintenance page.",
            )
        # FMP signals some errors with a 200 and an object instead of a list.
        if isinstance(batch, dict):
            raise SourceError(f"FMP /{endpoint} returned an error object: {redact(json.dumps(batch)[:300])}",
                              "Check the message above -- it is usually a key or plan problem.")
        if not isinstance(batch, list) or not batch:
            break
        records.extend(batch)
        if len(batch) < page_size:
            break
    log(f"FMP /{endpoint}: {len(records)} raw record(s).")
    return records


def first(d: dict, keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def parse_amount_midpoint(amount_str):
    """Members disclose amounts as brackets, not exact figures
    (e.g. '$1,001 - $15,000'). Return a rough midpoint for sorting/summing."""
    if not amount_str:
        return None
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", str(amount_str))
            if n.replace(",", "").isdigit()]
    if len(nums) >= 2:
        return (nums[0] + nums[1]) / 2
    if len(nums) == 1:
        return float(nums[0])
    return None


def normalize_date(value) -> str:
    """Return YYYY-MM-DD from the several shapes these feeds use."""
    if not value:
        return ""
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def normalize_fmp(rec: dict, chamber: str) -> dict:
    """FMP's field names are not fully documented and vary by plan/version, so
    check the plausible variants for each logical field. `--debug` dumps raw
    records if something still comes through blank."""
    # NOTE: deliberately no "office" here -- FMP puts the district code (e.g.
    # "CA11") in that field, which would silently replace the member's name.
    name = first(rec, ["representative", "senator", "name", "reportingName"]) or " ".join(
        p for p in (first(rec, ["firstName"], ""), first(rec, ["lastName"], "")) if p
    ).strip()
    amount_raw = first(rec, ["amount", "amountRange", "range"], "")
    return {
        "chamber": chamber,
        "name": (name or "Unknown member").strip(),
        "district": first(rec, ["district", "office", "state"], ""),
        "symbol": (first(rec, ["symbol", "ticker"], "") or "").strip().upper(),
        "asset": first(rec, ["assetDescription", "asset", "assetName"], ""),
        "type": first(rec, ["type", "transactionType"], ""),
        "amount": amount_raw,
        "amount_mid": parse_amount_midpoint(amount_raw),
        "transaction_date": normalize_date(first(rec, ["transactionDate", "date"], "")),
        "disclosure_date": normalize_date(first(rec, ["disclosureDate", "filingDate", "dateRecieved"], "")),
        "link": first(rec, ["link", "url"], ""),
        "source": "FMP",
    }


# --------------------------------------------------------------------------
# Source 2: House Clerk disclosure index (free fallback, no key)
# --------------------------------------------------------------------------

CLERK_FILING_TYPES = {
    "P": "Periodic Transaction Report",
    "A": "Amendment",
    "O": "Annual Report",
    "C": "Candidate Report",
    "D": "Termination Report",
    "W": "Withdrawal",
    "X": "Extension",
    "T": "Termination",
}


def fetch_clerk(years: list[int]) -> list[dict]:
    """Download and parse the House Clerk's own filing index.

    This is the authoritative source and needs no API key, but the index only
    lists WHO filed and WHEN -- the actual trades live inside the linked PDF,
    which is frequently a scan. So this is a graceful degradation, not a
    replacement for FMP.
    """
    items: list[dict] = []
    errors: list[str] = []
    for year in years:
        url = CLERK_ZIP.format(year=year)
        try:
            resp = SESSION.get(url, timeout=60)
            if resp.status_code >= 300:
                errors.append(f"{year}: HTTP {resp.status_code}")
                continue
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                if not names:
                    errors.append(f"{year}: no .txt index inside the zip")
                    continue
                raw = zf.read(names[0]).decode("utf-8-sig", errors="replace")
        except (requests.RequestException, zipfile.BadZipFile) as exc:
            errors.append(f"{year}: {exc}")
            continue

        lines = raw.splitlines()
        if not lines:
            continue
        headers = [h.strip() for h in lines[0].split("\t")]
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) != len(headers):
                continue
            row = dict(zip(headers, (p.strip() for p in parts)))
            if row.get("FilingType") != "P":
                continue  # trades only
            doc_id = row.get("DocID", "")
            name = " ".join(p for p in (row.get("First", ""), row.get("Last", ""), row.get("Suffix", "")) if p)
            items.append({
                "chamber": "House",
                "name": name.strip() or "Unknown member",
                "district": row.get("StateDst", ""),
                "symbol": "",
                "asset": "",  # the Clerk index carries no per-trade detail
                "type": "",
                "amount": "",
                "amount_mid": None,
                "transaction_date": "",
                "disclosure_date": normalize_date(row.get("FilingDate", "")),
                "link": CLERK_PTR_PDF.format(year=row.get("Year", year), doc_id=doc_id) if doc_id else "",
                "source": "House Clerk",
            })
    if errors and not items:
        raise SourceError("House Clerk feed unavailable: " + "; ".join(errors),
                          "The Clerk site may be down or blocking the runner.")
    if errors:
        log(f"warning: partial Clerk fetch -- {'; '.join(errors)}")
    log(f"House Clerk: {len(items)} PTR filing(s) across {years}.")
    return items


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def filing_id(item: dict) -> str:
    """Stable identity for a filing, so we never email the same one twice.

    Prefer the filing link (which embeds the Clerk document ID); otherwise
    hash the fields that together identify a single disclosed transaction."""
    if item.get("link"):
        basis = item["link"]
    else:
        basis = "|".join(str(item.get(k, "")) for k in
                         ("chamber", "name", "symbol", "type", "amount",
                          "transaction_date", "disclosure_date"))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def within_lookback(item: dict, days: int) -> bool:
    date_str = item.get("disclosure_date") or item.get("transaction_date")
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    today = datetime.now(CENTRAL).date()
    # Reject filing dates in the future -- a typo in a feed should not pin an
    # item to the top of the digest forever.
    return (today - timedelta(days=days)) <= d <= (today + timedelta(days=1))


def is_sale(item: dict) -> bool:
    return "sale" in str(item.get("type", "")).lower() or "sold" in str(item.get("type", "")).lower()


def is_purchase(item: dict) -> bool:
    t = str(item.get("type", "")).lower()
    return "purchase" in t or "buy" in t or "bought" in t


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def safe_link(url) -> str:
    """Only emit links we actually trust -- feed content is untrusted input and
    ends up inside an email we open."""
    text = str(url or "").strip()
    if text.startswith("https://") or text.startswith("http://"):
        return esc(text)
    return ""


def money(value) -> str:
    if value is None:
        return ""
    return f"${value:,.0f}"


def build_subject(items: list[dict]) -> str:
    today_str = datetime.now(CENTRAL).strftime("%b %-d")
    if not items:
        return f"Congressional Trading Digest — {today_str} — nothing new"
    tickers = [i["symbol"] for i in items if i.get("symbol")]
    top = ", ".join(t for t, _ in Counter(tickers).most_common(3))
    lead = f" — {top}" if top else ""
    return f"Congressional Trading Digest — {today_str} — {len(items)} new filing(s){lead}"


def build_email_html(items: list[dict], lookback_days: int, notes: list[str]) -> str:
    today_str = datetime.now(CENTRAL).strftime("%A, %B %d, %Y")
    note_html = ""
    if notes:
        note_html = (
            "<div style='margin:12px 0;padding:10px 12px;background:#fff8e1;"
            "border-left:3px solid #f0b400;font-size:12px;color:#5c4600;'>"
            + "<br>".join(esc(n) for n in notes)
            + "</div>"
        )

    disclaimer = (
        "<p style='color:#666;font-size:12px;line-height:1.5;'>"
        "Under the STOCK Act members have 30–45 days to disclose a trade, so this "
        "reflects what was <em>filed</em> recently, not what was traded recently. "
        "Amounts are the brackets members are required to report, not exact figures; "
        "totals below use bracket midpoints and are estimates only."
        "</p>"
    )

    if not items:
        return (
            f"<div style=\"font-family:-apple-system,Segoe UI,sans-serif;\">"
            f"<h2 style='margin-bottom:4px;'>Congressional Trading Digest</h2>"
            f"<div style='color:#666;font-size:13px;'>{esc(today_str)}</div>"
            f"{note_html}"
            f"<p>No new disclosures filed in the last {lookback_days} day(s).</p>"
            f"{disclaimer}</div>"
        )

    items = sorted(items, key=lambda r: (r.get("amount_mid") or 0), reverse=True)

    buys = sum(1 for i in items if is_purchase(i))
    sells = sum(1 for i in items if is_sale(i))
    est_total = sum(i["amount_mid"] for i in items if i.get("amount_mid"))
    tickers = Counter(i["symbol"] for i in items if i.get("symbol"))
    members = len({i["name"] for i in items})

    def stat(label: str, value: str) -> str:
        return (f"<td style='padding:8px 14px 8px 0;'>"
                f"<div style='font-size:11px;color:#777;text-transform:uppercase;"
                f"letter-spacing:.04em;'>{esc(label)}</div>"
                f"<div style='font-size:18px;font-weight:600;color:#111;'>{esc(value)}</div></td>")

    stats = "".join([
        stat("Filings", str(len(items))),
        stat("Members", str(members)),
        stat("Buys", str(buys)) if buys else "",
        stat("Sells", str(sells)) if sells else "",
        stat("Est. value", money(est_total)) if est_total else "",
    ])

    top_tickers = ""
    if tickers:
        chips = "".join(
            f"<span style='display:inline-block;padding:2px 8px;margin:2px 4px 2px 0;"
            f"background:#eef2f7;border-radius:10px;font-size:12px;'>"
            f"<b>{esc(sym)}</b> ×{count}</span>"
            for sym, count in tickers.most_common(8)
        )
        top_tickers = f"<div style='margin:10px 0 16px;'>{chips}</div>"

    cell = "padding:7px 10px;border-bottom:1px solid #eee;vertical-align:top;"

    def member_cell(it: dict) -> str:
        sub = " · ".join(p for p in (it.get("district"), it.get("chamber")) if p)
        sub_html = f"<div style='color:#888;font-size:11px;'>{esc(sub)}</div>" if sub else ""
        return f"{esc(it['name'])}{sub_html}"

    def type_cell(it: dict) -> str:
        text = esc(it.get("type")) or "—"
        if is_purchase(it):
            return f"<span style='color:#046c4e;font-weight:600;'>{text}</span>"
        if is_sale(it):
            return f"<span style='color:#b42318;font-weight:600;'>{text}</span>"
        return text

    def link_cell(it: dict) -> str:
        link = safe_link(it.get("link"))
        return f"<a href=\"{link}\" style='color:#1a56db;'>filing</a>" if link else "—"

    def plain(key: str, extra: str = ""):
        return lambda it: f"<span style='{extra}'>{esc(it.get(key)) or '—'}</span>" if extra \
            else (esc(it.get(key)) or "—")

    # (header, renderer, populated-predicate). A column whose predicate is false
    # for every row is dropped entirely -- otherwise the House Clerk fallback,
    # which has no per-trade detail, renders five columns of em-dashes.
    columns = [
        ("Member", member_cell, lambda it: True),
        ("Ticker", plain("symbol", "font-weight:600;"), lambda it: bool(it.get("symbol"))),
        ("Asset", plain("asset"), lambda it: bool(it.get("asset"))),
        ("Type", type_cell, lambda it: bool(it.get("type"))),
        ("Amount", plain("amount"), lambda it: bool(it.get("amount"))),
        ("Traded", plain("transaction_date"), lambda it: bool(it.get("transaction_date"))),
        ("Filed", plain("disclosure_date"), lambda it: bool(it.get("disclosure_date"))),
        ("", link_cell, lambda it: bool(safe_link(it.get("link")))),
    ]
    columns = [c for c in columns if any(c[2](it) for it in items)]

    rows = ""
    for it in items:
        big = (it.get("amount_mid") or 0) >= HIGHLIGHT_AMOUNT
        row_bg = "background:#fffbea;" if big else ""
        cells = "".join(f"<td style='{cell}'>{render(it)}</td>" for _, render, _ in columns)
        rows += f"<tr style='{row_bg}'>{cells}</tr>"

    head_cell = ("padding:7px 10px;border-bottom:2px solid #ddd;text-align:left;font-size:11px;"
                 "color:#555;text-transform:uppercase;letter-spacing:.04em;")
    header_html = "".join(f'<th style="{head_cell}">{esc(h)}</th>' for h, _, _ in columns)

    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111;">
  <h2 style="margin:0 0 4px;">Congressional Trading Digest</h2>
  <div style="color:#666;font-size:13px;">{esc(today_str)} · filed in the last {lookback_days} day(s)</div>
  {note_html}
  <table style="border-collapse:collapse;margin:14px 0 4px;"><tr>{stats}</tr></table>
  {top_tickers}
  <table style="border-collapse:collapse;font-size:13px;width:100%;">
    <thead><tr>{header_html}</tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {disclaimer}
</div>"""


def send_email(subject: str, html_body: str) -> None:
    if not RESEND_API_KEY:
        raise SourceError("RESEND_API_KEY is not set.",
                          "Add it under Settings -> Secrets and variables -> Actions.")
    if not RECIPIENT_EMAIL:
        raise SourceError("RECIPIENT_EMAIL is not set.", "Add it as a repository secret.")

    resp = SESSION.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": FROM_EMAIL, "to": [RECIPIENT_EMAIL], "subject": subject, "html": html_body},
        timeout=30,
    )
    if resp.status_code >= 300:
        body = redact(resp.text[:400])
        lowered = body.lower()
        if resp.status_code in (401, 403) and "testing emails" in lowered:
            hint = (f"Resend only lets an unverified sender deliver to the address on your Resend "
                    f"account. Either set RECIPIENT_EMAIL to that address, or verify a domain in "
                    f"Resend and set FROM_EMAIL to an address on it. Currently sending "
                    f"{FROM_EMAIL} -> {RECIPIENT_EMAIL}.")
        elif resp.status_code in (401, 403):
            hint = "Resend rejected the API key, the from-address, or the recipient. See the body above."
        elif resp.status_code == 422:
            hint = f"Resend rejected the payload -- check that FROM_EMAIL ({FROM_EMAIL}) is a valid address."
        else:
            hint = "Unexpected Resend response."
        raise SourceError(f"Resend returned HTTP {resp.status_code}: {body}", hint)
    log(f"Email sent to {RECIPIENT_EMAIL}: {redact(resp.text[:200])}")


# --------------------------------------------------------------------------
# Data gathering
# --------------------------------------------------------------------------


def gather(debug: bool = False) -> tuple[list[dict], list[str]]:
    """Return (items, notes). Notes explain any degradation to the reader."""
    notes: list[str] = []
    items: list[dict] = []
    house_ok = False

    if FMP_API_KEY:
        endpoints = [("house-latest", "House")]
        if INCLUDE_SENATE:
            endpoints.append(("senate-latest", "Senate"))
        for endpoint, chamber in endpoints:
            try:
                raw = fetch_fmp(endpoint)
                if debug:
                    print(f"\n--- raw {endpoint} (first 3 of {len(raw)}) ---")
                    print(json.dumps(raw[:3], indent=2))
                items.extend(normalize_fmp(r, chamber) for r in raw)
                if chamber == "House":
                    house_ok = True
            except SourceError as exc:
                log(f"FMP {chamber} failed: {exc}")
                if exc.hint:
                    log(f"  -> {exc.hint}")
                notes.append(f"{chamber} data via FMP unavailable: {exc.hint or exc}")
            except requests.RequestException as exc:
                # Connection/DNS/TLS failure rather than an HTTP status. Treat it
                # the same way, so a network blip still falls back to the Clerk
                # feed instead of failing the whole run.
                detail = redact(str(exc))[:200]
                log(f"FMP {chamber} network failure: {detail}")
                notes.append(f"{chamber} data via FMP unreachable (network error).")
    else:
        log("FMP_API_KEY not set -- using the free House Clerk feed only.")
        notes.append("No FMP key configured; showing House Clerk filings only (no per-trade detail).")

    if not house_ok:
        today = datetime.now(CENTRAL).date()
        years = sorted({today.year, (today - timedelta(days=LOOKBACK_DAYS + 5)).year})
        try:
            clerk = fetch_clerk(years)
            items.extend(clerk)
            notes.append("Falling back to the House Clerk index: filings and PDF links only, "
                         "no ticker/amount detail (those live inside the PDF).")
        except SourceError as exc:
            log(f"Clerk fallback failed: {exc}")
            notes.append(f"House Clerk fallback also failed: {exc}")

    return items, notes


# --------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------


def cmd_doctor() -> int:
    """Check every dependency independently and say exactly what is broken.

    This exists because the previous version failed with a bare traceback and
    exit code 1, which cannot distinguish 'bad FMP key' from 'plan does not
    include this endpoint' from 'Resend refused the recipient'.
    """
    in_actions = bool(os.environ.get("GITHUB_ACTIONS"))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    secrets_url = (f"https://github.com/{repo}/settings/secrets/actions"
                   if repo else "Settings -> Secrets and variables -> Actions")

    print("=" * 68)
    print("  Congressional Trading Digest -- configuration check")
    print("=" * 68)
    failures = 0
    remedies: list[str] = []

    def check(label: str, ok: bool, detail: str = "", optional: bool = False,
              fix: str = "") -> None:
        nonlocal failures
        mark = "PASS" if ok else ("WARN" if optional else "FAIL")
        print(f"[{mark}] {label}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")
        if ok:
            return
        if fix:
            remedies.append(fix)
        if optional:
            # Surfaces in the Actions UI without failing the job.
            if in_actions:
                print(f"::warning title={label}::{detail.splitlines()[0] if detail else 'check failed'}")
            return
        failures += 1
        if in_actions:
            print(f"::error title={label}::{detail.splitlines()[0] if detail else 'check failed'}")

    # 1. Environment
    def mask(v: str) -> str:
        return f"set ({len(v)} chars, {v[:4]}...{v[-2:]})" if len(v) > 6 else ("set (short!)" if v else "MISSING")

    check("FMP_API_KEY", bool(FMP_API_KEY), mask(FMP_API_KEY) + ("" if FMP_API_KEY else
          "\nOptional -- without it the digest falls back to the House Clerk feed."), optional=True,
          fix=f"Optional: add the repository secret FMP_API_KEY for ticker/amount detail  ({secrets_url})")
    check("RESEND_API_KEY", bool(RESEND_API_KEY), mask(RESEND_API_KEY),
          fix=f"Add the repository secret RESEND_API_KEY  ({secrets_url})")
    check("RECIPIENT_EMAIL", bool(RECIPIENT_EMAIL), RECIPIENT_EMAIL or "MISSING",
          fix=f"Add the repository secret RECIPIENT_EMAIL  ({secrets_url})")
    check("FROM_EMAIL", bool(FROM_EMAIL), FROM_EMAIL)

    # 2. Timezone data
    try:
        now_ct = datetime.now(CENTRAL)
        check("Timezone (America/Chicago)", True,
              f"now = {now_ct:%Y-%m-%d %H:%M %Z} (UTC offset {now_ct:%z})")
    except Exception as exc:
        check("Timezone (America/Chicago)", False, f"{exc}\nInstall tzdata (it is in requirements.txt).")

    # 3. FMP endpoints
    if FMP_API_KEY:
        for endpoint in ("house-latest", "senate-latest"):
            try:
                resp = SESSION.get(f"{FMP_BASE}/{endpoint}",
                                   params={"page": 0, "limit": 1, "apikey": FMP_API_KEY}, timeout=30)
                if resp.status_code >= 300:
                    err = _fmp_error(resp, endpoint)
                    check(f"FMP /{endpoint}", False, f"{err}\n-> {err.hint}", optional=True)
                else:
                    data = resp.json()
                    if isinstance(data, list):
                        sample = list(data[0].keys()) if data else []
                        check(f"FMP /{endpoint}", True,
                              f"HTTP 200, {len(data)} record(s). Fields: {', '.join(sample) or 'n/a'}")
                    else:
                        check(f"FMP /{endpoint}", False,
                              f"HTTP 200 but body is not a list: {redact(json.dumps(data)[:200])}")
            except requests.RequestException as exc:
                check(f"FMP /{endpoint}", False, str(exc), optional=True)
    else:
        print("[SKIP] FMP endpoints (no key configured)")

    # 4. Resend key -- validated without sending anything
    if RESEND_API_KEY:
        try:
            resp = SESSION.get("https://api.resend.com/domains",
                               headers={"Authorization": f"Bearer {RESEND_API_KEY}"}, timeout=30)
            if resp.status_code == 200:
                domains = (resp.json() or {}).get("data") or []
                verified = [d.get("name") for d in domains if d.get("status") == "verified"]
                detail = f"Key valid. Verified domains: {', '.join(verified) if verified else 'none'}"
                if not verified and not FROM_EMAIL.endswith("@resend.dev"):
                    detail += (f"\nWARNING: FROM_EMAIL is {FROM_EMAIL} but no domain is verified. "
                               f"Resend will reject this. Use onboarding@resend.dev instead.")
                if not verified:
                    detail += ("\nNote: with no verified domain, Resend only delivers to the address "
                               "on your Resend account. RECIPIENT_EMAIL must be that address.")
                check("Resend API key", True, detail)
            else:
                check("Resend API key", False,
                      f"HTTP {resp.status_code}: {redact(resp.text[:200])}\n-> The key is wrong or revoked.")
        except requests.RequestException as exc:
            check("Resend API key", False, str(exc))
    else:
        print("[SKIP] Resend key check (no key configured)")

    # 5. Clerk fallback
    try:
        year = datetime.now(CENTRAL).year
        resp = SESSION.get(CLERK_ZIP.format(year=year), timeout=60)
        ok = resp.status_code == 200 and resp.content[:2] == b"PK"
        check("House Clerk fallback feed", ok,
              f"HTTP {resp.status_code}, {len(resp.content):,} bytes from {CLERK_ZIP.format(year=year)}")
    except requests.RequestException as exc:
        check("House Clerk fallback feed", False, str(exc))

    # 6. State file
    state = load_state()
    check("State file", True,
          f"{STATE_PATH.relative_to(REPO_ROOT)}: "
          + (f"present, last sent {state.get('last_sent_date')}, {len(state.get('seen', {}))} id(s) remembered"
             if state.get("_existed") else
             "not committed yet -- the send-window check runs in strict single-hour mode until it is"))

    # 7. Send window
    ok, reason = should_send(state, force=False)
    print(f"[INFO] Send decision right now: {'SEND' if ok else 'SKIP'} -- {reason}")

    print("-" * 68)
    if remedies:
        print("WHAT TO DO NEXT")
        for i, remedy in enumerate(remedies, 1):
            print(f"  {i}. {remedy}")
        if not (RESEND_API_KEY and RECIPIENT_EMAIL):
            print()
            print("  If you believe you already added these: repository secrets are")
            print("  per-repository and are NOT inherited from another repo, an")
            print("  organisation without access granted, or a fork. Environment")
            print("  secrets also need `environment:` declared on the job, which this")
            print("  workflow does not use -- add them as *repository* secrets.")
        print("-" * 68)
    print(f"{failures} required check(s) failed." if failures else "All required checks passed.")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write("## Digest configuration check\n\n")
                fh.write(f"**{failures} required check(s) failed.**\n\n" if failures
                         else "**All required checks passed.**\n\n")
                if remedies:
                    fh.write("### What to do next\n\n")
                    for remedy in remedies:
                        fh.write(f"- {remedy}\n")
        except OSError:
            pass

    return 1 if failures else 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the congressional trading digest.")
    parser.add_argument("--doctor", action="store_true",
                        help="check every dependency and report what is broken; send nothing")
    parser.add_argument("--force", action="store_true",
                        help="bypass the time-of-day and already-sent-today checks")
    parser.add_argument("--debug", action="store_true",
                        help="print raw API records so field names can be verified")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the digest and write it to digest-preview.html instead of emailing")
    args = parser.parse_args(argv)

    # Env equivalents, so the workflow can drive this without changing args.
    force = args.force or env_bool("FORCE_RUN")
    debug = args.debug or env_bool("DEBUG_DUMP")
    dry_run = args.dry_run or env_bool("DRY_RUN")

    if args.doctor:
        return cmd_doctor()

    state = load_state()
    ok, reason = should_send(state, force)
    if not ok:
        log(f"Not sending: {reason}. Exiting cleanly.")
        return 0
    log(f"Sending: {reason}.")

    items, notes = gather(debug=debug)
    if debug:
        log(f"Debug dump complete: {len(items)} normalized item(s). No email sent.")
        print(json.dumps(items[:5], indent=2, default=str))
        return 0

    recent = [i for i in items if within_lookback(i, LOOKBACK_DAYS)]
    log(f"{len(recent)} of {len(items)} item(s) fall inside the {LOOKBACK_DAYS}-day lookback.")

    if MIN_AMOUNT > 0:
        before = len(recent)
        recent = [i for i in recent if (i.get("amount_mid") or 0) >= MIN_AMOUNT
                  or i.get("amount_mid") is None]
        log(f"MIN_AMOUNT={MIN_AMOUNT}: {before} -> {len(recent)} item(s).")

    seen = state.get("seen", {})
    fresh, duplicates = [], 0
    for item in recent:
        fid = filing_id(item)
        if fid in seen:
            duplicates += 1
            continue
        item["_id"] = fid
        fresh.append(item)
    if duplicates:
        log(f"Skipped {duplicates} filing(s) already included in a previous digest.")

    if not fresh and not SEND_WHEN_EMPTY and not force:
        log("Nothing new to report and SEND_WHEN_EMPTY is false -- not sending.")
        # Still record the date so a later firing today does not re-check.
        state["last_sent_date"] = datetime.now(CENTRAL).date().isoformat()
        save_state(state)
        return 0

    subject = build_subject(fresh)
    body = build_email_html(fresh, LOOKBACK_DAYS, notes)

    if dry_run:
        out = REPO_ROOT / "digest-preview.html"
        out.write_text(body, encoding="utf-8")
        log(f"Dry run: subject = {subject!r}")
        log(f"Dry run: wrote {out} ({len(body):,} bytes). No email sent, state unchanged.")
        return 0

    send_email(subject, body)

    today = datetime.now(CENTRAL).date().isoformat()
    for item in fresh:
        seen[item["_id"]] = today
    state["seen"] = seen
    state["last_sent_date"] = today
    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SourceError as exc:
        log(f"ERROR: {exc}")
        if exc.hint:
            log(f"  -> {exc.hint}")
        log("Run `python scripts/fetch_and_email.py --doctor` to check every dependency.")
        sys.exit(1)
    except requests.RequestException as exc:
        log(f"ERROR: network failure: {redact(str(exc))}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
