"""Tiny versioned-SQL migration runner:  python -m app.migrate"""
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import DEFAULT_DATABASE_URL

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_ADVISORY_LOCK_ID = 727_274_001   # so two app instances starting together cannot migrate at once


def run_migrations(conn) -> list[str]:
    """conn must be an autocommit connection with dict_row. Returns the versions applied now."""
    applied_now = []
    conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_ID,))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():                      # each migration is all-or-nothing
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_ID,))
    return applied_now


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as connection:
        applied = run_migrations(connection)
    print("Applied:", ", ".join(applied) if applied else "nothing (already up to date)")
