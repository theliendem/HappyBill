"""Postgres access. Read-only queries with a statement timeout; rows come back as dicts."""
import atexit
import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/claritybill")

_pool = None


def pool():
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL, min_size=1, max_size=8, open=True,
            kwargs={"row_factory": dict_row, "options": "-c statement_timeout=10000 -c default_transaction_read_only=on"},
        )
        atexit.register(_pool.close)
    return _pool


def query(sql, params=None):
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None
