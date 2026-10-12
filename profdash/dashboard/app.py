"""
profdash web UI.

Server-rendered Jinja2 + HTMX + Alpine.js + Tailwind. All data access goes
through profdash.dashboard.db — no ORM.

Display timezone comes from profile.toml (`timezone` key, default UTC).
Run with:  prof serve   (or)   uvicorn profdash.dashboard.app:app
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import paths
from ..profile import load_profile
from . import db
from .db import (
    APPLICATION_STATUS_COLORS,
    APPLICATION_STATUS_LABELS,
    APPLICATION_STATUS_TAXONOMY,
    COLUMN_LABELS,
    STATUS_TAXONOMY,
)

_profile = load_profile()
_LOCAL_TZ = _profile.tz


def _tz_label(tz: ZoneInfo) -> str:
    """Human-readable tz name+offset for the footer, e.g. 'UTC+03:30'."""
    from datetime import datetime as _dt
    off = _dt.now(tz).strftime("%z")
    sign = "+" if off[0] == "+" else "\u2212"
    pretty = f"{sign}{off[1:3]}:{off[3:]}"
    return f"{str(tz)} ({pretty})" if str(tz) != "UTC" else "UTC"


TZ_LABEL = _tz_label(_LOCAL_TZ)

app = FastAPI(title="profdash")


@app.on_event("startup")
def _upgrade_schema() -> None:
    """Databases from earlier versions get the research-area columns."""
    import sqlite3
    from ..schema import create_all
    try:
        path = db.db_path()
        if path.exists():
            conn = sqlite3.connect(str(path), timeout=10)
            try:
                create_all(conn)
            finally:
                conn.close()
    except Exception:
        pass  # surfaced later by the first query, with a clearer message

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")


def _to_local(value):
    """Convert a UTC datetime string to the configured local display time."""
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
        return dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
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

templates.env.filters["localtz"] = _to_local
templates.env.filters["tehran"] = _to_local  # legacy alias
templates.env.filters["days_until"] = _days_until
templates.env.globals["today"] = date.today().isoformat()
templates.env.globals["TZ_LABEL"] = TZ_LABEL
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
    followups = db.retry_once_on_lock(db.get_follow_up_professors,
                                      _profile.outreach.follow_up_days, ih)
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
        "areas": db.retry_once_on_lock(db.get_areas_for_professor, professor_id),
    }


@app.get("/professor/{professor_id}", response_class=HTMLResponse)
def professor_detail(request: Request, professor_id: str):
    ctx = _professor_detail_ctx(professor_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Professor not found")
    p = ctx["p"]
    parts = [x for x in (p.get("university"), p.get("location_city"),
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
def follow_ups_page(request: Request, days: int | None = None):
    threshold = days if days is not None else _profile.outreach.follow_up_days
    all_contacted = db.retry_once_on_lock(db.get_all_contacted_professors,
                                          _include_hidden(request))
    overdue = [p for p in all_contacted if p["days_waiting"] is not None and p["days_waiting"] > threshold]
    waiting = [p for p in all_contacted if p["days_waiting"] is None or p["days_waiting"] <= threshold]
    return templates.TemplateResponse(request, "follow_ups.html",
                                      {"overdue": overdue, "waiting": waiting, "days": threshold})


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
    return templates.TemplateResponse(request, "university_detail.html", {"u": detail})


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
        limit=700,
    )
    options = db.retry_once_on_lock(db.get_deadline_filter_options)
    return templates.TemplateResponse(request, "deadlines.html",
                                      {"rows": rows, "total": total,
                                       "options": options, "qp": qp})


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
def shortlist_page(request: Request, track: str = "both", min_score: int | None = None):
    floor = min_score if min_score is not None else _profile.outreach.min_score_shortlist
    rows = db.retry_once_on_lock(db.get_shortlist, track=track, min_score=floor,
                                 include_hidden=_include_hidden(request))
    return templates.TemplateResponse(request, "shortlist.html",
                                      {"rows": rows, "track": track, "min_score": floor})


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
