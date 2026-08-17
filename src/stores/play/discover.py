"""Play new-app discovery.

Phase 0 measured every candidate channel. The honest summary (see FINDINGS.md §3): Play
carries the best signal in the system — exact daily installs — and the worst discovery.
`NEW_FREE`/`NEW_PAID` are dead, sitemaps are dateless and mixed with books, and
cross-store name matching from iOS finds only ~17%. What is left, all verified 2026-08-17:

    D1 search sweep       — works once wrapped; ranks by popularity, so young-app recall is poor
    D2 developer pages    — `developer?id=<NAME>` 200 + parseable (`dev?id=` is 404)
    D4 charts/categories  — `/store/apps/top` ~95 ids, `/store/apps/category/<CAT>` 43–62

D3 (similar apps) is parked: `similar?id=` is 404, so it would need the detail page's
similar cluster parsed out. Phase 5 work.

Consequence to keep in mind while reading dashboard rows: Play discovery *lags*. Apps are
generally found once they have traction, not on release day. iOS is where day-zero
detection lives.
"""

from __future__ import annotations

import re
import urllib.parse

from google_play_scraper import search as _play_search

from ...lib.http import Fetcher, ThinResult

APP_ID_RE = re.compile(r"/store/apps/details\?id=([a-zA-Z0-9._]+)")

DEV_URL = "https://play.google.com/store/apps/developer?id={dev}&hl=en&gl=us"
TOP_URL = "https://play.google.com/store/apps/top?hl=en&gl=us"
CATEGORY_URL = "https://play.google.com/store/apps/category/{cat}?hl=en&gl=us"

# Install-band ceiling below which a search hit is a plausible young candidate. Search
# results carry `installs` for free while release date costs a detail fetch, so this is the
# cheapest available youth proxy — though Phase 0's quick run could not yet prove it beats
# random. Treated as an ordering hint, never as a filter that drops apps.
LOW_BAND = 100_000

CATEGORIES = [
    "GAME_PUZZLE", "GAME_CASUAL", "GAME_ARCADE", "GAME_ACTION", "GAME_SIMULATION",
    "GAME_ROLE_PLAYING", "GAME_STRATEGY", "GAME_WORD", "GAME_BOARD", "GAME_CARD",
    "PRODUCTIVITY", "TOOLS", "HEALTH_AND_FITNESS", "FINANCE", "PHOTOGRAPHY",
    "SOCIAL", "COMMUNICATION", "EDUCATION", "ENTERTAINMENT", "LIFESTYLE",
    "MUSIC_AND_AUDIO", "SHOPPING", "TRAVEL_AND_LOCAL", "PERSONALIZATION", "MEDICAL",
    "ART_AND_DESIGN", "BOOKS_AND_REFERENCE", "BUSINESS", "FOOD_AND_DRINK", "WEATHER",
]

SEARCH_TERMS = [
    "habit tracker", "ai photo editor", "budget planner", "sleep sounds", "workout log",
    "focus timer", "recipe manager", "language flashcards", "mood journal",
    "receipt scanner", "ai assistant", "meditation timer", "expense split", "plant care",
    "pomodoro", "water reminder", "step counter", "voice recorder transcribe",
    "pdf scanner", "calorie counter", "period tracker", "baby log", "dog training",
    "chess puzzles", "sudoku daily", "word game offline", "idle tycoon",
    "merge puzzle", "escape room", "cozy farm game",
]


def install_band(value) -> int | None:
    """Parse Play's display install string ('1,000,000+') into its band floor."""
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def _ids_from_html(html: str | None) -> list[str]:
    return sorted(set(APP_ID_RE.findall(html))) if html else []


def _has_app_links(text: str) -> bool:
    """Expectation predicate for `Fetcher.get`.

    This is the assertion that catches the NEW_FREE failure mode: a 200 response with a
    complete HTML shell and zero app links. Without it, a silently-emptied page reads as
    "this developer has no apps".
    """
    return "/store/apps/details?id=" in text


# ---------------------------------------------------------------------------
# D1 — keyword search sweep
# ---------------------------------------------------------------------------


def search_sweep(fetcher: Fetcher, terms: list[str] | None = None) -> list[tuple[str, str]]:
    """Sweep keywords, returning (app_id, 'search') pairs, low-install candidates first.

    Wrapping is mandatory, not defensive: google-play-scraper 1.2.7 raises TypeError from
    an unguarded index chain at search.py:41 whenever a query has no strong top result. Five
    of twelve probe queries died that way. An unwrapped sweep dies on its first obscure term.
    """
    terms = terms or SEARCH_TERMS
    banded: dict[str, int | None] = {}

    for term in terms:
        try:
            hits = fetcher.guarded(_play_search, term, n_hits=100, lang="en", country="us")
        except ThinResult:
            # A genuinely thin result set. Not a failure, and not evidence of anything.
            print(f"  search {term!r}: thin result set")
            continue
        if hits is None:
            print(f"  search {term!r}: failed after retries")
            continue
        for h in hits:
            aid = h.get("appId")
            if aid and aid not in banded:
                banded[aid] = install_band(h.get("installs"))
        print(f"  search {term!r}: {len(hits)} hits (pool {len(banded)})")

    def sort_key(item: tuple[str, int | None]) -> tuple[int, int]:
        aid, band = item
        if band is None:
            return (1, 0)
        return (0, band) if band < LOW_BAND else (2, band)

    ordered = sorted(banded.items(), key=sort_key)
    return [(aid, "search") for aid, _ in ordered]


# ---------------------------------------------------------------------------
# D2 — developer pages
# ---------------------------------------------------------------------------


def developer_crawl(fetcher: Fetcher, developers: list[str]) -> list[tuple[str, str]]:
    """Crawl developer pages for sibling apps.

    Highest-precision Play channel: a developer already in the database shipping something
    new is exactly what we want to catch. Limited by definition to developers we know, so
    it compounds rather than bootstraps.
    """
    found: set[str] = set()
    for dev in developers:
        url = DEV_URL.format(dev=urllib.parse.quote_plus(dev))
        ids = _ids_from_html(fetcher.get(url, expect=_has_app_links))
        found.update(ids)
        print(f"  developer {dev!r}: {len(ids)} apps")
    return [(aid, "developer") for aid in sorted(found)]


# ---------------------------------------------------------------------------
# D4 — charts and category pages
# ---------------------------------------------------------------------------


def chart_scrape(fetcher: Fetcher, categories: list[str] | None = None) -> list[tuple[str, str]]:
    """Scrape the global top page plus category pages.

    A late signal by construction — an app that charts has usually already broken out — but
    cheap, reliable, and the only channel that needs no seeds. Together with D1 it carries
    the cold start.
    """
    categories = categories if categories is not None else CATEGORIES
    found: set[str] = set()

    top_ids = _ids_from_html(fetcher.get(TOP_URL, expect=_has_app_links))
    found.update(top_ids)
    print(f"  top charts: {len(top_ids)} apps")

    for cat in categories:
        ids = _ids_from_html(fetcher.get(CATEGORY_URL.format(cat=cat), expect=_has_app_links))
        found.update(ids)
        print(f"  category {cat}: {len(ids)} apps")

    return [(aid, "chart") for aid in sorted(found)]
