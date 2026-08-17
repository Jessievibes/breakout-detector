"""Play app detail → normalized row.

One request returns metadata, install counts, and the ratings histogram (verified
2026-08-12). This is the cheapest high-value fetch in the system and the only source of
exact install counts anywhere in either store.

Tooling note (spec §5): google-play-scraper is pinned, because the parser breaks when
Google changes the page layout and an unpinned bump is an unannounced production change.
The library is also the *only* thing standing between us and hand-parsing a server-rendered
page, so `check_play_detail` treats its silence as failure rather than as data.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from google_play_scraper import app as _play_app

from ...lib.guards import check_play_detail
from ...lib.http import Fetcher

# Play renders release dates in a few shapes depending on locale negotiation.
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")


def parse_released(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc).date()
        except ValueError:
            continue
    return None


def fetch_detail(fetcher: Fetcher, store_app_id: str) -> dict:
    """Fetch and normalize one Play app.

    Raises AppNotFound (delisted), ParserFailure (drift — see guards), or Blocked (IP).
    Never returns a partially-populated row with nulls standing in for real values.
    """
    raw = fetcher.guarded(_play_app, store_app_id, lang="en", country="us")
    if raw is None:
        raise RuntimeError(f"{store_app_id}: detail fetch exhausted retries")

    check_play_detail(raw, store_app_id)

    return {
        "store_app_id": store_app_id,
        # app fields
        "name": raw.get("title"),
        "developer": raw.get("developer"),
        "developer_id": str(raw["developerId"]) if raw.get("developerId") else None,
        "category": raw.get("genreId"),
        "released": parse_released(raw.get("released")),
        "price": raw.get("price") or 0,
        "icon_url": raw.get("icon"),
        # snapshot fields
        "install_exact": raw.get("realInstalls"),
        "install_min": raw.get("minInstalls"),
        "rating_count": raw.get("ratings"),
        "avg_rating": round(raw["score"], 4) if raw.get("score") else None,
        "review_count": raw.get("reviews"),
        "version": raw.get("version"),
    }


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
    """Split a normalized detail row into its `app` and `snapshot` halves."""
    return (
        {k: row[k] for k in APP_FIELDS if k in row},
        {k: row[k] for k in SNAPSHOT_FIELDS if k in row},
    )
