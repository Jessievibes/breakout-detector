"""Unit tests for the guards and parsers — no network, no database.

These cover the logic that decides whether a number reaches the database, which is the
logic most likely to fail silently. Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import date

from src.lib.guards import (
    NullRateTracker,
    ParserFailure,
    SuspectData,
    apple_feed_looks_throttled,
    check_install_delta,
    check_play_detail,
    classify_rating_drop,
)
from src.lib.http import FetchStats
from src.stores.play.detail import parse_released
from src.stores.play.discover import install_band


class TestPlayDetailGuard(unittest.TestCase):
    def test_accepts_exact_installs(self):
        check_play_detail({"realInstalls": 470_723, "minInstalls": 100_000, "ratings": 8_000}, "x")

    def test_rejects_null_installs_with_ratings(self):
        """The core parser-drift case: ratings present, installs missing."""
        with self.assertRaises(ParserFailure):
            check_play_detail({"realInstalls": None, "ratings": 36_000_000}, "com.spotify.music")

    def test_rejects_banded_only(self):
        """Falling back to the band floor is not an exact count and must not pass as one."""
        with self.assertRaises(ParserFailure):
            check_play_detail({"realInstalls": None, "minInstalls": 1_000_000}, "x")

    def test_allows_genuinely_empty_app(self):
        """A brand-new app with no ratings and no installs is plausible, not drift."""
        check_play_detail({"realInstalls": None, "minInstalls": None, "ratings": None}, "x")


class TestDeltaGuards(unittest.TestCase):
    def test_install_increase_ok(self):
        check_install_delta(1_000, 1_200, "x")

    def test_install_decrease_rejected(self):
        """Play never decrements lifetime installs, so a fall is our bug."""
        with self.assertRaises(SuspectData):
            check_install_delta(1_200, 1_000, "x")

    def test_missing_readings_are_not_errors(self):
        check_install_delta(None, 1_000, "x")
        check_install_delta(1_000, None, "x")

    def test_flat_is_ok(self):
        check_install_delta(1_000, 1_000, "x")

    def test_rating_drop_is_relaunch_not_failure(self):
        """iOS rating counts legitimately reset on release — a signal, not a parse error."""
        self.assertEqual(classify_rating_drop(5_000, 12), "relaunch")
        self.assertIsNone(classify_rating_drop(12, 5_000))
        self.assertIsNone(classify_rating_drop(None, 5_000))


class TestNullRateTracker(unittest.TestCase):
    def test_silent_below_threshold(self):
        t = NullRateTracker("t")
        for _ in range(99):
            t.record(True)
        t.record(False, "one miss")
        t.check()  # 1% — normal attrition

    def test_fails_above_threshold(self):
        t = NullRateTracker("t")
        for _ in range(80):
            t.record(True)
        for i in range(20):
            t.record(False, f"miss {i}")
        with self.assertRaises(ParserFailure):
            t.check()  # 20% — Google changed the page

    def test_small_samples_do_not_trip(self):
        """Two failures out of three is not evidence; it is a tiny batch."""
        t = NullRateTracker("t")
        t.record(True)
        t.record(False, "a")
        t.record(False, "b")
        t.check()

    def test_examples_are_capped(self):
        t = NullRateTracker("t")
        for i in range(50):
            t.record(False, f"miss {i}")
        self.assertEqual(len(t.examples), 5)


class TestAppleThrottleDetection(unittest.TestCase):
    def test_empty_and_linkless_is_throttled(self):
        self.assertTrue(apple_feed_looks_throttled("<feed></feed>", 0))

    def test_empty_with_links_is_a_real_last_page(self):
        self.assertFalse(
            apple_feed_looks_throttled('<feed><link rel="next" href="..."/></feed>', 0)
        )

    def test_populated_feed_is_never_throttled(self):
        self.assertFalse(apple_feed_looks_throttled("<feed></feed>", 50))


class TestParsers(unittest.TestCase):
    def test_release_date_formats(self):
        self.assertEqual(parse_released("May 27, 2014"), date(2014, 5, 27))
        self.assertEqual(parse_released("27 May 2014"), date(2014, 5, 27))
        self.assertEqual(parse_released("September 6, 2013"), date(2013, 9, 6))

    def test_release_date_junk(self):
        for junk in (None, "", "  ", "coming soon", 12345):
            self.assertIsNone(parse_released(junk))

    def test_install_band(self):
        self.assertEqual(install_band("1,000,000+"), 1_000_000)
        self.assertEqual(install_band("500+"), 500)
        self.assertIsNone(install_band(None))
        self.assertIsNone(install_band("Varies"))


class TestDeveloperUrls(unittest.TestCase):
    """Play serves numeric-id developers from dev?id= and name-id developers from
    developer?id=. The wrong form returns a clean 404, so getting this wrong loses half of
    channel D2 without raising anything."""

    def test_numeric_id_prefers_dev_form(self):
        from src.stores.play.discover import developer_urls

        urls = developer_urls("Todoist Inc.", "4949773854634494965")
        self.assertIn("/dev?id=4949773854634494965", urls[0])

    def test_name_id_prefers_developer_form(self):
        from src.stores.play.discover import developer_urls

        urls = developer_urls("Spotify AB", "Spotify+AB")
        self.assertIn("/developer?id=Spotify+AB", urls[0])

    def test_name_case_does_not_double_encode_plus(self):
        """developerId comes back pre-encoded ('Spotify+AB'); re-encoding makes '%2B' and 404s.
        The raw `developer` name must be what gets quoted."""
        from src.stores.play.discover import developer_urls

        for url in developer_urls("Spotify AB", "Spotify+AB"):
            self.assertNotIn("%2B", url)

    def test_comma_in_name_is_encoded_not_dropped(self):
        from src.stores.play.discover import developer_urls

        urls = developer_urls("Notion Labs, Inc.", "Notion+Labs,+Inc.")
        self.assertIn("Notion+Labs%2C+Inc.", urls[0])

    def test_fallback_form_is_offered(self):
        """A 404 is indistinguishable from 'no apps', so the other form must be tried."""
        from src.stores.play.discover import developer_urls

        self.assertEqual(len(developer_urls("Todoist Inc.", "4949773854634494965")), 2)

    def test_no_identifiers_yields_nothing(self):
        from src.stores.play.discover import developer_urls

        self.assertEqual(developer_urls(None, None), [])


class TestFetchStats(unittest.TestCase):
    def test_anomaly_rate_is_per_call_not_per_attempt(self):
        """Retries must not inflate the health signal. One URL failing three times is one
        failed call — the first live run aborted at a bogus 10.7% because a single empty
        category page counted as three anomalies."""
        s = FetchStats()
        s.requests = 30  # attempts, retries included
        s.calls = 10
        s.failed_calls = 1
        s.empty_200 = 3
        self.assertAlmostEqual(s.anomaly_rate, 0.10)

    def test_no_requests_no_division_error(self):
        self.assertEqual(FetchStats().anomaly_rate, 0.0)

    def test_serializes_codes_as_strings(self):
        s = FetchStats()
        s.requests = 1
        s.codes[200] += 1
        self.assertEqual(s.as_dict()["codes"], {"200": 1})


class TestDegradationGuard(unittest.TestCase):
    """The guard must fire on a burning IP and stay quiet on one awkward URL."""

    def _fetcher(self):
        from src.lib.http import Fetcher

        return Fetcher(delay=(0, 0))

    def test_one_bad_url_does_not_trip_it(self):
        f = self._fetcher()
        for _ in range(40):
            f._record_call(True)
        f._record_call(False)  # a single empty page, e.g. Play's MEDICAL category
        self.assertEqual(f.stats.failed_calls, 1)

    def test_sustained_failure_rate_trips_it(self):
        from src.lib.http import Blocked

        f = self._fetcher()
        with self.assertRaises(Blocked):
            for i in range(60):
                f._record_call(i % 4 != 0)  # 25% failing

    def test_consecutive_failures_trip_it_early(self):
        """A burning IP shows as many different URLs failing in a row, before the rate
        over the whole run has had time to climb."""
        from src.lib.http import Blocked

        f = self._fetcher()
        for _ in range(200):
            f._record_call(True)
        with self.assertRaises(Blocked):
            for _ in range(8):
                f._record_call(False)

    def test_success_resets_the_consecutive_counter(self):
        f = self._fetcher()
        for _ in range(7):
            f._record_call(False)
        f._record_call(True)
        self.assertEqual(f._consecutive_failures, 0)

    def test_small_samples_do_not_trip_the_rate_check(self):
        f = self._fetcher()
        for _ in range(5):
            f._record_call(False)  # 100% failing, but only 5 calls
        self.assertEqual(f.stats.calls, 5)


class TestDbColumnAllowlist(unittest.TestCase):
    def test_refuses_unknown_column(self):
        """last_snapshot_metric interpolates a column name, so it must be allowlisted."""
        from src.lib import db

        with self.assertRaises(ValueError):
            db.last_snapshot_metric(None, 1, "install_exact; drop table app")


if __name__ == "__main__":
    unittest.main(verbosity=2)
