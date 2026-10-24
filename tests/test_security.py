"""Password hashing, tokens, rate limiter and permissions. No database needed."""
import threading

import pytest

from app import security
from app.security import (PERMISSIONS, RateLimiter, burn_password_check, has_permission, hash_password,
                          hash_token, new_token, parse_rate, validate_password, verify_password)

PASSWORD = "Correct-Horse-Battery-9"


# ---------------- passwords
def test_hash_verifies_the_right_password_only():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + "x", stored)
    assert not verify_password("", stored)


def test_every_hash_has_its_own_salt():
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$1$2$3", "bcrypt$a$b$c$d$e",
                                    "scrypt$16384$8$1$!!!$!!!", "scrypt$99999999$8$1$AAAA$AAAA", None])
def test_damaged_or_foreign_hashes_fail_quietly(stored):
    assert verify_password(PASSWORD, stored) is False


def test_oversized_and_odd_passwords_are_refused_cheaply():
    stored = hash_password(PASSWORD)
    assert verify_password("x" * 100_000, stored) is False
    assert verify_password("\ud800", stored) is False            # lone surrogate cannot be encoded
    with pytest.raises(ValueError):
        hash_password("x" * 129)


def test_unicode_passwords_work():
    stored = hash_password("пароль-Ünïcode-密码-123")
    assert verify_password("пароль-Ünïcode-密码-123", stored)


def test_burn_check_never_raises():
    burn_password_check("whatever")


@pytest.mark.parametrize("password,fragment", [
    ("short1", "at least 10"), ("1234567890", "too common"), ("aaaaaaaaaaaa", "repetitive"),
    ("x" * 129, "at most 128")])
def test_weak_passwords_are_rejected_with_a_reason(password, fragment):
    assert any(fragment in problem for problem in validate_password(password))


def test_password_must_not_contain_the_email_name():
    assert validate_password("Sara.Ali-2026!!", "sara.ali@example.com")
    assert not validate_password("Correct-Horse-Battery-9", "sara.ali@example.com")
    assert not validate_password("Correct-Horse-Battery-9")


# ---------------- tokens
def test_tokens_are_random_prefixed_and_only_the_hash_is_derived():
    token, stored = new_token()
    other, _ = new_token()
    assert token.startswith("inv_") and len(token) > 40 and token != other
    assert stored == hash_token(token) and token not in stored and len(stored) == 64


# ---------------- permissions
def test_roles_only_ever_gain_permissions():
    assert PERMISSIONS["viewer"] < PERMISSIONS["staff"] < PERMISSIONS["admin"]


def test_viewers_cannot_write_and_only_admins_manage_users():
    assert not any(p.endswith(":write") or p.endswith(":manage") for p in PERMISSIONS["viewer"])
    assert has_permission("staff", "orders:write") and not has_permission("staff", "users:manage")
    assert has_permission("admin", "users:manage")
    assert not has_permission("nobody", "orders:read") and not has_permission("viewer", "made:up")


# ---------------- rate limiter
class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_parse_rate():
    assert parse_rate("120/minute") == (120, 60.0)
    assert parse_rate("5 / second") == (5, 1.0)
    assert parse_rate("10/hours") == (10, 3600.0)
    for bad in ("", "abc", "0/minute", "10/day", "-1/minute", "10 per minute"):
        with pytest.raises(ValueError):
            parse_rate(bad)


def test_limit_is_enforced_and_refusals_are_not_counted():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    assert [limiter.check("k", 3, 60)[0] for _ in range(3)] == [True, True, True]
    allowed, retry = limiter.check("k", 3, 60)
    assert allowed is False and retry == 60
    clock.now += 59
    assert limiter.check("k", 3, 60) == (False, 1)               # the window has not moved on yet
    clock.now += 2
    assert limiter.check("k", 3, 60)[0] is True                  # the oldest three have expired


def test_keys_are_independent():
    limiter = RateLimiter(clock=FakeClock())
    assert limiter.check("a", 1, 60)[0] and not limiter.check("a", 1, 60)[0]
    assert limiter.check("b", 1, 60)[0]


def test_window_slides_instead_of_resetting_all_at_once():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    limiter.check("k", 2, 10)
    clock.now += 6
    limiter.check("k", 2, 10)
    clock.now += 5                                               # first event is 11s old, second is 5s old
    assert limiter.check("k", 2, 10)[0] is True
    assert limiter.check("k", 2, 10)[0] is False


def test_concurrent_callers_never_exceed_the_limit():
    limiter = RateLimiter()
    results, lock = [], threading.Lock()
    barrier = threading.Barrier(64)

    def hit():
        barrier.wait()
        allowed = limiter.check("shared", 20, 60)[0]
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=hit) for _ in range(64)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 20 and results.count(False) == 44


def test_memory_stays_bounded_when_keys_explode():
    limiter = RateLimiter(clock=FakeClock(), max_keys=100)
    for i in range(1000):
        limiter.check(f"attacker-{i}", 5, 60)
    assert len(limiter._events) <= 100
