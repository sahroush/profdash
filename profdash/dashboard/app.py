"""
Professor Dashboard web UI.

Server-rendered Jinja2 + HTMX + Alpine.js + Tailwind. All data access goes
through dashboard.db — no ORM.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .db import (
    APPLICATION_EVENT_TYPES,
    APPLICATION_STATUS_COLORS,
    APPLICATION_STATUS_LABELS,
    APPLICATION_STATUS_TAXONOMY,
    COLUMN_LABELS,
    STATUS_TAXONOMY,
)

_TZ_TEHRAN = ZoneInfo("Asia/Tehran")

app = FastAPI(title="Professor Dashboard")

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")


def _to_tehran(value):
    """Convert a UTC datetime string to Tehran local display time."""
    if not value:
        return ""
    try:
        s = str(value).strip()
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_TZ_TEHRAN).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)[:16].replace("T", " ")


def _days_until(value):
    """Days from today until a YYYY-MM-DD string. Negative = past."""
    if not value:
        return None
    try:
        target = datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
        return (target - date.today()).days
    except (ValueError, TypeError):
        return None


# --- Professor outreach status colors (light-bg, dark-bg, light-text, dark-text)
STATUS_COLORS: dict[str, tuple[str, str, str, str]] = {
    "not_contacted": ("bg-gray-100", "dark:bg-gray-700/50", "text-gray-600", "dark:text-gray-300"),
    "new": ("bg-gray-100", "dark:bg-gray-700/50", "text-gray-600", "dark:text-gray-300"),
    "contacted": ("bg-blue-100", "dark:bg-blue-900/40", "text-blue-700", "dark:text-blue-300"),
    "no_response": ("bg-yellow-100", "dark:bg-yellow-900/40", "text-yellow-700", "dark:text-yellow-300"),
    "replied_template": ("bg-purple-100", "dark:bg-purple-900/40", "text-purple-700", "dark:text-purple-300"),
    "replied_interested": ("bg-green-100", "dark:bg-green-900/40", "text-green-700", "dark:text-green-300"),
    "replied_no_funding": ("bg-orange-100", "dark:bg-orange-900/40", "text-orange-700", "dark:text-orange-300"),
    "replied_rejected": ("bg-red-100", "dark:bg-red-900/40", "text-red-700", "dark:text-red-300"),
    "conditional_accept": ("bg-emerald-100", "dark:bg-emerald-900/40", "text-emerald-700", "dark:text-emerald-300"),
    "needs_follow_up": ("bg-amber-100", "dark:bg-amber-900/40", "text-amber-700", "dark:text-amber-300"),
    "withdrawn": ("bg-gray-200", "dark:bg-gray-700", "text-gray-500", "dark:text-gray-400"),
}

STATUS_LABELS: dict[str, str] = {
    "not_contacted": "Not Contacted",
    "new": "Not Contacted",
    "contacted": "Contacted",
    "no_response": "No Response",
    "replied_template": "Replied (Template)",
    "replied_interested": "Replied (Interested)",
    "replied_no_funding": "Replied (No Funding)",
    "replied_rejected": "Replied (Rejected)",
    "conditional_accept": "Conditional Accept",
    "needs_follow_up": "Needs Follow-up",
    "withdrawn": "Withdrawn",
}

REC_COLORS: dict[str, tuple[str, str, str, str]] = {
    "MUST APPLY": ("bg-green-100", "dark:bg-green-900/50", "text-green-800", "dark:text-green-300"),
    "GOOD FIT": ("bg-sky-100", "dark:bg-sky-900/50", "text-sky-800", "dark:text-sky-300"),
    "MEDIUM FIT": ("bg-amber-100", "dark:bg-amber-900/50", "text-amber-800", "dark:text-amber-300"),
    "BACKUP / LOW FIT": ("bg-gray-100", "dark:bg-gray-700/50", "text-gray-600", "dark:text-gray-300"),
    "DO NOT APPLY": ("bg-red-100", "dark:bg-red-900/50", "text-red-800", "dark:text-red-300"),
}

STAGE_LABELS: dict[str, str] = {
    "triaged": "Awaiting Review",
    "triaged_drop": "Triaged / Dropped",
    "hard_excluded": "Hard Excluded",
    "deep_reviewed": "Deep Reviewed",
}

templates.env.filters["tehran"] = _to_tehran
templates.env.filters["days_until"] = _days_until
templates.env.globals["today"] = date.today().isoformat()
templates.env.globals["STATUS_TAXONOMY"] = STATUS_TAXONOMY
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["STATUS_COLORS"] = STATUS_COLORS
templates.env.globals["REC_COLORS"] = REC_COLORS
templates.env.globals["STAGE_LABELS"] = STAGE_LABELS
templates.env.globals["APP_OPEN_TYPES"] = db.APPLICATION_OPEN_TYPES
templates.env.globals["get_hidden_countries"] = db.get_hidden_countries
templates.env.globals["get_show_hidden"] = db.get_show_hidden
templates.env.globals["APPLICATION_STATUS_TAXONOMY"] = APPLICATION_STATUS_TAXONOMY
templates.env.globals["APPLICATION_STATUS_LABELS"] = APPLICATION_STATUS_LABELS
templates.env.globals["APPLICATION_STATUS_COLORS"] = APPLICATION_STATUS_COLORS
templates.env.globals["COLUMN_LABELS"] = COLUMN_LABELS
templates.env.globals["APPLICATION_EVENT_TYPES"] = APPLICATION_EVENT_TYPES
templates.env.globals["get_favorite_universities"] = db.get_favorite_universities
templates.env.globals["is_favorite_university"] = db.is_favorite_university
templates.env.globals["get_university_tiers"] = db.get_university_tiers
templates.env.globals["TIER_STYLES"] = db.TIER_STYLES

# Map-page globals: SVG <title> lookup for hide-any-country support.
# NE title spellings differ from stored DB location_country values, so the client
# resolves a typed country name through these aliases before matching <title>s.
MAP_TITLE_ALIASES: dict[str, list[str]] = {
    "United States": ["United States of America", "Puerto Rico"],
    "United Kingdom": ["United Kingdom"],
    "Czech Republic": ["Czechia"],
    "South Korea": ["South Korea"],
    "North Korea": ["North Korea"],
    "Congo": ["Congo", "Dem. Rep. Congo"],
    "Macedonia": ["Macedonia"],
    "Hong Kong": ["Hong Kong"],
    "Macao": ["Macao"],
    "Singapore": ["Singapore"],
    "Iran": ["Iran"],
    "Taiwan": ["Taiwan"],
    "Palestine": ["Palestine"],
    "Western Sahara": ["W. Sahara"],
    "Ivory Coast": ["Côte d'Ivoire"],
    "Swaziland": ["eSwatini"],
    "South Sudan": ["S. Sudan"],
    "Democratic Republic of the Congo": ["Dem. Rep. Congo"],
    "Republic of the Congo": ["Congo"],
    "United Arab Emirates": ["United Arab Emirates"],
    "Central African Republic": ["Central African Rep."],
    "Equatorial Guinea": ["Eq. Guinea"],
    "Dominican Republic": ["Dominican Rep."],
    "Falkland Islands": ["Falkland Is."],
    "French Southern and Antarctic Lands": ["Fr. S. Antarctic Lands"],
    "Bosnia and Herzegovina": ["Bosnia and Herz."],
    "Solomon Islands": ["Solomon Is."],
    "East Timor": ["Timor-Leste"],
    "Laos": ["Laos"],
    "Vietnam": ["Vietnam"],
    "Burma": ["Myanmar"],
}

# Every title present in the vendored SVG (for the datalist suggestions).
from pathlib import Path as _Path
_svg_src = (_Path(__file__).resolve().parent / "templates" / "_world_map.html").read_text()
ALL_MAP_TITLES: list[str] = sorted(set(re.findall(r"<title>([^<]+)</title>", _svg_src)))

templates.env.globals["all_titles"] = ALL_MAP_TITLES
templates.env.globals["title_aliases"] = MAP_TITLE_ALIASES


# --- Health -----------------------------------------------------------------


# --- View-level country hiding ------------------------------------------------


def _include_hidden(request: Request) -> bool:
    return request.query_params.get("show_hidden") == "1"


@app.post("/settings/hidden-countries")
async def set_hidden_countries(request: Request):
    payload = await request.json()
    if "countries" in payload:
        countries = payload["countries"]
        if not isinstance(countries, list):
            raise HTTPException(status_code=400, detail="countries must be a list")
        db.set_hidden_countries([str(c) for c in countries])
    if "show_hidden" in payload:
        db.set_show_hidden(bool(payload["show_hidden"]))
    return {"ok": True, "hidden": db.get_hidden_countries(),
            "show_hidden": db.get_show_hidden()}


@app.get("/health")
def health():
    try:
        n = db.retry_once_on_lock(db.count_professors)
        return {"status": "ok", "professors": n}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    ih = _include_hidden(request)
    stats = db.retry_once_on_lock(db.get_pipeline_stats, ih)
    confirmations = db.retry_once_on_lock(db.get_pending_confirmations)
    followups = db.retry_once_on_lock(db.get_follow_up_professors, 21, ih)
    deadlines_rows = db.retry_once_on_lock(db.get_upcoming_application_deadlines, 8, ih)
    open_apps = db.retry_once_on_lock(db.get_open_applications, 8, ih)
    positions_rows, _ = db.retry_once_on_lock(db.get_positions, filter_status="new", limit=5,
                                              include_hidden=ih)
    tasks = db.retry_once_on_lock(db.get_agent_tasks, limit=6)

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "stats": stats,
            "confirmations": confirmations[:5],
            "confirmations_total": len(confirmations),
            "followups": followups[:5],
            "followups_total": len(followups),
            "deadlines": deadlines_rows,
            "open_apps": open_apps,
            "positions_new": positions_rows,
            "tasks": tasks,
        },
    )


# --- Professors table -------------------------------------------------------

PAGE_SIZE = 50


def _table_params(request: Request) -> dict:
    qp = request.query_params
    search = (qp.get("search") or "").strip()
    filters = {k: v for k, v in qp.items() if k in db.FILTERABLE_COLUMNS and v}
    sort = qp.get("sort") or "id"
    direction = "desc" if qp.get("direction") == "desc" else "asc"
    try:
        page = max(1, int(qp.get("page") or 1))
    except ValueError:
        page = 1
    return {"search": search, "filters": filters, "sort": sort,
            "direction": direction, "page": page,
            "show_hidden": "1" if qp.get("show_hidden") == "1" else ""}


def _build_qs(params: dict, **overrides) -> str:
    merged = {**params["filters"], "search": params["search"],
              "sort": params["sort"], "direction": params["direction"],
              "page": params["page"], "show_hidden": params.get("show_hidden", "")}
    for k, v in overrides.items():
        if v in (None, ""):
            merged.pop(k, None)
        else:
            merged[k] = v
    return urlencode({k: v for k, v in merged.items() if v not in ("", None)})


def _table_context(request: Request, params: dict, options=None) -> dict:
    offset = (params["page"] - 1) * PAGE_SIZE
    rows, total = db.retry_once_on_lock(
        db.list_professors,
        sort=params["sort"], direction=params["direction"],
        limit=PAGE_SIZE, offset=offset,
        search=params["search"], filters=params["filters"],
        include_hidden=_include_hidden(request),
    )
    stats = db.retry_once_on_lock(db.get_stats, params["search"], params["filters"],
                                  _include_hidden(request))
    if options is None:
        options = db.retry_once_on_lock(db.get_filter_options)
    pages = max(1, math.ceil(total / PAGE_SIZE))

    return {
        "rows": rows,
        "total": total,
        "pages": pages,
        "stats": stats,
        "options": options,
        "params": params,
        "qs": partial(_build_qs, params),
    }


@app.get("/professors", response_class=HTMLResponse)
def professors_page(request: Request):
    params = _table_params(request)
    options = db.retry_once_on_lock(db.get_filter_options)
    ctx = _table_context(request, params, options)
    ctx["filter_options_json"] = json.dumps(options)
    return templates.TemplateResponse(request, "professors.html", ctx)


@app.get("/table-rows")
def table_rows(request: Request):
    params = _table_params(request)
    return templates.TemplateResponse(request, "_table_response.html",
                                      _table_context(request, params))


@app.post("/professor/{professor_id}/status")
def professor_status(professor_id: str, request: Request, status: str = Form(...)):
    db.change_professor_status(professor_id, status)
    row = db.get_professor_by_id(professor_id)
    return templates.TemplateResponse(request, "_row.html", {"r": row})


@app.post("/bulk-status")
async def bulk_status(request: Request):
    payload = await request.json()
    ids = [str(i) for i in payload.get("ids", [])]
    status = payload.get("status")
    updated = db.bulk_change_status(ids, status)
    return {"updated": updated}


# --- Professor detail -------------------------------------------------------


def _professor_detail_ctx(professor_id: str) -> dict | None:
    prof = db.get_professor_by_id(professor_id)
    if not prof:
        return None
    return {
        "p": prof,
        "evidence": db.retry_once_on_lock(db.get_evidence_for_professor, professor_id),
        "history": db.retry_once_on_lock(db.get_status_history, professor_id),
        "papers": db.retry_once_on_lock(db.get_paper_recommendations, professor_id),
        "tasks": db.retry_once_on_lock(db.get_agent_tasks, professor_id=professor_id),
        "uni_ctx": db.retry_once_on_lock(
            db.get_professor_university_context, prof.get("university")),
        "draft": db.retry_once_on_lock(db.get_latest_email_draft, professor_id),
    }


@app.get("/professor/{professor_id}", response_class=HTMLResponse)
def professor_detail(request: Request, professor_id: str):
    ctx = _professor_detail_ctx(professor_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Professor not found")
    p = ctx["p"]
    parts = [x for x in (p.get("location_city"),
                         p.get("location_country")) if x]
    ctx["subtitle"] = " \u00b7 ".join(parts)
    return templates.TemplateResponse(request, "professor_detail.html", ctx)


@app.post("/professor/{professor_id}/notes")
def save_notes(request: Request, professor_id: str, notes: str = Form("")):
    db.update_professor_notes(professor_id, notes.strip())
    p = db.get_professor_by_id(professor_id)
    return templates.TemplateResponse(request, "_notes_card.html", {"p": p})


@app.post("/professor/{professor_id}/status-detail")
def status_detail(request: Request, professor_id: str, status: str = Form(...)):
    db.change_professor_status(professor_id, status)
    p = db.get_professor_by_id(professor_id)
    history = db.get_status_history(professor_id)
    return templates.TemplateResponse(request, "_status_card.html",
                                      {"p": p, "history": history})


@app.post("/professor/{professor_id}/task/{task_type}")
def create_task(request: Request, professor_id: str, task_type: str):
    if task_type not in ("fetch_papers", "draft_email"):
        raise HTTPException(status_code=400, detail="Invalid task type")
    try:
        db.create_agent_task(professor_id, task_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task type")
    tasks = db.retry_once_on_lock(db.get_agent_tasks, professor_id=professor_id)
    return templates.TemplateResponse(request, "_agent_section.html",
                                      {"tasks": tasks, "p": {"id": professor_id}})


# --- Confirmations (Gmail suggestion queue) ----------------------------------


@app.get("/confirmations", response_class=HTMLResponse)
def confirmations_page(request: Request):
    items = db.retry_once_on_lock(db.get_pending_confirmations)
    return templates.TemplateResponse(request, "confirmations.html", {"items": items})


def _confirmations_fragment(request: Request):
    items = db.retry_once_on_lock(db.get_pending_confirmations)
    return templates.TemplateResponse(request, "_confirmations_list.html", {"items": items})


@app.post("/confirmations/{history_id}/confirm")
def confirm_item(request: Request, history_id: int):
    ok = db.confirm_status_change(history_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Suggestion not found or already resolved")
    return _confirmations_fragment(request)


@app.post("/confirmations/{history_id}/reject")
def reject_item(request: Request, history_id: int):
    ok = db.reject_status_change(history_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Suggestion not found or already resolved")
    return _confirmations_fragment(request)


@app.post("/confirmations/{history_id}/edit")
def edit_item(request: Request, history_id: int, status: str = Form(...), note: str = Form("")):
    try:
        db.update_confirmation_status(history_id, status, note.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status")
    ok = db.confirm_status_change(history_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Suggestion not found or already resolved")
    return _confirmations_fragment(request)


# --- Follow-ups ---------------------------------------------------------------


@app.get("/follow-ups", response_class=HTMLResponse)
def follow_ups_page(request: Request, days: int = 21):
    all_contacted = db.retry_once_on_lock(db.get_all_contacted_professors,
                                          _include_hidden(request))
    overdue = db.retry_once_on_lock(db.get_follow_up_professors, days, _include_hidden(request))
    due_ids = {p["id"] for p in overdue}
    waiting = [p for p in all_contacted if p["id"] not in due_ids]
    return templates.TemplateResponse(request, "follow_ups.html",
                                      {"overdue": overdue, "waiting": waiting, "days": days})


# --- Universities ------------------------------------------------------------


@app.get("/universities", response_class=HTMLResponse)
def universities_page(request: Request):
    qp = request.query_params
    search = (qp.get("search") or "").strip()
    filters = {k: v for k, v in qp.items() if k in db.FILTERABLE_COLUMNS and v}
    groups = db.retry_once_on_lock(db.get_university_groups, search, filters,
                                   _include_hidden(request))
    options = db.retry_once_on_lock(db.get_filter_options)
    return templates.TemplateResponse(request, "universities.html",
                                      {"groups": groups, "options": options,
                                       "search": search, "filters": filters})


@app.get("/university/{slug}", response_class=HTMLResponse)
def university_detail(request: Request, slug: str):
    detail = db.retry_once_on_lock(db.get_university_detail, slug)
    if detail is None:
        profs = db.retry_once_on_lock(db.get_professors_by_university, slug)
        if not profs:
            raise HTTPException(status_code=404, detail="University not found")
        country = next((p["location_country"] for p in profs if p.get("location_country")), "")
        detail = {
            "university": profs[0]["university"],
            "normalized_name": slug,
            "location_country": country,
            "department_url": None,
            "funding_note": None,
            "programs": [],
            "deadlines": [],
            "professors": profs,
        }
    return templates.TemplateResponse(request, "university_detail.html", {"u": detail,
                                       "is_fav": db.is_favorite_university(detail["university"]) or db.is_favorite_university(detail["normalized_name"]),
                                       "uni_tier": db.get_university_tier(detail["normalized_name"]) or db.get_university_tier(detail["university"])})


@app.post("/university/{slug}/tier")
def university_set_tier(slug: str, tier: str = Form("")):
    """Assign/clear tier. Empty value clears. Redirect back to the uni page."""
    db.retry_once_on_lock(db.set_university_tier, slug, tier or None)
    return RedirectResponse(f"/university/{quote(slug)}", status_code=303)


@app.post("/university/{slug}/favorite")
def university_toggle_favorite(slug: str):
    db.retry_once_on_lock(db.toggle_favorite_university, slug)
    return RedirectResponse(f"/university/{slug}", status_code=303)


# --- Tier board (/tiers) --------------------------------------------------------

def _tier_board_ctx(request: Request) -> dict:
    board = db.retry_once_on_lock(db.get_tier_board, _include_hidden(request))
    tiers = {t: board.get(t, []) for t in db.UNIVERSITY_TIERS}
    stats = {
        "tiered": sum(len(v) for v in tiers.values()),
        "applied": sum(u["applied"] for v in tiers.values() for u in v),
    }
    per_tier = {}
    for t, unis in tiers.items():
        per_tier[t] = {
            "universities": len(unis),
            "contacted": sum(u["contacted_count"] for u in unis),
            "applied": sum(u["applied"] for u in unis),
        }
    return {"tiers": tiers, "per_tier": per_tier, **stats}


@app.get("/tiers", response_class=HTMLResponse)
def tiers_page(request: Request):
    return templates.TemplateResponse(request, "tiers.html", _tier_board_ctx(request))


@app.get("/tiers-board")
def tiers_board_fragment(request: Request):
    """HTMX fragment: tier columns after a change."""
    return templates.TemplateResponse(request, "_tier_columns.html", _tier_board_ctx(request))




# --- Countries: world map + country detail ------------------------------------


def _country_fill(contacted: int, prof_count: int, hidden: bool = False) -> str:
    """Tint for a country path. Hidden countries get red; visible ones lerp
    surface-2 toward accent by the CONTACTED RATIO (contacted / total profs).
    Empty string = base fill (no data or 0% contacted)."""
    if hidden:
        # muted red, flat regardless of count (dark-theme red-500 family)
        return "rgb(127 45 55)"
    if prof_count <= 0:
        return ""
    t = min(1.0, contacted / prof_count) ** 0.5  # sqrt curve: small ratios still visible
    if t <= 0:
        return ""
    # mix rgb(var(--c-surface-2)) toward rgb(var(--c-accent)) in sRGB channel space
    base = (26, 30, 39)      # dark theme surface-2
    accent = (96, 165, 250)  # dark theme accent
    mix = tuple(round(b + (a - b) * t) for b, a in zip(base, accent))
    return f"rgb({mix[0]} {mix[1]} {mix[2]})"


@app.get("/map", response_class=HTMLResponse)
def map_page(request: Request):
    ih = _include_hidden(request)
    # Always compute stats WITHOUT the hidden filter so hidden countries render
    # red on the map; ?show_hidden=1 additionally lists them in the side tables.
    stats = db.retry_once_on_lock(db.get_country_stats, True)  # unfiltered: hidden show red
    hidden_set = set(db.get_hidden_countries())
    mapped, unmapped = [], []
    for s in stats:
        s["hidden"] = s["country"] in hidden_set
        if s["hidden"]:
            s["href"] = None  # honor the hiding: no link while hidden
        else:
            s["href"] = "/country/" + quote(s["country"])
        s["fill"] = _country_fill(s["contacted"], s["prof_count"], s["hidden"])
        if s["iso"]:
            mapped.append(s)
        elif not s["hidden"]:
            unmapped.append(s)
    # Two stored spellings can share one ISO shape (UK / United Kingdom -> GB):
    # keep the entry with more professors so the map links to the main page.
    links: dict = {}
    for s in mapped:
        if s["hidden"]:
            continue  # red fill only, no link
        cur = links.get(s["iso"])
        if cur is None or s["prof_count"] > cur["n"]:
            links[s["iso"]] = {"href": s["href"], "fill": s["fill"],
                               "label": s["country"], "n": s["prof_count"],
                               "c": s["contacted"],
                               "pct": round(100 * s["contacted"] / s["prof_count"])
                                      if s["prof_count"] else 0}
    # Red painting happens client-side by matching SVG <title> names against the
    # raw hidden list — covers arbitrary (non-indexed) countries too.
    return templates.TemplateResponse(request, "map.html",
                                      {"stats": stats, "mapped": mapped,
                                       "unmapped": unmapped, "links": links,
                                       "hidden_names": db.get_hidden_countries(),
                                       "show_hidden": ih})


@app.get("/country/{name}", response_class=HTMLResponse)
def country_page(request: Request, name: str):
    include_hidden = _include_hidden(request)
    detail = db.retry_once_on_lock(db.get_country_detail, name, include_hidden)
    if detail is None:
        raise HTTPException(status_code=404, detail="Country not found")
    info = db.retry_once_on_lock(db.get_country_info, name)
    # group programs by university for the universities card
    unis: dict[str, list[dict]] = {}
    for prog in detail["programs"]:
        unis.setdefault(prog["university"], []).append(prog)
    return templates.TemplateResponse(request, "country.html",
                                      {"c": detail, "info": info, "unis": unis,
                                       "show_hidden": include_hidden})


# --- Deadlines ----------------------------------------------------------------


@app.get("/deadlines", response_class=HTMLResponse)
def deadlines_page(request: Request):
    qp = request.query_params
    rows, total = db.retry_once_on_lock(
        db.get_all_deadlines,
        search=(qp.get("search") or "").strip(),
        filter_university=qp.get("university") or "",
        filter_track=qp.get("track") or "",
        filter_deadline_type=qp.get("deadline_type") or "",
        filter_confidence=qp.get("confidence") or "",
        upcoming_only=qp.get("upcoming", "1") == "1",
        favorites_only=qp.get("favorites", "") == "1",
        limit=700,
    )
    options = db.retry_once_on_lock(db.get_deadline_filter_options)
    return templates.TemplateResponse(request, "deadlines.html",
                                      {"rows": rows, "total": total,
                                       "options": options, "qp": qp,
                                       "favorites": db.get_favorite_universities()})


@app.get("/open-applications", response_class=HTMLResponse)
def open_applications_page(request: Request):
    ih = _include_hidden(request)
    rows = db.retry_once_on_lock(db.get_open_applications, 1000, ih)
    return templates.TemplateResponse(request, "open_applications.html",
                                      {"rows": rows, "total": len(rows), "qp": request.query_params})


# --- Positions ----------------------------------------------------------------


@app.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request):
    qp = request.query_params
    rows, total = db.retry_once_on_lock(
        db.get_positions,
        search=(qp.get("search") or "").strip(),
        filter_track=qp.get("track") or "",
        filter_recommendation=qp.get("recommendation") or "",
        filter_status=qp.get("status") or "",
        filter_institution=qp.get("institution") or "",
        filter_application_status=qp.get("application_status") or "",
        limit=100, include_hidden=_include_hidden(request),
    )
    options = db.retry_once_on_lock(db.get_position_filter_options)
    return templates.TemplateResponse(request, "positions.html",
                                      {"rows": rows, "total": total,
                                       "options": options, "qp": qp})


@app.post("/position/{position_id}/application-status")
def position_application_status(request: Request, position_id: str,
                                application_status: str = Form(...)):
    if not db.update_position_application_status(position_id, application_status):
        raise HTTPException(status_code=404, detail="Position not found")
    row = next((r for r in db.get_positions(limit=10000)[0] if r["id"] == position_id), None)
    return templates.TemplateResponse(request, "_position_row.html", {"p": row})


# --- Shortlist ------------------------------------------------------------------


@app.get("/shortlist", response_class=HTMLResponse)
def shortlist_page(request: Request, track: str = "both", min_score: int = 7):
    rows = db.retry_once_on_lock(db.get_shortlist, track=track, min_score=min_score,
                                 include_hidden=_include_hidden(request))
    return templates.TemplateResponse(request, "shortlist.html",
                                      {"rows": rows, "track": track, "min_score": min_score})


@app.post("/professor/{professor_id}/mark-contacted")
def mark_contacted(request: Request, professor_id: str,
                   track: str = Form("both"), min_score: int = Form(7)):
    try:
        db.change_professor_status(professor_id, "contacted")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status")
    rows = db.retry_once_on_lock(db.get_shortlist, track=track, min_score=min_score,
                                 include_hidden=_include_hidden(request))
    return templates.TemplateResponse(request, "_shortlist_table.html",
                                      {"rows": rows, "track": track, "min_score": min_score})


# --- Pipeline stats --------------------------------------------------------------


@app.get("/pipeline-stats", response_class=HTMLResponse)
def pipeline_stats_page(request: Request):
    stats = db.retry_once_on_lock(db.get_pipeline_stats, _include_hidden(request))
    return templates.TemplateResponse(request, "pipeline_stats.html", {"stats": stats})


# --- Global search -----------------------------------------------------------------


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = ""):
    # Global search is a lookup tool: it deliberately ignores view-level
    # country hiding so you can find people regardless of browse preferences.
    results = db.retry_once_on_lock(db.get_global_search_results, q.strip(), True)
    n = sum(len(v) for v in results.values())
    return templates.TemplateResponse(request, "search.html",
                                      {"q": q.strip(), "results": results, "n": n})


# --- Activity feed -------------------------------------------------------------------


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, page: int = 1, status: str = ""):
    per_page = 30
    offset = (max(1, page) - 1) * per_page
    tasks = db.retry_once_on_lock(db.get_agent_tasks, status=status or None,
                                  limit=per_page + 1, offset=offset)
    has_more = len(tasks) > per_page
    return templates.TemplateResponse(request, "activity.html",
                                      {"tasks": tasks[:per_page], "page": max(1, page),
                                       "status": status, "has_more": has_more})


# --- Applications tracker -------------------------------------------------------


@app.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request):
    rows = db.retry_once_on_lock(db.get_applications)
    program_options = db.retry_once_on_lock(db.get_program_picker_options)
    return templates.TemplateResponse(request, "applications.html",
                                      {"rows": rows,
                                       "program_options": program_options})


@app.post("/applications/create")
def applications_create(request: Request, name: str = Form(...),
                        url: str = Form(""), status: str = Form("not_applied"),
                        notes: str = Form(""), program_id: str = Form(""),
                        semester: str = Form("")):
    if not url.strip() and program_id:
        autofill = db.get_program_autofill(program_id)
        if autofill and autofill.get("department_url"):
            url = autofill["department_url"]
    try:
        app_id = db.create_application(name, url, status, notes,
                                       program_id=program_id, semester=semester)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status")
    if program_id and semester:
        db.sync_application_events(app_id, program_id, semester)
    return RedirectResponse("/applications", status_code=303)


@app.post("/applications/{app_id}/update")
def applications_update(request: Request, app_id: str, name: str = Form(...),
                        url: str = Form(""), notes: str = Form(""),
                        program_id: str = Form(""), semester: str = Form("")):
    if not url.strip() and program_id:
        autofill = db.get_program_autofill(program_id)
        if autofill and autofill.get("department_url"):
            url = autofill["department_url"]
    if not db.update_application(app_id, name, url, notes,
                                 program_id=program_id, semester=semester):
        raise HTTPException(status_code=404, detail="Application not found")
    if program_id and semester:
        db.sync_application_events(app_id, program_id, semester)
    return RedirectResponse("/applications", status_code=303)


@app.post("/applications/{app_id}/delete")
def applications_delete(request: Request, app_id: str):
    db.delete_application(app_id)
    return RedirectResponse("/applications", status_code=303)


@app.get("/applications/prof-search")
def applications_prof_search(q: str = ""):
    """Narrowed professor list for the add-professor datalist."""
    return {"profs": db.get_application_professor_options(search=q.strip())}


@app.get("/applications/program-semesters")
def applications_program_semesters(program_id: str):
    return {"semesters": db.get_semester_options(program_id)}


@app.get("/applications/program-autofill")
def applications_program_autofill(program_id: str, semester: str = ""):
    """Program URL + what events would sync for a program+cycle."""
    data = db.get_program_autofill(program_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Program not found")
    deadlines = data["deadlines"]
    if semester:
        deadlines = [d for d in deadlines if d.get("cycle_label") == semester]
    return {"department_url": data.get("department_url"),
            "university": data.get("university"),
            "program_name": data.get("program_name"),
            "deadlines": deadlines}


@app.post("/applications/{app_id}/status")
def applications_status(request: Request, app_id: str, application_status: str = Form(...)):
    if not db.update_application_status(app_id, application_status):
        raise HTTPException(status_code=400, detail="Invalid status")
    rows = db.retry_once_on_lock(db.get_applications)
    row = next((r for r in rows if r["id"] == app_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    return templates.TemplateResponse(request, "_application_tbody.html",
                                      {"a": row,
                                       "program_options": db.retry_once_on_lock(db.get_program_picker_options)})


@app.post("/applications/{app_id}/professors/add")
def applications_prof_add(request: Request, app_id: str, professor_id: str = Form(...)):
    if not db.add_application_professor(app_id, professor_id.strip()):
        raise HTTPException(status_code=400, detail="Unknown professor or application")
    return RedirectResponse("/applications", status_code=303)


@app.post("/applications/{app_id}/professors/{professor_id}/remove")
def applications_prof_remove(request: Request, app_id: str, professor_id: str):
    db.remove_application_professor(app_id, professor_id)
    return RedirectResponse("/applications", status_code=303)


@app.post("/applications/{app_id}/events")
def applications_event_add(request: Request, app_id: str, event_type: str = Form(...),
                           event_date: str = Form(...), note: str = Form("")):
    if not db.add_application_event(app_id, event_type, event_date, note):
        raise HTTPException(status_code=400, detail="Invalid event")
    return RedirectResponse("/applications", status_code=303)


@app.post("/applications/{app_id}/events/{event_id}/delete")
def applications_event_delete(request: Request, app_id: str, event_id: str):
    db.delete_application_event(app_id, event_id)
    return RedirectResponse("/applications", status_code=303)
