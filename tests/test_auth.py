"""Authentication: login, sessions, lockout, password rules (needs TEST_DATABASE_URL)."""
import re
import threading

import pytest

from app import auth
from app.errors import AppError
from tests.conftest import DEFAULT_PASSWORD, make_user, scalar
from tests.test_api import make_product


def login(client, email, password):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_login_returns_a_token_and_a_safe_profile(web):
    user = make_user(web.pool, "staff", name="Sara Ali")
    r = login(web.anon(), user["email"], user["password"])
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["access_token"].startswith("inv_")
    assert body["user"]["email"] == user["email"] and body["user"]["role"] == "staff"
    assert "scrypt" not in r.text and "password" not in str(body["user"]).lower()
    me = web.anon().get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["full_name"] == "Sara Ali"


def test_every_kind_of_login_failure_looks_the_same(web):
    real = make_user(web.pool, "staff")
    disabled = make_user(web.pool, "staff")
    with web.pool.connection() as conn:
        conn.execute("UPDATE users SET is_active = false WHERE id = %s", (disabled["id"],))
    attempts = [login(web.anon(), "nobody@example.com", "whatever-123456"),            # unknown email
                login(web.anon(), real["email"], "wrong-password-123"),                  # wrong password
                login(web.anon(), disabled["email"], disabled["password"])]              # right password, disabled
    for r in attempts:
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert len({r.json()["error"]["message"] for r in attempts}) == 1                  # nothing to tell them apart


def test_account_locks_after_repeated_failures_and_unlocks_later(web):
    user = make_user(web.pool, "staff")
    anon = web.anon()
    for _ in range(5):
        assert login(anon, user["email"], "wrong-password-123").status_code == 401
    assert scalar(web.pool, "SELECT locked_until IS NOT NULL FROM users WHERE id = %s", (user["id"],)) is True
    assert login(anon, user["email"], user["password"]).status_code == 401              # correct password, still locked
    assert scalar(web.pool, "SELECT count(*) FROM security_events WHERE event = 'account_locked'") == 1

    with web.pool.connection() as conn:                                                  # time passes
        conn.execute("UPDATE users SET locked_until = now() - interval '1 minute' WHERE id = %s", (user["id"],))
    assert login(anon, user["email"], user["password"]).status_code == 200
    assert scalar(web.pool, "SELECT failed_logins FROM users WHERE id = %s", (user["id"],)) == 0


def test_one_typo_after_a_lock_expires_does_not_lock_again(web):
    user = make_user(web.pool, "staff")
    with web.pool.connection() as conn:
        conn.execute("UPDATE users SET failed_logins = 5, locked_until = now() - interval '1 minute' WHERE id = %s",
                     (user["id"],))
    assert login(web.anon(), user["email"], "wrong-password-123").status_code == 401
    assert scalar(web.pool, "SELECT failed_logins FROM users WHERE id = %s", (user["id"],)) == 1
    assert scalar(web.pool, "SELECT locked_until IS NULL FROM users WHERE id = %s", (user["id"],)) is True


def test_parallel_password_guessing_cannot_dodge_the_lockout(web):
    user = make_user(web.pool, "staff")
    settings = web.app.state.settings
    barrier, results = threading.Barrier(12), []

    def guess():
        barrier.wait()
        try:
            with web.pool.connection() as conn:
                auth.login(conn, user["email"], "wrong-password-123", "1.2.3.4", "test", settings)
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=guess) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert set(results) == {"INVALID_CREDENTIALS"}
    assert scalar(web.pool, "SELECT failed_logins >= 5 AND locked_until IS NOT NULL FROM users WHERE id = %s",
                  (user["id"],)) is True
    assert login(web.anon(), user["email"], user["password"]).status_code == 401


def test_every_route_requires_a_token(web):
    anon = web.anon()
    public = {("GET", "/health"), ("POST", "/auth/login")}
    checked = 0
    for path, methods in web.app.openapi()["paths"].items():
        for method in methods:
            if (method.upper(), path) in public:
                continue
            url = re.sub(r"\{[^}]+\}", "1", path)
            r = anon.request(method.upper(), url, json={} if method.upper() in ("POST", "PATCH") else None)
            assert r.status_code == 401, f"{method.upper()} {path} answered {r.status_code} without a token"
            assert r.json()["error"]["code"] == "UNAUTHORIZED" and r.headers["www-authenticate"] == "Bearer"
            checked += 1
    assert checked >= 28


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "Bearer inv_notarealtoken",
                                    "Bearer " + "x" * 500, "inv_sometoken", "Token abc"])
def test_malformed_or_unknown_tokens_are_rejected(web, header):
    r = web.anon().get("/products", headers={"Authorization": header})
    assert r.status_code == 401 and r.json()["error"]["code"] in ("UNAUTHORIZED", "INVALID_TOKEN")


def test_tokens_in_the_url_are_not_accepted(web):
    c = web.client("admin")
    assert web.anon().get("/products", params={"access_token": c.token}).status_code == 401


def test_logout_revokes_the_token(web):
    c = web.client("staff")
    assert c.get("/auth/me").status_code == 200
    assert c.post("/auth/logout").status_code == 204
    assert c.get("/auth/me").status_code == 401


def test_expired_sessions_stop_working(web):
    c = web.client("staff")
    with web.pool.connection() as conn:
        conn.execute("UPDATE sessions SET expires_at = now() - interval '1 second'")
    r = c.get("/auth/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_TOKEN"


def test_deactivating_a_user_signs_them_out_at_once(web):
    admin, staff = web.client("admin"), web.client("staff")
    assert staff.get("/products").status_code == 200
    assert admin.patch(f"/users/{staff.user['id']}", json={"is_active": False}).status_code == 200
    assert staff.get("/products").status_code == 401
    assert login(web.anon(), staff.user["email"], staff.user["password"]).status_code == 401
    assert admin.patch(f"/users/{staff.user['id']}", json={"is_active": True}).status_code == 200
    assert login(web.anon(), staff.user["email"], staff.user["password"]).status_code == 200


def test_role_changes_apply_immediately(web):
    admin, staff = web.client("admin"), web.client("staff")
    assert staff.post("/products", json={"sku": "A", "name": "A", "price": "1.00"}).status_code == 201
    assert admin.patch(f"/users/{staff.user['id']}", json={"role": "viewer"}).status_code == 200
    r = staff.post("/products", json={"sku": "B", "name": "B", "price": "1.00"})        # same token, new role
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_the_last_admin_cannot_be_removed(web):
    admin = web.client("admin")
    me = admin.user["id"]
    for change in ({"role": "viewer"}, {"is_active": False}):
        r = admin.patch(f"/users/{me}", json=change)
        assert r.status_code == 409 and r.json()["error"]["code"] == "LAST_ADMIN"
    second = web.client("admin")
    assert admin.patch(f"/users/{me}", json={"role": "staff"}).status_code == 200          # now someone else remains
    assert second.patch(f"/users/{me}", json={"role": "admin"}).status_code == 200


def test_user_creation_rules(client):
    ok = client.post("/users", json={"email": "New.Person@Example.com", "full_name": "New Person",
                                     "password": DEFAULT_PASSWORD, "role": "staff"})
    assert ok.status_code == 201 and ok.json()["email"] == "new.person@example.com"
    assert "password" not in ok.text and "scrypt" not in ok.text

    dup = client.post("/users", json={"email": "NEW.person@example.com", "full_name": "X",
                                      "password": DEFAULT_PASSWORD, "role": "viewer"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "EMAIL_TAKEN"

    weak = client.post("/users", json={"email": "weak@example.com", "full_name": "Weak", "password": "short",
                                       "role": "viewer"})
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "WEAK_PASSWORD"
    assert any("at least 10" in d["issue"] for d in weak.json()["error"]["details"])
    for bad in ({"email": "not-an-email"}, {"role": "superuser"}, {"full_name": ""}):
        body = {"email": "x@example.com", "full_name": "X", "password": DEFAULT_PASSWORD, "role": "viewer", **bad}
        assert client.post("/users", json=body).status_code == 422
    listing = client.get("/users")
    assert listing.status_code == 200 and "scrypt" not in listing.text and "password" not in listing.text.lower()


def test_changing_your_password(web):
    a = web.client("staff")
    b = web.anon()
    token_b = login(b, a.user["email"], a.user["password"]).json()["access_token"]       # a second device
    new = "Another-Strong-Pass-77"
    assert a.post("/auth/change-password", json={"current_password": "nope-nope-nope", "new_password": new}).status_code == 400
    assert a.post("/auth/change-password", json={"current_password": a.user["password"], "new_password": "short"}).status_code == 422
    assert a.post("/auth/change-password", json={"current_password": a.user["password"],
                                                 "new_password": a.user["password"]}).status_code == 422
    assert a.post("/auth/change-password", json={"current_password": a.user["password"], "new_password": new}).status_code == 204

    assert b.get("/auth/me", headers={"Authorization": f"Bearer {token_b}"}).status_code == 401   # other device signed out
    assert a.get("/auth/me").status_code == 200                                                    # this one stays in
    assert login(web.anon(), a.user["email"], a.user["password"]).status_code == 401
    assert login(web.anon(), a.user["email"], new).status_code == 200


def test_admin_password_reset_signs_the_user_out_and_clears_a_lock(web):
    admin, staff = web.client("admin"), web.client("staff")
    with web.pool.connection() as conn:
        conn.execute("UPDATE users SET failed_logins = 5, locked_until = now() + interval '10 minutes' WHERE id = %s",
                     (staff.user["id"],))
    r = admin.post(f"/users/{staff.user['id']}/reset-password", json={"new_password": "Fresh-Temporary-Pass-1"})
    assert r.status_code == 200
    assert staff.get("/auth/me").status_code == 401
    assert login(web.anon(), staff.user["email"], "Fresh-Temporary-Pass-1").status_code == 200
    weak = admin.post(f"/users/{staff.user['id']}/reset-password", json={"new_password": "1234567890"})
    assert weak.status_code == 422


def test_security_events_are_recorded_and_contain_no_secrets(web):
    admin = web.client("admin")
    staff = make_user(web.pool, "staff")
    login(web.anon(), "ghost@example.com", "Secret-Guess-12345")
    login(web.anon(), staff["email"], staff["password"])
    events = admin.get("/security/events").json()["items"]
    names = {e["event"] for e in events}
    assert {"user_created", "login_ok", "login_failed"} <= names
    assert "Secret-Guess-12345" not in str(events) and "inv_" not in str(events)
    assert web.client("staff").get("/security/events").status_code == 403


def test_secrets_are_stored_hashed(web):
    c = web.client("staff")
    row = None
    with web.pool.connection() as conn:
        row = conn.execute("SELECT u.password_hash, s.token_hash FROM users u JOIN sessions s ON s.user_id = u.id "
                           "WHERE u.id = %s", (c.user["id"],)).fetchone()
    assert row["password_hash"].startswith("scrypt$") and c.user["password"] not in row["password_hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", row["token_hash"]) and c.token not in row["token_hash"]
