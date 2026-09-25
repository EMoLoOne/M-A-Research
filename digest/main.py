"""Entry point: python -m digest <command>.

Commands:
  run        Process feedback, research, and send today's digest (the scheduled job).
  feedback   Only fold new feedback into preferences/notes.md.
  profile    Print the current preference profile and a "what I've learned" summary.
  preview    Render an HTML file from a saved curated digest JSON (no API calls, no email).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import learn, llm, notes, render, state
from .config import ROOT, SUBJECT_PREFIX, Config, load_config
from .curate import CuratedDigest

log = logging.getLogger("digest")

MAX_LOOKBACK_HOURS = 72
EARLY_TOLERANCE = timedelta(minutes=45)


# ---------------------------------------------------------------- scheduling


def should_send_now(cfg: Config, now_local: datetime, run_state: dict) -> tuple[bool, str]:
    """GitHub cron runs in UTC, so the workflow fires twice (covering DST); only one run sends."""
    today = now_local.date().isoformat()
    if run_state.get("last_sent_date") == today:
        return False, f"already sent today ({today})"
    hh, mm = map(int, cfg.send_time.split(":"))
    target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now_local < target - EARLY_TOLERANCE:
        return False, f"too early: {now_local:%H:%M} local, target {cfg.send_time}"
    return True, "ok"


def compute_window(cfg: Config, now_utc: datetime, run_state: dict) -> tuple[datetime, datetime]:
    start = now_utc - timedelta(hours=cfg.lookback_hours)
    last = run_state.get("last_sent_at")
    if last:
        # If a day was missed, reach back to the previous send (capped) so nothing falls through.
        last_dt = datetime.fromisoformat(last)
        start = min(start, max(last_dt, now_utc - timedelta(hours=MAX_LOOKBACK_HOURS)))
    return start, now_utc


def notes_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return None
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return f"{server}/{repo}/blob/{branch}/preferences/notes.md"


# ---------------------------------------------------------------- feedback


def process_feedback(cfg: Config, today: date, run_state: dict) -> list[str]:
    """Collect email replies + feedback/inbox.md, update notes.md, return change bullets."""
    from .mailer import fetch_feedback

    items: list[dict] = []
    seen = set(run_state.get("processed_feedback_ids", []))
    since = date.fromisoformat(run_state.get("last_feedback_check", (today - timedelta(days=14)).isoformat()))
    try:
        emails = fetch_feedback(cfg, since, seen)
    except Exception:
        log.exception("Could not read feedback mailbox; continuing without email feedback")
        emails = []
    for m in emails:
        items.append({"channel": "Email reply", "subject": m.subject, "date": m.date, "text": m.body})

    inbox = notes.read_inbox()
    if inbox:
        items.append({"channel": "feedback/inbox.md", "date": today.isoformat(), "text": inbox})

    if not items:
        run_state["last_feedback_check"] = today.isoformat()
        return []

    log.info("Applying %d feedback item(s) to the preference profile", len(items))
    try:
        update = learn.apply_feedback(cfg, notes.get_profile(), items, state.load_recent_digests(today), today)
    except llm.ConfigurationError:
        raise
    except Exception:
        # Leave the inbox and message ids untouched so this feedback is retried next run.
        log.exception("Could not apply feedback today; it will be retried on the next run")
        return []
    notes.write_profile_and_log(update.updated_profile, learn.format_log_entry(today, items, update))
    if inbox:
        notes.clear_inbox()
    run_state["processed_feedback_ids"] = (list(seen) + [m.message_id for m in emails])[-500:]
    run_state["last_feedback_check"] = today.isoformat()
    run_state.setdefault("feedback_count", 0)
    run_state["feedback_count"] += len(items)
    return update.changes


# ---------------------------------------------------------------- digest


def wants_profile_summary(cfg: Config, today: date, run_state: dict) -> bool:
    if not run_state.get("last_profile_summary"):
        return True
    return today.strftime("%A").lower() == cfg.profile_summary_weekday.lower()


def build_email(cfg: Config, digest: CuratedDigest, today: date, window_desc: str,
                feedback_changes: list[str], include_profile: bool, since_summary: str | None):
    sections = render.organize(digest)
    n = sum(len(s.deals) for s in sections)
    subject = f"{SUBJECT_PREFIX} · {today.isoformat()} · {n} deal{'s' if n != 1 else ''}"
    feedback_subject = f"Re: {SUBJECT_PREFIX} · {today.isoformat()} feedback"

    profile_md = learned = None
    if include_profile:
        notes_text = notes.read_notes()
        profile_md = notes.get_profile(notes_text)
        try:
            learned = learn.summarize_learning(cfg, profile_md, notes.get_log(notes_text), since_summary)
        except Exception:
            log.exception("Learning summary failed; showing profile without it")

    html = render.render_html(
        digest=digest, sections=sections, date_label=today.strftime("%A, %B %-d, %Y"),
        window_desc=window_desc, reply_to=cfg.reply_address, feedback_subject=feedback_subject,
        feedback_changes=feedback_changes, profile_markdown=profile_md, learned_summary=learned,
        notes_url=notes_url(),
    )
    text = render.render_text(digest, sections, today.isoformat(), feedback_changes)
    return subject, html, text, sections


def preflight(cfg: Config, dry_run: bool) -> list[str]:
    """Configuration problems that would make the run fail; checked before any API spend."""
    problems = []
    try:
        llm.check_credentials()
    except llm.ConfigurationError as e:
        problems.append(str(e))
    if not dry_run and not cfg.smtp:
        problems.append("SMTP_USERNAME / SMTP_PASSWORD are not set, so the digest cannot be emailed. "
                        "Add them as repository secrets, or run with --dry-run.")
    return problems


def report_failure(cfg: Config, today: date, run_state: dict, error: BaseException, out: Path) -> None:
    """Leave a visible trace of a failed run: an HTML report and (once a day) an email."""
    from html import escape

    from .mailer import send_digest

    msg = f"{type(error).__name__}: {error}"
    body = (f"<p>Today's Consumer M&amp;A Digest could not be produced.</p><pre style='white-space:pre-wrap'>"
            f"{escape(msg)}</pre><p>The next scheduled run will try again. Check the GitHub Actions log for details.</p>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_name("error.html").write_text(body)
    if cfg.smtp and run_state.get("last_failure_notice") != today.isoformat():
        try:
            send_digest(cfg, f"{SUBJECT_PREFIX} · {today.isoformat()} · run failed", body, msg)
            run_state["last_failure_notice"] = today.isoformat()
            state.save_run_state(run_state)
        except Exception:
            log.exception("Could not send failure notice")


def cmd_run(args) -> int:
    cfg = load_config()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(cfg.tz)
    today = now_local.date()
    run_state = state.load_run_state()

    if not args.force:
        ok, why = should_send_now(cfg, now_local, run_state)
        if not ok:
            log.info("Not sending: %s", why)
            return 0

    problems = preflight(cfg, args.dry_run)
    if problems:
        for p in problems:
            log.error("Setup problem: %s", p)
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::error title=Digest setup::{p}", flush=True)
        return 2

    try:
        return _run_digest(cfg, args, now_utc, now_local, today, run_state)
    except Exception as e:
        log.exception("Digest run failed")
        if not args.dry_run:
            report_failure(cfg, today, run_state, e, Path(args.out))
        return 1


def _run_digest(cfg: Config, args, now_utc: datetime, now_local: datetime, today: date, run_state: dict) -> int:
    from . import curate, research
    from .mailer import send_digest

    feedback_changes = [] if args.skip_feedback else process_feedback(cfg, today, run_state)
    state.save_run_state(run_state)  # persist processed feedback ids even if a later step fails
    profile = notes.get_profile()

    start, end = compute_window(cfg, now_utc, run_state)
    window_desc = (f"{start.astimezone(cfg.tz):%b %-d %-I:%M %p} to {end.astimezone(cfg.tz):%b %-d %-I:%M %p} "
                   f"{now_local.tzname()}")
    log.info("Window: %s", window_desc)

    raw, failed_sectors = research.research_all(cfg, profile, start, end, today.isoformat())
    digest = curate.curate(cfg, profile, raw, state.recent_sent_deals(today, cfg.dedupe_days),
                           f"{start:%Y-%m-%d %H:%M} UTC to {end:%Y-%m-%d %H:%M} UTC ({window_desc})")

    if failed_sectors:
        digest.tuning_notes.append("Search failed today for: " + ", ".join(failed_sectors)
                                   + ". Deals in those sectors may be missing.")

    include_profile = wants_profile_summary(cfg, today, run_state)
    subject, html, text, sections = build_email(cfg, digest, today, window_desc, feedback_changes,
                                                include_profile, run_state.get("last_profile_summary"))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    (out.with_suffix(".json")).write_text(digest.model_dump_json(indent=2))
    log.info("Wrote %s", out)

    if args.dry_run:
        log.info("Dry run: not sending, not updating state (subject would be %r)", subject)
        return 0

    send_digest(cfg, subject, html, text)

    shown = [dict(d.model_dump(), id=deal_id) for s in sections for deal_id, d in s.deals]
    state.save_digest(today, {"subject": subject, "window": window_desc, "deals": shown,
                              "rumors": [r.model_dump() for r in digest.rumors]})
    state.record_sent_deals(today, shown)
    run_state["last_sent_date"] = today.isoformat()
    run_state["last_sent_at"] = now_utc.isoformat()
    if include_profile:
        run_state["last_profile_summary"] = today.isoformat()
    state.save_run_state(run_state)
    return 0


def cmd_feedback(args) -> int:
    cfg = load_config()
    run_state = state.load_run_state()
    changes = process_feedback(cfg, datetime.now(cfg.tz).date(), run_state)
    state.save_run_state(run_state)
    print("\n".join(f"- {c}" for c in changes) or "No new feedback.")
    return 0


def cmd_profile(args) -> int:
    cfg = load_config()
    text = notes.read_notes()
    print(notes.get_profile(text))
    if not args.no_summary:
        print("\n## What I've learned\n")
        print(learn.summarize_learning(cfg, notes.get_profile(text), notes.get_log(text),
                                       state.load_run_state().get("last_profile_summary")))
    return 0


def cmd_preview(args) -> int:
    cfg = load_config()
    digest = CuratedDigest.model_validate_json(Path(args.fixture).read_text())
    today = datetime.now(cfg.tz).date()
    sections = render.organize(digest)
    html = render.render_html(
        digest=digest, sections=sections, date_label=today.strftime("%A, %B %-d, %Y"),
        window_desc="Preview", reply_to=cfg.reply_address,
        feedback_subject=f"Re: {SUBJECT_PREFIX} · {today.isoformat()} feedback",
        feedback_changes=["(preview) Example of a change applied from your feedback"],
        profile_markdown=notes.get_profile() if args.with_profile else None,
        learned_summary="- (preview) No feedback yet." if args.with_profile else None,
        notes_url=notes_url(),
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(html)
    print(f"Wrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="digest")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="build and send today's digest")
    r.add_argument("--force", action="store_true", help="ignore the send-time/already-sent guard")
    r.add_argument("--dry-run", action="store_true", help="build the email but do not send it")
    r.add_argument("--skip-feedback", action="store_true")
    r.add_argument("--out", default=str(ROOT / "out" / "digest.html"))
    r.set_defaults(func=cmd_run)

    sub.add_parser("feedback", help="fold new feedback into the notes file").set_defaults(func=cmd_feedback)

    pr = sub.add_parser("profile", help="show the current preference profile")
    pr.add_argument("--no-summary", action="store_true")
    pr.set_defaults(func=cmd_profile)

    pv = sub.add_parser("preview", help="render HTML from a curated digest JSON")
    pv.add_argument("fixture")
    pv.add_argument("--out", default=str(ROOT / "out" / "preview.html"))
    pv.add_argument("--with-profile", action="store_true")
    pv.set_defaults(func=cmd_preview)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
