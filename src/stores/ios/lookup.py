"""iTunes Lookup — batch enrichment, 200 ids per request.

The economics here are completely different from Play. Play costs one page fetch per app;
Apple returns 200 apps in a single call, so enriching 400 iOS apps is 2 requests against
Play's 400. That is why the iOS pipeline can afford to snapshot its whole catalogue daily
while Play works through a queue.

`userRatingCount` is the iOS velocity source (spec §7). It is per-storefront — this pulls
`us` only, so it is a consistent series rather than a global total. That is fine for velocity,
which cares about the delta, but it means the absolute number understates the app's real
audience and should never be compared against a Play install count directly.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timezone

from ...lib.http import Fetcher

LOOKUP_URL = "https://itunes.apple.com/lookup"
BATCH_SIZE = 200


def _parse_apple_date(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except ValueError:
        return None


def _has_results(text: str) -> bool:
    return '"trackId"' in text


def normalize(r: dict) -> dict:
    """One lookup result → the fields the schema wants."""
    return {
        "store_app_id": str(r.get("trackId")),
        # app fields
        "name": r.get("trackName"),
        "developer": r.get("sellerName") or r.get("artistName"),
        "developer_id": str(r["artistId"]) if r.get("artistId") else None,
        "category": r.get("primaryGenreName"),
        "released": _parse_apple_date(r.get("releaseDate")),
        "price": r.get("price") if r.get("price") is not None else 0,
        "icon_url": r.get("artworkUrl512") or r.get("artworkUrl100"),
        # snapshot fields
        "install_exact": None,  # Apple publishes no install count, at any granularity
        "install_min": None,
        "rating_count": r.get("userRatingCount"),
        "avg_rating": round(r["averageUserRating"], 4) if r.get("averageUserRating") else None,
        "review_count": None,  # filled by the review backfill, not available here
        "version": r.get("version"),
    }


def lookup_batch(fetcher: Fetcher, track_ids: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Look up to 200 ids. Returns ({track_id: normalized}, [missing ids]).

    Missing ids are returned explicitly rather than silently dropped: Apple omits results for
    apps that are delisted or unavailable in this storefront, and an app that vanishes from
    lookup while its neighbours resolve is a real signal (delist it) — not something to
    confuse with a failed request, which returns None for the whole batch instead.
    """
    if not track_ids:
        return {}, []

    url = f"{LOOKUP_URL}?" + urllib.parse.urlencode(
        {"id": ",".join(track_ids), "country": "us", "entity": "software"}
    )
    text = fetcher.get(url, expect=_has_results)
    if text is None:
        # Could not determine. Not "these apps are all gone".
        raise RuntimeError(f"lookup failed for {len(track_ids)} ids after retries")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"lookup returned unparseable JSON: {e}") from e

    found: dict[str, dict] = {}
    for r in payload.get("results", []):
        tid = str(r.get("trackId") or "")
        if tid:
            found[tid] = normalize(r)

    missing = [t for t in track_ids if t not in found]
    return found, missing


def batches(items: list[str], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


APP_FIELDS = ("name", "developer", "developer_id", "category", "released", "price", "icon_url")
SNAPSHOT_FIELDS = (
    "install_exact",
    "install_min",
    "rating_count",
    "avg_rating",
    "review_count",
    "version",
)


def split_detail(row: dict) -> tuple[dict, dict]:
    return (
        {k: row[k] for k in APP_FIELDS if k in row},
        {k: row[k] for k in SNAPSHOT_FIELDS if k in row and row[k] is not None},
    )
