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
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set.\n"
            "  Supabase → Project Settings → Database → Connection string → URI\n"
            "  Local: put it in .env (gitignored) and run via `python -m src.jobs.<name>`"
        )
    return url


@contextmanager
def connect():
    """One transaction per job step. Commits on clean exit, rolls back on exception.

    Rolling back matters: a discovery run that dies halfway should not leave half its apps
    inserted with no snapshot, because the enrich queue would then treat them as done.
    """
    with psycopg.connect(dsn(), row_factory=dict_row, autocommit=False) as conn:
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


def insert_apps_bulk(conn, store: str, rows: list[tuple[str, str]]) -> int:
    """Bulk-register discovered app ids as (store_app_id, discovered_via).

    Discovery only needs to know an app *exists*; enrichment fills in the rest. Keeping
    this insert minimal is what lets a discovery job register 500 apps in one statement
    and still be idempotent.

    Returns the count of genuinely new apps — the number that matters for channel yield.
    """
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into app (store, store_app_id, discovered_via)
            values (%s, %s, %s)
            on conflict (store, store_app_id) do nothing
            """,
            [(store, aid, via) for aid, via in rows],
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


def enrich_queue(conn, store: str, limit: int) -> list[dict]:
    """Apps due for enrichment, never-enriched first, then stalest.

    Ordering by `last_enriched nulls first` means newly discovered apps get their first
    snapshot before established apps get their next one — the young apps are the entire
    point of the system.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, store_app_id, name, released
              from app
             where store = %s and delisted = false
             order by last_enriched nulls first, id
             limit %s
            """,
            [store, limit],
        )
        return cur.fetchall()


def developer_crawl_seeds(conn, limit: int) -> list[str]:
    """Distinct Play developer names for channel D2.

    Names, not numeric ids: `developer?id=<NAME>` is the working URL form; `dev?id=` is
    404 (verified 2026-08-17).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select developer, count(*) as n
              from app
             where store = 'play' and developer is not null and delisted = false
             group by developer
             order by n desc, developer
             limit %s
            """,
            [limit],
        )
        return [r["developer"] for r in cur.fetchall()]


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


def apply_sql_file(conn, path: str) -> None:
    with open(path) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
