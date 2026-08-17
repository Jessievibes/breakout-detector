"""Structured run logging (spec §4, §6.4).

`run_log` is the early-warning system for IP degradation, so it has one hard requirement:
**the log row must survive the job's failure.** It therefore uses its own autocommit
connection, independent of the job's transaction. If the job rolls back, the log stays.

"Alert" in this system means exactly what spec §6 says: the job exits non-zero, the Actions
run goes red, GitHub emails the repo owner. No separate alerting infrastructure.
"""

from __future__ import annotations

import json
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg

from . import db


class RunLog:
    def __init__(self, job: str):
        self.job = job
        self.id: int | None = None
        self.stats: dict = {}
        self._conn = None

    def _open(self) -> None:
        try:
            self._conn = psycopg.connect(db.dsn(), autocommit=True)
        except Exception as e:
            # Never let logging failure mask the actual job. Degrade to stdout.
            print(f"! run_log unavailable ({type(e).__name__}); continuing without it")
            self._conn = None

    def start(self) -> None:
        self._open()
        if not self._conn:
            return
        with self._conn.cursor() as cur:
            cur.execute("insert into run_log (job) values (%s) returning id", [self.job])
            self.id = cur.fetchone()[0]

    def update(self, **stats) -> None:
        """Merge stats as the job progresses, so a crashed run still leaves numbers behind."""
        self.stats.update(stats)
        if not (self._conn and self.id):
            return
        with self._conn.cursor() as cur:
            cur.execute(
                "update run_log set stats = %s where id = %s",
                [json.dumps(self.stats, default=str), self.id],
            )

    def finish(self, ok: bool) -> None:
        if self._conn and self.id:
            with self._conn.cursor() as cur:
                cur.execute(
                    "update run_log set ended_at = now(), ok = %s, stats = %s where id = %s",
                    [ok, json.dumps(self.stats, default=str), self.id],
                )
        if self._conn:
            self._conn.close()


@contextmanager
def run(job: str):
    """Wrap a job. Yields the RunLog; re-raises after recording failure.

    Usage:
        with log.run("enrich") as rl:
            ...
            rl.update(fetched=n, **fetcher.stats.as_dict())
    """
    rl = RunLog(job)
    rl.start()
    started = datetime.now(timezone.utc)
    print(f"=== {job} starting {started.isoformat(timespec='seconds')} ===")
    try:
        yield rl
    except Exception as e:
        rl.update(error=f"{type(e).__name__}: {e}")
        rl.finish(ok=False)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"=== {job} FAILED after {elapsed:.0f}s: {type(e).__name__}: {e} ===")
        traceback.print_exc()
        # Loud failure is the point (spec §6): red run → email.
        sys.exit(1)
    else:
        rl.finish(ok=True)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"=== {job} ok in {elapsed:.0f}s — {json.dumps(rl.stats, default=str)} ===")
