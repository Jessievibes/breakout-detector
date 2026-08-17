"""Play new-app discovery.

`NEW_FREE`/`NEW_PAID` are dead, sitemaps are dateless and mixed with books, and cross-store
name matching from iOS finds only ~17%. Three channels survive, all verified 2026-08-17:

    D1 search sweep       — works once wrapped; THE young-app channel (see below)
    D2 developer pages    — two URL forms, numeric vs name; high precision, needs seeds
    D4 charts/categories  — `/store/apps/top` ~95 ids, category pages 43–62

**Search is where new Play apps come from.** Measured on the first live batch, not inferred:

    channel   enriched   under 120d   median age
    search        80        42 (53%)      115d      youngest: 1 day
    chart        147         1 ( 1%)     2079d      youngest: 104 days

Charts are a *terrible* youth channel — their median find is five and a half years old, which
makes sense: charting is what breaking out looks like, so by then it has happened. Their real
value is supplying developer names to seed D2.

This corrects the Phase 0 conclusion recorded here earlier, which held that search found no
young apps. That test stratified by a 100,000-install threshold — coarse enough to put nearly
the entire store in one bucket — and so measured nothing. The signal lives below 1,000
installs and is strongest below 100.

D3 (similar apps) is parked: `similar?id=` is 404, so it would need the detail page's similar
cluster parsed out. Phase 5 work.
"""

from __future__ import annotations

import re
import urllib.parse

from google_play_scraper import search as _play_search

from ...lib.http import Fetcher, ThinResult

APP_ID_RE = re.compile(r"/store/apps/details\?id=([a-zA-Z0-9._]+)")

# Play uses TWO developer URL forms and they are NOT interchangeable. Verified 2026-08-17
# by reading the links Play's own detail pages emit:
#
#     developerId numeric ('4949773854634494965')  ->  dev?id=<numeric>        200
#                                                      developer?id=<numeric>  404
#     developerId is a name ('Spotify AB')         ->  developer?id=<name>     200
#                                                      dev?id=<name>           404
#
# Roughly half of sampled apps use each form, so picking one and hoping loses half of D2 —
# silently, because the wrong form returns a clean 404 rather than an error.
#
# Note: use the raw `developer` NAME with quote_plus, never the `developerId` string for the
# name case — the library returns that pre-encoded ('Spotify+AB'), and re-encoding turns the
# '+' into '%2B' and 404s.
DEV_NAME_URL = "https://play.google.com/store/apps/developer?id={dev}&hl=en&gl=us"
DEV_NUMERIC_URL = "https://play.google.com/store/apps/dev?id={dev}&hl=en&gl=us"
TOP_URL = "https://play.google.com/store/apps/top?hl=en&gl=us"
CATEGORY_URL = "https://play.google.com/store/apps/category/{cat}?hl=en&gl=us"

# Install-band thresholds for youth. Search results carry `installs` for free while release
# date costs a detail fetch, so this is the cheapest youth signal available before enrichment.
#
# Calibrated on the first live batch (search-discovered, n=80), NOT guessed:
#     <100 installs   -> 93% under 120 days old, median age 19d
#     100-1k          -> 43%, median 152d
#     1k-10k          -> 20%, median 284d
#
# Phase 0 tested this with a 100,000 threshold and called it "no better than random". That
# threshold was three orders of magnitude too coarse — nearly every app on the store falls
# under it, so the strata were indistinguishable. The signal is real and lives below 1,000.
VERY_LOW_BAND = 100
LOW_BAND = 1_000

CATEGORIES = [
    "GAME_PUZZLE", "GAME_CASUAL", "GAME_ARCADE", "GAME_ACTION", "GAME_SIMULATION",
    "GAME_ROLE_PLAYING", "GAME_STRATEGY", "GAME_WORD", "GAME_BOARD", "GAME_CARD",
    "PRODUCTIVITY", "TOOLS", "HEALTH_AND_FITNESS", "FINANCE", "PHOTOGRAPHY",
    "SOCIAL", "COMMUNICATION", "EDUCATION", "ENTERTAINMENT", "LIFESTYLE",
    "MUSIC_AND_AUDIO", "SHOPPING", "TRAVEL_AND_LOCAL", "PERSONALIZATION", "MEDICAL",
    "ART_AND_DESIGN", "BOOKS_AND_REFERENCE", "BUSINESS", "FOOD_AND_DRINK", "WEATHER",
]

SEARCH_TERMS = [
    # Search is Play's young-app channel (53% of finds under 120 days), and its recall is
    # bounded by ~30 results per term. So coverage scales with the number of terms, not with
    # effort per term — going from 30 to ~180 is the cheapest available improvement to the
    # funnel, and the funnel is what limits everything downstream.
    #
    # Terms are chosen to sit where indie developers actually ship: narrow utilities and
    # trackers, not "social network". Broad terms return entrenched incumbents; specific ones
    # return the long tail, which is where anything new lives.
    # trackers and loggers
    "habit tracker", "mood journal", "sleep tracker", "workout log", "period tracker",
    "water reminder", "step counter", "calorie counter", "weight tracker", "baby log",
    "expense tracker", "budget planner", "receipt scanner", "expense split", "debt payoff",
    "subscription tracker", "time tracker", "mileage log", "fuel log", "medication reminder",
    "blood pressure log", "glucose tracker", "symptom tracker", "cycle tracker",
    "plant care", "pet care log", "reading log", "movie tracker", "gratitude journal",
    "dream journal",
    # AI tools — the densest category for new launches right now
    "ai photo editor", "ai assistant", "ai chat", "ai video generator", "ai headshot",
    "ai voice changer", "ai music generator", "ai writing assistant", "ai resume builder",
    "ai interior design", "ai recipe", "ai study helper", "ai math solver", "ai translator",
    "ai avatar", "ai background remover", "ai photo enhancer", "ai logo maker",
    "ai presentation", "ai note taker",
    # productivity and focus
    "focus timer", "pomodoro", "task manager", "todo list", "note taking", "mind map",
    "kanban board", "daily planner", "goal tracker", "routine builder", "checklist app",
    "voice recorder transcribe", "pdf scanner", "document scanner", "signature pdf",
    "file manager", "clipboard manager", "password manager", "two factor authenticator",
    "qr code scanner",
    # learning
    "language flashcards", "spaced repetition", "vocabulary builder", "learn spanish",
    "learn japanese", "sign language", "chess puzzles", "math practice", "coding practice",
    "typing practice", "music theory", "guitar tuner", "piano lessons", "drum machine",
    "metronome", "ear training",
    # health and wellbeing
    "meditation timer", "breathing exercise", "sleep sounds", "white noise", "anxiety relief",
    "stretching routine", "yoga poses", "posture reminder", "eye exercise", "fasting timer",
    "hydration reminder", "cold shower", "quit smoking", "sobriety counter", "therapy journal",
    # home, food, life admin
    "recipe manager", "meal planner", "grocery list", "pantry tracker", "wine cellar",
    "cocktail recipes", "home inventory", "chore chart", "packing list", "trip planner",
    "flight tracker", "currency converter", "unit converter", "tip calculator",
    "split the bill", "car maintenance", "garden planner", "bird identifier",
    "plant identifier", "mushroom identifier",
    # small games, the indie long tail
    "idle tycoon", "merge puzzle", "escape room", "cozy farm game", "word game offline",
    "sudoku daily", "crossword puzzle", "jigsaw puzzle", "solitaire classic", "mahjong",
    "trivia quiz", "logic puzzle", "block puzzle", "match 3 offline", "roguelike deckbuilder",
    "incremental clicker", "physics sandbox", "pixel dungeon", "text adventure",
    "chess variant", "tower defense", "farm simulator", "cooking game", "fishing game",
    "survival craft", "bullet hell", "rhythm game", "hidden object", "coloring book",
    "drawing game",
    # creative and media
    "video editor", "photo collage", "meme maker", "sticker maker", "gif maker",
    "screen recorder", "podcast player", "audiobook player", "ebook reader", "comic reader",
    "radio streaming", "karaoke", "beat maker", "sample pad", "vocal remover",
    # utility niches
    "wifi analyzer", "speed test", "battery monitor", "storage cleaner", "app lock",
    "vpn free", "ad blocker", "call recorder", "spam blocker", "contact backup",
    "sms backup", "widget maker", "icon pack", "wallpaper 4k", "live wallpaper",
    "keyboard theme", "font changer", "flashlight", "compass", "level tool",
    "measure distance", "noise meter", "barcode scanner", "ocr text", "handwriting to text",
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


def search_sweep(fetcher: Fetcher, terms: list[str] | None = None) -> list[tuple[str, str, int | None]]:
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

    # Smallest band first: the enrich queue consumes this order, and enrichment capacity is
    # the scarce resource. Unknown bands sort after known-small ones but before known-large.
    ordered = sorted(
        banded.items(),
        key=lambda item: (item[1] is None, item[1] if item[1] is not None else 0),
    )
    tiny = sum(1 for _, b in ordered if b is not None and b < VERY_LOW_BAND)
    small = sum(1 for _, b in ordered if b is not None and b < LOW_BAND)
    print(f"  band profile: {tiny} under {VERY_LOW_BAND} installs, {small} under {LOW_BAND}")
    return [(aid, "search", band) for aid, band in ordered]


# ---------------------------------------------------------------------------
# D2 — developer pages
# ---------------------------------------------------------------------------


def developer_urls(name: str | None, developer_id: str | None) -> list[str]:
    """Ordered candidate URLs for one developer — best guess first, other form as fallback.

    Returning both rather than trusting the rule outright: a 404 here is indistinguishable
    from "this developer has no apps", so the fallback is what stops a silent zero.
    """
    did = (developer_id or "").strip()
    urls: list[str] = []
    if did.isdigit():
        urls.append(DEV_NUMERIC_URL.format(dev=urllib.parse.quote_plus(did)))
        if name:
            urls.append(DEV_NAME_URL.format(dev=urllib.parse.quote_plus(name)))
    else:
        if name:
            urls.append(DEV_NAME_URL.format(dev=urllib.parse.quote_plus(name)))
        if did and did != (name or "").replace(" ", "+"):
            urls.append(DEV_NAME_URL.format(dev=did))
    return urls


def developer_crawl(fetcher: Fetcher, developers: list[dict]) -> list[tuple[str, str, None]]:
    """Crawl developer pages for sibling apps.

    Highest-precision Play channel: a developer already in the database shipping something
    new is exactly what we want to catch. Limited by definition to developers we know, so it
    compounds rather than bootstraps.

    `developers` is a list of {"developer": name, "developer_id": id}. Empty results are
    counted and reported, because a developer page yielding nothing usually means a stale
    name or the wrong URL form — not a developer who shipped nothing.
    """
    found: set[str] = set()
    empty: list[str] = []

    for dev in developers:
        name, did = dev.get("developer"), dev.get("developer_id")
        ids: list[str] = []
        for url in developer_urls(name, did):
            ids = _ids_from_html(fetcher.get(url, expect=_has_app_links))
            if ids:
                break
        if ids:
            found.update(ids)
        else:
            empty.append(name or str(did))
        print(f"  developer {name!r}: {len(ids)} apps")

    if empty:
        # Not fatal — developers do get renamed and delisted — but a high rate means the
        # URL rule has drifted again, so make it visible rather than silently thin.
        print(f"  ! {len(empty)}/{len(developers)} developer pages yielded nothing: {empty[:5]}")

    return [(aid, "developer", None) for aid in sorted(found)]


# ---------------------------------------------------------------------------
# D4 — charts and category pages
# ---------------------------------------------------------------------------


def chart_scrape(fetcher: Fetcher, categories: list[str] | None = None) -> list[tuple[str, str, None]]:
    """Scrape the global top page plus category pages.

    A late signal by construction — an app that charts has usually already broken out — but
    cheap, reliable, and the only channel that needs no seeds. Together with D1 it carries
    the cold start.
    """
    categories = categories if categories is not None else CATEGORIES
    found: set[str] = set()
    empty: list[str] = []

    top_ids = _ids_from_html(fetcher.get(TOP_URL, expect=_has_app_links, retries=1))
    found.update(top_ids)
    print(f"  top charts: {len(top_ids)} apps")

    for cat in categories:
        # retries=1: some category pages are *reliably* empty rather than throttled —
        # MEDICAL serves a 1 MB page with zero app links while its neighbours are fine
        # (verified 2026-08-17). Retrying those twice just burns requests.
        ids = _ids_from_html(
            fetcher.get(CATEGORY_URL.format(cat=cat), expect=_has_app_links, retries=1)
        )
        if ids:
            found.update(ids)
        else:
            empty.append(cat)
        print(f"  category {cat}: {len(ids)} apps")

    if empty:
        print(f"  ! {len(empty)}/{len(categories)} categories returned no app links: {empty}")

    return [(aid, "chart", None) for aid in sorted(found)]
