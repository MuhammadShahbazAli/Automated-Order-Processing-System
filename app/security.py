"""Security primitives (standard library only): password hashing, session tokens, rate limiting, permissions."""
import base64
import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections import deque

# ---------------------------------------------------------------- passwords
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32          # scrypt cost: ~16 MiB and ~50-100 ms per hash
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128                        # also stops "hash a 10 MB password" denial of service

_COMMON_PASSWORDS = {
    "1234567890", "0123456789", "qwertyuiop", "password123", "password1234", "passw0rd123", "letmein1234",
    "welcome1234", "administrator", "1q2w3e4r5t", "abcdefghij", "inventory123", "inventory1234",
    "changeme123", "iloveyou123", "admin12345", "admin123456", "qwerty12345", "11111111111", "password12",
}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """Salted scrypt hash, stored as 'scrypt$n$r$p$salt$hash'."""
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("password too long")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. Never raises: a damaged or foreign hash simply fails."""
    try:
        if not isinstance(password, str) or not isinstance(stored, str) or len(password) > MAX_PASSWORD_LENGTH:
            return False
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt" or (int(n), int(r), int(p)) != (_N, _R, _P):   # refuse unexpected (costly) parameters
            return False
        expected_bytes = base64.b64decode(expected, validate=True)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt, validate=True),
                                n=_N, r=_R, p=_P, dklen=len(expected_bytes))
        return hmac.compare_digest(digest, expected_bytes)
    except (ValueError, TypeError, UnicodeError):
        return False


_dummy_hash: str | None = None


def burn_password_check(password: str) -> None:
    """Do the same amount of work as a real check, so 'unknown account' and 'wrong password' take equally long."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("not-a-real-password")
    verify_password(password, _dummy_hash)


def validate_password(password: str, email: str | None = None) -> list[str]:
    """Returns a list of problems (empty = acceptable)."""
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"Use at most {MAX_PASSWORD_LENGTH} characters.")
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS:
        problems.append("This password is too common.")
    if len(set(password)) < 4:
        problems.append("This password is too repetitive.")
    if email:
        name = email.split("@")[0].lower()
        if lowered == email.lower() or (len(name) >= 4 and name in lowered):
            problems.append("The password must not contain your email name.")
    return problems


# ---------------------------------------------------------------- session tokens
TOKEN_PREFIX = "inv_"      # recognisable by secret scanners if a token ever leaks into a repository


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """Returns (token to give to the client, hash to store). Only the hash is ever saved."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, hash_token(token)


# ---------------------------------------------------------------- roles and permissions
ROLES = ("viewer", "staff", "admin")
_VIEWER = {"products:read", "stock:read", "orders:read"}
_STAFF = _VIEWER | {"products:write", "stock:write", "orders:write"}
_ADMIN = _STAFF | {"reports:read", "ops:read", "ops:write", "notifications:manage", "audit:read", "users:manage"}
PERMISSIONS = {"viewer": frozenset(_VIEWER), "staff": frozenset(_STAFF), "admin": frozenset(_ADMIN)}


def has_permission(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, frozenset())


# ---------------------------------------------------------------- rate limiting
_RATE_RE = re.compile(r"^(\d+)\s*/\s*(second|sec|minute|min|hour)s?$", re.IGNORECASE)
_UNITS = {"second": 1.0, "sec": 1.0, "minute": 60.0, "min": 60.0, "hour": 3600.0}


def parse_rate(text: str) -> tuple[int, float]:
    """'120/minute' -> (120, 60.0)."""
    match = _RATE_RE.match(text.strip())
    if not match or int(match.group(1)) < 1:
        raise ValueError(f"Invalid rate '{text}'. Use a form like '60/minute' (units: second, minute, hour).")
    return int(match.group(1)), _UNITS[match.group(2).lower()]


class RateLimiter:
    """Sliding-window limiter, thread-safe. State is per process: with several server processes each
    keeps its own counts (use a shared store such as Redis if you need exact global limits)."""

    def __init__(self, clock=time.monotonic, max_keys: int = 50_000):
        self._clock, self._max_keys = clock, max_keys
        self._events: dict[str, deque] = {}
        self._windows: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds). A refused call is NOT counted."""
        now = self._clock()
        with self._lock:
            events = self._events.get(key)
            if events is None:
                if len(self._events) >= self._max_keys:
                    self._purge(now)
                events = self._events[key] = deque()
            self._windows[key] = window
            cutoff = now - window
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False, max(1, math.ceil(events[0] + window - now))
            events.append(now)
            return True, 0

    def _purge(self, now: float) -> None:
        for key in [k for k, q in self._events.items() if not q or q[-1] <= now - self._windows.get(k, 0)]:
            self._events.pop(key, None)
            self._windows.pop(key, None)
        if len(self._events) >= self._max_keys:                 # still full: drop the oldest tenth
            for key in list(self._events)[: self._max_keys // 10]:
                self._events.pop(key, None)
                self._windows.pop(key, None)
