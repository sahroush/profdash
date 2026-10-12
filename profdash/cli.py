"""`prof` — the profdash command line interface.

Typical first run:
    mkdir my-outreach && cd my-outreach
    prof init --demo          # or: prof import csrankings
    prof serve                # open http://localhost:8000
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import webbrowser
from pathlib import Path

from . import paths
from .profile import load_profile


def _db_arg(args) -> Path:
    return paths.find_db(getattr(args, "db", None))


# --- subcommands ---------------------------------------------------------------


def cmd_init(args):
    db_path = paths.suggest_db(create_parent=True)
    if db_path.exists() and not args.force:
        print(f"Database already exists at {db_path} (use --force to re-init schema).")
    else:
        from .schema import create_all
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        create_all(conn)
        conn.close()
        print(f"Created database: {db_path}")

    profile_path = Path.cwd() / "profile.toml"
    if not profile_path.exists():
        example = Path(__file__).resolve().parent / "profiles" / "example.toml"
        try:
            profile_path.write_text(example.read_text())
            print(f"Copied starter profile: {profile_path}")
        except OSError:
            print("Could not copy example profile (installed as wheel?). "
                  "See docs/setup.md.")
    else:
        print(f"Profile already exists: {profile_path}")

    if args.demo:
        from .demo import seed_demo
        counts = seed_demo(db_path)
        print(f"Seeded demo data: {counts['professors']} professors, "
              f"{counts['universities']} universities.")
        print("(Wipe later with `prof demo --reset`.)")
    else:
        print("\nNext steps:")
        print("  prof import csrankings      # pull real CS faculty data")
        print("  prof serve                  # start the dashboard")


def cmd_demo(args):
    db_path = _db_arg(args)
    if args.reset:
        from .demo import reset_db
        reset_db(db_path)
        print("Demo database wiped.")
        return
    from .demo import seed_demo
    counts = seed_demo(db_path)
    print(f"Seeded demo data: {counts}")


def cmd_import(args):
    db_path = _db_arg(args)
    from .ingest import csrankings
    from . import areas
    for tok in args.area or []:
        try:
            areas.resolve(tok)
        except ValueError as e:
            raise SystemExit(f"error: {e}")
    stats = csrankings.run_import(
        db_path,
        countries_filter=args.country or None,
        regions_filter=args.region or None,
        areas_filter=args.area or None,
        area_years=args.area_years or None,
        limit=args.limit,
        refresh=args.refresh,
        log=print,
    )
    print(f"\nImported: {stats['total_rows']} rows "
          f"({stats['inserted']} new, {stats['updated']} refreshed)")
    print(f"Research area assigned to {stats['areas_assigned']} professors")
    print(f"Dataset cache: {stats['cache']}")


def cmd_areas(args):
    from . import areas
    for group in areas.GROUPS:
        print(f"\n{group}")
        for a in areas.areas_in_group(group):
            confs = ", ".join(c.upper() for c in a.confs)
            if a.next_tier:
                confs += " (+ " + ", ".join(c.upper() for c in a.next_tier) + ")"
            print(f"  {a.code:<14} {a.title:<14} {confs}")
    print("\nUse with: prof import csrankings --area <code|name|group> ...")


def cmd_serve(args):
    import os
    import uvicorn
    # validate DB up-front with a friendly error
    db_path = paths.find_db(args.db)
    if args.db:
        os.environ["PROF_DB_PATH"] = str(db_path)
    url = f"http://{args.host}:{args.port}"
    print(f"profdash serving at {url}  (Ctrl+C to stop)")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run("profdash.dashboard.app:app",
                host=args.host, port=args.port, log_level="warning")


def cmd_worker_tasks(args):
    from .workers import tasks
    db_path = _db_arg(args)
    if args.status:
        for status, n in tasks.queue_status(db_path):
            print(f"  {status}: {n}")
        return
    processed = tasks.process_batch(db_path, batch_size=args.batch_size)
    tasks.log(f"Done. Processed {processed} task(s).")


def cmd_worker_gmail(args):
    from .workers import gmail_scan
    gmail_scan.main(db_path=_db_arg(args), profile=load_profile())


def cmd_setup_gmail(args):
    try:
        from .gmail import client
    except Exception as e:  # pragma: no cover
        print(f"Google libraries missing: {e}\n"
              f"Run: pip install profdash[gmail]")
        sys.exit(1)
    if not args.client_secret:
        print("First-time Gmail setup:\n"
              "  1. Go to https://console.cloud.google.com/apis/credentials\n"
              "  2. Create an OAuth client ID of type 'Desktop app'\n"
              "  3. Download the client secret JSON\n"
              "  4. Run: prof setup-gmail --client-secret /path/to/secret.json\n"
              "\nScopes granted: readonly + modify + compose (drafts only; "
              "nothing is ever sent automatically).")
        return
    token_path = client.run_oauth_flow(args.client_secret)
    print(f"Gmail token saved: {token_path}")
    print("Try it: prof worker gmail-scan --dry-run")


def cmd_digest(args):
    from .scoring import digest as digest_mod
    db_path = _db_arg(args)
    profile = load_profile()
    strong = set(profile.venues.strong) or None
    moderate = set(profile.venues.moderate) or None
    out_dir = Path(args.out) if args.out else None
    results = digest_mod.fetch_digests(args.ids, db_path,
                                       strong=strong, moderate=moderate)
    for pid, text in results.items():
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{pid}.digest").write_text(text)
            print(f"wrote {out_dir}/{pid}.digest")
        else:
            print(text)
            print("=" * 72)


JUDGMENT_TEMPLATE = {
    "professor_id": "<paste professor id here>",
    "fit_tier": "Strong|Moderate|Lower|Drop",
    "phd_recommendation": "MUST APPLY|GOOD FIT|MEDIUM FIT|BACKUP / LOW FIT|DO NOT APPLY",
    "final_score_phd": 7,
    "phd_why": ">20 chars explaining the call, citing fetched facts",
    "phd_funding_confidence": "likely|confirmed|uncertain|unknown",
    "masters_recommendation": None,
    "final_score_masters": None,
    "masters_why": None,
    "masters_funding_confidence": None,
    "evidence": [
        {"field_name": "research_fit",
         "claim": "what you concluded",
         "source_type": "dblp|web|email",
         "source_url": "https://...",
         "quote": "verbatim quote from the source"} ,
    ],
}


def cmd_apply(args):
    from .scoring.apply import JudgmentError, apply_file
    db_path = _db_arg(args)
    try:
        pid = apply_file(db_path, args.judgment)
    except JudgmentError as e:
        print(f"invalid judgment: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Applied judgment to {pid} (stage -> deep_reviewed)")


def cmd_audit(args):
    from .audit import audit as audit_fn
    for line in audit_fn(_db_arg(args)):
        print(line)


def cmd_backup(args):
    from .backup import backup
    backup(_db_arg(args), dest_dir=args.dest, keep=args.keep)


def cmd_migrate(args):
    import sqlite3
    from .migrations import run_migrations
    db_path = _db_arg(args)
    conn = sqlite3.connect(str(db_path))
    applied = run_migrations(conn)
    conn.close()
    print(f"Migrations applied: {', '.join(applied) if applied else '(none needed)'}")


# --- parser ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="prof",
        description="profdash — self-hosted professor outreach tracker")
    ap.add_argument("--version", action="store_true", help="print version and exit")

    sub = ap.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="create DB + profile in this directory")
    p_init.add_argument("--demo", action="store_true", help="also seed demo data")
    p_init.add_argument("--force", action="store_true", help="(re)create schema even if DB exists")
    p_init.set_defaults(fn=cmd_init)

    p_demo = sub.add_parser("demo", help="seed/reset demo data")
    p_demo.add_argument("--reset", action="store_true", help="wipe the database instead")
    p_demo.add_argument("--db", help="path to profdash.sqlite")
    p_demo.set_defaults(fn=cmd_demo)

    p_imp = sub.add_parser("import", help="import professors from a dataset")
    imp_sub = p_imp.add_subparsers(dest="dataset")
    p_cs = imp_sub.add_parser("csrankings", help="CSRankings faculty dataset (~10k CS faculty)")
    p_cs.add_argument("--country", action="append",
                      help='filter by country name, e.g. --country "South Korea" (repeatable)')
    p_cs.add_argument("--region", action="append",
                      help="filter by region: europe|asia|northamerica|... (repeatable)")
    p_cs.add_argument("--area", action="append",
                      help="keep only professors whose primary area matches: an area code/name "
                           "(vision, sec, 'Comp. Bio', ...) or a group "
                           "(group:ai|systems|theory|interdisciplinary); repeatable. "
                           "See `prof areas`")
    p_cs.add_argument("--area-years", type=int, default=10,
                      help="years of publications used to pick the primary area "
                           "(default 10; 0 = all time)")
    p_cs.add_argument("--limit", type=int, help="cap imported rows (testing)")
    p_cs.add_argument("--refresh", action="store_true", help="re-download dataset files")
    p_cs.add_argument("--db", help="path to profdash.sqlite")
    p_cs.set_defaults(fn=cmd_import)

    p_areas = sub.add_parser("areas", help="list the CS research areas and their conferences")
    p_areas.set_defaults(fn=cmd_areas)

    p_serve = sub.add_parser("serve", help="run the dashboard")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    p_serve.add_argument("--db", help="path to profdash.sqlite")
    p_serve.set_defaults(fn=cmd_serve)

    p_wt = sub.add_parser("worker", help="background workers")
    w_sub = p_wt.add_subparsers(dest="worker")
    p_tasks = w_sub.add_parser("tasks", help="process agent task queue (papers/drafts)")
    p_tasks.add_argument("--batch-size", type=int, default=5)
    p_tasks.add_argument("--status", action="store_true", help="show queue status")
    p_tasks.add_argument("--db", help="path to profdash.sqlite")
    p_tasks.set_defaults(fn=cmd_worker_tasks)
    p_mail = w_sub.add_parser("gmail-scan", help="scan Gmail replies -> confirmations queue")
    p_mail.add_argument("--dry-run", action="store_true")
    p_mail.add_argument("--db", help="path to profdash.sqlite")
    p_mail.set_defaults(fn=cmd_worker_gmail)

    p_sg = sub.add_parser("setup-gmail", help="one-time Gmail OAuth setup")
    p_sg.add_argument("--client-secret", help="OAuth client secret JSON from Google Cloud Console")
    p_sg.set_defaults(fn=cmd_setup_gmail)

    p_dg = sub.add_parser("digest", help="DBLP publication digests for scoring")
    p_dg.add_argument("ids", nargs="+", help="professor ids")
    p_dg.add_argument("--out", help="write each digest to <dir>/<id>.digest instead of stdout")
    p_dg.add_argument("--db", help="path to profdash.sqlite")
    p_dg.set_defaults(fn=cmd_digest)

    p_ap = sub.add_parser("apply", help="apply one scoring judgment JSON file")
    p_ap.add_argument("judgment", help="path to judgment JSON")
    p_ap.add_argument("--db", help="path to profdash.sqlite")
    p_ap.set_defaults(fn=cmd_apply)

    p_au = sub.add_parser("audit", help="database quality report (read-only)")
    p_au.add_argument("--db", help="path to profdash.sqlite")
    p_au.set_defaults(fn=cmd_audit)

    p_bk = sub.add_parser("backup", help="snapshot the database")
    p_bk.add_argument("--dest", help="backup directory (default ~/backups/profdash)")
    p_bk.add_argument("--keep", type=int, default=14, help="how many backups to keep")
    p_bk.add_argument("--db", help="path to profdash.sqlite")
    p_bk.set_defaults(fn=cmd_backup)

    p_mi = sub.add_parser("migrate", help="run pending additive migrations")
    p_mi.add_argument("--db", help="path to profdash.sqlite")
    p_mi.set_defaults(fn=cmd_migrate)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.version:
        from . import __version__
        print(__version__)
        return 0
    if not getattr(args, "command", None):
        ap.print_help()
        return 1
    if getattr(args, "fn", None) is None:
        print("Choose a subcommand (e.g. `prof import csrankings`).")
        return 1
    try:
        args.fn(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
