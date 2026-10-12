"""Seed a demo database so new users can explore profdash instantly.

Creates fictional-but-realistic professors, scores, evidence, papers,
tasks, deadlines and positions. Clearly fake (example.edu domains) and
safe to wipe:  prof demo --reset
"""
from __future__ import annotations

import datetime
import sqlite3

from . import areas as areas_mod
from .schema import create_all

# (name, university, country, tier, phd_rec, phd_score, status)
DEMO_PROFESSORS = [
    ("Ada Lovelace", "Analytical Engines University", "United Kingdom",
     "Strong", "MUST APPLY", 9, "not_contacted"),
    ("Grace Hopper", "Nautical Computing Institute", "United States",
     "Strong", "MUST APPLY", 8, "contacted"),
    ("Edsger Dijkstra", "Eindhoven Flow University", "Netherlands",
     "Moderate", "GOOD FIT", 7, "replied_interested"),
    ("Donald Knuth", "Typography & Algorithms College", "United States",
     "Strong", "MUST APPLY", 10, "no_response"),
    ("Barbara Liskov", "Abstraction Systems University", "United States",
     "Moderate", "GOOD FIT", 7, "replied_template"),
    ("John Backus", "Fortran Plains University", "Canada",
     "Lower", "BACKUP / LOW FIT", 4, "not_contacted"),
    ("Jean Bartik", "ENIAC Heritage University", "United States",
     "Moderate", "MEDIUM FIT", 6, "needs_follow_up"),
    ("Alan Turing", "Enigma Park University", "United Kingdom",
     "Strong", "MUST APPLY", 9, "conditional_accept"),
    ("Radia Perlman", "Spanning Trees Institute", "United States",
     "Moderate", "GOOD FIT", 7, "replied_no_funding"),
    ("Tim Berners-Lee", "Hypertext Global University", "Switzerland",
     "Lower", "DO NOT APPLY", 3, "not_contacted"),
    ("Margaret Hamilton", "Lunar Module Software University", "United States",
     "Strong", "GOOD FIT", 8, "not_contacted"),
    ("Dennis Ritchie", "Bell Labs Memorial University", "United States",
     "Moderate", "MEDIUM FIT", 6, "contacted"),
]

# professor name -> {area code: paper score}; first-listed is the primary area
DEMO_AREAS = {
    "Ada Lovelace": {"act": 9.0, "log": 2.5},
    "Grace Hopper": {"plan": 7.5, "soft": 3.0},
    "Edsger Dijkstra": {"act": 6.0, "plan": 4.0},
    "Donald Knuth": {"act": 12.0},
    "Barbara Liskov": {"ops": 8.0, "plan": 5.0},
    "John Backus": {"plan": 4.0},
    "Jean Bartik": {"arch": 5.0, "hpc": 2.0},
    "Alan Turing": {"ai": 6.0, "log": 5.5, "crypt": 3.0},
    "Radia Perlman": {"comm": 9.0, "sec": 2.0},
    "Tim Berners-Lee": {"inforet": 7.0},
    "Margaret Hamilton": {"soft": 8.0, "bed": 4.0},
    "Dennis Ritchie": {"ops": 6.5, "sec": 1.5},
}

EVIDENCE = [
    ("research_fit", "Active at top theory venues in the last 3 years",
     "dblp", "2025 [STOC] Faster algorithms for X | 2024 [FOCS] Y ..."),
    ("funding", "Group page mentions funded openings",
     "web", "\"We have several fully funded PhD openings for Fall 2027\""),
]

PAPERS = [
    ("Faster Algorithms for Constrained Subproblems", "STOC", 2025,
     "Core venue (STOC). Directly relevant to your research interests."),
    ("Improved Bounds for Distributed Aggregation", "PODC", 2024,
     "Core venue (PODC). Directly relevant to your research interests."),
]

DEADLINES = [
    ("application", 45, "Fall 2027 intake", "Applications open Sep 15"),
    ("scholarship", 80, "Fall 2027 intake", "Fellowship deadline"),
]


def _slug(name: str, university: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", f"{name}-{university}".lower()).strip("-")


def seed_demo(db_path) -> dict:
    conn = sqlite3.connect(str(db_path), timeout=15)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    today = datetime.date.today()
    try:
        create_all(conn)
        counts = {}
        for i, (name, uni, country, tier, rec, score, status) in enumerate(DEMO_PROFESSORS):
            pid = _slug(name, uni)
            contacted = (today - datetime.timedelta(days=25 + i)).isoformat() \
                if status != "not_contacted" else None
            conn.execute(
                """INSERT OR REPLACE INTO professors
                   (id, created_at, updated_at, professor, university,
                    location_country, homepage, email, dblp_url,
                    status, last_contacted_at, stage, fit_tier, reviewed_at,
                    phd_recommendation, final_score_phd, phd_why,
                    phd_funding_confidence, masters_recommendation,
                    final_score_masters, masters_why)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, now, now, name, uni, country,
                 f"https://cs.example.edu/~{name.split()[0].lower()}",
                 f"{name.split()[0].lower()}@example.edu",
                 f"https://dblp.org/search?q={name.replace(' ', '+')}",
                 status, contacted, "deep_reviewed", tier, now,
                 rec, score,
                 f"Demo entry: {tier.lower()} fit based on recent output and group activity.",
                 "likely" if score >= 8 else "uncertain",
                 "GOOD FIT" if score >= 6 else None,
                 max(0, score - 2) if score >= 6 else None,
                 "Demo masters note." if score >= 6 else None))
            scores = DEMO_AREAS.get(name, {})
            prim = areas_mod.primary_area(scores)
            if prim:
                conn.execute("UPDATE professors SET area=?, area_group=? WHERE id=?",
                             (prim.title, prim.group, pid))
                conn.execute("DELETE FROM professor_areas WHERE professor_id=?", (pid,))
                conn.executemany(
                    "INSERT INTO professor_areas(professor_id, area, score) VALUES (?,?,?)",
                    [(pid, c, v) for c, v in scores.items()])
            # evidence + history + papers for a few
            if i % 3 == 0:
                for field_name, claim, src_type, quote in EVIDENCE:
                    conn.execute(
                        """INSERT INTO evidence(professor_id, stage, field_name,
                           claim, source_type, source_url, quote_or_paraphrase, fetched_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (pid, "deep_review", field_name, claim, src_type,
                         f"https://example.edu/{field_name}", quote, now))
                conn.execute(
                    """INSERT INTO status_history(professor_id, at, status, note, confirmed)
                       VALUES (?,?,?, ?, 1)""",
                    (pid, now, status, f"Demo seed status ({status})"))
            if i % 4 == 0:
                for rank, (title, venue, year, note) in enumerate(PAPERS, 1):
                    conn.execute(
                        """INSERT INTO paper_recommendations
                           (professor_id, title, venue, year, relevance_note,
                            source_url, fetched_at, rank)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (pid, title, venue, year, note,
                         "https://dblp.org", now, rank))
        # university program rows for grouping page
        unis = sorted({(uni, country) for _, uni, country, *_ in DEMO_PROFESSORS})
        for j, (uni, country) in enumerate(unis):
            conn.execute(
                """INSERT OR REPLACE INTO university_programs
                   (id, created_at, updated_at, university, location_country,
                    department_url, track, program_name, funding_info,
                    funding_confidence, visa_notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (f"demo-prog-{j}", now, now, uni, country,
                 f"https://cs.example.edu/admissions", "phd",
                 f"PhD in Computer Science",
                 "Full tuition waiver + stipend (demo data)",
                 "likely", "Student visa required; check embassy timelines (demo)."))
            for k, (dtype, days_out, cycle, desc) in enumerate(DEADLINES):
                conn.execute(
                    """INSERT INTO program_deadlines
                       (university_program_id, deadline_type, deadline_date,
                        deadline_date_confidence, cycle_label, description, source_url)
                       VALUES (?,?,?,?,?,?,?)""",
                    (f"demo-prog-{j}", dtype,
                     (today + datetime.timedelta(days=days_out + j)).isoformat(),
                     "firm", cycle, desc, "https://example.edu"))
        counts["professors"] = len(DEMO_PROFESSORS)
        counts["universities"] = len(unis)
        conn.commit()
        return counts
    finally:
        conn.close()


def reset_db(db_path) -> None:
    p = __import__("pathlib").Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        fp = p.with_name(p.name + suffix)
        if fp.exists():
            fp.unlink()
