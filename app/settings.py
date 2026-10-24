"""Safe configuration: everything is read from the environment, validated at startup, and never printed with secrets."""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .security import parse_rate

ENVIRONMENTS = ("development", "test", "production")
_WEAK_DB_PASSWORDS = {"", "inventory", "postgres", "password", "admin", "123456", "changeme", "secret"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """Raised at startup when the configuration is invalid or unsafe. The message lists every problem."""


def mask_url(url: str) -> str:
    """postgresql://user:secret@host/db -> postgresql://user:***@host/db (safe to log)."""
    try:
        parts = urlsplit(url)
        if parts.password is None:
            return url
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        return parts._replace(netloc=f"{parts.username or ''}:***@{host}{port}").geturl()
    except ValueError:
        return "<unparseable url>"


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    token_ttl_hours: int = 12
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    rate_ip: str = "1200/minute"          # per client address, all routes
    rate_read: str = "600/minute"         # per signed-in user, GET requests
    rate_write: str = "120/minute"        # per signed-in user, POST/PATCH/...
    rate_login: str = "10/minute"         # per client address, POST /auth/login
    cors_origins: tuple = ()
    docs_enabled: bool = True
    trust_proxy_headers: bool = False
    hsts: bool = False
    max_body_bytes: int = 1_000_000
    log_format: str = "text"
    log_level: str = "INFO"
    pool_timeout: float = 10.0
    security_event_retention_days: int = 180

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        errors: list[str] = []

        def text(name, default):
            return env.get(name, default).strip()

        def integer(name, default):
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return int(raw)
            except ValueError:
                errors.append(f"{name} must be a whole number (got '{raw}').")
                return default

        def number(name, default):
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return float(raw)
            except ValueError:
                errors.append(f"{name} must be a number (got '{raw}').")
                return default

        def boolean(name, default):
            raw = env.get(name)
            if raw is None:
                return default
            value = raw.strip().lower()
            if value in _TRUE:
                return True
            if value in _FALSE:
                return False
            errors.append(f"{name} must be true or false (got '{raw}').")
            return default

        app_env = text("APP_ENV", "development").lower()
        if app_env not in ENVIRONMENTS:
            errors.append(f"APP_ENV must be one of {', '.join(ENVIRONMENTS)} (got '{app_env}').")
        production = app_env == "production"
        settings = cls(
            app_env=app_env,
            token_ttl_hours=integer("TOKEN_TTL_HOURS", 12),
            max_failed_logins=integer("MAX_FAILED_LOGINS", 5),
            lockout_minutes=integer("LOCKOUT_MINUTES", 15),
            rate_ip=text("RATE_LIMIT_IP", "1200/minute"),
            rate_read=text("RATE_LIMIT_READ", "600/minute"),
            rate_write=text("RATE_LIMIT_WRITE", "120/minute"),
            rate_login=text("RATE_LIMIT_LOGIN", "10/minute"),
            cors_origins=tuple(o.strip() for o in env.get("CORS_ORIGINS", "").split(",") if o.strip()),
            docs_enabled=boolean("DOCS_ENABLED", not production),
            trust_proxy_headers=boolean("TRUST_PROXY_HEADERS", False),
            hsts=boolean("ENABLE_HSTS", False),
            max_body_bytes=integer("MAX_BODY_BYTES", 1_000_000),
            log_format=text("LOG_FORMAT", "json" if production else "text").lower(),
            log_level=text("LOG_LEVEL", "INFO").upper(),
            pool_timeout=number("DB_POOL_TIMEOUT", 10.0),
            security_event_retention_days=integer("SECURITY_EVENT_RETENTION_DAYS", 180),
        )
        if errors:
            raise ConfigError("Invalid configuration:\n - " + "\n - ".join(errors))
        return settings

    def problems(self, database_url: str, email_backend: str = "file") -> tuple[list[str], list[str]]:
        """Returns (errors, warnings). Errors stop the app from starting."""
        errors: list[str] = []
        warnings: list[str] = []

        for name in ("rate_ip", "rate_read", "rate_write", "rate_login"):
            try:
                parse_rate(getattr(self, name))
            except ValueError as exc:
                errors.append(f"RATE_LIMIT_{name[5:].upper()}: {exc}")
        if not 1 <= self.token_ttl_hours <= 720:
            errors.append("TOKEN_TTL_HOURS must be between 1 and 720.")
        if self.max_failed_logins < 1 or self.lockout_minutes < 1:
            errors.append("MAX_FAILED_LOGINS and LOCKOUT_MINUTES must be at least 1.")
        if self.max_body_bytes < 1024:
            errors.append("MAX_BODY_BYTES must be at least 1024.")
        if self.log_format not in ("json", "text"):
            errors.append("LOG_FORMAT must be 'json' or 'text'.")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            errors.append("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL.")
        if self.pool_timeout <= 0:
            errors.append("DB_POOL_TIMEOUT must be greater than 0.")
        for origin in self.cors_origins:
            if origin != "*" and not origin.startswith(("http://", "https://")):
                errors.append(f"CORS_ORIGINS entry '{origin}' must start with http:// or https://.")

        try:
            parts = urlsplit(database_url)
            weak = (parts.password or "").lower() in _WEAK_DB_PASSWORDS or parts.password == parts.username
        except ValueError:
            weak, parts = True, None
            errors.append("DATABASE_URL could not be parsed.")
        if weak:
            message = "DATABASE_URL uses a missing or guessable database password."
            (errors if self.is_production else warnings).append(
                message + (" Set a strong password before running in production." if self.is_production else ""))

        if self.is_production:
            if "*" in self.cors_origins:
                errors.append("CORS_ORIGINS='*' is not allowed in production; list the exact origins.")
            if self.docs_enabled:
                warnings.append("API docs are enabled in production (set DOCS_ENABLED=0 to hide them).")
            if email_backend == "file":
                warnings.append("EMAIL_BACKEND=file: emails are written to disk, not sent.")
            if not self.hsts:
                warnings.append("ENABLE_HSTS is off; turn it on when the API is served over HTTPS.")
        return errors, warnings

    def summary(self, database_url: str, email_backend: str) -> dict:
        """Safe to log: secrets are masked."""
        return {
            "env": self.app_env, "database": mask_url(database_url), "email_backend": email_backend,
            "docs_enabled": self.docs_enabled, "cors_origins": list(self.cors_origins),
            "rate_limits": {"ip": self.rate_ip, "read": self.rate_read, "write": self.rate_write,
                            "login": self.rate_login},
            "token_ttl_hours": self.token_ttl_hours, "trust_proxy_headers": self.trust_proxy_headers,
        }
