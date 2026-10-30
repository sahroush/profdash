#!/usr/bin/env python3
"""
Test dedup mechanism in scan_gmail_replies.py.

Tests three scenarios:
1. First scan of a message → 1 status_history row created
2. Second scan (same message, no confirmation) → NO duplicate row
3. Reject the row, scan again → still NO new row

Uses the real DB and the real dedup functions, but mocks gmail_search
to return a fixed fake message with a known message ID.
"""
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scan_gmail_replies as scanner

# Use a temp DB
TMP_DB = Path(tempfile.mktemp(suffix=".sqlite"))
scanner.DB_PATH = str(TMP_DB)


def setup_db(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS professors (
            id TEXT PRIMARY KEY,
            professor TEXT,
            email TEXT,
            last_contacted_at TEXT,
            status TEXT DEFAULT 'not_contacted'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            professor_id TEXT NOT NULL,
            at TEXT,
            status TEXT,
            note TEXT,
            confirmed INTEGER DEFAULT 0,
            rejected INTEGER DEFAULT 0,
            gmail_message_id TEXT DEFAULT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_status_history_gmail_msg_id "
        "ON status_history(gmail_message_id) WHERE gmail_message_id IS NOT NULL"
    )
    # Insert a test professor
    conn.execute(
        "INSERT INTO professors (id, professor, email, last_contacted_at, status) "
        "VALUES ('test-prof-1', 'Test Prof', 'testprof@example.edu', '2025-01-01', 'contacted')"
    )
    conn.commit()


FAKE_MESSAGE = {
    "id": "FAKE_MSG_ID_12345",
    "subject": "Re: PhD inquiry",
    "from": "testprof@example.edu",
    "date": "Thu, 10 Jul 2026 10:00:00 +0000",
    "snippet": "Thank you for your interest in our lab.",
    "body": "Thank you for your interest in our lab. Unfortunately, I am not taking new PhD students at this time. I wish you the best of luck with your search.",
}


def run_scan():
    """Run one scan pass with mocked Gmail search."""
    with patch.object(scanner, "gmail_search", return_value=[FAKE_MESSAGE]):
        with patch.object(scanner, "gmail_search_raw_python", return_value=[FAKE_MESSAGE]):
            scanner.scan(dry_run=False)


def count_rows_for_msg(conn, msg_id="FAKE_MSG_ID_12345"):
    return conn.execute(
        "SELECT COUNT(*) FROM status_history WHERE gmail_message_id = ?",
        (msg_id,),
    ).fetchone()[0]


def main():
    conn = sqlite3.connect(str(TMP_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    setup_db(conn)

    results = []

    # ── Step 1: First scan → should create 1 row ──
    run_scan()
    count = count_rows_for_msg(conn)
    results.append(("Step 1", count == 1, f"expected 1 row, got {count}"))

    # ── Step 2: Second scan (no confirmation) → 0 new rows ──
    run_scan()
    count = count_rows_for_msg(conn)
    results.append(("Step 2", count == 1, f"expected still 1 row, got {count}"))

    # ── Step 3: Reject the row, then scan again → 0 new rows ──
    row_id = conn.execute(
        "SELECT id FROM status_history WHERE gmail_message_id = 'FAKE_MSG_ID_12345'"
    ).fetchone()[0]
    conn.execute(
        "UPDATE status_history SET rejected = 1 WHERE id = ?", (row_id,)
    )
    conn.commit()
    run_scan()
    count = count_rows_for_msg(conn)
    results.append(("Step 3", count == 1, f"expected still 1 row after reject+rescan, got {count}"))

    # ── Report ──
    all_pass = all(p for _, p, _ in results)
    for name, passed, detail in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status} {name}: {detail}")
    print()
    if all_pass:
        print("All dedup tests passed.")
    else:
        print("DEDUP TESTS FAILED!")
        sys.exit(1)

    # Cleanup
    conn.close()
    TMP_DB.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
