"""Rate limiting (needs TEST_DATABASE_URL). Each test starts its own app, so limits never leak between tests."""
from tests.conftest import WebHarness, make_settings, make_user, running_app
from tests.test_api import make_product


def test_repeated_logins_are_throttled_even_with_the_right_password(db_url, pool):
    with running_app(db_url, settings=make_settings(rate_login="3/minute")) as app:
        web = WebHarness(app, pool)
        user = make_user(pool, "staff")
        anon = web.anon()
        bad = {"email": user["email"], "password": "wrong-password-123"}
        assert [anon.post("/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 401]

        r = anon.post("/auth/login", json={"email": user["email"], "password": user["password"]})
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
        error = r.json()["error"]
        assert error["code"] == "RATE_LIMITED" and error["details"][0]["retry_after_seconds"] >= 1


def test_write_and_read_limits_are_separate_and_per_user(db_url, pool):
    with running_app(db_url, settings=make_settings(rate_write="5/minute", rate_read="8/minute")) as app:
        web = WebHarness(app, pool)
        alice, bob = web.client("staff"), web.client("staff")
        codes = [alice.post("/products", json={"sku": f"A-{i}", "name": "x", "price": "1.00"}).status_code
                 for i in range(6)]
        assert codes == [201] * 5 + [429]
        assert alice.get("/products").status_code == 200                        # reading has its own allowance
        assert bob.post("/products", json={"sku": "B-1", "name": "x", "price": "1.00"}).status_code == 201

        reads = [alice.get("/products").status_code for _ in range(8)]          # 1 read already used above
        assert reads[:7] == [200] * 7 and reads[7] == 429
        assert bob.get("/products").status_code == 200                           # Bob is unaffected by Alice


def test_refused_requests_do_not_extend_the_wait(db_url, pool):
    with running_app(db_url, settings=make_settings(rate_write="2/minute")) as app:
        alice = WebHarness(app, pool).client("staff")
        for i in range(2):
            assert alice.post("/products", json={"sku": f"S-{i}", "name": "x", "price": "1.00"}).status_code == 201
        waits = [int(alice.post("/products", json={"sku": "S-9", "name": "x", "price": "1.00"}).headers["retry-after"])
                 for _ in range(5)]
        assert max(waits) <= 60 and min(waits) >= 1


def test_the_per_address_limit_protects_public_endpoints_too(db_url, pool):
    with running_app(db_url, settings=make_settings(rate_ip="5/minute")) as app:
        anon = WebHarness(app, pool).anon()
        assert [anon.get("/health").status_code for _ in range(5)] == [200] * 5
        r = anon.get("/health")
        assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"


def test_a_forged_forwarded_header_cannot_dodge_the_limit_by_default(db_url, pool):
    with running_app(db_url, settings=make_settings(rate_ip="3/minute")) as app:
        anon = WebHarness(app, pool).anon()
        codes = [anon.get("/health", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(5)]
        assert codes == [200, 200, 200, 429, 429]


def test_forwarded_header_is_honoured_only_when_explicitly_trusted(db_url, pool):
    with running_app(db_url, settings=make_settings(rate_ip="3/minute", trust_proxy_headers=True)) as app:
        anon = WebHarness(app, pool).anon()
        different = [anon.get("/health", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(6)]
        assert different == [200] * 6                                            # each real client has its own bucket
        same = [anon.get("/health", headers={"X-Forwarded-For": "10.9.9.9, 10.0.0.1"}).status_code for _ in range(4)]
        assert same == [200, 200, 200, 429]
