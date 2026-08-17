"""Apple customer reviews RSS — the day-one history source.

This is what makes an app scoreable before we have snapshots of our own. Every review carries
a timestamp, so backfilling them reconstructs an arrival curve that predates our first sight
of the app. Without it, a newly discovered app has no history and no momentum until we have
watched it for a week.

Hard limits, all verified 2026-08-17:
  * 10 pages x 50 entries = 500 reviews maximum per storefront
  * Text reviews only — roughly 5% of star ratings (Spotify: 36.1M ratings, 1.85M reviews)
  * Timestamps carry local offsets and MUST be normalized to UTC at ingest

The 500 cap truncates about 10% of apps aged 30-90 days (stratified sample, n=10). For those,
`review_daily` understates early history — which is one reason spec v2 moved iOS velocity to
`Delta userRatingCount` and left reviews as the cold-start signal rather than the main one.

The failure mode that matters (spec §6.1): a throttled request returns HTTP 200 with an empty
feed and blank pagination links. Writing that as "this app has no reviews" would be a lie
that never corrects itself, because backfill only runs once per app.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from xml.etree import ElementTree

from ...lib.guards import apple_feed_looks_throttled
from ...lib.http import Fetcher

REVIEWS_URL = (
    "https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={track_id}"
    "/sortby=mostrecent/xml"
)

MAX_PAGES = 10
PAGE_SIZE = 50

ATOM = "{http://www.w3.org/2005/Atom}"
IM = "{http://itunes.apple.com/rss}"


class Throttled(RuntimeError):
    """Apple returned an empty, linkless feed. Unknown, not zero."""


def _text(node, tag: str) -> str | None:
    found = node.find(tag)
    return found.text if found is not None and found.text else None


def parse_page(xml_text: str) -> list[dict]:
    """Parse one reviews page into review dicts.

    The feed's first `entry` is sometimes the app itself rather than a review. Entries
    without an `im:rating` are skipped on that basis rather than by position, since position
    is not guaranteed.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    out: list[dict] = []
    for entry in root.findall(f"{ATOM}entry"):
        rating_raw = _text(entry, f"{IM}rating")
        if rating_raw is None:
            continue  # the app-info entry, not a review
        try:
            rating = int(rating_raw)
        except ValueError:
            continue

        review_id = _text(entry, f"{ATOM}id")
        updated = _text(entry, f"{ATOM}updated")
        if not review_id or not updated:
            continue

        try:
            # Offsets like -07:00 are normal here; store UTC so day-bucketing is consistent.
            posted_at = datetime.fromisoformat(updated).astimezone(timezone.utc)
        except ValueError:
            continue

        out.append(
            {
                "review_id": review_id.strip(),
                "posted_at": posted_at,
                "rating": rating if 1 <= rating <= 5 else None,
                "version": _text(entry, f"{IM}version"),
            }
        )
    return out


def _has_link(xml_text: str) -> bool:
    return bool(re.search(r"<link[^>]", xml_text))


def fetch_all_reviews(
    fetcher: Fetcher, track_id: str, country: str = "us", max_pages: int = MAX_PAGES
) -> tuple[list[dict], bool]:
    """Page through an app's reviews. Returns (reviews, truncated).

    `truncated` means we hit the 10-page ceiling with a full last page, so older history
    exists that Apple will not serve.

    Raises Throttled rather than returning a partial set. Spec §6.3: a partial backfill must
    not be persisted as complete, because `reviews_backfilled` is a one-way flag — the app
    would never be revisited and the gap would be permanent.
    """
    collected: list[dict] = []
    pages_read = 0

    for page in range(1, max_pages + 1):
        url = REVIEWS_URL.format(country=country, page=page, track_id=track_id)
        text = fetcher.get(url, retries=2)
        if text is None:
            raise Throttled(f"{track_id}: page {page} failed after retries")

        batch = parse_page(text)
        if not batch and apple_feed_looks_throttled(text, 0) and not _has_link(text):
            raise Throttled(f"{track_id}: page {page} empty with no pagination links")

        pages_read = page
        collected.extend(batch)

        if len(batch) < PAGE_SIZE:
            break  # short page == last page

    truncated = pages_read >= max_pages and len(collected) >= max_pages * PAGE_SIZE
    return collected, truncated
