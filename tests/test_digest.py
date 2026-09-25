from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from digest import main, notes, render
from digest.config import load_config
from digest.curate import CuratedDigest
from digest.mailer import DIGEST_HEADER, is_feedback, strip_quoted
from digest.research import search_budget, sector_weights

FIXTURE = Path(__file__).parent / "fixtures" / "sample_digest.json"


@pytest.fixture
def digest():
    return CuratedDigest.model_validate_json(FIXTURE.read_text())


def test_organize_orders_sectors_and_numbers_deals(digest):
    sections = render.organize(digest)
    assert [s.sector for s in sections][:2] == ["Food & Beverage", "Pets"]
    ids = [i for s in sections for i, _ in s.deals]
    assert ids == [f"D{n}" for n in range(1, len(ids) + 1)]
    fb = sections[0].deals
    assert fb[0][1].significance >= fb[1][1].significance


def test_organize_applies_caps(digest):
    digest.format.max_deals_per_sector = 1
    digest.format.max_total_deals = 3
    sections = render.organize(digest)
    assert all(len(s.deals) == 1 for s in sections)
    assert sum(len(s.deals) for s in sections) == 3


def test_render_html_has_feedback_links_and_values(digest):
    sections = render.organize(digest)
    html = render.render_html(
        digest=digest, sections=sections, date_label="Friday", window_desc="w", reply_to="me@example.com",
        feedback_subject="Re: Consumer M&A Digest · 2026-09-25 feedback", feedback_changes=["x"],
        profile_markdown="## Profile\n- Pets: 1.0", learned_summary="- learned", notes_url=None,
    )
    assert "mailto:me@example.com?subject=Re%3A%20Consumer" in html
    assert "D1%20%2B" in html and "$2.4B" in html
    assert "invests in" in html  # minority stake verb
    assert "Rumors" in html and "learned: your current preference profile" in html


def test_fmt_usd():
    assert render.fmt_usd_millions(2400) == "$2.4B"
    assert render.fmt_usd_millions(1000) == "$1B"
    assert render.fmt_usd_millions(850) == "$850M"


def test_sector_weights_parsing():
    profile = "### Sector priorities\n- Pets: 2.0\n- Food & Beverage: 0\n* Retail & E-commerce: 9"
    w = sector_weights(profile, ["Pets", "Food & Beverage", "Retail & E-commerce", "Other"])
    assert w == {"Pets": 2.0, "Food & Beverage": 0.0, "Retail & E-commerce": 3.0, "Other": 1.0}
    assert search_budget(8, 0) == 0
    assert search_budget(8, 0.1) == 3
    assert search_budget(8, 2.0) == 16


def test_default_notes_profile_parses_all_sectors():
    cfg = load_config()
    w = sector_weights(notes.get_profile(), list(cfg.sector_groups))
    assert set(w.values()) == {1.0}


def test_notes_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "notes.md"
    p.write_text("# T\n<!-- PROFILE:START -->\nold\n<!-- PROFILE:END -->\n\n## Feedback log\n<!-- c -->\n### old entry\n")
    monkeypatch.setattr(notes, "NOTES_PATH", p)
    notes.write_profile_and_log("## Current preference profile\nnew", "### 2026-09-25\nentry")
    text = p.read_text()
    assert notes.get_profile(text) == "## Current preference profile\nnew"
    assert text.index("### 2026-09-25") < text.index("### old entry")


def test_inbox_read_and_clear(tmp_path, monkeypatch):
    p = tmp_path / "inbox.md"
    p.write_text("<!-- help -->\nD3 +\nmore pets\n")
    monkeypatch.setattr(notes, "INBOX_PATH", p)
    assert notes.read_inbox() == "D3 +\nmore pets"
    notes.clear_inbox()
    assert notes.read_inbox() == ""
    assert "help" in p.read_text()


def test_strip_quoted():
    body = "D2 +\nmore pets\n\nOn Fri, Sep 25, 2026 at 7:00 AM Digest <x@y.com> wrote:\n> old stuff"
    assert strip_quoted(body) == "D2 +\nmore pets"


def _msg(subject, sender, digest=False):
    m = EmailMessage()
    m["Subject"], m["From"] = subject, sender
    if digest:
        m[DIGEST_HEADER] = "1"
    return m


def test_is_feedback_filters():
    allowed = ["me@example.com"]
    assert is_feedback(_msg("Re: Consumer M&A Digest · 2026-09-25 · 6 deals", "Me <me@example.com>"), allowed)
    assert not is_feedback(_msg("Re: Consumer M&A Digest", "evil@example.com"), allowed)
    assert not is_feedback(_msg("Consumer M&A Digest · 2026-09-25", "me@example.com", digest=True), allowed)
    assert not is_feedback(_msg("Lunch?", "me@example.com"), allowed)


@pytest.mark.parametrize(
    "utc,expected",
    [
        (datetime(2026, 7, 1, 13, 50, tzinfo=ZoneInfo("UTC")), True),    # 6:50 PDT
        (datetime(2026, 12, 1, 13, 50, tzinfo=ZoneInfo("UTC")), False),  # 5:50 PST: wait for 2nd cron
        (datetime(2026, 12, 1, 14, 50, tzinfo=ZoneInfo("UTC")), True),   # 6:50 PST
    ],
)
def test_should_send_now_handles_dst(utc, expected):
    cfg = load_config()
    ok, _ = main.should_send_now(cfg, utc.astimezone(cfg.tz), {})
    assert ok is expected


def test_should_not_send_twice():
    cfg = load_config()
    now = datetime(2026, 7, 1, 8, 0, tzinfo=cfg.tz)
    ok, why = main.should_send_now(cfg, now, {"last_sent_date": "2026-07-01"})
    assert not ok and "already" in why


def test_window_extends_after_missed_day():
    cfg = load_config()
    now = datetime(2026, 7, 3, 14, 0, tzinfo=ZoneInfo("UTC"))
    start, _ = main.compute_window(cfg, now, {"last_sent_at": "2026-07-01T14:00:00+00:00"})
    assert (now - start).total_seconds() / 3600 == 48
    start, _ = main.compute_window(cfg, now, {"last_sent_at": "2026-06-01T14:00:00+00:00"})
    assert (now - start).total_seconds() / 3600 == 72


def test_profile_to_html_handles_tight_lists_and_headings():
    html = render.profile_to_html("## Current preference profile\n### Sector priorities\nWeights:\n- Pets: 2.0\n- Retail: 1.0")
    assert "<li>Pets: 2.0</li>" in html
    assert "<h2>" not in html and "<h1>" not in html
