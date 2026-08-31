"""Unit tests for the digest's pure logic. No network, no email.

Run:  python -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_and_email as m  # noqa: E402

CT = m.CENTRAL


def ct(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CT)


class TestAmountParsing(unittest.TestCase):
    def test_bracket_midpoint(self):
        self.assertEqual(m.parse_amount_midpoint("$1,001 - $15,000"), 8000.5)
        self.assertEqual(m.parse_amount_midpoint("$1,000,001 - $5,000,000"), 3000000.5)

    def test_single_value_and_junk(self):
        self.assertEqual(m.parse_amount_midpoint("Over $50,000,000"), 50000000.0)
        self.assertIsNone(m.parse_amount_midpoint(""))
        self.assertIsNone(m.parse_amount_midpoint(None))
        self.assertIsNone(m.parse_amount_midpoint("undetermined"))


class TestDateNormalisation(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(m.normalize_date("2026-08-21"), "2026-08-21")
        self.assertEqual(m.normalize_date("8/21/2026"), "2026-08-21")   # Clerk feed
        self.assertEqual(m.normalize_date("2026-08-21T00:00:00Z"), "2026-08-21")
        self.assertEqual(m.normalize_date("garbage"), "")
        self.assertEqual(m.normalize_date(None), "")


class TestNormalizeFMP(unittest.TestCase):
    def test_office_does_not_masquerade_as_the_member_name(self):
        # Regression: "office" holds a district code like "CA11". If it is in
        # the name-candidate list it silently replaces the member's name.
        rec = {"firstName": "Nancy", "lastName": "Pelosi", "office": "CA11", "district": "CA11"}
        out = m.normalize_fmp(rec, "House")
        self.assertEqual(out["name"], "Nancy Pelosi")
        self.assertEqual(out["district"], "CA11")

    def test_representative_field_wins(self):
        out = m.normalize_fmp({"representative": "Josh Gottheimer"}, "House")
        self.assertEqual(out["name"], "Josh Gottheimer")

    def test_ticker_is_uppercased_and_chamber_tagged(self):
        out = m.normalize_fmp({"ticker": " nvda "}, "Senate")
        self.assertEqual(out["symbol"], "NVDA")
        self.assertEqual(out["chamber"], "Senate")

    def test_missing_everything_still_yields_a_row(self):
        out = m.normalize_fmp({}, "House")
        self.assertEqual(out["name"], "Unknown member")
        self.assertIsNone(out["amount_mid"])


class TestLookback(unittest.TestCase):
    def setUp(self):
        self.today = datetime.now(CT).date()

    def item(self, days_ago):
        return {"disclosure_date": (self.today - timedelta(days=days_ago)).isoformat(),
                "transaction_date": ""}

    def test_inside_and_outside(self):
        self.assertTrue(m.within_lookback(self.item(0), 3))
        self.assertTrue(m.within_lookback(self.item(3), 3))
        self.assertFalse(m.within_lookback(self.item(4), 3))

    def test_far_future_dates_are_rejected(self):
        # A typo'd feed date must not pin an item to the digest forever.
        future = {"disclosure_date": (self.today + timedelta(days=30)).isoformat(),
                  "transaction_date": ""}
        self.assertFalse(m.within_lookback(future, 3))

    def test_falls_back_to_transaction_date(self):
        item = {"disclosure_date": "", "transaction_date": self.today.isoformat()}
        self.assertTrue(m.within_lookback(item, 3))

    def test_no_usable_date(self):
        self.assertFalse(m.within_lookback({"disclosure_date": "", "transaction_date": ""}, 3))


class TestFilingIdentity(unittest.TestCase):
    def test_link_gives_a_stable_id(self):
        a = {"link": "https://example.gov/a.pdf", "name": "X"}
        b = {"link": "https://example.gov/a.pdf", "name": "Y different"}
        self.assertEqual(m.filing_id(a), m.filing_id(b))

    def test_distinct_trades_get_distinct_ids(self):
        base = {"chamber": "House", "name": "A", "symbol": "NVDA", "type": "Purchase",
                "amount": "$1,001 - $15,000", "transaction_date": "2026-08-01",
                "disclosure_date": "2026-08-20", "link": ""}
        other = dict(base, symbol="AAPL")
        self.assertNotEqual(m.filing_id(base), m.filing_id(other))
        self.assertEqual(m.filing_id(base), m.filing_id(dict(base)))


class TestRenderingSafety(unittest.TestCase):
    def test_html_is_escaped(self):
        item = m.normalize_fmp(
            {"representative": "A", "assetDescription": "<script>alert(1)</script>",
             "disclosureDate": datetime.now(CT).date().isoformat(), "symbol": "X",
             "type": "Purchase", "amount": "$1,001 - $15,000"}, "House")
        html = m.build_email_html([item], 3, [])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_unsafe_link_schemes_are_dropped(self):
        self.assertEqual(m.safe_link("javascript:alert(1)"), "")
        self.assertEqual(m.safe_link("data:text/html,x"), "")
        self.assertEqual(m.safe_link(""), "")
        self.assertTrue(m.safe_link("https://example.gov/a.pdf"))

    def test_empty_columns_are_dropped(self):
        # House Clerk fallback shape: no ticker/type/amount/trade date.
        item = {"chamber": "House", "name": "A", "district": "CA11", "symbol": "",
                "asset": "", "type": "", "amount": "", "amount_mid": None,
                "transaction_date": "", "disclosure_date": "2026-08-21",
                "link": "https://example.gov/a.pdf"}
        html = m.build_email_html([item], 3, [])
        self.assertNotIn(">TICKER<", html.upper())
        self.assertIn("FILED", html.upper())

    def test_empty_digest_renders(self):
        self.assertIn("No new disclosures", m.build_email_html([], 3, []))


class TestSendWindow(unittest.TestCase):
    """The scheduling logic is the part most likely to fail silently."""

    WITH_STATE = {"last_sent_date": None, "seen": {}, "_existed": True}

    def test_cdt_first_firing_sends_second_is_blocked_by_state(self):
        # During CDT: 12:00 UTC = 07:00 CT, 13:00 UTC = 08:00 CT.
        ok, _ = m.should_send(dict(self.WITH_STATE), False, ct(2026, 8, 30, 7, 0))
        self.assertTrue(ok)
        sent = {"last_sent_date": "2026-08-30", "seen": {}, "_existed": True}
        ok, why = m.should_send(sent, False, ct(2026, 8, 30, 8, 0))
        self.assertFalse(ok)
        self.assertIn("already sent today", why)

    def test_cst_first_firing_is_too_early(self):
        # During CST: 12:00 UTC = 06:00 CT -- must not send.
        ok, _ = m.should_send(dict(self.WITH_STATE), False, ct(2026, 12, 15, 6, 0))
        self.assertFalse(ok)
        ok, _ = m.should_send(dict(self.WITH_STATE), False, ct(2026, 12, 15, 7, 0))
        self.assertTrue(ok)

    def test_late_cron_still_sends(self):
        # GitHub's scheduler is routinely late; 40 minutes used to mean no email.
        ok, _ = m.should_send(dict(self.WITH_STATE), False, ct(2026, 8, 30, 7, 40))
        self.assertTrue(ok)
        ok, _ = m.should_send(dict(self.WITH_STATE), False, ct(2026, 8, 30, 9, 30))
        self.assertTrue(ok)

    def test_outside_window_entirely(self):
        ok, _ = m.should_send(dict(self.WITH_STATE), False, ct(2026, 8, 30, 22, 0))
        self.assertFalse(ok)

    def test_strict_mode_without_a_state_file(self):
        # With no committed state there is no already-sent guard, so only the
        # exact hour may send -- otherwise both cron firings would email.
        fresh = {"last_sent_date": None, "seen": {}, "_existed": False}
        self.assertTrue(m.should_send(dict(fresh), False, ct(2026, 8, 30, 7, 55))[0])
        self.assertFalse(m.should_send(dict(fresh), False, ct(2026, 8, 30, 8, 5))[0])

    def test_force_overrides_everything(self):
        sent = {"last_sent_date": "2026-08-30", "seen": {}, "_existed": True}
        self.assertTrue(m.should_send(sent, True, ct(2026, 8, 30, 22, 0))[0])


class TestRedaction(unittest.TestCase):
    """requests embeds the full request URL -- including ?apikey=... -- in
    connection-error messages, and those strings reach the Actions log AND the
    email body. Both scrubbing paths must hold."""

    def test_known_key_is_masked_anywhere_it_appears(self):
        m.FMP_API_KEY = "abcd1234secretkey"
        try:
            out = m.redact("connection to host failed carrying abcd1234secretkey inline")
            self.assertNotIn("abcd1234secretkey", out)
            self.assertIn("abcd...ey", out)
        finally:
            m.FMP_API_KEY = ""

    def test_apikey_query_param_is_scrubbed_even_when_the_key_is_unknown(self):
        out = m.redact("GET /stable/house-latest?page=0&apikey=UNKNOWN_KEY_98765&limit=100")
        self.assertNotIn("UNKNOWN_KEY_98765", out)
        self.assertIn("apikey=<redacted>", out)
        self.assertIn("limit=100", out)   # the rest of the URL survives

    def test_short_values_are_left_alone(self):
        m.FMP_API_KEY = "abc"
        try:
            self.assertEqual(m.redact("harmless abc text"), "harmless abc text")
        finally:
            m.FMP_API_KEY = ""


class TestSubject(unittest.TestCase):
    def test_mentions_count_and_top_tickers(self):
        items = [{"symbol": "NVDA"}, {"symbol": "NVDA"}, {"symbol": "AAPL"}]
        subject = m.build_subject(items)
        self.assertIn("3 new filing(s)", subject)
        self.assertIn("NVDA", subject)

    def test_empty(self):
        self.assertIn("nothing new", m.build_subject([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
