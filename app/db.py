"""Connection pool. Connections are in autocommit mode: reads run immediately, and every
write path wraps its work in an explicit `with conn.transaction():` block (real BEGIN/COMMIT)."""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import POOL_MAX, POOL_MIN


def create_pool(url: str, min_size: int = POOL_MIN, max_size: int = POOL_MAX) -> ConnectionPool:
    return ConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        timeout=10,                                   # wait for a free connection, then fail fast
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,                                   # opened in the app lifespan
    )
