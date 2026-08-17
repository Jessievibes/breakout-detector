"""The single network chokepoint (spec §3).

Every outbound request in this system goes through `Fetcher` — including calls made by
google-play-scraper, via `Fetcher.guarded()`. Scattering fetches defeats throttling,
backoff, and IP-failure detection all at once, and IP failure is the documented ceiling on
this whole project.

Two invariants worth stating plainly:

1. **A failed fetch returns None, never a falsy datum.** `None` means "could not
   determine". Callers must never coerce it to 0, [], or "". Writing a zero you cannot
   distinguish from a block is how a scraper silently poisons its own history.

2. **Anomalies are tracked, not just retried.** Store IPs degrade gradually rather than
   failing cleanly, so the run itself must notice the trend and abort. See `Blocked`.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Apple documents ~20 req/min. Play has no published limit; 2–5 s jitter survived a
# 100-fetch residential run with zero anomalies (Phase 0 T2).
APPLE_DELAY = (3.0, 4.5)
PLAY_DELAY = (2.0, 5.0)

# Abort a run when this share of requests is anomalous (spec §6.4). The minimum sample
# stops a two-request job from tripping on a single blip.
ANOMALY_ABORT_RATE = 0.10
ANOMALY_MIN_SAMPLE = 20


class Blocked(RuntimeError):
    """The runner IP appears to be burning. Raised mid-run so the job dies loudly.

    This is the failure the spec cares about most: not a crash, but a slow slide into
    empty responses that would otherwise be written to the database as facts.
    """


@dataclass
class FetchStats:
    """Per-run counters. Serialized verbatim into run_log.stats."""

    requests: int = 0
    http_403: int = 0
    http_429: int = 0
    http_5xx: int = 0
    empty_200: int = 0
    errors: int = 0
    retries: int = 0
    codes: Counter = field(default_factory=Counter)

    @property
    def anomalies(self) -> int:
        return self.http_403 + self.http_429 + self.http_5xx + self.empty_200 + self.errors

    @property
    def anomaly_rate(self) -> float:
        return self.anomalies / self.requests if self.requests else 0.0

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "http_403": self.http_403,
            "http_429": self.http_429,
            "http_5xx": self.http_5xx,
            "empty_200": self.empty_200,
            "errors": self.errors,
            "retries": self.retries,
            "anomaly_rate": round(self.anomaly_rate, 4),
            "codes": {str(k): v for k, v in self.codes.items()},
        }

    def __str__(self) -> str:
        return (
            f"{self.requests} req, 403={self.http_403} 429={self.http_429} "
            f"5xx={self.http_5xx} empty200={self.empty_200} err={self.errors} "
            f"({self.anomaly_rate:.1%} anomalous)"
        )


class Fetcher:
    def __init__(
        self,
        delay: tuple[float, float] = PLAY_DELAY,
        max_retries: int = 2,
        timeout: int = 30,
        abort_on_degradation: bool = True,
    ):
        self.delay = delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.abort_on_degradation = abort_on_degradation
        self.stats = FetchStats()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = UA
        self._last_request_at: float | None = None

    # -- pacing ------------------------------------------------------------

    def _throttle(self) -> None:
        """Sleep so consecutive requests are at least `delay` apart.

        Measured from the *end* of the previous request, and skipped entirely on the first
        call, so a job doesn't pay the delay before doing any work.
        """
        if self._last_request_at is None:
            return
        target = random.uniform(*self.delay)
        waited = time.monotonic() - self._last_request_at
        if waited < target:
            time.sleep(target - waited)

    def _check_degradation(self) -> None:
        if not self.abort_on_degradation:
            return
        if self.stats.requests >= ANOMALY_MIN_SAMPLE and self.stats.anomaly_rate > ANOMALY_ABORT_RATE:
            raise Blocked(
                f"anomaly rate {self.stats.anomaly_rate:.1%} over "
                f"{self.stats.requests} requests exceeds {ANOMALY_ABORT_RATE:.0%} — "
                f"treating the runner IP as burning. {self.stats}"
            )

    # -- the one public fetch ---------------------------------------------

    def get(self, url: str, expect: Callable[[str], bool] | None = None) -> str | None:
        """GET with jitter, retry, and empty-response detection.

        `expect` asserts the body actually carries the entity we asked for. A 200 that
        fails it counts as `empty_200` — the failure mode proven live by Play's dead
        NEW_FREE cluster URL, which answers 200 with a complete HTML shell and zero app
        links. Status codes lie; assert on content.

        Returns the body, or None meaning "unknown". Never returns a placeholder.
        """
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.stats.requests += 1
            if attempt:
                self.stats.retries += 1
            try:
                r = self._session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                self.stats.errors += 1
                self._last_request_at = time.monotonic()
                self._backoff(attempt, f"{type(e).__name__}")
                continue
            finally:
                self._last_request_at = time.monotonic()

            self.stats.codes[r.status_code] += 1

            if r.status_code == 200:
                if expect is None or expect(r.text):
                    self._check_degradation()
                    return r.text
                self.stats.empty_200 += 1
                self._check_degradation()
                self._backoff(attempt, "200-but-empty")
                continue

            if r.status_code == 403:
                self.stats.http_403 += 1
            elif r.status_code == 429:
                self.stats.http_429 += 1
            elif r.status_code >= 500:
                self.stats.http_5xx += 1
            elif r.status_code == 404:
                # Genuinely absent, not throttled. Don't retry, don't count as anomalous.
                self._check_degradation()
                return None

            self._check_degradation()
            self._backoff(attempt, f"HTTP {r.status_code}")
        return None

    def guarded(self, fn: Callable, *args, **kwargs):
        """Run a third-party call (google-play-scraper) under our throttle and counters.

        The library owns its own HTTP, so this cannot inspect status codes — but it can
        still pace the call, count it, and classify the exception. Without this, every
        Play detail fetch would bypass the chokepoint entirely.

        Returns None on failure. `NotFoundError` is re-raised as `AppNotFound` so callers
        can distinguish "delisted" from "we got blocked".
        """
        from google_play_scraper.exceptions import NotFoundError

        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.stats.requests += 1
            if attempt:
                self.stats.retries += 1
            try:
                return fn(*args, **kwargs)
            except NotFoundError:
                self._last_request_at = time.monotonic()
                raise AppNotFound(str(args[:1]))
            except TypeError as e:
                # google-play-scraper 1.2.7 search.py:41 indexes an unguarded chain and
                # raises this on thin result sets. A parse shape we don't handle, not a
                # network fault — retrying cannot help.
                self._last_request_at = time.monotonic()
                raise ThinResult(str(e))
            except Exception as e:
                self.stats.errors += 1
                self._last_request_at = time.monotonic()
                self._check_degradation()
                self._backoff(attempt, f"{type(e).__name__}")
                continue
            finally:
                self._last_request_at = time.monotonic()
        return None

    def _backoff(self, attempt: int, why: str) -> None:
        if attempt >= self.max_retries:
            return
        wait = (2**attempt) * 3 + random.uniform(0, 2)
        print(f"    retry {attempt + 1}/{self.max_retries} after {why} — {wait:.1f}s")
        time.sleep(wait)


class AppNotFound(LookupError):
    """The store says this app does not exist. Mark delisted; stop refetching."""


class ThinResult(ValueError):
    """The library could not parse a (probably empty) result set. Not a network failure."""


def apple_fetcher() -> Fetcher:
    return Fetcher(delay=APPLE_DELAY)


def play_fetcher() -> Fetcher:
    return Fetcher(delay=PLAY_DELAY)
