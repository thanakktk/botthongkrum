"""
Database access helper
======================================================================
Single place to get a PostgreSQL connection from the .env config.
Trading DECISIONS never live in SQL — this only provides connectivity
for state persistence, audit writes, and read-side aggregations.
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def dsn() -> str:
    """Prefer an explicit DATABASE_URL; otherwise assemble from PG* parts."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("PGUSER", "postgres")
    pwd = os.getenv("PGPASSWORD", "")
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("PGDATABASE", "postgres")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(dsn(), autocommit=autocommit)
