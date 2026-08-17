"""Play review history via batchexecute continuation tokens.

The Play mirror of `stores/ios/reviews.py`, and for the same reason: review timestamps are
the only *retrospective* signal either store exposes. Everything else — installs, ratings,
chart rank — is a snapshot of now, so history has to be accumulated day by day. Reviews reach
backwards, which is what lets an app discovered today be scored today.

Verified 2026-08-17 that this reaches launch for young apps: `com.sych.plantdoctor`, found
via search, returned reviews back to 2026-05-10.

Two differences from Apple worth knowing:

  * **No page cap.** Apple stops at 500 reviews; Play will keep serving continuation tokens
    until the app runs out. For a large app that is effectively unbounded — 300 Duolingo
    reviews spanned a single day — so this caps pages itself. The cap is a cost control, not
    a store limit, and `truncated` records when it bites.
  * **Reviews are the expensive call here.** Play has no batch endpoint, so this is the one
    place where iOS is cheaper per app. Backfill is time-boxed for exactly this reason.
"""

from __future__ import annotations

from datetime import datetime, timezone

from google_play_scraper import Sort
from google_play_scraper import reviews as _play_reviews

from ...lib.http import Fetcher

PAGE_SIZE = 200
# ~800 reviews. A cost ceiling, not a store limit — and deliberately lower than it was:
# one run spent its whole budget reaching this cap on a single established app. Inside the
# 120-day scoring window an app with 800+ written reviews has already broken out loudly
# enough that exhaustive history adds nothing.
MAX_PAGES = 4


class Throttled(RuntimeError):
    """Play stopped returning reviews mid-walk. Unknown, not zero."""


def _normalize(raw: dict) -> dict | None:
    review_id = raw.get("reviewId")
    at = raw.get("at")
    if not review_id or not isinstance(at, datetime):
        return None
    # The library returns naive datetimes in UTC. Stamp the timezone rather than assuming
    # it downstream, so day-bucketing in review_daily matches the iOS path exactly.
    posted_at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    score = raw.get("score")
    return {
        "review_id": str(review_id),
        "posted_at": posted_at.astimezone(timezone.utc),
        "rating": score if isinstance(score, int) and 1 <= score <= 5 else None,
        "version": raw.get("reviewCreatedVersion"),
    }


def fetch_all_reviews(
    fetcher: Fetcher, store_app_id: str, max_pages: int = MAX_PAGES
) -> tuple[list[dict], bool]:
    """Page an app's reviews newest-first. Returns (reviews, truncated).

    Raises Throttled rather than returning a partial set: `reviews_backfilled` is a one-way
    flag, so a partial history marked complete would leave a permanent gap (spec §6.3).
    """
    collected: list[dict] = []
    token = None
    pages = 0

    for page in range(max_pages):
        try:
            batch, token = fetcher.guarded(
                _play_reviews,
                store_app_id,
                lang="en",
                country="us",
                count=PAGE_SIZE,
                sort=Sort.NEWEST,
                continuation_token=token,
            ) or (None, None)
        except Exception as e:  # noqa: BLE001 - re-raised with context below
            if page == 0:
                raise Throttled(f"{store_app_id}: first page failed ({type(e).__name__})") from e
            # Partial walk. Refuse it rather than persist an incomplete history as complete.
            raise Throttled(
                f"{store_app_id}: page {page + 1} failed after {len(collected)} reviews"
            ) from e

        if batch is None:
            raise Throttled(f"{store_app_id}: page {page + 1} returned nothing")

        pages = page + 1
        for raw in batch:
            row = _normalize(raw)
            if row:
                collected.append(row)

        # An empty first page is a real answer (no reviews yet); later, it is the end.
        if not batch or token is None:
            break

    truncated = pages >= max_pages and token is not None
    return collected, truncated
