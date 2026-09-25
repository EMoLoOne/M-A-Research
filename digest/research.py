"""Step 1: web research, one parallel request per sector group."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import llm
from .config import Config

log = logging.getLogger(__name__)

RESEARCH_SYSTEM = """\
You are an M&A research analyst covering consumer-facing companies. You find newly announced
M&A transactions using web search and report only what your sources support.

Rules:
- Only report deals whose announcement (press release, filing, or first credible report) falls
  inside the time window you are given. Older deals that merely got a news mention do not count,
  unless there is a material new development in the window (e.g. signed definitive agreement,
  closing, regulatory block, price increase) - label those as updates.
- Never invent or estimate deal values. Report a value only if a source states it.
- Prefer primary sources (press releases, filings) and reputable financial/trade press.
- Search broadly: newswires, trade publications for the sector, PE/deal news sites, and
  queries naming the date. Try several phrasings (acquires, to acquire, merger, agrees to buy,
  take private, divests, sells, completes acquisition).
"""


def sector_weights(profile: str, groups: list[str]) -> dict[str, float]:
    """Read '- <Sector>: <weight>' lines from the profile. Missing sectors default to 1.0."""
    weights = {}
    for g in groups:
        m = re.search(rf"^\s*[-*]\s*{re.escape(g)}\s*:\s*([0-9]*\.?[0-9]+)", profile, re.MULTILINE)
        weights[g] = max(0.0, min(3.0, float(m.group(1)))) if m else 1.0
    return weights


def search_budget(base: int, weight: float) -> int:
    if weight <= 0:
        return 0
    return max(3, min(20, round(base * weight)))


def _group_prompt(sector: str, scope: str, window_start: datetime, window_end: datetime,
                  local_date: str, profile: str) -> str:
    return f"""\
Find every consumer M&A transaction in this sector announced in the window below.

Sector: {sector}
Scope: {scope}

Window: {window_start:%Y-%m-%d %H:%M} UTC to {window_end:%Y-%m-%d %H:%M} UTC
(the reader's digest date is {local_date}).

The reader's preference profile (use it to decide which sources to trust and which deal
types to include; do not let it stop you from reporting clearly significant deals):
<preference_profile>
{profile}
</preference_profile>

For each deal, report:
- acquirer (and co-investors if relevant), target
- deal value exactly as stated (currency and amount), or "undisclosed"
- deal type (acquisition, merger, take-private, carve-out, minority stake, update on prior deal, ...)
- announcement date/time and status (announced, completed, terminated)
- sub-sector and a one-sentence description of what the target does and why it matters
- geography of the target
- source URLs (at least one; primary source first) and the publisher names

Also list separately, briefly, any credible rumors / "in talks" reports you came across,
marked RUMOR, with their sources.

If you find nothing that fits the window, say so plainly. Output plain text, one deal per block.
"""


class ResearchError(RuntimeError):
    pass


def research_all(cfg: Config, profile: str, window_start: datetime, window_end: datetime,
                 local_date: str) -> tuple[dict[str, str], list[str]]:
    """Returns (notes by sector, sectors whose research failed)."""
    weights = sector_weights(profile, list(cfg.sector_groups))
    jobs = {}
    for sector, scope in cfg.sector_groups.items():
        budget = search_budget(cfg.searches_per_group, weights[sector])
        if budget == 0:
            log.info("Skipping %s (weight 0 in preferences)", sector)
            continue
        jobs[sector] = (scope, budget)

    def run(sector: str) -> tuple[str, str | None, str | None]:
        scope, budget = jobs[sector]
        log.info("Researching %s (%d searches)", sector, budget)
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": budget},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max(3, budget // 2)},
        ]
        try:
            text = llm.run_with_tools(
                models=cfg.models["research"],
                system=RESEARCH_SYSTEM,
                prompt=_group_prompt(sector, scope, window_start, window_end, local_date, profile),
                tools=tools,
                effort=cfg.effort["research"],
                what=f"Research for {sector}",
            )
            return sector, text, None
        except llm.ConfigurationError:
            raise
        except Exception as e:  # one failing sector should not sink the whole digest
            log.exception("Research failed for %s", sector)
            return sector, None, str(e)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, list(jobs)))

    ok = {s: t for s, t, _ in results if t is not None}
    failed = {s: err for s, _, err in results if err is not None}
    if jobs and not ok:
        raise ResearchError("All sector research requests failed: " + "; ".join(f"{s}: {e}" for s, e in failed.items()))
    # Tell the curator which sectors are missing so it doesn't read silence as "no deals".
    for s in failed:
        ok[s] = "RESEARCH UNAVAILABLE for this sector today (request failed); report no deals for it."
    return ok, list(failed)
