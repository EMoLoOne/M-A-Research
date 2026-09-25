"""Read and update the running preferences notes file (preferences/notes.md)."""

from __future__ import annotations

import re

from .config import INBOX_PATH, NOTES_PATH

PROFILE_START = "<!-- PROFILE:START -->"
PROFILE_END = "<!-- PROFILE:END -->"
LOG_HEADER = "## Feedback log"
INBOX_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def read_notes() -> str:
    return NOTES_PATH.read_text()


def get_profile(notes: str | None = None) -> str:
    notes = notes if notes is not None else read_notes()
    start, end = notes.find(PROFILE_START), notes.find(PROFILE_END)
    if start == -1 or end == -1:
        return notes
    return notes[start + len(PROFILE_START) : end].strip()


def get_log(notes: str | None = None) -> str:
    notes = notes if notes is not None else read_notes()
    idx = notes.find(LOG_HEADER)
    return notes[idx:] if idx != -1 else ""


def write_profile_and_log(new_profile: str, log_entry: str | None) -> None:
    """Replace the profile block and prepend a log entry (newest first)."""
    notes = read_notes()
    start, end = notes.find(PROFILE_START), notes.find(PROFILE_END)
    if start == -1 or end == -1:
        raise ValueError("notes.md is missing the PROFILE:START/END markers")
    notes = notes[: start + len(PROFILE_START)] + "\n" + new_profile.strip() + "\n" + notes[end:]

    if log_entry:
        idx = notes.find(LOG_HEADER)
        if idx == -1:
            notes = notes.rstrip() + f"\n\n{LOG_HEADER}\n"
            idx = notes.find(LOG_HEADER)
        # Insert after the header line and its explanatory comment, if present.
        after = notes.find("\n", idx) + 1
        comment = re.match(r"<!--.*?-->\n", notes[after:], re.DOTALL)
        if comment:
            after += comment.end()
        notes = notes[:after] + "\n" + log_entry.strip() + "\n" + notes[after:]
    NOTES_PATH.write_text(notes)


def read_inbox() -> str:
    if not INBOX_PATH.exists():
        return ""
    return INBOX_COMMENT.sub("", INBOX_PATH.read_text()).strip()


def clear_inbox() -> None:
    """Keep the instructional comment, drop everything the user wrote."""
    text = INBOX_PATH.read_text()
    m = INBOX_COMMENT.search(text)
    INBOX_PATH.write_text((m.group(0) + "\n") if m else "")
