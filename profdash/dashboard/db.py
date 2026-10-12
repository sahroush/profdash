from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from .. import areas, paths


# Status taxonomy from PLAN.md
STATUS_TAXONOMY = [
    "not_contacted",
    "contacted",
    "no_response",
    "replied_template",
    "replied_interested",
    "replied_no_funding",
    "replied_rejected",
    "conditional_accept",
    "needs_follow_up",
    "withdrawn",
]


def db_path() -> Path:
    """Resolve the active SQLite file via profdash.paths (env/cwd conventions)."""
    return paths.find_db()


# Columns displayed in the table view (Prompt 1). Centralized here so
# route templates and the SELECT stay in sync.
TABLE_COLUMNS: list[str] = [
    "professor",
    "university",
    "location_country",
    "area",
    "fit_tier",
    "phd_recommendation",
    "final_score_phd",
    "masters_recommendation",
    "final_score_masters",
    "status",
]

# Short display names for table headers (fallback: column name title-cased)
COLUMN_LABELS: dict[str, str] = {
    "professor": "Name",
    "university": "University",
    "location_country": "Country",
    "area": "Area",
    "area_group": "Group",
    "fit_tier": "Tier",
    "phd_recommendation": "PhD Rec",
    "final_score_phd": "PhD Score",
    "masters_recommendation": "MS Rec",
    "final_score_masters": "MS Score",
    "status": "Status",
    "csrankings_rank": "CS Rank",
    "id": "ID",
    "stage": "Stage",
}

# Columns that may be sorted. Everything in TABLE_COLUMNS plus the implicit
# ID for stable ordering on ties.
_SORTABLE_COLUMNS = set(TABLE_COLUMNS) | {"id"}


def _order_by(sort: str, direction: str) -> str:
    """ORDER BY clause. Sorting by area orders by group first (AI, Systems,
    Theory, Interdisciplinary) then area; professors with no area go last."""
    if sort == "area":
        group_rank = ("CASE area_group "
                      + " ".join(f"WHEN '{g}' THEN {i}" for i, g in enumerate(areas.GROUPS))
                      + " ELSE 99 END")
        return (f"area IS NULL, {group_rank} {direction}, area {direction}, "
                f"professor ASC")
    return f"{sort} {direction}"

# Columns that can be filtered. These have discrete values worth filtering on.
_FILTERABLE_COLUMNS: list[str] = [
    "phd_recommendation",
    "masters_recommendation",
    "location_country",
    "area_group",
    "area",
    "status",
    "fit_tier",
    "university",
    "stage",
]


def connect() -> sqlite3.Connection:
    """Open a connection with WAL + short-write-friendly PRAGMAs."""
    p = db_path()
    if not p.exists():
        raise FileNotFoundError(
            f"Database not found at {p}. "
            f"Run `prof init` or pass --db / set PROF_DB_PATH."
        )
    conn = sqlite3.connect(
        str(p),
        timeout=10.0,            # wait up to 10s if another process holds the lock
        isolation_level=None,    # autocommit; transactions are explicit via BEGIN
        check_same_thread=False, # FastAPI workers may hand off across threads
    )
    # WAL is persistent (WAL mode is stored in the DB header). Re-asserting
    # it on every connect is cheap and idempotent.
    conn.execute("PRAGMA journal_mode=WAL;")
    # Read-only queries benefit from query planner stats; harmless for writes.
    conn.execute("PRAGMA optimize;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit BEGIN/COMMIT/ROLLBACK wrapper. Used for any future writes."""
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def retry_once_on_lock(fn, *args, **kwargs):
    """If sqlite raises 'database is locked' (busy_timeout exhausted), retry once.

    Cron workers may hold a brief write transaction; we don't want
    to surface a 500 just because we overlapped them by a few ms.
    """
    try:
        return fn(*args, **kwargs)
    except sqlite3.OperationalError as e:
        if "locked" not in str(e).lower():
            raise
        return fn(*args, **kwargs)


# --- Read queries -----------------------------------------------------------


def _normalize_university(name: str) -> str:
    """Normalize university name for grouping: lowercase, strip punctuation/whitespace."""
    if not name:
        return ""
    # Lowercase, remove punctuation, collapse whitespace
    normalized = name.lower()
    normalized = re.sub(r"[^\w\s]", "", normalized)  # remove punctuation
    normalized = re.sub(r"\s+", " ", normalized).strip()  # collapse whitespace
    return normalized


def count_professors() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM professors").fetchone()[0]


def count_professors_filtered(search: str = "", filters: dict[str, str] | None = None,
                              include_hidden: bool = False) -> int:
    """Count professors matching search and filters."""
    filters = filters or {}
    where_clauses = []
    params = []

    if search:
        where_clauses.append("(professors.professor LIKE ? OR professors.university LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    if hide_cond:
        where_clauses.append(hide_cond)
        params.extend(hide_params)

    for col, val in filters.items():
        if col in _FILTERABLE_COLUMNS:
            where_clauses.append(f"professors.{col} = ?")
            params.append(val)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM professors {where_sql}", params
        ).fetchone()[0]


def get_filter_options() -> dict[str, list[str]]:
    """Return distinct values for each filterable column (for dynamic selects)."""
    with connect() as conn:
        options = {}
        for col in _FILTERABLE_COLUMNS:
            rows = conn.execute(
                f"SELECT DISTINCT {col} FROM professors WHERE {col} IS NOT NULL AND {col} != '' ORDER BY {col}"
            ).fetchall()
            options[col] = [r[0] for r in rows]
        return options


def get_stats(search: str = "", filters: dict[str, str] | None = None,
              include_hidden: bool = False) -> dict[str, Any]:
    """Return summary counts for the stats bar."""
    filters = filters or {}
    where_clauses = []
    params = []

    if search:
        where_clauses.append("(professor LIKE ? OR university LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    if hide_cond:
        where_clauses.append(hide_cond)
        params.extend(hide_params)

    for col, val in filters.items():
        if col in _FILTERABLE_COLUMNS:
            where_clauses.append(f"{col} = ?")
            params.append(val)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with connect() as conn:
        # Total count
        total = conn.execute(
            f"SELECT COUNT(*) FROM professors {where_sql}", params
        ).fetchone()[0]

        # Breakdown by status
        status_rows = conn.execute(
            f"SELECT status, COUNT(*) FROM professors {where_sql} "
            f"GROUP BY status ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()
        status_breakdown = {r[0]: r[1] for r in status_rows}

        # Breakdown by fit_tier
        fit_rows = conn.execute(
            f"SELECT fit_tier, COUNT(*) FROM professors {where_sql} "
            f"GROUP BY fit_tier ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()
        fit_breakdown = {r[0] if r[0] else "unrated": r[1] for r in fit_rows}

        # Breakdown by phd_recommendation
        phd_rows = conn.execute(
            f"SELECT phd_recommendation, COUNT(*) FROM professors {where_sql} "
            f"GROUP BY phd_recommendation ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()
        phd_breakdown = {r[0] if r[0] else "unrated": r[1] for r in phd_rows}

        # Breakdown by masters_recommendation
        ms_rows = conn.execute(
            f"SELECT masters_recommendation, COUNT(*) FROM professors {where_sql} "
            f"GROUP BY masters_recommendation ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()
        ms_breakdown = {r[0] if r[0] else "unrated": r[1] for r in ms_rows}

        # Count of rows with non-null final_score_phd
        scored_phd = conn.execute(
            f"SELECT COUNT(*) FROM professors {where_sql} {'AND' if where_sql else 'WHERE'} final_score_phd IS NOT NULL",
            params,
        ).fetchone()[0]

        # Count of rows with non-null final_score_masters
        scored_masters = conn.execute(
            f"SELECT COUNT(*) FROM professors {where_sql} {'AND' if where_sql else 'WHERE'} final_score_masters IS NOT NULL",
            params,
        ).fetchone()[0]

        contacted = conn.execute(
            f"SELECT COUNT(*) FROM professors {where_sql} {'AND' if where_sql else 'WHERE'} status NOT IN ('not_contacted', 'new')",
            params,
        ).fetchone()[0]

        # Breakdown by research area, ordered like areas.AREAS (group, then area)
        area_rows = conn.execute(
            f"SELECT area, area_group, COUNT(*) FROM professors {where_sql} "
            f"{'AND' if where_sql else 'WHERE'} area IS NOT NULL GROUP BY area, area_group",
            params,
        ).fetchall()
        order = {a.title: i for i, a in enumerate(areas.AREAS)}
        area_breakdown = [
            {"area": r[0], "group": r[1], "count": r[2]}
            for r in sorted(area_rows, key=lambda r: order.get(r[0], 999))
        ]

    return {
        "total": total,
        "status_breakdown": status_breakdown,
        "fit_breakdown": fit_breakdown,
        "phd_breakdown": phd_breakdown,
        "ms_breakdown": ms_breakdown,
        "scored_phd": scored_phd,
        "scored_masters": scored_masters,
        "contacted": contacted,
        "not_contacted": total - contacted,
        "area_breakdown": area_breakdown,
    }


def list_professors(
    sort: str = "id",
    direction: str = "asc",
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    filters: dict[str, str] | None = None,
    include_hidden: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_count) for the paginated table."""
    filters = filters or {}

    # Validate sort column
    if sort not in _SORTABLE_COLUMNS:
        sort = "id"
    direction = "DESC" if direction.lower() == "desc" else "ASC"

    where_clauses = []
    params = []

    if search:
        where_clauses.append("(professor LIKE ? OR university LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    if hide_cond:
        where_clauses.append(hide_cond)
        params.extend(hide_params)

    for col, val in filters.items():
        if col in _FILTERABLE_COLUMNS:
            where_clauses.append(f"{col} = ?")
            params.append(val)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with connect() as conn:
        # Total count
        total = conn.execute(
            f"SELECT COUNT(*) FROM professors {where_sql}", params
        ).fetchone()[0]

        # Paginated rows — always include id/stage for links/badges even if
        # not part of TABLE_COLUMNS
        select_cols = ", ".join(["id", "stage"] + TABLE_COLUMNS)
        sql = f"""
            SELECT {select_cols}
            FROM professors
            {where_sql}
            ORDER BY {_order_by(sort, direction)}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()

    return [dict(r) for r in rows], total


# --- University grouping (Prompt 3) -----------------------------------------


def get_university_groups(search: str = "", filters: dict[str, str] | None = None,
                          include_hidden: bool = False) -> list[dict[str, Any]]:
    """Get universities grouped by normalized name, with counts and flags.

    Returns list of dicts with:
      - normalized_name: normalized university name for grouping
      - display_name: original university name (first seen as display)
      - country: location_country
      - total_professors: total count
      - contacted_count: count where status != 'new'
    """
    filters = filters or {}
    where_clauses = []
    params = []

    if search:
        where_clauses.append("(professors.professor LIKE ? OR professors.university LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    if hide_cond:
        where_clauses.append(hide_cond)
        params.extend(hide_params)

    for col, val in filters.items():
        if col in _FILTERABLE_COLUMNS:
            where_clauses.append(f"professors.{col} = ?")
            params.append(val)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with connect() as conn:
        # Get all professors with university/country, then group in Python
        # since we need to normalize the university name
        sql = f"""
            SELECT university, location_country, status
            FROM professors
            {where_sql}
        """
        rows = conn.execute(sql, params).fetchall()

    # Group by normalized university + country
    groups = {}
    for row in rows:
        uni = row["university"] or ""
        country = row["location_country"] or ""
        status = row["status"] or "new"

        norm_uni = _normalize_university(uni)
        key = (norm_uni, country.lower())

        if key not in groups:
            groups[key] = {
                "normalized_name": norm_uni,
                "display_name": uni,  # Use first seen as display name
                "university": uni,    # Template expects this
                "country": country,
                "location_country": country,  # Template expects this
                "total_professors": 0,
                "contacted_count": 0,
            }

        groups[key]["total_professors"] += 1
        if status not in ("not_contacted", "new"):
            groups[key]["contacted_count"] += 1

    # Convert to list and sort by total_professors desc
    result = list(groups.values())
    result.sort(key=lambda x: (-x["total_professors"], x["display_name"]))
    return result


# --- Professor detail (Prompt 3) --------------------------------------------


def get_professor_by_id(professor_id: str) -> Optional[dict[str, Any]]:
    """Get full professor details by ID."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM professors WHERE id = ?", (professor_id,)
        ).fetchone()
        if row:
            return dict(row)
        return None


def get_areas_for_professor(professor_id: str) -> list[dict[str, Any]]:
    """Per-area paper scores for one professor, strongest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT area, score FROM professor_areas WHERE professor_id = ? "
            "ORDER BY score DESC", (professor_id,)).fetchall()
    out = []
    for r in rows:
        a = areas.BY_CODE.get(r["area"])
        if a:
            out.append({"code": a.code, "title": a.title, "group": a.group,
                        "score": r["score"]})
    return out


def get_evidence_for_professor(professor_id: str) -> list[dict[str, Any]]:
    """Get all evidence rows for a professor."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT claim, source_type, quote_or_paraphrase, source_url, stage, fetched_at "
            "FROM evidence WHERE professor_id = ? ORDER BY fetched_at DESC",
            (professor_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_professor_status(professor_id: str, status: str, notes: str = "") -> bool:
    """Update professor status and append to history with confirmed=1."""
    if status not in STATUS_TAXONOMY:
        raise ValueError(f"Invalid status: {status}")

    with connect() as conn:
        # Update professor status
        conn.execute(
            "UPDATE professors SET status = ?, notes = ?, updated_at = datetime('now') WHERE id = ?",
            (status, notes, professor_id),
        )
        # Append to status_history with confirmed=1 (direct user action)
        conn.execute(
            "INSERT INTO status_history (professor_id, at, status, note, confirmed) VALUES (?, datetime('now'), ?, ?, 1)",
            (professor_id, status, notes),
        )
    return True


def update_professor_notes(professor_id: str, notes: str) -> bool:
    """Update professor notes field."""
    with connect() as conn:
        conn.execute(
            "UPDATE professors SET notes = ? WHERE id = ?",
            (notes, professor_id),
        )
    return True


def change_professor_status(professor_id: str, status: str) -> bool:
    """Change status only — leaves existing notes untouched. Logs history.

    Transitions into 'contacted' also stamp last_contacted_at (drives the
    follow-ups page and the Gmail scanner's since-window).
    """
    if status not in STATUS_TAXONOMY:
        raise ValueError(f"Invalid status: {status}")
    with connect() as conn:
        with transaction(conn):
            cur = conn.execute(
                "UPDATE professors SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status, professor_id),
            )
            if status == "contacted":
                conn.execute(
                    "UPDATE professors SET last_contacted_at = datetime('now') WHERE id = ?",
                    (professor_id,),
                )
            conn.execute(
                "INSERT INTO status_history (professor_id, at, status, confirmed) VALUES (?, datetime('now'), ?, 1)",
                (professor_id, status),
            )
            return cur.rowcount > 0


def bulk_change_status(professor_ids: list[str], status: str) -> int:
    """Bulk status change without touching notes. Returns rows updated."""
    if status not in STATUS_TAXONOMY:
        raise ValueError(f"Invalid status: {status}")
    stamp = status == "contacted"
    updated = 0
    with connect() as conn:
        with transaction(conn):
            for pid in professor_ids:
                cur = conn.execute(
                    "UPDATE professors SET status = ?, updated_at = datetime('now') WHERE id = ?",
                    (status, pid),
                )
                if stamp:
                    conn.execute(
                        "UPDATE professors SET last_contacted_at = datetime('now') WHERE id = ?",
                        (pid,),
                    )
                conn.execute(
                    "INSERT INTO status_history (professor_id, at, status, confirmed) VALUES (?, datetime('now'), ?, 1)",
                    (pid, status),
                )
                updated += cur.rowcount
    return updated


# Expose for app.py
FILTERABLE_COLUMNS = _FILTERABLE_COLUMNS


def get_status_history(professor_id: str) -> list[dict[str, Any]]:
    """Get status history for a professor, reverse chronological."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT at, status, note, confirmed FROM status_history WHERE professor_id = ? ORDER BY at DESC",
            (professor_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_agent_task(professor_id: str, task_type: str) -> int:
    """Insert a new pending agent task. Returns the task ID."""
    if task_type not in ("fetch_papers", "draft_email"):
        raise ValueError(f"Invalid task_type: {task_type}")

    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO agent_tasks
               (professor_id, task_type, status, requested_at)
               VALUES (?, ?, 'pending', datetime('now'))""",
            (professor_id, task_type),
        )
        return cursor.lastrowid


def get_agent_tasks(
    professor_id: str | None = None,
    task_type: str | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Get agent tasks with optional filters."""
    where_clauses = []
    params = []

    if professor_id:
        where_clauses.append("professor_id = ?")
        params.append(professor_id)

    if task_type:
        where_clauses.append("task_type = ?")
        params.append(task_type)

    if status:
        where_clauses.append("status = ?")
        params.append(status)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with connect() as conn:
        rows = conn.execute(
            f"""SELECT *
               FROM agent_tasks
               {where_sql}
               ORDER BY requested_at DESC
               LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]


def update_agent_task_status(
    task_id: int,
    status: str,
    result_summary: str = "",
    result_payload: str = "",
    error_message: str = "",
) -> bool:
    """Update an agent task's status and result."""
    if status not in ("pending", "in_progress", "done", "failed"):
        raise ValueError(f"Invalid status: {status}")

    with connect() as conn:
        if status == "in_progress":
            conn.execute(
                """UPDATE agent_tasks
                   SET status = ?, started_at = datetime('now')
                   WHERE id = ?""",
                (status, task_id),
            )
        elif status in ("done", "failed"):
            conn.execute(
                """UPDATE agent_tasks
                   SET status = ?, completed_at = datetime('now'),
                       result_summary = ?, result_payload = ?, error_message = ?
                   WHERE id = ?""",
                (status, result_summary, result_payload, error_message, task_id),
            )
        else:
            conn.execute(
                "UPDATE agent_tasks SET status = ? WHERE id = ?",
                (status, task_id),
            )
    return True


def bulk_update_status(
    professor_ids: list[str],
    status: str,
    note: str = "",
    at: str = None,  # ISO format datetime string for backdating
) -> int:
    """Bulk update status for multiple professors with optional backdated timestamp.
    Returns number of professors updated.
    """
    if status not in STATUS_TAXONOMY:
        raise ValueError(f"Invalid status: {status}")

    if not professor_ids:
        return 0

    with connect() as conn:
        # Use transaction for bulk update
        with transaction(conn):
            updated = 0
            for pid in professor_ids:
                # Update professor
                conn.execute(
                    "UPDATE professors SET status = ?, notes = ?, updated_at = datetime('now') WHERE id = ?",
                    (status, note, pid),
                )
                # Insert history row with backdated timestamp if provided
                if at:
                    conn.execute(
                        "INSERT INTO status_history (professor_id, at, status, note, confirmed) VALUES (?, ?, ?, ?, 1)",
                        (pid, at, status, note),
                    )
                else:
                    conn.execute(
                        "INSERT INTO status_history (professor_id, at, status, note, confirmed) VALUES (?, datetime('now'), ?, ?, 1)",
                        (pid, status, note),
                    )
                updated += 1
            return updated


# --- Paper recommendations (Prompt 6) ------------------------------------------


def clear_paper_recommendations(professor_id: str) -> int:
    """Remove existing paper recommendations for a professor (before re-fetching)."""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM paper_recommendations WHERE professor_id = ?",
            (professor_id,),
        )
        return cur.rowcount


def insert_paper_recommendation(
    professor_id: str,
    title: str,
    venue: str,
    year: int,
    relevance_note: str,
    source_url: str = "",
    rank: int = 0,
) -> int:
    """Insert a single paper recommendation row. Returns the row ID."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO paper_recommendations
               (professor_id, title, venue, year, relevance_note,
                source_url, fetched_at, rank)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
            (professor_id, title, venue, year, relevance_note,
             source_url, rank),
        )
        return cur.lastrowid


def get_paper_recommendations(professor_id: str) -> list[dict[str, Any]]:
    """Get paper recommendations for a professor, ordered by rank."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM paper_recommendations
               WHERE professor_id = ?
               ORDER BY rank ASC, year DESC""",
            (professor_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- Confirmations (Prompt 7 — Gmail reply scanning) -------------------------------


def get_pending_confirmations() -> list[dict[str, Any]]:
    """Get all unconfirmed, non-rejected status_history rows (Gmail scan suggestions)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT sh.id, sh.professor_id, sh.at, sh.status, sh.note,
                      p.professor, p.university, p.email, p.status as current_status
               FROM status_history sh
               JOIN professors p ON p.id = sh.professor_id
               WHERE sh.confirmed = 0 AND (sh.rejected = 0 OR sh.rejected IS NULL)
               ORDER BY sh.at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def confirm_status_change(history_id: int) -> bool:
    """Confirm a pending status change: update professors.status and set confirmed=1.

    Returns True on success, False if the row doesn't exist or is already confirmed.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT professor_id, status FROM status_history WHERE id = ? AND confirmed = 0",
            (history_id,),
        ).fetchone()
        if not row:
            return False

        professor_id = row[0]
        new_status = row[1]

        with transaction(conn):
            conn.execute(
                "UPDATE professors SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (new_status, professor_id),
            )
            conn.execute(
                "UPDATE status_history SET confirmed = 1 WHERE id = ?",
                (history_id,),
            )
        return True


def reject_status_change(history_id: int) -> bool:
    """Reject a pending status change: set rejected=1 without touching professors.status."""
    with connect() as conn:
        cur = conn.execute(
            """UPDATE status_history SET rejected = 1
               WHERE id = ? AND confirmed = 0 AND (rejected = 0 OR rejected IS NULL)""",
            (history_id,),
        )
        return cur.rowcount > 0


def update_confirmation_status(history_id: int, new_status: str, note: str = "") -> bool:
    """Edit-then-confirm: update the suggested status/note on a pending row before confirming."""
    if new_status not in STATUS_TAXONOMY:
        raise ValueError(f"Invalid status: {new_status}")

    with connect() as conn:
        conn.execute(
            "UPDATE status_history SET status = ?, note = ? WHERE id = ? AND confirmed = 0",
            (new_status, note, history_id),
        )
    return True


def get_config_value(key: str) -> Optional[str]:
    """Read a value from the config table."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None


def set_config_value(key: str, value: str) -> None:
    """Write a value from the config table."""
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )


# --- View-level country hiding (config-driven) ---------------------------------

HIDDEN_COUNTRIES_KEY = "hidden_countries"
SHOW_HIDDEN_KEY = "show_hidden"


def get_hidden_countries() -> list[str]:
    """Countries hidden across the UI (comma-separated in config). Empty = show all."""
    raw = get_config_value(HIDDEN_COUNTRIES_KEY) or ""
    return [c.strip() for c in raw.split(",") if c.strip()]


def set_hidden_countries(countries: list[str]) -> None:
    cleaned = sorted({c.strip() for c in countries if c and c.strip()})
    set_config_value(HIDDEN_COUNTRIES_KEY, ", ".join(cleaned))


def get_show_hidden() -> bool:
    """Sticky 'temporarily reveal hidden rows' mode. Applies to every page."""
    return (get_config_value(SHOW_HIDDEN_KEY) or "") == "1"


def set_show_hidden(value: bool) -> None:
    set_config_value(SHOW_HIDDEN_KEY, "1" if value else "0")


def _hide_countries_sql(alias: str = "", include_hidden: bool = False) -> tuple[str, list]:
    """Return (condition, params) excluding hidden countries, or ("", []) when
    nothing is hidden, the sticky reveal mode is on, or include_hidden is set.
    Condition has NO leading AND — callers place it."""
    if include_hidden or get_show_hidden():
        return "", []
    countries = get_hidden_countries()
    if not countries:
        return "", []
    col = f"{alias}.location_country" if alias else "location_country"
    placeholders = ",".join("?" for _ in countries)
    return f"{col} NOT IN ({placeholders})", list(countries)


def get_follow_up_professors(days: int = 21, include_hidden: bool = False) -> list[dict[str, Any]]:
    """Get professors who were contacted more than `days` ago with no reply."""
    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    hide_seg = f"AND {hide_cond}" if hide_cond else ""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, professor, university, location_country, email,
                      status, last_contacted_at, fit_tier, final_score_phd,
                      CAST(julianday('now') - julianday(last_contacted_at) AS INTEGER) as days_waiting
               FROM professors
               WHERE status = 'contacted'
                 AND last_contacted_at IS NOT NULL
                 AND julianday('now') - julianday(last_contacted_at) > ?
                 {hide_seg}
               ORDER BY last_contacted_at ASC""",
            (days, *hide_params),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_contacted_professors(include_hidden: bool = False) -> list[dict[str, Any]]:
    """Get ALL professors with status 'contacted'."""
    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    hide_seg = f"AND {hide_cond}" if hide_cond else ""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, professor, university, location_country, email,
                      status, last_contacted_at, fit_tier, final_score_phd,
                      CASE WHEN last_contacted_at IS NOT NULL
                           THEN CAST(julianday('now') - julianday(last_contacted_at) AS INTEGER)
                           ELSE NULL END as days_waiting
               FROM professors
               WHERE status = 'contacted'
                 {hide_seg}
               ORDER BY last_contacted_at ASC""",
            hide_params,
        ).fetchall()
        return [dict(r) for r in rows]


# =====================================================================
# NEW: Deadlines, University Detail, Positions, Shortlist, Pipeline Stats
# =====================================================================


def get_all_deadlines(
    search: str = "",
    filter_university: str = "",
    filter_track: str = "",
    filter_deadline_type: str = "",
    filter_confidence: str = "",
    upcoming_only: bool = True,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Get all program deadlines with optional filters.
    
    Returns (rows, total_count) with joined university program info.
    """
    where_clauses = []
    params = []

    if upcoming_only:
        where_clauses.append("pd.deadline_date >= date('now') AND pd.deadline_date IS NOT NULL")
    
    if search:
        where_clauses.append("(up.university LIKE ? OR pd.cycle_label LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    if filter_university:
        where_clauses.append("up.university = ?")
        params.append(filter_university)

    if filter_track:
        where_clauses.append("up.track = ?")
        params.append(filter_track)

    if filter_deadline_type:
        where_clauses.append("pd.deadline_type = ?")
        params.append(filter_deadline_type)

    if filter_confidence:
        where_clauses.append("pd.deadline_date_confidence = ?")
        params.append(filter_confidence)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with connect() as conn:
        # Total count
        total = conn.execute(
            f"""SELECT COUNT(*) 
                FROM program_deadlines pd
                JOIN university_programs up ON up.id = pd.university_program_id
                {where_sql}""",
            params,
        ).fetchone()[0]

        # Rows
        sql = f"""
            SELECT 
                pd.id,
                pd.university_program_id,
                pd.deadline_type,
                pd.deadline_date,
                pd.deadline_date_confidence,
                pd.cycle_label,
                pd.description,
                pd.source_url,
                pd.calendar_event_created,
                pd.calendar_event_id,
                pd.reminder_event_id,
                up.university,
                up.track,
                up.funding_confidence,
                up.program_name,
                up.location_country
            FROM program_deadlines pd
            JOIN university_programs up ON up.id = pd.university_program_id
            {where_sql}
            ORDER BY pd.deadline_date ASC NULLS LAST
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()

    return [dict(r) for r in rows], total


def get_deadline_filter_options() -> dict[str, list[str]]:
    """Get distinct values for deadline filters."""
    with connect() as conn:
        options = {}
        
        rows = conn.execute("""
            SELECT DISTINCT up.university 
            FROM university_programs up 
            JOIN program_deadlines pd ON pd.university_program_id = up.id
            ORDER BY up.university
        """).fetchall()
        options["university"] = [r[0] for r in rows]

        rows = conn.execute("""
            SELECT DISTINCT up.track 
            FROM university_programs up 
            WHERE up.track IS NOT NULL AND up.track != ''
            ORDER BY up.track
        """).fetchall()
        options["track"] = [r[0] for r in rows]

        rows = conn.execute("""
            SELECT DISTINCT deadline_type 
            FROM program_deadlines 
            WHERE deadline_type IS NOT NULL AND deadline_type != ''
            ORDER BY deadline_type
        """).fetchall()
        options["deadline_type"] = [r[0] for r in rows]

        rows = conn.execute("""
            SELECT DISTINCT deadline_date_confidence 
            FROM program_deadlines 
            WHERE deadline_date_confidence IS NOT NULL AND deadline_date_confidence != ''
            ORDER BY deadline_date_confidence
        """).fetchall()
        options["deadline_date_confidence"] = [r[0] for r in rows]

    return options


def get_university_detail(university_slug: str) -> Optional[dict[str, Any]]:
    """Get full detail for a university including programs, deadlines, and professors.
    
    university_slug is the normalized name (e.g., 'national taiwan university').
    """
    with connect() as conn:
        # Get university programs (resolve slug to raw stored name variants)
        prog_names = [university_slug] + _resolve_university_names(university_slug, "university_programs")
        prog_ph = ",".join("?" for _ in prog_names)
        prog_rows = conn.execute(
            f"""SELECT * FROM university_programs
                WHERE LOWER(university) IN ({prog_ph})
                ORDER BY track""",
            [n.lower() for n in prog_names],
        ).fetchall()
        
        if not prog_rows:
            return None
        
        programs = [dict(r) for r in prog_rows]
        
        # Get deadlines for all programs at this university
        program_ids = [p["id"] for p in programs]
        placeholders = ",".join("?" for _ in program_ids)
        deadline_rows = conn.execute(
            f"""SELECT pd.*, up.track, up.university
                FROM program_deadlines pd
                JOIN university_programs up ON up.id = pd.university_program_id
                WHERE pd.university_program_id IN ({placeholders})
                ORDER BY pd.deadline_date ASC NULLS LAST""",
            program_ids,
        ).fetchall()
        deadlines = [dict(r) for r in deadline_rows]
        
        # Get professors at this university (from professors table)
        prof_names = [university_slug] + _resolve_university_names(university_slug, "professors")
        prof_ph = ",".join("?" for _ in prof_names)
        hide_cond, hide_params = _hide_countries_sql()
        prof_rows = conn.execute(
            f"""SELECT id, professor, university, location_country, fit_tier,
                      phd_recommendation, final_score_phd, masters_recommendation,
                      final_score_masters, status, email, homepage
               FROM professors
               WHERE LOWER(university) IN ({prof_ph})
               {('AND ' + hide_cond) if hide_cond else ''}
               ORDER BY
                 CASE
                   WHEN phd_recommendation = 'MUST APPLY' THEN 1
                   WHEN phd_recommendation = 'GOOD FIT' THEN 2
                   WHEN masters_recommendation = 'MUST APPLY' THEN 3
                   WHEN masters_recommendation = 'GOOD FIT' THEN 4
                   WHEN phd_recommendation = 'MEDIUM FIT' THEN 5
                   ELSE 6
                 END,
                 final_score_phd DESC NULLS LAST""",
            [n.lower() for n in prof_names] + list(hide_params),
        ).fetchall()
        professors = [dict(r) for r in prof_rows]
        
        # Use first program's university name as display
        display_name = programs[0]["university"]
        location_country = programs[0]["location_country"]
        department_url = programs[0]["department_url"]
        funding_info = programs[0].get("funding_info")

    return {
        "university": display_name,
        "normalized_name": university_slug,
        "location_country": location_country,
        "department_url": department_url,
        "funding_note": funding_info,
        "programs": programs,
        "deadlines": deadlines,
        "professors": professors,
    }


def get_positions(
    search: str = "",
    filter_track: str = "",
    filter_recommendation: str = "",
    filter_status: str = "",
    filter_institution: str = "",
    filter_application_status: str = "",
    limit: int = 100,
    offset: int = 0,
    include_hidden: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Get positions with optional filters."""
    where_clauses = []
    params = []

    if search:
        where_clauses.append("(title LIKE ? OR institution LIKE ? OR professor_name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    if hide_cond:
        where_clauses.append(hide_cond)
        params.extend(hide_params)

    if filter_track:
        where_clauses.append("track = ?")
        params.append(filter_track)

    if filter_recommendation:
        where_clauses.append("recommendation = ?")
        params.append(filter_recommendation)

    if filter_status:
        where_clauses.append("status = ?")
        params.append(filter_status)

    if filter_institution:
        where_clauses.append("institution = ?")
        params.append(filter_institution)

    if filter_application_status:
        where_clauses.append("application_status = ?")
        params.append(filter_application_status)

    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM positions {where_sql}", params
        ).fetchone()[0]

        sql = f"""
            SELECT * FROM positions
            {where_sql}
            ORDER BY 
                CASE recommendation
                    WHEN 'MUST APPLY' THEN 1
                    WHEN 'GOOD FIT' THEN 2
                    WHEN 'MEDIUM FIT' THEN 3
                    WHEN 'BACKUP / LOW FIT' THEN 4
                    WHEN 'DO NOT APPLY' THEN 5
                    ELSE 6
                END,
                deadline ASC NULLS LAST,
                score DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()

    return [dict(r) for r in rows], total


def get_position_filter_options() -> dict[str, list[str]]:
    """Get distinct values for position filters."""
    with connect() as conn:
        options = {}
        
        rows = conn.execute("""
            SELECT DISTINCT track FROM positions 
            WHERE track IS NOT NULL AND track != '' ORDER BY track
        """).fetchall()
        options["track"] = [r[0] for r in rows]

        rows = conn.execute("""
            SELECT DISTINCT recommendation FROM positions 
            WHERE recommendation IS NOT NULL AND recommendation != '' 
            ORDER BY recommendation
        """).fetchall()
        options["recommendation"] = [r[0] for r in rows]

        rows = conn.execute("""
            SELECT DISTINCT status FROM positions 
            WHERE status IS NOT NULL AND status != '' ORDER BY status
        """).fetchall()
        options["status"] = [r[0] for r in rows]

        rows = conn.execute("""
            SELECT DISTINCT institution FROM positions 
            WHERE institution IS NOT NULL AND institution != '' ORDER BY institution
        """).fetchall()
        options["institution"] = [r[0] for r in rows]

    return options


def get_shortlist(
    track: str = "both",  # 'phd', 'masters', 'both'
    min_score: int = 7,
    max_results: int = 50,
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    """Get shortlist of top professors: MUST APPLY / GOOD FIT, not contacted, sorted by score."""
    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    hide_seg = f"AND {hide_cond}" if hide_cond else ""
    with connect() as conn:
        if track == "phd":
            sql = f"""
                SELECT id, professor, university, location_country, fit_tier,
                       phd_recommendation, final_score_phd, masters_recommendation,
                       final_score_masters, status, email, homepage, csrankings_rank
                FROM professors
                WHERE (phd_recommendation IN ('MUST APPLY', 'GOOD FIT')
                       AND final_score_phd >= ?)
                  AND (status = 'not_contacted' OR status = 'new')
                  {hide_seg}
                ORDER BY
                    CASE phd_recommendation WHEN 'MUST APPLY' THEN 1 ELSE 2 END,
                    final_score_phd DESC
                LIMIT ?
            """
            params = [min_score, *hide_params, max_results]
        elif track == "masters":
            sql = f"""
                SELECT id, professor, university, location_country, fit_tier,
                       phd_recommendation, final_score_phd, masters_recommendation,
                       final_score_masters, status, email, homepage, csrankings_rank
                FROM professors
                WHERE (masters_recommendation IN ('MUST APPLY', 'GOOD FIT')
                       AND final_score_masters >= ?)
                  AND (status = 'not_contacted' OR status = 'new')
                  {hide_seg}
                ORDER BY
                    CASE masters_recommendation WHEN 'MUST APPLY' THEN 1 ELSE 2 END,
                    final_score_masters DESC
                LIMIT ?
            """
            params = [min_score, *hide_params, max_results]
        else:  # both
            sql = f"""
                SELECT id, professor, university, location_country, fit_tier,
                       phd_recommendation, final_score_phd, masters_recommendation,
                       final_score_masters, status, email, homepage, csrankings_rank
                FROM professors
                WHERE ((phd_recommendation IN ('MUST APPLY', 'GOOD FIT')
                        AND final_score_phd >= ?)
                       OR (masters_recommendation IN ('MUST APPLY', 'GOOD FIT')
                           AND final_score_masters >= ?))
                  AND (status = 'not_contacted' OR status = 'new')
                  {hide_seg}
                ORDER BY
                    CASE
                      WHEN phd_recommendation = 'MUST APPLY' OR masters_recommendation = 'MUST APPLY' THEN 1
                      ELSE 2
                    END,
                    COALESCE(final_score_phd, 0) + COALESCE(final_score_masters, 0) DESC
                LIMIT ?
            """
            params = [min_score, min_score, *hide_params, max_results]
        
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_pipeline_stats(include_hidden: bool = False) -> dict[str, Any]:
    """Get comprehensive pipeline statistics for the dashboard overview."""
    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    hide_seg = f"AND {hide_cond}" if hide_cond else ""
    with connect() as conn:
        # Stage breakdown
        stage_rows = conn.execute(
            f"""SELECT stage, COUNT(*) as cnt
               FROM professors
               WHERE 1=1 {hide_seg}
               GROUP BY stage ORDER BY cnt DESC""",
            hide_params,
        ).fetchall()
        stage_breakdown = {r[0] if r[0] else "unknown": r[1] for r in stage_rows}

        # Hard exclude breakdown
        hard_exclude_rows = conn.execute(
            f"""SELECT
                  SUM(CASE WHEN phd_hard_exclude = 1 THEN 1 ELSE 0 END) as phd_hard_excluded,
                  SUM(CASE WHEN masters_hard_exclude = 1 THEN 1 ELSE 0 END) as masters_hard_excluded
               FROM professors
               WHERE 1=1 {hide_seg}""",
            hide_params,
        ).fetchone()

        # Deep reviewed breakdown by recommendation (PhD)
        phd_rec_rows = conn.execute(
            f"""SELECT phd_recommendation, COUNT(*) as cnt
                FROM professors
                WHERE stage = 'deep_reviewed' AND phd_recommendation IS NOT NULL
                  {hide_seg}
                GROUP BY phd_recommendation""",
            hide_params,
        ).fetchall()
        phd_rec_breakdown = {r[0]: r[1] for r in phd_rec_rows}

        # Deep reviewed breakdown by recommendation (Masters)
        ms_rec_rows = conn.execute(
            f"""SELECT masters_recommendation, COUNT(*) as cnt
                FROM professors
                WHERE stage = 'deep_reviewed' AND masters_recommendation IS NOT NULL
                  {hide_seg}
                GROUP BY masters_recommendation""",
            hide_params,
        ).fetchall()
        ms_rec_breakdown = {r[0]: r[1] for r in ms_rec_rows}

        # Contact status for deep reviewed
        contact_rows = conn.execute(
            f"""SELECT status, COUNT(*) as cnt
                FROM professors
                WHERE stage = 'deep_reviewed'
                  {hide_seg}
                GROUP BY status ORDER BY cnt DESC""",
            hide_params,
        ).fetchall()
        contact_breakdown = {r[0] if r[0] else "unknown": r[1] for r in contact_rows}

        # Upcoming deadlines (next 14 days)
        upcoming_deadlines = conn.execute(
            """SELECT COUNT(*) FROM program_deadlines
               WHERE deadline_date IS NOT NULL
                 AND deadline_date >= date('now')
                 AND deadline_date <= date('now', '+14 days')"""
        ).fetchone()[0]

        # Positions needing review
        positions_new = conn.execute(
            """SELECT COUNT(*) FROM positions WHERE status = 'new'"""
        ).fetchone()[0]

        # Professors with pending agent tasks
        pending_tasks = conn.execute(
            """SELECT COUNT(DISTINCT professor_id) FROM agent_tasks WHERE status IN ('pending', 'in_progress')"""
        ).fetchone()[0]

        # Follow-ups needed (contacted > 21 days ago)
        followups = conn.execute(
            f"""SELECT COUNT(*) FROM professors
                WHERE status = 'contacted'
                  AND last_contacted_at IS NOT NULL
                  AND julianday('now') - julianday(last_contacted_at) > 21
                  {hide_seg}""",
            hide_params,
        ).fetchone()[0]

        # Total professors
        total = conn.execute(
            f"SELECT COUNT(*) FROM professors WHERE 1=1 {hide_seg}",
            hide_params,
        ).fetchone()[0]

    return {
        "total": total,
        "stage_breakdown": stage_breakdown,
        "phd_hard_excluded": hard_exclude_rows[0] if hard_exclude_rows else 0,
        "masters_hard_excluded": hard_exclude_rows[1] if hard_exclude_rows else 0,
        "phd_rec_breakdown": phd_rec_breakdown,
        "ms_rec_breakdown": ms_rec_breakdown,
        "contact_breakdown": contact_breakdown,
        "upcoming_deadlines_14d": upcoming_deadlines,
        "positions_new": positions_new,
        "pending_tasks": pending_tasks,
        "followups_needed": followups,
    }


def get_global_search_results(query: str, limit: int = 20,
                              include_hidden: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Search across professors, universities, deadlines, and positions.

    Returns dict with keys: professors, universities, deadlines, positions
    """
    if not query or len(query.strip()) < 2:
        return {"professors": [], "universities": [], "deadlines": [], "positions": []}

    q = f"%{query.strip()}%"
    results = {}
    hide_cond, hide_params = _hide_countries_sql(include_hidden=include_hidden)
    prof_hide_seg = f"AND {hide_cond}" if hide_cond else ""

    with connect() as conn:
        # Professors
        rows = conn.execute(
            f"""SELECT id, professor, university, location_country, fit_tier,
                      phd_recommendation, final_score_phd, masters_recommendation,
                      final_score_masters, status
               FROM professors
               WHERE professor LIKE ? OR university LIKE ? OR notes LIKE ?
                 {prof_hide_seg}
               ORDER BY
                 CASE
                   WHEN phd_recommendation = 'MUST APPLY' THEN 1
                   WHEN phd_recommendation = 'GOOD FIT' THEN 2
                   ELSE 3
                 END,
                 final_score_phd DESC NULLS LAST
               LIMIT ?""",
            (q, q, q, *hide_params, limit),
        ).fetchall()
        results["professors"] = [dict(r) for r in rows]
        
        # Universities (from university_programs)
        rows = conn.execute(
            """SELECT DISTINCT id, university, location_country, track, funding_confidence
               FROM university_programs
               WHERE university LIKE ? OR department_url LIKE ?
               LIMIT ?""",
            (q, q, limit),
        ).fetchall()
        results["universities"] = [dict(r) for r in rows]
        
        # Deadlines
        rows = conn.execute(
            """SELECT pd.id, pd.university_program_id, pd.deadline_type, pd.deadline_date,
                      pd.deadline_date_confidence, pd.cycle_label,
                      up.university, up.track
               FROM program_deadlines pd
               JOIN university_programs up ON up.id = pd.university_program_id
               WHERE up.university LIKE ? OR pd.cycle_label LIKE ?
               ORDER BY pd.deadline_date ASC NULLS LAST
               LIMIT ?""",
            (q, q, limit),
        ).fetchall()
        results["deadlines"] = [dict(r) for r in rows]
        
        # Positions
        pos_hide_seg = f"AND {hide_cond}" if hide_cond else ""
        rows = conn.execute(
            f"""SELECT id, title, institution, location_country, track,
                      deadline, recommendation, score, status
               FROM positions
               WHERE (title LIKE ? OR institution LIKE ? OR professor_name LIKE ?)
                 {pos_hide_seg}
               ORDER BY
                 CASE recommendation
                   WHEN 'MUST APPLY' THEN 1
                   WHEN 'GOOD FIT' THEN 2
                   ELSE 3
                 END,
                 score DESC
               LIMIT ?""",
            (q, q, q, *hide_params, limit),
        ).fetchall()
        results["positions"] = [dict(r) for r in rows]
    
    return results


def get_professor_university_context(university: str) -> dict[str, Any]:
    """Get deadlines and positions for a professor's university.
    
    Returns dict with 'deadlines' and 'positions' lists, plus 'university_slug'.
    """
    if not university:
        return {"deadlines": [], "positions": [], "university_slug": None}
    
    with connect() as conn:
        # Get upcoming deadlines for this university
        deadline_rows = conn.execute(
            """SELECT pd.*, up.track, up.university, up.program_name
               FROM program_deadlines pd
               JOIN university_programs up ON up.id = pd.university_program_id
               WHERE LOWER(up.university) = LOWER(?)
                 AND (pd.deadline_date IS NULL OR pd.deadline_date >= date('now'))
               ORDER BY pd.deadline_date ASC NULLS LAST
               LIMIT 10""",
            (university,),
        ).fetchall()
        deadlines = [dict(r) for r in deadline_rows]
        
        # Get open positions at this university
        pos_rows = conn.execute(
            """SELECT * FROM positions
               WHERE LOWER(institution) = LOWER(?)
               ORDER BY COALESCE(digest_date, created_at) DESC NULLS LAST
               LIMIT 5""",
            (university,),
        ).fetchall()
        positions = [dict(r) for r in pos_rows]
    
    return {
        "deadlines": deadlines,
        "positions": positions,
        "university_slug": university,
    }


# --- Position application status -----------------------------------------

APPLICATION_STATUS_TAXONOMY = [
    "not_applied",
    "not_interested",
    "preparing_application",
    "applied",
    "email_sent",
    "awaiting_response",
    "interview_scheduled",
    "rejected",
    "offered",
    "withdrawn",
]

APPLICATION_STATUS_LABELS = {
    "not_applied": "Not Applied",
    "not_interested": "Not Interested",
    "preparing_application": "Preparing Application",
    "applied": "Applied",
    "email_sent": "Email Sent",
    "awaiting_response": "Awaiting Response",
    "interview_scheduled": "Interview Scheduled",
    "rejected": "Rejected",
    "offered": "Offered",
    "withdrawn": "Withdrawn",
}

APPLICATION_STATUS_COLORS = {
    "not_applied": ("bg-gray-100", "dark:bg-gray-700", "text-gray-600", "dark:text-gray-400"),
    "not_interested": ("bg-red-100", "dark:bg-red-900/40", "text-red-700", "dark:text-red-300"),
    "preparing_application": ("bg-yellow-100", "dark:bg-yellow-900/40", "text-yellow-700", "dark:text-yellow-300"),
    "applied": ("bg-blue-100", "dark:bg-blue-900/40", "text-blue-700", "dark:text-blue-300"),
    "email_sent": ("bg-indigo-100", "dark:bg-indigo-900/40", "text-indigo-700", "dark:text-indigo-300"),
    "awaiting_response": ("bg-purple-100", "dark:bg-purple-900/40", "text-purple-700", "dark:text-purple-300"),
    "interview_scheduled": ("bg-cyan-100", "dark:bg-cyan-900/40", "text-cyan-700", "dark:text-cyan-300"),
    "rejected": ("bg-red-200", "dark:bg-red-900/40", "text-red-800", "dark:text-red-300"),
    "offered": ("bg-green-100", "dark:bg-green-900/40", "text-green-700", "dark:text-green-300"),
    "withdrawn": ("bg-gray-200", "dark:bg-gray-700", "text-gray-500", "dark:text-gray-400"),
}


def update_position_application_status(
    position_id: str,
    application_status: str,
    application_notes: str = "",
) -> bool:
    """Update the application status (and optionally notes) for a position.

    Returns True if the row was updated.
    """
    if application_status not in APPLICATION_STATUS_TAXONOMY:
        return False
    with connect() as conn:
        cursor = conn.execute(
            """UPDATE positions
               SET application_status = ?,
                   application_notes = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (application_status, application_notes, position_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_position_application_status_options() -> list[str]:
    """Return the list of valid application status values."""
    return APPLICATION_STATUS_TAXONOMY

def _resolve_university_names(slug: str, table: str = "professors") -> list[str]:
    """Raw university names whose normalized form matches the slug.

    Grid links use normalized slugs (punctuation stripped), but stored names
    contain periods/apostrophes/hyphens ('Massachusetts Inst. of Technology',
    "King's College London") — exact matching alone 404s those pages.
    """
    target = _normalize_university(slug)
    if not target:
        return []
    with connect() as conn:
        rows = conn.execute(f"SELECT DISTINCT university FROM {table}").fetchall()
    return [r[0] for r in rows
            if r[0] and _normalize_university(r[0]) == target]


def get_professors_by_university(university_slug: str) -> list[dict[str, Any]]:
    """Professors at a university (normalized-name match), ranked like the detail view.

    Fallback for universities that exist in the professors table but have no
    university_programs rows, so /university/{slug} always resolves.
    """
    names = [university_slug] + _resolve_university_names(university_slug, "professors")
    placeholders = ",".join("?" for _ in names)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, professor, university, location_country, fit_tier,
                      phd_recommendation, final_score_phd, masters_recommendation,
                      final_score_masters, status, email, homepage
               FROM professors
               WHERE LOWER(university) IN ({placeholders})
               ORDER BY
                 CASE
                   WHEN phd_recommendation = 'MUST APPLY' THEN 1
                   WHEN phd_recommendation = 'GOOD FIT' THEN 2
                   WHEN masters_recommendation = 'MUST APPLY' THEN 3
                   WHEN masters_recommendation = 'GOOD FIT' THEN 4
                   WHEN phd_recommendation = 'MEDIUM FIT' THEN 5
                   ELSE 6
                 END,
                 final_score_phd DESC NULLS LAST""",
            [n.lower() for n in names],
        ).fetchall()
        return [dict(r) for r in rows]


# --- Home-page helpers: application open/close focus --------------------------

APPLICATION_OPEN_TYPES = (
    "application_open", "application_opens", "application_window_open",
    "application_portal_opens", "web_entry_open",
)

APPLICATION_CLOSE_TYPES = (
    "application", "application_close", "application_deadline",
    "application_window_close", "web_entry_close", "admission_period_end",
    "deadline", "admission",
)


def get_upcoming_application_deadlines(limit: int = 8,
                                       include_hidden: bool = False) -> list[dict[str, Any]]:
    """Next application open/close dates (excludes interviews, results, etc.)."""
    open_ph = ",".join("?" for _ in APPLICATION_OPEN_TYPES)
    close_ph = ",".join("?" for _ in APPLICATION_CLOSE_TYPES)
    hide_cond, hide_params = _hide_countries_sql("up", include_hidden=include_hidden)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT pd.id, pd.deadline_type, pd.deadline_date,
                       pd.deadline_date_confidence, pd.cycle_label,
                       up.university, up.track, up.program_name, up.location_country
                FROM program_deadlines pd
                JOIN university_programs up ON up.id = pd.university_program_id
                WHERE pd.deadline_date IS NOT NULL
                  AND pd.deadline_date >= date('now')
                  AND (pd.deadline_type IN ({close_ph}) OR pd.deadline_type IN ({open_ph}))
                  {('AND ' + hide_cond) if hide_cond else ''}
                ORDER BY pd.deadline_date ASC
                LIMIT ?""",
            (*APPLICATION_CLOSE_TYPES, *APPLICATION_OPEN_TYPES, *hide_params, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_open_applications(limit: int = 8,
                          include_hidden: bool = False) -> list[dict[str, Any]]:
    """Programs accepting applications right now.

    A program counts as open if its earliest future/present closing date
    exists and there is no opening date still in the future (i.e. the
    window has opened or was never explicitly recorded).
    """
    close_ph = ",".join("?" for _ in APPLICATION_CLOSE_TYPES)
    open_ph = ",".join("?" for _ in APPLICATION_OPEN_TYPES)
    hide_cond, hide_params = _hide_countries_sql("up", include_hidden=include_hidden)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT up.id AS program_id, up.university, up.track,
                       up.program_name, up.location_country, up.funding_confidence,
                       MIN(c.deadline_date) AS closes_on,
                       CAST(julianday(MIN(c.deadline_date)) - julianday('now') AS INTEGER)
                           AS days_left
                FROM university_programs up
                JOIN program_deadlines c
                  ON c.university_program_id = up.id
                 AND c.deadline_type IN ({close_ph})
                 AND c.deadline_date IS NOT NULL
                 AND c.deadline_date >= date('now')
                WHERE NOT EXISTS (
                    SELECT 1 FROM program_deadlines o
                    WHERE o.university_program_id = up.id
                      AND o.deadline_type IN ({open_ph})
                      AND o.deadline_date IS NOT NULL
                      AND o.deadline_date > date('now')
                )
                  {('AND ' + hide_cond) if hide_cond else ''}
                GROUP BY up.id
                ORDER BY closes_on ASC
                LIMIT ?""",
            (*APPLICATION_CLOSE_TYPES, *APPLICATION_OPEN_TYPES, *hide_params, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_email_draft(professor_id: str) -> Optional[dict[str, Any]]:
    """Newest locally-stored email draft for a professor, or None.

    draft_email tasks save subject/body JSON to result_payload when Gmail
    is unavailable; without this the drafted text is invisible in the UI.
    """
    with connect() as conn:
        row = conn.execute(
            """SELECT result_payload, status, completed_at, requested_at
               FROM agent_tasks
               WHERE professor_id = ? AND task_type = 'draft_email'
                 AND result_payload IS NOT NULL AND result_payload != ''
               ORDER BY COALESCE(completed_at, requested_at) DESC
               LIMIT 1""",
            (professor_id,),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["result_payload"])
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "subject": payload.get("subject", ""),
            "body": payload.get("body", ""),
            "gmail_saved": bool(payload.get("gmail_saved")),
            "task_status": row["status"],
            "at": row["completed_at"] or row["requested_at"],
        }
