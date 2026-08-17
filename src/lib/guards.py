"""Silent-failure guards (spec §6).

The governing rule: **a job that cannot distinguish "no data" from "blocked" must fail
loudly, not write zeros.** Every function here exists to keep a plausible-looking zero out
of the database, because a zero in `snapshot` is indistinguishable from a real measurement
one week later — and it corrupts every delta computed across it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# Spec §6.2: a batch whose parser-null rate exceeds this is a parser failure, not a run of
# unusual apps.
NULL_RATE_LIMIT = 0.05


class ParserFailure(RuntimeError):
    """A field we depend on came back null while its neighbours were populated."""


class SuspectData(RuntimeError):
    """A value that cannot be true (a negative install delta, say)."""


# ---------------------------------------------------------------------------
# Play detail
# ---------------------------------------------------------------------------


def check_play_detail(d: dict, store_app_id: str) -> None:
    """Spec §6.2. `realInstalls` null while `ratings` is populated is a parser failure —
    NOT an app with no installs. Google does not ship apps with ratings and no installs.

    Raises ParserFailure so the caller fails the row and logs it, rather than writing a
    null install count that later reads as "flat growth".
    """
    if d.get("realInstalls") is None and d.get("ratings"):
        raise ParserFailure(
            f"{store_app_id}: realInstalls is null but ratings={d['ratings']} — "
            f"parser drift, not a zero-install app"
        )
    # minInstalls without realInstalls means we fell back to the banded floor, which is too
    # coarse for velocity. Treat as failure so it doesn't masquerade as an exact count.
    if d.get("realInstalls") is None and d.get("minInstalls"):
        raise ParserFailure(
            f"{store_app_id}: only banded minInstalls={d['minInstalls']} available, "
            f"no exact count"
        )


@dataclass
class NullRateTracker:
    """Fails a batch, not just a row, when parser nulls stop looking like bad luck."""

    label: str
    total: int = 0
    nulls: int = 0
    examples: list[str] = field(default_factory=list)

    def record(self, ok: bool, detail: str = "") -> None:
        self.total += 1
        if not ok:
            self.nulls += 1
            if len(self.examples) < 5:
                self.examples.append(detail)

    @property
    def rate(self) -> float:
        return self.nulls / self.total if self.total else 0.0

    def check(self, minimum_sample: int = 20) -> None:
        if self.total >= minimum_sample and self.rate > NULL_RATE_LIMIT:
            raise ParserFailure(
                f"{self.label}: {self.nulls}/{self.total} rows null ({self.rate:.1%}) "
                f"exceeds {NULL_RATE_LIMIT:.0%}. Examples: {json.dumps(self.examples)}"
            )


# ---------------------------------------------------------------------------
# Apple feeds and reviews
# ---------------------------------------------------------------------------


def apple_feed_looks_throttled(text: str, entry_count: int) -> bool:
    """Spec §6.1. Apple's reviews throttle returns HTTP 200 with an empty feed and blank
    pagination links. Empty *and* linkless means throttled; empty *with* links can be a
    genuine last page.
    """
    if entry_count > 0:
        return False
    has_links = 'rel="next"' in text or "rel='next'" in text or "<link" in text
    return not has_links


# ---------------------------------------------------------------------------
# Deltas
# ---------------------------------------------------------------------------


def check_install_delta(prev: int | None, cur: int | None, store_app_id: str) -> None:
    """Cumulative install counts cannot fall. If they do, we mis-parsed one of them.

    Google does not decrement realInstalls; uninstalls are not subtracted from the
    lifetime figure. So a negative delta is our bug, and scoring it would invent a crash.
    """
    if prev is None or cur is None:
        return
    if cur < prev:
        raise SuspectData(
            f"{store_app_id}: install_exact fell {prev:,} → {cur:,}. "
            f"Impossible; treat as parser failure."
        )


def classify_rating_drop(prev: int | None, cur: int | None) -> str | None:
    """Spec §6.6. iOS rating counts *can* fall: developers reset ratings on a version
    release. That is not a parser bug — it is a relaunch signal.

    Returns 'relaunch' when a drop is detected, else None. The caller nulls that window's
    velocity (the delta is meaningless) and sets relaunch_suspect, because a developer
    wiping their rating history is exactly the behaviour `trust` should penalise.
    """
    if prev is None or cur is None:
        return None
    return "relaunch" if cur < prev else None
