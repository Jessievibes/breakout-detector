"""Preflight checks — fail before doing work, with a diagnosis that is safe to publish.

Why this exists: GitHub does not serve Actions logs anonymously, so a failed run on a public
repo shows *which step* failed but not why. Splitting the checks into separately-named steps
makes the failure legible from the public API alone — the step name is the diagnosis.

Everything printed here is deliberately safe: presence, length, scheme, host suffix, and
character classes. Never the password, never the whole URL.
"""

from __future__ import annotations

import argparse
import socket
import sys
import urllib.parse

from ..lib import db


def check_config() -> int:
    """Validate the shape of DATABASE_URL without connecting."""
    try:
        url = db.dsn()
    except SystemExit as e:
        print(e)
        return 1

    print(f"  DATABASE_URL present ({len(url)} chars)")
    problems = []

    if "[" in url or "]" in url:
        problems.append("contains [ ] — the [YOUR-PASSWORD] placeholder was never replaced")

    p = urllib.parse.urlparse(url)
    # urlparse is lazy: it accepts the string and only raises when a component is *read*.
    # An unencoded '@' or ':' in the password shifts the host/port boundary, so .port is
    # where the damage surfaces. Reading it defensively turns a traceback into a diagnosis.
    try:
        port = p.port
    except ValueError:
        print("  MALFORMED — the password contains characters that must be percent-encoded")
        print("  (@ : / ? # % or a space shift where the host and port begin)")
        print("  Fix: reset the Supabase password to letters and digits only, then re-copy.")
        return 1

    if p.scheme not in ("postgresql", "postgres"):
        problems.append(f"scheme is {p.scheme!r}, expected postgresql")
    if not p.hostname:
        problems.append("no host")
    if not p.username:
        problems.append("no username")
    if not p.password:
        problems.append("no password")

    print(f"  scheme={p.scheme}  host=...{(p.hostname or '')[-30:]}  port={port}")
    print(f"  user={'set' if p.username else 'MISSING'}  password={'set' if p.password else 'MISSING'}")

    if p.hostname and "pooler.supabase.com" in p.hostname and p.port == 5432:
        print("  using the session pooler — correct for these jobs")
    elif p.port == 6543:
        print("  note: transaction pooler (6543). Works, but session mode (5432) is preferred")
    elif p.hostname and p.hostname.startswith("db.") and "supabase" in p.hostname:
        problems.append(
            "direct connection host — Supabase serves this over IPv6 only, and Actions "
            "runners have no IPv6. Use the session pooler instead"
        )

    if problems:
        print("\n  PROBLEMS:")
        for x in problems:
            print(f"    - {x}")
        return 1
    print("\n  config shape OK")
    return 0


def check_connect() -> int:
    """Resolve, open a socket, then authenticate — each failure distinguishable."""
    url = db.dsn()
    p = urllib.parse.urlparse(url)
    try:
        host, port = p.hostname, p.port or 5432
    except ValueError:
        print("  MALFORMED URL — run the config check for details")
        return 1

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        families = {"IPv4" if i[0] == socket.AF_INET else "IPv6" for i in infos}
        print(f"  DNS ok — {len(infos)} address(es), {', '.join(sorted(families))}")
    except socket.gaierror as e:
        print(f"  DNS FAILED for host: {e}")
        return 1

    try:
        with socket.create_connection((host, port), timeout=15):
            print(f"  TCP ok — port {port} reachable")
    except OSError as e:
        print(f"  TCP FAILED to port {port}: {e}")
        return 1

    import psycopg

    try:
        with psycopg.connect(url, connect_timeout=25, prepare_threshold=None) as conn:
            with conn.cursor() as cur:
                cur.execute("select current_database(), current_user")
                dbname, user = cur.fetchone()
                print(f"  AUTH ok — connected to {dbname} as {user}")
        return 0
    except psycopg.OperationalError as e:
        msg = str(e)
        if "password authentication failed" in msg:
            print("  AUTH FAILED — wrong password for this user.")
            print("  The secret's value probably differs from the local .env that works.")
            print("  Copy it exactly:  grep '^DATABASE_URL=' .env | sed 's/^DATABASE_URL=//' | pbcopy")
        else:
            print(f"  CONNECT FAILED — {msg[:180]}")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight configuration checks")
    ap.add_argument("check", choices=["config", "connect"])
    args = ap.parse_args()
    return check_config() if args.check == "config" else check_connect()


if __name__ == "__main__":
    sys.exit(main())
