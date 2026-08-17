"""Postgres access for the job pipeline.

Deviation from spec §3 worth stating: the jobs talk to Postgres directly via psycopg,
not through the Supabase REST client. Batch upserts, `on conflict do update`, window
functions, and transactional multi-table writes are all native SQL and awkward over REST.
The Supabase client stays where it belongs — the dashboard's server components.

Connection string comes from DATABASE_URL (Supabase → Project Settings → Database →
Connection string → URI). Prefer the pooler on port 6543 for short-lived Actions jobs.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()  # .env for local runs; Actions injects real env vars and this is a no-op


def dsn() -> str:
    """The connection string, defensively cleaned.

    Two mistakes are common enough when copying a value into a GitHub secret that silently
    tolerating them beats failing with an opaque libpq error: a trailing newline, and pasting
    the whole `DATABASE_URL=...` line into a field that wants only the value.
    """
    url = (os.environ.get("DATABASE_URL") or "").strip().strip('"').strip("'")

    if url.startswith("DATABASE_URL="):
        print("! DATABASE_URL contained its own name as a prefix — stripping it.")
        print("  (In a GitHub secret, the value field takes only the URL.)")
        url = url[len("DATABASE_URL=") :].strip()

    if not url:
        raise SystemExit(
            "DATABASE_URL is not set.\n"
            "  Supabase → Connect → Session pooler → URI (replace [YOUR-PASSWORD])\n"
            "  Local:   put it in .env (gitignored), run via `python -m src.jobs.<name>`\n"
            "  Actions: repo Settings → Secrets and variables → Actions"
        )
    return url


def raw_connect(*, autocommit: bool = False, row_factory=dict_row):
    """Single place that opens a Postgres connection.

    `prepare_threshold=None` disables psycopg's automatic prepared statements. This is not
    an optimization — it is required against Supabase's transaction pooler (port 6543),
    which multiplexes connections and cannot keep a prepared statement alive between
    statements. Leaving it on produces "prepared statement _pg3_N already exists" errors
    that appear only after a job has run a query several times, which is a miserable thing
    to debug. Harmless on a session-mode or direct connection, so it is set unconditionally.
    """
    return psycopg.connect(
        dsn(),
        row_factory=row_factory,
        autocommit=autocommit,
        prepare_threshold=None,
    )


@contextmanager
def connect():
    """One transaction per job step. Commits on clean exit, rolls back on exception.

    Rolling back matters: a discovery run that dies halfway should not leave half its apps
    inserted with no snapshot, because the enrich queue would then treat them as done.
    """
    with raw_connect(autocommit=False) as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def upsert_app(conn, *, store: str, store_app_id: str, discovered_via: str, **fields) -> int:
    """Insert or update an app, returning its id.

    `first_seen` is never overwritten — it is the basis of the discovery-latency KPI
    (median(first_seen − released) per channel), which is how we learn whether a channel
    is finding apps early or just finding apps. Same for `discovered_via`: first channel
    to see an app keeps the credit.
    """
    cols = {k: v for k, v in fields.items() if v is not None}
    names = ["store", "store_app_id", "discovered_via", *cols.keys()]
    placeholders = ", ".join(["%s"] * len(names))
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols) or "store = excluded.store"

    sql = f"""
        insert into app ({", ".join(names)})
        values ({placeholders})
        on conflict (store, store_app_id) do update set {updates}
        returning id
    """
    with conn.cursor() as cur:
        cur.execute(sql, [store, store_app_id, discovered_via, *cols.values()])
        return cur.fetchone()["id"]


def insert_apps_bulk(conn, store: str, rows: list[tuple[str, str, int | None]]) -> int:
    """Bulk-register discovered apps as (store_app_id, discovered_via, discovery_installs).

    Discovery only needs to know an app *exists*; enrichment fills in the rest. Keeping this
    insert minimal is what lets a discovery job register 500 apps in one statement and still
    be idempotent.

    `discovery_installs` is carried because it is the only youth signal available before
    enrichment, and it decides the enrich queue's order — see sql/003.

    Returns the count of genuinely new apps — the number that matters for channel yield.
    """
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into app (store, store_app_id, discovered_via, discovery_installs)
            values (%s, %s, %s, %s)
            on conflict (store, store_app_id) do update
               set discovery_installs = coalesce(app.discovery_installs, excluded.discovery_installs)
            """,
            [(store, aid, via, band) for aid, via, band in rows],
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def upsert_snapshot(conn, app_id: int, day: date, **metrics) -> None:
    """Write one app-day row. `day` is the date the run *intends* to cover.

    Idempotent by design (spec §8): Actions cron fires late, and a re-run must overwrite
    cleanly rather than duplicating or skipping.
    """
    cols = {k: v for k, v in metrics.items() if v is not None}
    if not cols:
        return
    names = ["app_id", "day", *cols.keys()]
    placeholders = ", ".join(["%s"] * len(names))
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            insert into snapshot ({", ".join(names)})
            values ({placeholders})
            on conflict (app_id, day) do update set {updates}
            """,
            [app_id, day, *cols.values()],
        )


def mark_enriched(conn, app_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update app set last_enriched = now(), enrich_failures = 0 where id = %s",
            [app_id],
        )


def record_enrich_failure(conn, app_id: int, delist: bool = False) -> None:
    """Count consecutive failures; delist after repeated ones so the queue stops churning.

    Delisting on the *first* 404 would be wrong — a transient block looks identical. Three
    strikes is cheap insurance.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            update app
               set enrich_failures = enrich_failures + 1,
                   delisted = case when %s or enrich_failures + 1 >= 3 then true else delisted end
             where id = %s
            """,
            [delist, app_id],
        )


def set_flags(conn, app_id: int, **flags) -> None:
    if not flags:
        return
    sets = ", ".join(f"{k} = %s" for k in flags)
    with conn.cursor() as cur:
        cur.execute(f"update app set {sets} where id = %s", [*flags.values(), app_id])


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def enrich_queue(conn, store: str, limit: int, via: str | None = None) -> list[dict]:
    """Apps due for enrichment, never-enriched first, then stalest.

    Ordering by `last_enriched nulls first` means newly discovered apps get their first
    snapshot before established apps get their next one — the young apps are the entire
    point of the system.

    `via` restricts to one discovery channel. That is a diagnostic, not a normal mode: it
    is how you measure a channel's discovery latency (median `first_seen − released`) and
    so decide whether it earns its request budget.
    """
    sql = """
        select id, store_app_id, name, released, discovered_via
          from app
         where store = %s and delisted = false
    """
    params: list = [store]
    if via:
        sql += " and discovered_via = %s"
        params.append(via)
    # Never-enriched first, and among those, smallest discovery band first — apps under 100
    # installs are ~93% likely to be under 120 days old, and finding those is the whole point.
    # Discovery outruns the enrich budget several times over, so this ordering is what decides
    # whether the system surfaces new apps or merely re-measures established ones.
    sql += """
        order by last_enriched nulls first,
                 discovery_installs nulls last,
                 id
        limit %s
    """
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def developer_crawl_seeds(conn, limit: int) -> list[dict]:
    """Distinct Play developers for channel D2, as {developer, developer_id}.

    Both fields are needed, not just the name: Play serves numeric-id developers from
    `dev?id=<numeric>` and name-id developers from `developer?id=<NAME>`, and the wrong
    form returns a clean 404 rather than an error (verified 2026-08-17). Roughly half of
    sampled apps use each form, so passing names alone silently loses half of D2.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select developer,
                   min(developer_id) as developer_id,
                   count(*) as n
              from app
             where store = 'play' and developer is not null and delisted = false
             group by developer
             order by n desc, developer
             limit %s
            """,
            [limit],
        )
        return [
            {"developer": r["developer"], "developer_id": r["developer_id"]}
            for r in cur.fetchall()
        ]


def last_snapshot_metric(conn, app_id: int, column: str) -> int | None:
    """Most recent non-null value of one snapshot column, for delta sanity checks."""
    if column not in {"install_exact", "rating_count", "review_count"}:
        raise ValueError(f"refusing to interpolate unknown column {column!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select {column} as v
              from snapshot
             where app_id = %s and {column} is not null
             order by day desc
             limit 1
            """,
            [app_id],
        )
        row = cur.fetchone()
        return row["v"] if row else None


def upsert_ranks(conn, store: str, ranks: dict[str, int], day: date) -> int:
    """Write chart positions into today's snapshot rows.

    Runs from the discovery job, before enrichment fills in the metrics — the upsert lets
    the two halves land in either order. `least` keeps the *best* rank when an app charts in
    several genres, and ignores nulls, so a metrics-only row is upgraded rather than wiped.

    Apps not yet in `app` are skipped by the join rather than inserted: rank without identity
    is not useful, and discovery has already registered everything it saw.
    """
    if not ranks:
        return 0
    sids, positions = zip(*ranks.items())
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into snapshot (app_id, day, best_rank)
            select a.id, %s::date, t.rank
              from unnest(%s::text[], %s::int[]) as t(sid, rank)
              join app a on a.store = %s::store_kind and a.store_app_id = t.sid
            on conflict (app_id, day) do update
               set best_rank = least(snapshot.best_rank, excluded.best_rank)
            """,
            [day, list(sids), list(positions), store],
        )
        return cur.rowcount or 0


def backfill_queue(conn, store: str, limit: int) -> list[dict]:
    """Apps whose review history has never been pulled, most-rated first.

    Ordering by rating count, not discovery time, because the budget is requests and written
    reviews run at roughly 5% of star ratings. The first live backfill spent 36 of 40 requests
    on apps with no ratings at all and recovered 12 reviews; those apps have no history to
    recover, while a well-rated young app can have hundreds.

    Apps not yet enriched (no snapshot, so no rating count) sort last rather than being
    excluded — they are unknown, not known-empty, and the next run will have their counts.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select a.id, a.store_app_id, a.name, a.released, s.rating_count
              from app a
              left join lateral (
                    select rating_count
                      from snapshot
                     where app_id = a.id and rating_count is not null
                     order by day desc
                     limit 1
                   ) s on true
             where a.store = %s
               and a.reviews_backfilled = false
               and a.delisted = false
             order by s.rating_count desc nulls last, a.first_seen
             limit %s
            """,
            [store, limit],
        )
        return cur.fetchall()


def insert_review_events(conn, app_id: int, reviews: list[dict], country: str = "us") -> int:
    """Bulk-insert reviews. Idempotent on (app_id, review_id)."""
    if not reviews:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into review_event (app_id, review_id, posted_at, rating, version, country)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (app_id, review_id) do nothing
            """,
            [
                (app_id, r["review_id"], r["posted_at"], r.get("rating"), r.get("version"), country)
                for r in reviews
            ],
        )
        return cur.rowcount or 0


def mark_backfilled(conn, app_id: int) -> None:
    """Flip the one-way flag. Only call after a *complete* page walk — a partial history
    marked complete is never revisited and the gap becomes permanent (spec §6.3)."""
    with conn.cursor() as cur:
        cur.execute("update app set reviews_backfilled = true where id = %s", [app_id])


def apply_sql_file(conn, path: str) -> None:
    with open(path) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
