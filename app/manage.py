"""Command-line administration.

    python -m app.manage check-config               validate .env without starting the server
    python -m app.manage create-admin --email you@example.com --name "Your Name"
    python -m app.manage reset-password --email you@example.com      (if every admin is locked out)
    python -m app.manage export-openapi > openapi.json
Passwords are typed at a hidden prompt, never passed on the command line (shell history!).
"""
import argparse
import getpass
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from . import auth, security
from .config import DEFAULT_DATABASE_URL
from .errors import AppError
from .migrate import run_migrations
from .settings import ConfigError, Settings, mask_url


def _connect():
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    run_migrations(conn)
    return conn


def _ask_password(email: str) -> str:
    while True:
        first = getpass.getpass("New password: ")
        problems = security.validate_password(first, email)
        if problems:
            print("  - " + "\n  - ".join(problems))
            continue
        if getpass.getpass("Repeat password: ") != first:
            print("  The two entries differ. Try again.")
            continue
        return first


def check_config() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(exc)
        return 1
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    backend = os.environ.get("EMAIL_BACKEND", "file").lower()
    errors, warnings = settings.problems(url, backend)
    print("Configuration (secrets masked):")
    print(json.dumps(settings.summary(url, backend), indent=2))
    for w in warnings:
        print("WARNING:", w)
    for e in errors:
        print("ERROR:  ", e)
    print("OK" if not errors else "NOT OK")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config")
    sub.add_parser("export-openapi")
    create = sub.add_parser("create-admin")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    reset = sub.add_parser("reset-password")
    reset.add_argument("--email", required=True)
    args = parser.parse_args()

    if args.command == "check-config":
        return check_config()
    if args.command == "export-openapi":
        from .main import app
        print(json.dumps(app.openapi(), indent=2))
        return 0
    try:
        conn = _connect()
    except psycopg.OperationalError as exc:
        print(f"Cannot connect to {mask_url(os.environ.get('DATABASE_URL', DEFAULT_DATABASE_URL))}: {exc}")
        return 1
    try:
        if args.command == "create-admin":
            user = auth.create_user(conn, args.email, args.name, _ask_password(args.email), "admin")
            print(f"Created admin #{user['id']} {user['email']}")
        else:
            row = conn.execute("SELECT id FROM users WHERE lower(email) = %s", (args.email.strip().lower(),)).fetchone()
            if row is None:
                print("No such user.")
                return 1
            auth.admin_reset_password(conn, row["id"], _ask_password(args.email), actor_id=row["id"])
            print("Password reset. All sessions for this user were signed out and any lock was cleared.")
    except AppError as exc:
        print(f"{exc.code}: {exc.message}")
        for d in exc.details:
            print("  -", d.get("issue", d))
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
