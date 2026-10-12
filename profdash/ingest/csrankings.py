"""Import CS faculty from the CSRankings dataset.

CSRankings (https://github.com/emeryberger/CSRankings, MIT-licensed) tracks
~10k active US/EU/Asia CS faculty across top venues. Its per-letter CSVs
give name / affiliation / homepage / Google Scholar id / ORCID;
institutions.csv maps affiliation -> region + ISO country; dblp-aliases.csv
resolves DBLP name variants.

Names in csrankings.csv are exactly DBLP author names, including
disambiguation numbers ("Aaron Roth 0001") — which is what makes DBLP
queries work without extra resolution.

Usage:
    prof import csrankings [--country "Germany" ...] [--region europe]
                           [--area ai --area security | --area systems]
                           [--area-years N] [--limit N] [--refresh] [--db PATH]

Research areas: generated-author-info.csv gives per-author, per-conference
paper counts. They are rolled up into the 27 CSRankings areas (see
profdash.areas); a professor's primary area is the one with the highest
author-adjusted paper score over the last N years (default 10).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import sqlite3
import time
from pathlib import Path

import httpx

from .. import areas as areas_mod

BASE_URL = "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages"
LETTER_FILES = [f"csrankings-{c}.csv" for c in "abcdefghijklmnopqrstuvwxyz"]
AUTHOR_INFO_FILE = "generated-author-info.csv"
NEEDED_FILES = LETTER_FILES + ["institutions.csv", "countries.csv", "dblp-aliases.csv",
                               AUTHOR_INFO_FILE]
DEFAULT_AREA_YEARS = 10

# ISO alpha-2 -> country display name lives in countries.csv via alpha_2.
REGION_NAMES = {
    "northamerica": "North America",
    "southamerica": "South America",
    "europe": "Europe",
    "asia": "Asia",
    "africa": "Africa",
    "oceania": "Oceania",
    "middleeast": "Middle East",
}

# Some affiliations in csrankings.csv are abbreviations not present in
# institutions.csv; map common ones so country assignment doesn't fail.
ABBREV_INSTITUTIONS = {
    "MIT": "Massachusetts Institute of Technology",
    "UMass Amherst": "University of Massachusetts Amherst",
    "U. Texas Austin": "University of Texas at Austin",
    "U. Maryland": "University of Maryland, College Park",
    "U. Illinois Urbana-Champaign": "University of Illinois at Urbana-Champaign",
    "U. Wisconsin-Madison": "University of Wisconsin-Madison",
}


def cache_dir(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
    else:
        base = Path.home() / ".cache" / "profdash" / "csrankings"
        p = base.resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(name: str, university: str) -> str:
    raw = f"{name}-{university}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or hashlib.sha1(raw.encode()).hexdigest()[:12]


def refresh_cache(cache: Path, *, client: httpx.Client | None = None,
                  log=print) -> None:
    """Download any missing/older-than-a-week dataset files."""
    own_client = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        for fname in NEEDED_FILES:
            dst = cache / fname
            if dst.exists() and (time.time() - dst.stat().st_mtime) < 7 * 86400:
                continue
            url = f"{BASE_URL}/{fname}"
            log(f"  downloading {fname} ...")
            r = client.get(url)
            r.raise_for_status()
            tmp = dst.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(dst)
    finally:
        if own_client:
            client.close()


def load_datasets(cache: Path) -> tuple[dict[str, dict], dict[str, str], list[dict]]:
    """Returns (institutions by name, alias->canonical author, people rows)."""
    institutions: dict[str, dict] = {}
    with open(cache / "institutions.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            institutions[row["institution"].strip()] = {
                "region": row["region"].strip(),
                "countryabbrv": row["countryabbrv"].strip().lower(),
                "homepage": row.get("homepage", "").strip(),
            }

    aliases: dict[str, str] = {}
    alias_file = cache / "dblp-aliases.csv"
    if alias_file.exists():
        with open(alias_file, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                aliases[row["alias"]] = row["name"]

    countries: dict[str, str] = {}
    cfile = cache / "countries.csv"
    if cfile.exists():
        with open(cfile, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                countries[row["alpha_2"].strip().lower()] = row["name"].strip()

    people: list[dict] = []
    seen_names: set[str] = set()
    for fname in LETTER_FILES:
        fp = cache / fname
        if not fp.exists():
            continue
        text = fp.read_text(encoding="utf-8", errors="replace")
        # CSRankings letter files sometimes repeat a header mid-file.
        text = "\n".join(
            line for i, line in enumerate(text.splitlines())
            if i == 0 or not line.startswith("name,affiliation")
        )
        for row in csv.DictReader(io.StringIO(text)):
            name = (row.get("name") or "").strip()
            affil = (row.get("affiliation") or "").strip()
            if not name or not affil:
                continue
            key = f"{name}|{affil}"
            if key in seen_names:
                continue
            seen_names.add(key)
            people.append({
                "name": name,
                "affiliation": affil,
                "homepage": (row.get("homepage") or "").strip(),
                "scholarid": (row.get("scholarid") or "").strip(),
                "orcid": (row.get("orcid") or "").strip(),
            })
    return institutions, aliases, people


def load_area_scores(cache: Path, *, years: int | None = DEFAULT_AREA_YEARS,
                     this_year: int | None = None
                     ) -> dict[tuple[str, str], dict[str, float]]:
    """(name, affiliation) -> {area code: author-adjusted paper score}.

    Only conferences CSRankings maps to an area are counted. `years` limits
    the window to the most recent N years (None = all time).
    """
    fp = cache / AUTHOR_INFO_FILE
    out: dict[tuple[str, str], dict[str, float]] = {}
    if not fp.exists():
        return out
    min_year = None
    if years:
        min_year = (this_year or dt.date.today().year) - years + 1
    with open(fp, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            area = areas_mod.CONF_TO_AREA.get((row.get("area") or "").strip())
            if area is None:
                continue
            try:
                year = int(row["year"])
                score = float(row["adjustedcount"])
            except (KeyError, TypeError, ValueError):
                continue
            if min_year is not None and year < min_year:
                continue
            key = ((row.get("name") or "").strip(), (row.get("dept") or "").strip())
            per = out.setdefault(key, {})
            per[area] = per.get(area, 0.0) + score
    return out


def store_areas(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Write professor_areas + primary area/group for every row with area data.

    Area data is derived metadata (always refreshed), unlike scoring and
    outreach columns which belong to the user.
    """
    n = 0
    for r in rows:
        scores = r.get("area_scores") or {}
        if not scores:
            continue
        pid = _slug(r["name"], r["affiliation"])
        conn.execute("DELETE FROM professor_areas WHERE professor_id=?", (pid,))
        conn.executemany(
            "INSERT INTO professor_areas(professor_id, area, score) VALUES (?,?,?)",
            [(pid, code, round(v, 3)) for code, v in scores.items()])
        prim = areas_mod.primary_area(scores)
        conn.execute("UPDATE professors SET area=?, area_group=? WHERE id=?",
                     (prim.title, prim.group, pid))
        n += 1
    return n


def resolve_country(institutions: dict, countries: dict[str, str],
                    affiliation: str) -> tuple[str, str]:
    """-> (country_name, region_name)"""
    inst = institutions.get(affiliation)
    if inst is None and affiliation in ABBREV_INSTITUTIONS:
        inst = institutions.get(ABBREV_INSTITUTIONS[affiliation])
    if inst is None:
        return "", ""
    code = inst.get("countryabbrv", "")
    country = countries.get(code, code.upper())
    region = REGION_NAMES.get(inst.get("region", ""), inst.get("region", ""))
    return country, region


def upsert_professors(conn: sqlite3.Connection, rows: list[dict], *,
                      now: str, log=print) -> dict:
    """Insert new professors / refresh metadata on existing ones.

    Never touches scoring columns or outreach state — those belong to the
    user's pipeline. Existing rows only get homepage/scholar/orcid/country
    refreshed when they were empty before.
    """
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    for r in rows:
        pid = _slug(r["name"], r["affiliation"])
        existing = conn.execute(
            "SELECT homepage, scholar, orcid, location_city FROM professors WHERE id=?",
            (pid,)).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO professors
                   (id, created_at, updated_at, professor, university,
                    location_country, homepage, dblp_url, orcid, scholar,
                    status, stage, last_csv_sync_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'not_contacted', 'triaged', ?)""",
                (pid, now, now, r["name"], r["affiliation"],
                 r["country"], r["homepage"] or None,
                 _dblp_search_url(r["name"]), r["orcid"] or None,
                 r["scholarid"] or None, now))
            stats["inserted"] += 1
        else:
            sets, vals = ["updated_at=?", "last_csv_sync_at=?"], [now, now]
            cur_homepage, cur_scholar, cur_orcid, cur_country = (
                conn.execute(
                    """SELECT homepage, scholar, orcid, location_country
                       FROM professors WHERE id=?""", (pid,)).fetchone())
            if not cur_homepage and r["homepage"]:
                sets.append("homepage=?"); vals.append(r["homepage"])
            if not cur_scholar and r["scholarid"]:
                sets.append("scholar=?"); vals.append(r["scholarid"])
            if not cur_orcid and r["orcid"]:
                sets.append("orcid=?"); vals.append(r["orcid"])
            if not cur_country and r["country"]:
                sets.append("location_country=?"); vals.append(r["country"])
            conn.execute(f"""UPDATE professors SET {', '.join(sets)}
                             WHERE id=?""", (*vals, pid))
            stats["updated"] += 1
    return stats


def _dblp_search_url(name: str) -> str:
    clean = re.sub(r"\s+\d+$", "", name)  # drop DBLP disambiguation number
    from urllib.parse import quote
    return f"https://dblp.org/search?q={quote(clean)}"


def run_import(db_path: Path, cache_dir_arg: str | None = None, *,
               countries_filter: list[str] | None = None,
               regions_filter: list[str] | None = None,
               areas_filter: list[str] | None = None,
               area_years: int | None = DEFAULT_AREA_YEARS,
               limit: int | None = None, refresh: bool = False,
               log=print) -> dict:
    """Full import pass. Returns stats for CLI display."""
    from ..schema import create_all

    cache = cache_dir(cache_dir_arg)
    refresh_cache(cache, log=log)

    institutions, _aliases, people = load_datasets(cache)
    countries: dict[str, str] = {}
    cfile = cache / "countries.csv"
    if cfile.exists():
        with open(cfile, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                countries[row["alpha_2"].strip().lower()] = row["name"].strip()

    want_countries = {c.lower() for c in (countries_filter or [])}
    want_regions = {r.lower() for r in (regions_filter or [])}
    want_areas = {a.code for tok in (areas_filter or [])
                  for a in areas_mod.resolve(tok)}
    area_scores = load_area_scores(cache, years=area_years)

    enriched = []
    no_country = 0
    for p in people:
        country, region = resolve_country(institutions, countries, p["affiliation"])
        if want_countries and country.lower() not in want_countries:
            continue
        if want_regions and region.lower() not in want_regions:
            continue
        scores = area_scores.get((p["name"], p["affiliation"]), {})
        if want_areas:
            prim = areas_mod.primary_area(scores)
            if prim is None or prim.code not in want_areas:
                continue
        if not country:
            no_country += 1
        enriched.append({**p, "country": country, "region": region,
                         "area_scores": scores})

    if limit:
        enriched = enriched[:limit]

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        create_all(conn)
        stats = upsert_professors(conn, enriched, now=now, log=log)
        stats["areas_assigned"] = store_areas(conn, enriched)
        conn.commit()
    finally:
        conn.close()

    log(f"  ({no_country} rows had unmapped affiliations — imported without country)")
    return {"total_rows": len(enriched), **stats,
            "cache": str(cache)}
