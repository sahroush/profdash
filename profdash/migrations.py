"""Idempotent, additive migrations for existing profdash databases.

Fresh databases get everything from schema.create_all(); these steps only
matter for databases created by earlier profdash versions (or imported
from other layouts). Each step checks whether it is needed before writing.
"""
from __future__ import annotations

import re
import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _legacy_status_rename(conn: sqlite3.Connection) -> None:
    """'new' was renamed to 'not_contacted'; keep both readable."""
    row = conn.execute(
        "SELECT COUNT(*) FROM professors WHERE status = 'new'").fetchone()
    if row[0]:
        conn.execute("UPDATE professors SET status = 'not_contacted' WHERE status = 'new'")


def _normalize_rec_dashes(conn: sqlite3.Connection) -> None:
    """Recommendation vocabulary uses ' -- ' suffix separators; some rows
    ended up with em/en dashes. Normalize so badge rendering splits right."""
    for col in ("phd_recommendation", "masters_recommendation", "recommendation"):
        if not _column_exists(conn, "professors" if col != "recommendation" else "positions", col):
            continue
        table = "professors" if col != "recommendation" else "positions"
        conn.execute(
            f"""UPDATE {table} SET {col} = REPLACE(REPLACE(REPLACE({col},
                '\u2014', ' -- '), '\u2013', ' -- '), '  ', ' ')
                WHERE {col} LIKE '%\u2014%' OR {col} LIKE '%\u2013%'"""
        )


def _add_area_columns(conn: sqlite3.Connection) -> None:
    """Research-area columns/tables arrived after v0.1.0."""
    from .schema import DDL, ensure_columns
    for stmt in DDL:
        if "professor_areas" in stmt:
            conn.execute(stmt)
    ensure_columns(conn)


STEPS = [
    ("legacy-status-rename", _legacy_status_rename),
    ("normalize-rec-dashes", _normalize_rec_dashes),
    ("add-area-columns", _add_area_columns),
]


def run_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply all pending steps. Returns names of steps actually applied."""
    applied = []
    for name, fn in STEPS:
        try:
            fn(conn)
            applied.append(name)
        except sqlite3.OperationalError:
            pass  # table missing on a fresh DB — nothing to migrate
    conn.commit()
    return applied
