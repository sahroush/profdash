"""Canonical SQLite schema for profdash.

Single source of truth — `prof init` runs create_all(). All subsequent
schema changes are additive via idempotent migrations (see migrations.py).

Conventions:
  * timestamps are ISO-8601 TEXT in UTC
  * professors.id is a TEXT slug derived from name + university
  * never drop or repurpose columns; add new ones in migrations
"""
from __future__ import annotations

import sqlite3

DDL: list[str] = [
    # --- core entity -----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS professors (
        id                          TEXT PRIMARY KEY,
        created_at                  TEXT,
        updated_at                  TEXT,
        professor                   TEXT NOT NULL,
        university                  TEXT,
        location_city               TEXT,
        location_country            TEXT,
        homepage                    TEXT,
        dblp_url                    TEXT,
        orcid                       TEXT,
        scholar                     TEXT,
        email                       TEXT,
        csrankings_rank             INTEGER,
        prestige_band               TEXT,

        -- outreach tracker
        status                      TEXT DEFAULT 'not_contacted',
        last_contacted_at           TEXT,
        next_action_at              TEXT,
        notes                       TEXT,

        -- pipeline progress
        stage                       TEXT DEFAULT 'triaged',
        fit_tier                    TEXT,
        reviewed_at                 TEXT,

        -- Master's track scoring
        masters_hard_exclude        INTEGER DEFAULT 0,
        masters_exclusion_reason    TEXT,
        masters_funding_confidence  TEXT,
        masters_springboard_signal  TEXT,
        masters_recommendation      TEXT,
        masters_why                 TEXT,
        final_score_masters         INTEGER,

        -- PhD track scoring
        phd_hard_exclude            INTEGER DEFAULT 0,
        phd_exclusion_reason        TEXT,
        phd_funding_confidence      TEXT,
        phd_recommendation          TEXT,
        phd_why                     TEXT,
        final_score_phd             INTEGER,

        last_csv_sync_at            TEXT,
        exclusion_reason            TEXT,
        funding_confidence          TEXT
    )
    """,
    # --- outreach history --------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS status_history (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        professor_id      TEXT NOT NULL,
        at                TEXT,
        status            TEXT,
        note              TEXT,
        confirmed         INTEGER DEFAULT 1,
        rejected          INTEGER DEFAULT 0,
        gmail_message_id  TEXT DEFAULT NULL
    )
    """,
    # --- evidence / sources (scoring audit trail) --------------------------
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        professor_id         TEXT NOT NULL,
        stage                TEXT NOT NULL,
        field_name           TEXT,
        claim                TEXT,
        source_type          TEXT,
        source_url           TEXT,
        quote_or_paraphrase  TEXT,
        fetched_at           TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        professor_id    TEXT NOT NULL,
        source_type     TEXT NOT NULL,
        url             TEXT,
        fetched_at      TEXT,
        raw_text_path   TEXT,
        sha256          TEXT,
        fetch_method    TEXT
    )
    """,
    # --- university / program intel ----------------------------------------
    """
    CREATE TABLE IF NOT EXISTS university_programs (
        id                     TEXT PRIMARY KEY,
        created_at             TEXT,
        updated_at             TEXT,

        university             TEXT NOT NULL,
        location_country       TEXT,
        department_url         TEXT,

        track                  TEXT,
        program_name           TEXT,

        admission_requirements TEXT,
        english_requirements   TEXT,
        tuition_info           TEXT,
        funding_info           TEXT,
        funding_confidence     TEXT,
        application_components TEXT,
        visa_notes             TEXT,
        notable_faculty        TEXT,

        source_urls            TEXT,
        raw_research_text      TEXT,
        research_date          TEXT,
        uncited_claims         TEXT,

        notes                  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS program_deadlines (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        university_program_id    TEXT NOT NULL,

        deadline_type            TEXT,
        deadline_date            TEXT,
        deadline_date_confidence TEXT,
        cycle_label              TEXT,
        description              TEXT,
        source_url               TEXT,

        calendar_event_created   INTEGER DEFAULT 0,
        calendar_event_id        TEXT,
        reminder_event_id        TEXT
    )
    """,
    # --- agent task queue ----------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS agent_tasks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        professor_id    TEXT NOT NULL,
        task_type       TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'pending',
        requested_at    TEXT,
        started_at      TEXT,
        completed_at    TEXT,
        result_summary  TEXT,
        result_payload  TEXT,
        error_message   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_recommendations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        professor_id    TEXT NOT NULL,
        title           TEXT,
        venue           TEXT,
        year            INTEGER,
        relevance_note  TEXT,
        source_url      TEXT,
        fetched_at      TEXT,
        rank            INTEGER
    )
    """,
    # --- positions board -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS positions (
        id                   TEXT PRIMARY KEY,
        created_at           TEXT,
        updated_at           TEXT,

        source_type          TEXT,
        source_url           TEXT,
        source_email_id      TEXT,
        mailing_list         TEXT,

        title                TEXT,
        institution          TEXT,
        location_country     TEXT,
        professor_name       TEXT,
        professor_id         TEXT,

        track                TEXT,
        deadline             TEXT,
        funding_stated       TEXT,
        raw_text_path        TEXT,

        fit_tier             TEXT,
        hard_exclude         INTEGER DEFAULT 0,
        exclusion_reason     TEXT,
        recommendation       TEXT,
        score                INTEGER,
        why                  TEXT,

        status               TEXT DEFAULT 'new',
        digest_date          TEXT,
        application_status   TEXT DEFAULT 'not_applied',
        application_notes    TEXT DEFAULT ''
    )
    """,
    # --- key/value runtime config ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS config (
        key    TEXT PRIMARY KEY,
        value  TEXT
    )
    """,
    # Applications tracker (application pipeline: status, related professors, events)
    """CREATE TABLE IF NOT EXISTS applications (
        id           TEXT PRIMARY KEY,
        created_at   TEXT,
        updated_at   TEXT,
        name         TEXT NOT NULL,
        url          TEXT,
        status       TEXT NOT NULL DEFAULT 'not_applied',
        program_id   TEXT,
        semester     TEXT,
        notes        TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS application_professors (
        application_id TEXT NOT NULL,
        professor_id   TEXT NOT NULL,
        PRIMARY KEY (application_id, professor_id)
    )""",
    """CREATE TABLE IF NOT EXISTS application_events (
        id             TEXT PRIMARY KEY,
        application_id TEXT NOT NULL,
        event_type     TEXT NOT NULL,
        event_date     TEXT,
        note           TEXT,
        created_at     TEXT
    )""",
    # University tier board (S/A/B application tiers, per normalized university name)
    """CREATE TABLE IF NOT EXISTS university_tiers (
        normalized_name TEXT PRIMARY KEY,
        display_name    TEXT,
        tier            TEXT NOT NULL,
        updated_at      TEXT
    )""",
    # Country research notes (visa / PR / funding landscape per country)
    """CREATE TABLE IF NOT EXISTS country_info (
        country           TEXT PRIMARY KEY,
        visa_summary      TEXT,
        pr_path           TEXT,
        funding_landscape TEXT,
        sources           TEXT,
        updated_at        TEXT,
        notes             TEXT
    )""",
]

INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_professors_status ON professors(status)",
    "CREATE INDEX IF NOT EXISTS idx_professors_stage ON professors(stage)",
    "CREATE INDEX IF NOT EXISTS idx_status_history_prof ON status_history(professor_id)",
    """CREATE INDEX IF NOT EXISTS idx_status_history_gmail_msg_id
       ON status_history(gmail_message_id) WHERE gmail_message_id IS NOT NULL""",
    "CREATE INDEX IF NOT EXISTS idx_evidence_professor ON evidence(professor_id)",
    "CREATE INDEX IF NOT EXISTS idx_sources_professor ON sources(professor_id)",
    "CREATE INDEX IF NOT EXISTS idx_university_programs_university ON university_programs(university)",
    """CREATE INDEX IF NOT EXISTS idx_program_deadlines_program
       ON program_deadlines(university_program_id)""",
    "CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_agent_tasks_professor_type ON agent_tasks(professor_id, task_type)",
    "CREATE INDEX IF NOT EXISTS idx_paper_recs_professor ON paper_recommendations(professor_id)",
    """CREATE INDEX IF NOT EXISTS idx_paper_recs_professor_year
       ON paper_recommendations(professor_id, year DESC)""",
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_positions_source_email ON positions(source_email_id)",
    "CREATE INDEX IF NOT EXISTS idx_app_events_app ON application_events(application_id)",
]


def create_all(conn: sqlite3.Connection) -> None:
    """Idempotent schema creation. Safe to run repeatedly."""
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in DDL:
        conn.execute(stmt)
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()
