"""Step 2: merge, dedupe, filter and rank the raw research into a structured digest."""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel

from . import llm
from .config import Config


class Source(BaseModel):
    publisher: str
    url: str


class Deal(BaseModel):
    acquirer: str
    target: str
    sector: str
    subsector: str
    deal_type: str
    status: str
    value_usd_millions: Optional[float]
    value_text: str
    description: str
    significance: int
    is_update: bool
    sources: list[Source]


class Rumor(BaseModel):
    headline: str
    sector: str
    source: Source


class FormatDirectives(BaseModel):
    max_deals_per_sector: int
    max_total_deals: int
    show_sources: bool
    include_rumors: bool


class CuratedDigest(BaseModel):
    headline: str
    deals: list[Deal]
    rumors: list[Rumor]
    format: FormatDirectives
    tuning_notes: list[str]


CURATION_SYSTEM = """\
You are the editor of a daily consumer M&A email digest for one reader. You receive raw research
notes from several analysts and the reader's preference profile, and you produce the final,
structured list of deals. Accuracy matters more than volume: a deal with no supporting source
URL in the notes, or announced outside the window, is dropped.
"""


def curate(cfg: Config, profile: str, research: dict[str, str], recent_deals: list[dict],
           window_desc: str) -> CuratedDigest:
    sectors = list(cfg.sector_groups)
    notes = "\n\n".join(f'<analyst_notes sector="{s}">\n{t}\n</analyst_notes>' for s, t in research.items())
    prompt = f"""\
Digest window: {window_desc}

<preference_profile>
{profile}
</preference_profile>

Deals already sent in recent digests (do not repeat these unless the notes show a material new
development in the window, in which case set is_update=true and say what changed):
<already_sent>
{json.dumps(recent_deals, indent=1)}
</already_sent>

{notes}

Produce the digest:
1. Merge duplicates (the same deal may appear under several analysts). Keep the best sources,
   primary source first.
2. Drop anything outside the window, anything not consumer-facing, rumors (they go in `rumors`),
   and anything the preference profile says to exclude.
3. `sector` must be exactly one of: {json.dumps(sectors)}. Reassign if an analyst misfiled it.
4. `value_usd_millions`: the stated value converted to USD millions (null if undisclosed).
   `value_text`: human-readable as stated, e.g. "$1.2B", "EUR 450M (~$490M)", "Undisclosed".
5. `description`: one line, at most ~25 words: what the target is and why the deal matters.
6. `significance` (1-100): overall importance to THIS reader. Start from deal size, brand
   recognition, strategic weight and acquirer prominence, then adjust using the profile's sector
   weights, size preferences, geography and source trust. Rate deals the reader cares less about
   lower rather than dropping them, unless the profile says to exclude them.
7. `format`: take max deals per sector/total, source links and rumor inclusion from the profile's
   Formatting section (defaults: 8, 40, true, false).
8. `headline`: one sentence summarizing the day (e.g. the biggest deal and overall volume).
9. `tuning_notes`: up to 3 short notes on how the preference profile changed what is shown today
   (e.g. "Ranked pet deals higher per your feedback"). Empty if nothing notable.
"""
    result = llm.parse(model=cfg.models["curation"], system=CURATION_SYSTEM, prompt=prompt,
                       schema=CuratedDigest)
    # Guard against labels outside the configured set.
    for d in result.deals:
        if d.sector not in cfg.sector_groups:
            d.sector = next((s for s in sectors if s.lower() in d.sector.lower()), sectors[-1])
    return result
