"""
Database bootstrap
======================================================================
Ensures the target database exists, then applies schema.sql (idempotent —
all DDL uses IF NOT EXISTS / CREATE OR REPLACE). Safe to re-run.

Run:  ./env/Scripts/python.exe db_setup.py
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql
from dotenv import load_dotenv

import db

load_dotenv()

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def _server_dsn() -> str:
    """DSN pointed at the 'postgres' maintenance DB (for CREATE DATABASE)."""
    user = os.getenv("PGUSER", "postgres")
    pwd = os.getenv("PGPASSWORD", "")
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    return f"postgresql://{user}:{pwd}@{host}:{port}/postgres"


def ensure_database() -> str:
    target = os.getenv("PGDATABASE", "FTMO")
    with psycopg.connect(_server_dsn(), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target,)
        ).fetchone()
        if exists:
            print(f"[ok] database {target!r} already exists")
        else:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
            print(f"[ok] created database {target!r}")
    return target


def seed_account_profile() -> None:
    """Upsert the single active account (id=1) from .env. The Compliance Engine
    derives all its rules from this profile."""
    profile = {
        "login": int(os.getenv("MT5_LOGIN", "0")),
        "variant": os.getenv("ACCOUNT_VARIANT", "standard"),
        "path": os.getenv("ACCOUNT_PATH", "2-step"),
        "phase": os.getenv("ACCOUNT_PHASE", "challenge"),
        "initial_capital": float(os.getenv("ACCOUNT_INITIAL_CAPITAL", "100000")),
    }
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO account_profile
                (id, login, variant, path, phase, initial_capital, updated_at)
            VALUES (1, %(login)s, %(variant)s, %(path)s, %(phase)s,
                    %(initial_capital)s, now())
            ON CONFLICT (id) DO UPDATE SET
                login           = EXCLUDED.login,
                variant         = EXCLUDED.variant,
                path            = EXCLUDED.path,
                phase           = EXCLUDED.phase,
                initial_capital = EXCLUDED.initial_capital,
                updated_at      = now()
            """,
            profile,
        )
        conn.commit()
    print(f"[ok] account_profile seeded: login={profile['login']} "
          f"{profile['variant']}/{profile['path']}/{profile['phase']} "
          f"cap={profile['initial_capital']:,.0f}")


def apply_schema() -> None:
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    with db.connect() as conn:
        conn.execute(ddl)
        conn.commit()
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        ).fetchall()
        procs = conn.execute(
            "SELECT routine_name, routine_type FROM information_schema.routines "
            "WHERE routine_schema = 'public' ORDER BY routine_name"
        ).fetchall()
    print(f"[ok] schema applied. Tables: {[r[0] for r in rows]}")
    print(f"[ok] routines: {[f'{r[0]}({r[1].lower()})' for r in procs]}")


if __name__ == "__main__":
    ensure_database()
    apply_schema()
    seed_account_profile()
    print("\n[done] database is ready.")
