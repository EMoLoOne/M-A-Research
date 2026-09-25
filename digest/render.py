"""Step 3: turn the curated digest into an email-safe HTML body (plus a plain-text fallback)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from urllib.parse import quote

import markdown

from .curate import CuratedDigest, Deal

# Inline styles only: most email clients strip <style> blocks.
INK = "#1a1f2b"
MUTED = "#5f6b7a"
RULE = "#e3e7ec"
ACCENT = "#0b5cad"
PANEL = "#f5f7fa"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


@dataclass
class Section:
    sector: str
    deals: list[tuple[str, Deal]] = field(default_factory=list)  # (deal id, deal)


def fmt_usd_millions(m: float) -> str:
    if m >= 1000:
        return f"${m / 1000:,.1f}B".replace(".0B", "B")
    return f"${m:,.0f}M"


def organize(digest: CuratedDigest) -> list[Section]:
    """Group by sector; sectors ordered by their most significant deal, deals by significance.

    Applies the per-sector and total caps from the format directives, then numbers deals
    D1..Dn in display order so feedback can refer to them.
    """
    fmt = digest.format
    per_sector = max(1, fmt.max_deals_per_sector)
    total_cap = max(1, fmt.max_total_deals)

    by_sector: dict[str, list[Deal]] = {}
    for d in sorted(digest.deals, key=lambda d: (-d.significance, -(d.value_usd_millions or 0))):
        bucket = by_sector.setdefault(d.sector, [])
        if len(bucket) < per_sector:
            bucket.append(d)

    # Enforce the total cap by keeping the globally most significant deals.
    kept = sorted((d for ds in by_sector.values() for d in ds),
                  key=lambda d: (-d.significance, -(d.value_usd_millions or 0)))[:total_cap]
    kept_ids = {id(d) for d in kept}

    ordered = sorted(by_sector.items(), key=lambda kv: -max(d.significance for d in kv[1]))
    sections, n = [], 0
    for sector, deals in ordered:
        deals = [d for d in deals if id(d) in kept_ids]
        if not deals:
            continue
        sec = Section(sector)
        for d in deals:
            n += 1
            sec.deals.append((f"D{n}", d))
        sections.append(sec)
    return sections


def deal_verb(deal_type: str) -> str:
    t = deal_type.lower()
    if "merger" in t or "merge" in t:
        return "to merge with"
    if "minority" in t or "stake" in t or "investment" in t:
        return "invests in"
    return "acquires"


def profile_to_html(md: str) -> str:
    """Render hand- or model-edited Markdown robustly inside the email panel.

    Python-Markdown needs a blank line before a list, which edited notes often lack, and
    the profile's own headings are demoted so they sit under the panel title.
    """
    lines: list[str] = []
    for line in md.splitlines():
        is_item = re.match(r"^\s*([-*+]|\d+\.)\s", line) is not None
        if is_item and lines and lines[-1].strip() and not re.match(r"^\s*([-*+]|\d+\.)\s", lines[-1]):
            lines.append("")
        lines.append(re.sub(r"^(#{1,4})\s", lambda m: "#" * min(6, len(m.group(1)) + 2) + " ", line))
    html = markdown.markdown("\n".join(lines))
    html = re.sub(r"<h[3-6]>", f'<div style="font-weight:600;font-size:13px;margin:12px 0 4px;color:{INK};">', html)
    return re.sub(r"</h[3-6]>", "</div>", html)


def _mailto(address: str, subject: str, body: str) -> str:
    return f"mailto:{address}?subject={quote(subject)}&body={quote(body)}"


def _p(text: str, style: str = "") -> str:
    return f'<p style="margin:0 0 8px;{style}">{text}</p>'


def _panel(title: str, inner_html: str, border: str = ACCENT) -> str:
    return (
        f'<div style="background:{PANEL};border-left:3px solid {border};padding:12px 16px;margin:0 0 20px;">'
        f'<div style="font-weight:600;font-size:14px;margin:0 0 6px;color:{INK};">{escape(title)}</div>'
        f'<div style="font-size:13px;line-height:1.5;color:{INK};">{inner_html}</div></div>'
    )


def _deal_html(deal_id: str, d: Deal, show_sources: bool, reply_to: str, subject: str) -> str:
    label = f"{deal_id} {d.acquirer} / {d.target}"
    more = _mailto(reply_to, subject, f"{deal_id} +  ({d.acquirer} / {d.target}): more like this\n")
    less = _mailto(reply_to, subject, f"{deal_id} -  ({d.acquirer} / {d.target}): less like this\n")
    meta = " · ".join(escape(x) for x in [d.subsector, d.deal_type, d.status] if x)
    if d.is_update:
        meta = f'<span style="color:#9a5b00;font-weight:600;">UPDATE</span> · {meta}'
    sources = ""
    if show_sources and d.sources:
        links = " · ".join(
            f'<a href="{escape(s.url, quote=True)}" style="color:{ACCENT};text-decoration:none;">{escape(s.publisher)}</a>'
            for s in d.sources[:3]
        )
        sources = f'<span style="color:{MUTED};">Sources: {links}</span> &nbsp; '
    return f"""
<tr><td style="padding:12px 0;border-bottom:1px solid {RULE};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td style="font-size:15px;font-weight:600;color:{INK};vertical-align:top;">
      <span style="color:{MUTED};font-weight:500;font-size:12px;">{deal_id}</span>&nbsp;
      {escape(d.acquirer)} <span style="color:{MUTED};font-weight:400;">{deal_verb(d.deal_type)}</span> {escape(d.target)}
    </td>
    <td style="font-size:14px;font-weight:600;color:{INK};text-align:right;white-space:nowrap;vertical-align:top;padding-left:12px;">
      {escape(d.value_text or "Undisclosed")}
    </td>
  </tr></table>
  <div style="font-size:12px;color:{MUTED};margin:3px 0 5px;">{meta}</div>
  <div style="font-size:14px;line-height:1.45;color:{INK};margin:0 0 6px;">{escape(d.description)}</div>
  <div style="font-size:12px;">{sources}<a href="{more}" style="color:#1d7a3a;text-decoration:none;" title="{escape(label, quote=True)}">&#9650; More like this</a>
    &nbsp;<a href="{less}" style="color:#a3312a;text-decoration:none;">&#9660; Less like this</a></div>
</td></tr>"""


def render_html(
    *,
    digest: CuratedDigest,
    sections: list[Section],
    date_label: str,
    window_desc: str,
    reply_to: str,
    feedback_subject: str,
    feedback_changes: list[str],
    profile_markdown: str | None,
    learned_summary: str | None,
    notes_url: str | None,
) -> str:
    shown = [d for s in sections for _, d in s.deals]
    disclosed = [d.value_usd_millions for d in shown if d.value_usd_millions]
    stats = f"{len(shown)} deal{'s' if len(shown) != 1 else ''} across {len(sections)} sector{'s' if len(sections) != 1 else ''}"
    if disclosed:
        stats += f" · {fmt_usd_millions(sum(disclosed))} disclosed value"

    parts: list[str] = []
    parts.append(f"""
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:{MUTED};">Consumer M&amp;A Digest</div>
<h1 style="font-size:22px;margin:4px 0 6px;color:{INK};">{escape(date_label)}</h1>
<div style="font-size:12px;color:{MUTED};margin:0 0 14px;">{escape(window_desc)} · {escape(stats)}</div>
""")
    if digest.headline:
        parts.append(_p(escape(digest.headline), f"font-size:15px;line-height:1.5;color:{INK};margin-bottom:18px;"))

    if feedback_changes:
        items = "".join(f"<li>{escape(c)}</li>" for c in feedback_changes)
        parts.append(_panel("Updated from your feedback", f'<ul style="margin:0;padding-left:18px;">{items}</ul>', "#1d7a3a"))
    if digest.tuning_notes:
        parts.append(_p("Tuned for you: " + escape(" · ".join(digest.tuning_notes)),
                        f"font-size:12px;color:{MUTED};margin-bottom:16px;"))

    if not sections:
        parts.append(_p("No consumer M&amp;A announcements matched your filters in this window.",
                        f"font-size:14px;color:{INK};"))

    for sec in sections:
        parts.append(f"""
<h2 style="font-size:16px;margin:22px 0 2px;padding-bottom:6px;border-bottom:2px solid {INK};color:{INK};">
  {escape(sec.sector)} <span style="color:{MUTED};font-weight:400;font-size:13px;">({len(sec.deals)})</span></h2>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">""")
        for deal_id, d in sec.deals:
            parts.append(_deal_html(deal_id, d, digest.format.show_sources, reply_to, feedback_subject))
        parts.append("</table>")

    if digest.format.include_rumors and digest.rumors:
        items = "".join(
            f'<li style="margin-bottom:4px;">{escape(r.headline)} <span style="color:{MUTED};">({escape(r.sector)}, '
            f'<a href="{escape(r.source.url, quote=True)}" style="color:{ACCENT};">{escape(r.source.publisher)}</a>)</span></li>'
            for r in digest.rumors
        )
        parts.append(f'<h2 style="font-size:16px;margin:22px 0 8px;color:{INK};">Rumors &amp; in-talks</h2>'
                     f'<ul style="font-size:13px;line-height:1.45;padding-left:18px;color:{INK};">{items}</ul>')

    if profile_markdown:
        inner = ""
        if learned_summary:
            inner += profile_to_html(learned_summary)
        inner += profile_to_html(profile_markdown)
        inner += _p("Anything drifted? Reply with a correction, e.g. <em>\"Profile: pets should be 1.0, not 2.0\"</em>.",
                    f"color:{MUTED};margin-top:10px;")
        parts.append('<div style="margin-top:28px;"></div>')
        parts.append(_panel("What I've learned: your current preference profile", inner, "#6b4fbb"))

    general = _mailto(reply_to, feedback_subject,
                      "Ratings (deal id + or -, e.g. D2 +, D5 -):\n\n\nMore of:\n\nLess of:\n\nOther notes:\n")
    notes_link = (f' You can also edit <a href="{escape(notes_url, quote=True)}" style="color:{ACCENT};">'
                  f"the preference notes file</a> directly.") if notes_url else ""
    parts.append(f"""
<div style="margin-top:28px;padding-top:14px;border-top:1px solid {RULE};font-size:12px;line-height:1.55;color:{MUTED};">
  <div style="font-weight:600;color:{INK};font-size:13px;margin-bottom:4px;">Help tune tomorrow's digest</div>
  Reply to this email with any of these. Each one is read before the next digest:<br>
  &bull; <b>Rate deals:</b> <code>D3 +</code>, <code>D7 -</code>, or <code>D2 ++</code> for a strong signal<br>
  &bull; <b>Steer coverage:</b> "more pet and vet deals", "skip deals under $50M", "fewer franchisee deals"<br>
  &bull; <b>Sources &amp; format:</b> "trust Restaurant Business more", "hide sources", "max 5 per sector"<br>
  Or use the &#9650;/&#9660; links on each deal, or <a href="{general}" style="color:{ACCENT};">send a general note</a>.{notes_link}
</div>""")

    body = "".join(parts)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Consumer M&amp;A Digest · {escape(date_label)}</title></head>
<body style="margin:0;padding:0;background:#ffffff;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;"><tr><td align="center" style="padding:24px 16px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;font-family:{FONT};color:{INK};">
<tr><td>{body}</td></tr></table>
</td></tr></table></body></html>"""


def render_text(digest: CuratedDigest, sections: list[Section], date_label: str,
                feedback_changes: list[str]) -> str:
    lines = [f"Consumer M&A Digest - {date_label}", ""]
    if digest.headline:
        lines += [digest.headline, ""]
    if feedback_changes:
        lines += ["Updated from your feedback:"] + [f"  - {c}" for c in feedback_changes] + [""]
    if not sections:
        lines.append("No consumer M&A announcements matched your filters in this window.")
    for sec in sections:
        lines += [sec.sector.upper(), "-" * len(sec.sector)]
        for deal_id, d in sec.deals:
            lines.append(f"{deal_id}  {d.acquirer} {deal_verb(d.deal_type)} {d.target} - {d.value_text or 'Undisclosed'}")
            lines.append(f"    {d.description}")
            if d.sources:
                lines.append(f"    {d.sources[0].url}")
        lines.append("")
    lines += ["Feedback: reply with 'D3 +', 'D7 -', or a note on what you want more or less of."]
    return "\n".join(lines)
