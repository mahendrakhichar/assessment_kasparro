"""
db.py — asyncpg database connection pool and helpers.
"""
from __future__ import annotations

import json
import os
from typing import AsyncGenerator

import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["DATABASE_URL"],
            min_size=1,
            max_size=10,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Apply schema.sql to the connected database (idempotent)."""
    import pathlib
    sql = (pathlib.Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
