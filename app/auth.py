"""Accounts, sessions and login protection (PostgreSQL).

* Passwords: salted scrypt, never logged, never returned.
* Sessions: random opaque tokens; only their SHA-256 is stored, so tokens can be revoked instantly
  (logout, password change, user deactivated) and a database leak does not leak usable tokens.
* Login failures look identical whether the email exists, the password is wrong, the account is
  locked or disabled, and cost the same time, so nobody can probe which emails have accounts.
"""
import logging

import psycopg

from . import security
from .errors import AppError
from .settings import Settings

log = logging.getLogger("inventory.security")

GENERIC_LOGIN_ERROR = "Invalid email or password, or the account is temporarily locked."
USER_COLUMNS = "id, email, full_name, role, is_active, created_at, last_login_at, locked_until"


def record_event(conn, event: str, user_id: int | None = None, email: str | None = None,
                 ip: str | None = None, detail: str | None = None) -> None:
    """Security audit trail (database + structured log). Never raises: auditing must not break a request."""
    log.info(event, extra={"event": event, "subject_user_id": user_id, "ip": ip})
    try:
        conn.execute("INSERT INTO security_events (event, user_id, email, ip, detail) VALUES (%s, %s, %s, %s, %s)",
                     (event, user_id, email, ip, detail))
    except Exception:
        log.exception("Could not write security event", extra={"event": event})


def _weak_password(problems: list[str]) -> AppError:
    return AppError(422, "WEAK_PASSWORD", "The password does not meet the password policy.",
                    [{"field": "password", "issue": p} for p in problems])


def unauthorized(message: str, code: str = "UNAUTHORIZED") -> AppError:
    return AppError(401, code, message, headers={"WWW-Authenticate": "Bearer"})


# ---------------------------------------------------------------- users
def create_user(conn, email: str, full_name: str, password: str, role: str, actor_id: int | None = None) -> dict:
    email_n = email.strip().lower()
    if role not in security.ROLES:
        raise AppError(422, "VALIDATION_ERROR", f"Role must be one of: {', '.join(security.ROLES)}.",
                       [{"field": "role", "issue": "Unknown role"}])
    problems = security.validate_password(password, email_n)
    if problems:
        raise _weak_password(problems)
    try:
        user = conn.execute(
            f"INSERT INTO users (email, full_name, password_hash, role) VALUES (%s, %s, %s, %s) "
            f"RETURNING {USER_COLUMNS}", (email_n, full_name.strip(), security.hash_password(password), role),
        ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise AppError(409, "EMAIL_TAKEN", "An account with this email already exists.",
                       [{"field": "email", "issue": "Already registered"}]) from exc
    record_event(conn, "user_created", user["id"], email_n, detail=f"role={role}; by_user={actor_id}")
    return user


def get_user(conn, user_id: int) -> dict:
    return _get_user(conn, user_id)


def list_users(conn) -> dict:
    return {"items": conn.execute(f"SELECT {USER_COLUMNS} FROM users ORDER BY id").fetchall()}


def _get_user(conn, user_id: int) -> dict:
    user = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = %s", (user_id,)).fetchone()
    if user is None:
        raise AppError(404, "USER_NOT_FOUND", f"User {user_id} does not exist.",
                       [{"field": "user_id", "issue": "User not found"}])
    return user


def update_user(conn, user_id: int, actor_id: int, full_name: str | None = None, role: str | None = None,
                is_active: bool | None = None) -> dict:
    with conn.transaction():
        # Lock every active admin first, so two admins cannot demote each other at the same moment.
        conn.execute("SELECT id FROM users WHERE role = 'admin' AND is_active ORDER BY id FOR UPDATE").fetchall()
        target = conn.execute("SELECT id, email, role, is_active FROM users WHERE id = %s FOR UPDATE",
                              (user_id,)).fetchone()
        if target is None:
            raise AppError(404, "USER_NOT_FOUND", f"User {user_id} does not exist.",
                           [{"field": "user_id", "issue": "User not found"}])
        new_role = role if role is not None else target["role"]
        new_active = is_active if is_active is not None else target["is_active"]
        if target["role"] == "admin" and target["is_active"] and (new_role != "admin" or not new_active):
            others = conn.execute("SELECT count(*) AS n FROM users WHERE role = 'admin' AND is_active AND id <> %s",
                                  (user_id,)).fetchone()["n"]
            if others == 0:
                raise AppError(409, "LAST_ADMIN", "There must always be at least one active admin.",
                               [{"field": "role" if new_role != "admin" else "is_active",
                                 "issue": "This is the last active admin"}])
        if full_name is not None:
            conn.execute("UPDATE users SET full_name = %s WHERE id = %s", (full_name.strip(), user_id))
        conn.execute("UPDATE users SET role = %s, is_active = %s WHERE id = %s", (new_role, new_active, user_id))
        if not new_active:
            conn.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                         (user_id,))
    record_event(conn, "user_updated", user_id, target["email"],
                 detail=f"role={new_role}; active={new_active}; by_user={actor_id}")
    return _get_user(conn, user_id)


def admin_reset_password(conn, user_id: int, new_password: str, actor_id: int) -> dict:
    user = _get_user(conn, user_id)
    problems = security.validate_password(new_password, user["email"])
    if problems:
        raise _weak_password(problems)
    with conn.transaction():
        conn.execute("UPDATE users SET password_hash = %s, password_changed_at = now(), failed_logins = 0, "
                     "locked_until = NULL WHERE id = %s", (security.hash_password(new_password), user_id))
        conn.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
    record_event(conn, "password_reset_by_admin", user_id, user["email"], detail=f"by_user={actor_id}")
    return _get_user(conn, user_id)


# ---------------------------------------------------------------- login / sessions
def login(conn, email: str, password: str, ip: str, user_agent: str, settings: Settings) -> dict:
    email_n = email.strip().lower()
    user = conn.execute("SELECT *, now() AS db_now FROM users WHERE lower(email) = %s", (email_n,)).fetchone()
    locked = bool(user and user["locked_until"] and user["locked_until"] > user["db_now"])

    if user is None or locked or not user["is_active"]:
        security.burn_password_check(password)               # same cost as a real check
        verified = False
    else:
        verified = security.verify_password(password, user["password_hash"])

    if not verified:
        if user is not None and user["is_active"] and not locked:
            _register_failure(conn, user, ip, settings)
        else:
            record_event(conn, "login_failed", user["id"] if user else None, email_n, ip,
                         "locked" if locked else ("disabled" if user else "unknown_email"))
        raise unauthorized(GENERIC_LOGIN_ERROR, "INVALID_CREDENTIALS")

    token, token_hash = security.new_token()
    with conn.transaction():
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, ip, user_agent) "
            "VALUES (%s, %s, now() + make_interval(hours => %s), %s, %s)",
            (token_hash, user["id"], settings.token_ttl_hours, ip, user_agent))
        conn.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = now() WHERE id = %s",
                     (user["id"],))
        expires_at = conn.execute("SELECT expires_at FROM sessions WHERE token_hash = %s",
                                  (token_hash,)).fetchone()["expires_at"]
    record_event(conn, "login_ok", user["id"], email_n, ip)
    return {"access_token": token, "token_type": "bearer", "expires_at": expires_at, "user": _get_user(conn, user["id"])}


def _register_failure(conn, user: dict, ip: str, settings: Settings) -> None:
    with conn.transaction():                                  # row lock: parallel guesses cannot dodge the counter
        row = conn.execute("SELECT failed_logins, locked_until, now() AS t FROM users WHERE id = %s FOR UPDATE",
                           (user["id"],)).fetchone()
        expired_lock = row["locked_until"] is not None and row["locked_until"] <= row["t"]
        count = 1 if expired_lock else row["failed_logins"] + 1
        lock = count >= settings.max_failed_logins
        conn.execute("UPDATE users SET failed_logins = %s, locked_until = CASE WHEN %s THEN now() + "
                     "make_interval(mins => %s) ELSE NULL END WHERE id = %s",
                     (count, lock, settings.lockout_minutes, user["id"]))
    record_event(conn, "login_failed", user["id"], user["email"], ip, f"attempt {count}")
    if lock:
        record_event(conn, "account_locked", user["id"], user["email"], ip, f"for {settings.lockout_minutes} min")


def user_for_token(conn, token: str | None) -> dict | None:
    """The signed-in user for a bearer token, or None (unknown, expired, revoked or disabled account)."""
    if not token or len(token) > 200:
        return None
    row = conn.execute(
        "SELECT s.id AS session_id, u.id, u.email, u.full_name, u.role, u.is_active "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()",
        (security.hash_token(token),)).fetchone()
    if row is None or not row["is_active"]:
        return None
    conn.execute("UPDATE sessions SET last_used_at = now() WHERE id = %s "
                 "AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')", (row["session_id"],))
    return row


def logout(conn, user: dict, ip: str | None = None) -> None:
    conn.execute("UPDATE sessions SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (user["session_id"],))
    record_event(conn, "logout", user["id"], user["email"], ip)


def change_password(conn, user: dict, current_password: str, new_password: str, ip: str | None = None) -> None:
    row = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user["id"],)).fetchone()
    if not security.verify_password(current_password, row["password_hash"]):
        record_event(conn, "password_change_failed", user["id"], user["email"], ip)
        raise AppError(400, "INVALID_CURRENT_PASSWORD", "The current password is incorrect.",
                       [{"field": "current_password", "issue": "Incorrect"}])
    problems = security.validate_password(new_password, user["email"])
    if new_password == current_password:
        problems.append("The new password must be different from the current one.")
    if problems:
        raise _weak_password(problems)
    with conn.transaction():
        conn.execute("UPDATE users SET password_hash = %s, password_changed_at = now() WHERE id = %s",
                     (security.hash_password(new_password), user["id"]))
        conn.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND id <> %s AND revoked_at IS NULL",
                     (user["id"], user["session_id"]))          # every other device is signed out
    record_event(conn, "password_changed", user["id"], user["email"], ip)


def list_security_events(conn, limit: int) -> dict:
    return {"items": conn.execute("SELECT id, event, user_id, email, ip, detail, created_at FROM security_events "
                                  "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()}
