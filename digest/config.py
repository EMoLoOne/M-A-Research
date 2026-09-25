"""Configuration: config.yaml for behavior, environment variables for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
NOTES_PATH = ROOT / "preferences" / "notes.md"
INBOX_PATH = ROOT / "feedback" / "inbox.md"
STATE_DIR = ROOT / "state"
DIGESTS_DIR = STATE_DIR / "digests"
SENT_DEALS_PATH = STATE_DIR / "sent_deals.json"
RUN_STATE_PATH = STATE_DIR / "run_state.json"

SUBJECT_PREFIX = "Consumer M&A Digest"

DEFAULT_EFFORT = {"research": "high", "curation": "high", "feedback": "high"}


@dataclass
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str


@dataclass
class ImapSettings:
    host: str
    port: int
    username: str
    password: str
    mailbox: str


@dataclass
class Config:
    recipient: str
    timezone: str
    send_time: str
    lookback_hours: int
    models: dict[str, list[str]]
    effort: dict[str, str]
    searches_per_group: int
    sector_groups: dict[str, str]
    profile_summary_weekday: str
    dedupe_days: int
    smtp: SmtpSettings | None = None
    imap: ImapSettings | None = None
    feedback_senders: list[str] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def reply_address(self) -> str:
        """Where feedback replies go: the mailbox the IMAP poller reads."""
        if self.imap:
            return self.imap.username
        if self.smtp:
            return self.smtp.sender
        return self.recipient


def load_config(path: Path = ROOT / "config.yaml") -> Config:
    raw = yaml.safe_load(path.read_text())
    env = os.environ

    # Allow overriding the recipient without editing the file (e.g. from a CI secret).
    recipient = env.get("DIGEST_RECIPIENT") or raw["recipient"]

    smtp = None
    if env.get("SMTP_USERNAME") and env.get("SMTP_PASSWORD"):
        smtp = SmtpSettings(
            host=env.get("SMTP_HOST", "smtp.gmail.com"),
            port=int(env.get("SMTP_PORT", "465")),
            username=env["SMTP_USERNAME"],
            password=env["SMTP_PASSWORD"],
            sender=env.get("SMTP_FROM") or env["SMTP_USERNAME"],
        )

    imap = None
    imap_user = env.get("IMAP_USERNAME") or env.get("SMTP_USERNAME")
    imap_pass = env.get("IMAP_PASSWORD") or env.get("SMTP_PASSWORD")
    if imap_user and imap_pass and env.get("IMAP_DISABLED", "").lower() not in ("1", "true"):
        imap = ImapSettings(
            host=env.get("IMAP_HOST", "imap.gmail.com"),
            port=int(env.get("IMAP_PORT", "993")),
            username=imap_user,
            password=imap_pass,
            mailbox=env.get("IMAP_MAILBOX", "INBOX"),
        )

    # Only feedback from these addresses is accepted, so nobody else can steer the digest.
    senders = [s.strip().lower() for s in env.get("FEEDBACK_SENDERS", "").split(",") if s.strip()]
    if not senders:
        senders = [recipient.lower()]

    return Config(
        recipient=recipient,
        timezone=raw["timezone"],
        send_time=raw["send_time"],
        lookback_hours=int(raw.get("lookback_hours", 24)),
        models={role: ([m] if isinstance(m, str) else list(m)) for role, m in raw["models"].items()},
        effort={**DEFAULT_EFFORT, **(raw.get("effort") or {})},
        searches_per_group=int(raw.get("searches_per_group", 8)),
        sector_groups=raw["sector_groups"],
        profile_summary_weekday=raw.get("profile_summary_weekday", "Monday"),
        dedupe_days=int(raw.get("dedupe_days", 10)),
        smtp=smtp,
        imap=imap,
        feedback_senders=senders,
    )
