"""Connection pool. Connections are in autocommit mode: reads run immediately, and every
write path wraps its work in an explicit `with conn.transaction():` block (real BEGIN/COMMIT)."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import POOL_MAX, POOL_MIN


def create_pool(url: str, min_size: int = POOL_MIN, max_size: int = POOL_MAX, timeout: float = 10.0) -> ConnectionPool:
    options = {}
    check = getattr(ConnectionPool, "check_connection", None)   # psycopg_pool >= 3.2: drop dead connections silently
    if check is not None:
        options["check"] = check
    return ConnectionPool(
        url,
        min_size=min(min_size, max_size),
        max_size=max_size,
        timeout=timeout,                              # wait for a free connection, then fail fast (HTTP 503)
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,                                   # opened in the app lifespan
        **options,
    )
