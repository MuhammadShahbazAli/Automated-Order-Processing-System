"""Safe-configuration checks. No database needed."""
import pytest

from app.settings import ConfigError, Settings, mask_url

STRONG_URL = "postgresql://inventory:Xk9-long-random-secret@db.internal:5432/inventory"
WEAK_URL = "postgresql://inventory:inventory@127.0.0.1:5432/inventory"


def test_defaults_are_sensible_for_development():
    s = Settings.from_env({})
    assert s.app_env == "development" and s.docs_enabled and s.log_format == "text" and not s.cors_origins
    errors, warnings = s.problems(STRONG_URL)
    assert errors == [] and warnings == []


def test_production_defaults_hide_docs_and_use_json_logs():
    s = Settings.from_env({"APP_ENV": "production"})
    assert s.is_production and not s.docs_enabled and s.log_format == "json"


def test_weak_database_password_is_a_warning_in_development_and_an_error_in_production():
    errors, warnings = Settings.from_env({}).problems(WEAK_URL)
    assert errors == [] and any("guessable" in w for w in warnings)
    errors, _ = Settings.from_env({"APP_ENV": "production"}).problems(WEAK_URL)
    assert any("guessable" in e for e in errors)
    errors, _ = Settings.from_env({"APP_ENV": "production"}).problems(STRONG_URL)
    assert errors == []


@pytest.mark.parametrize("url", ["postgresql://u@host/db", "postgresql://u:postgres@host/db",
                                 "postgresql://same:same@host/db"])
def test_other_weak_urls_are_caught(url):
    errors, _ = Settings.from_env({"APP_ENV": "production"}).problems(url)
    assert errors


def test_wildcard_cors_is_refused_in_production_only():
    assert Settings.from_env({"CORS_ORIGINS": "*"}).problems(STRONG_URL)[0] == []
    errors, _ = Settings.from_env({"APP_ENV": "production", "CORS_ORIGINS": "*"}).problems(STRONG_URL)
    assert any("CORS" in e for e in errors)


def test_cors_origins_must_look_like_origins():
    errors, _ = Settings.from_env({"CORS_ORIGINS": "app.example.com"}).problems(STRONG_URL)
    assert any("http://" in e for e in errors)
    s = Settings.from_env({"CORS_ORIGINS": "https://a.example.com, https://b.example.com"})
    assert s.cors_origins == ("https://a.example.com", "https://b.example.com")


def test_production_warnings():
    _, warnings = Settings.from_env({"APP_ENV": "production", "DOCS_ENABLED": "1"}).problems(STRONG_URL, "file")
    text = " ".join(warnings)
    assert "docs are enabled" in text and "EMAIL_BACKEND=file" in text and "HSTS" in text


def test_bad_values_are_reported_all_at_once():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({"APP_ENV": "staging", "TOKEN_TTL_HOURS": "soon", "DOCS_ENABLED": "maybe",
                           "DB_POOL_TIMEOUT": "fast"})
    message = str(exc.value)
    for expected in ("APP_ENV", "TOKEN_TTL_HOURS", "DOCS_ENABLED", "DB_POOL_TIMEOUT"):
        assert expected in message


def test_semantic_problems():
    s = Settings.from_env({"RATE_LIMIT_WRITE": "lots", "TOKEN_TTL_HOURS": "0", "LOG_LEVEL": "LOUD",
                           "MAX_BODY_BYTES": "10"})
    errors, _ = s.problems(STRONG_URL)
    text = " ".join(errors)
    for expected in ("RATE_LIMIT_WRITE", "TOKEN_TTL_HOURS", "LOG_LEVEL", "MAX_BODY_BYTES"):
        assert expected in text


def test_mask_url_hides_only_the_password():
    assert mask_url(STRONG_URL) == "postgresql://inventory:***@db.internal:5432/inventory"
    assert mask_url("postgresql://host/db") == "postgresql://host/db"
    assert "Xk9" not in str(Settings().summary(STRONG_URL, "file"))
