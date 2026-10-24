"""Headers, request ids, CORS, docs switch, body limit, structured logs (needs TEST_DATABASE_URL)."""
import json
import re

from tests.conftest import WebHarness, make_settings, make_user, running_app


def test_security_headers_on_every_response(web):
    for r in (web.anon().get("/health"), web.anon().get("/products"), web.anon().get("/no-such-route")):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert "strict-transport-security" not in r.headers                    # only when ENABLE_HSTS is on


def test_hsts_is_added_when_enabled(db_url, pool):
    with running_app(db_url, settings=make_settings(hsts=True)) as app:
        r = WebHarness(app, pool).anon().get("/health")
        assert "max-age=" in r.headers["strict-transport-security"]


def test_request_ids_are_generated_echoed_and_shown_in_errors(web):
    anon = web.anon()
    generated = anon.get("/health").headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    assert anon.get("/health", headers={"X-Request-ID": "my-trace-id-123"}).headers["x-request-id"] == "my-trace-id-123"
    hostile = anon.get("/health", headers={"X-Request-ID": "bad id\twith spaces & <tags>"})
    assert re.fullmatch(r"[0-9a-f]{32}", hostile.headers["x-request-id"])       # not trusted, replaced

    r = anon.get("/products", headers={"X-Request-ID": "trace-for-support-1"})
    assert r.status_code == 401 and r.json()["error"]["request_id"] == "trace-for-support-1"
    missing = anon.get("/nope")
    assert missing.status_code == 404 and missing.json()["error"]["request_id"] == missing.headers["x-request-id"]


def test_errors_never_expose_internals(web):
    r = web.anon().get("/products/not-a-number", headers={"Authorization": "Bearer whatever"})
    text = r.text.lower()
    for leak in ("traceback", "psycopg", "select ", "site-packages", "line "):
        assert leak not in text


def test_docs_are_available_in_development_and_describe_security(web):
    anon = web.anon()
    assert anon.get("/docs").status_code == 200
    spec = anon.get("/openapi.json").json()
    assert spec["info"]["version"].startswith("5.")
    assert "HTTPBearer" in spec["components"]["securitySchemes"]
    assert {t["name"] for t in spec["tags"]} >= {"Auth", "Orders", "Products", "Users", "Operations"}
    assert "Idempotency-Key" in json.dumps(spec["paths"]["/orders"]["post"])
    login = spec["paths"]["/auth/login"]["post"]
    assert "security" not in login                                               # login itself is public
    assert spec["paths"]["/orders"]["post"]["security"]


def test_docs_can_be_switched_off(db_url, pool):
    with running_app(db_url, settings=make_settings(docs_enabled=False)) as app:
        anon = WebHarness(app, pool).anon()
        assert [anon.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")] == [404, 404, 404]
        assert anon.get("/health").status_code == 200


def test_cors_is_closed_unless_origins_are_listed(web):
    r = web.anon().get("/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_only_listed_origins(db_url, pool):
    with running_app(db_url, settings=make_settings(cors_origins=("https://app.example.com",))) as app:
        anon = WebHarness(app, pool).anon()
        ask = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,idempotency-key"}
        good = anon.options("/orders", headers={"Origin": "https://app.example.com", **ask})
        assert good.status_code == 200 and good.headers["access-control-allow-origin"] == "https://app.example.com"
        bad = anon.options("/orders", headers={"Origin": "https://evil.example.com", **ask})
        assert "access-control-allow-origin" not in bad.headers


def test_oversized_bodies_are_refused_early(db_url, pool):
    with running_app(db_url, settings=make_settings(max_body_bytes=2048)) as app:
        anon = WebHarness(app, pool).anon()
        r = anon.post("/auth/login", json={"email": "a@example.com", "password": "x" * 5000})
        assert r.status_code == 413 and r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert anon.post("/auth/login", json={"email": "a@example.com", "password": "x"}).status_code == 401


def test_access_log_is_structured_and_free_of_secrets(db_url, pool, capsys):
    settings = make_settings(log_format="json", log_level="INFO")
    with running_app(db_url, settings=settings) as app:
        web = WebHarness(app, pool)
        user = make_user(pool, "staff")
        web.anon().post("/auth/login", json={"email": user["email"], "password": "My-Secret-Pass-123"})
        token = web.anon().post("/auth/login", json={"email": user["email"], "password": user["password"]}
                                ).json()["access_token"]
        web.anon().get("/products", headers={"Authorization": f"Bearer {token}", "X-Request-ID": "log-trace-0001"})
    err = capsys.readouterr().err
    lines = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    access = [l for l in lines if l.get("event") == "http_request"]
    assert access, "no structured access lines were written"
    products = next(l for l in access if l["path"] == "/products")
    assert products["method"] == "GET" and products["status"] == 200 and products["request_id"] == "log-trace-0001"
    assert products["user_id"] == user["id"] and products["duration_ms"] >= 0
    failed = next(l for l in access if l["path"] == "/auth/login" and l["status"] == 401)
    assert failed["user_id"] is None
    for secret in ("My-Secret-Pass-123", user["password"], token):
        assert secret not in err
