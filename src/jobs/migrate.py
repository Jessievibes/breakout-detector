"""Apply sql/*.sql in filename order. Idempotent — safe to re-run on every deploy.

Runs with autocommit so each file's own `begin; ... commit;` governs its transaction; that
way a failing file rolls back on its own rather than poisoning the whole migration.
"""

from __future__ import annotations

import glob
import os
import sys

from ..lib import db

SQL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "sql")


def main() -> int:
    files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
    if not files:
        sys.exit(f"no .sql files found in {os.path.abspath(SQL_DIR)}")

    with db.raw_connect(autocommit=True, row_factory=None) as conn:
        for path in files:
            name = os.path.basename(path)
            with open(path) as f:
                sql = f.read()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                print(f"  applied {name}")
            except Exception as e:
                print(f"  FAILED {name}: {type(e).__name__}: {e}")
                return 1
    print(f"{len(files)} migration(s) applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
