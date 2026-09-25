"""Sending the digest (SMTP) and collecting feedback replies (IMAP)."""

from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from html import unescape

from .config import SUBJECT_PREFIX, Config

log = logging.getLogger(__name__)

DIGEST_HEADER = "X-Consumer-MA-Digest"


def send_digest(cfg: Config, subject: str, html: str, text: str) -> None:
    if not cfg.smtp:
        raise RuntimeError("SMTP_USERNAME / SMTP_PASSWORD are not set; cannot send email")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Consumer M&A Digest <{cfg.smtp.sender}>"
    msg["To"] = cfg.recipient
    msg["Reply-To"] = cfg.reply_address
    msg[DIGEST_HEADER] = "1"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    if cfg.smtp.port == 465:
        with smtplib.SMTP_SSL(cfg.smtp.host, cfg.smtp.port, context=ctx) as s:
            s.login(cfg.smtp.username, cfg.smtp.password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp.host, cfg.smtp.port) as s:
            s.starttls(context=ctx)
            s.login(cfg.smtp.username, cfg.smtp.password)
            s.send_message(msg)
    log.info("Sent digest to %s", cfg.recipient)


@dataclass
class FeedbackMessage:
    message_id: str
    sender: str
    subject: str
    date: str
    body: str


_QUOTE_START = re.compile(
    r"^(On .{0,200}wrote:|-{2,}\s*Original Message|From:\s.+|_{5,}|Sent from my )", re.IGNORECASE
)


def strip_quoted(body: str) -> str:
    """Keep only what the user typed above the quoted digest."""
    out = []
    for line in body.splitlines():
        if _QUOTE_START.match(line.strip()):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(blockquote|div class=\"gmail_quote\").*", "", html)  # drop quoted part
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", "", html))


def _body_text(msg: email.message.Message) -> str:
    plain, html = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and html is None:
            html = text
    return strip_quoted(plain if plain is not None else _html_to_text(html or ""))


def is_feedback(msg: email.message.Message, allowed_senders: list[str]) -> bool:
    if msg.get(DIGEST_HEADER):
        return False  # our own digest, e.g. when sender and recipient share a mailbox
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if sender not in allowed_senders:
        return False
    subject = str(msg.get("Subject", ""))
    return SUBJECT_PREFIX.lower() in subject.lower() and (
        re.match(r"^\s*(re|fwd?|aw)\s*:", subject, re.IGNORECASE) is not None or "feedback" in subject.lower()
    )


def fetch_feedback(cfg: Config, since: date, seen_ids: set[str]) -> list[FeedbackMessage]:
    """Return feedback replies from allowed senders that have not been processed yet."""
    if not cfg.imap:
        log.info("IMAP not configured; skipping email feedback")
        return []
    results: list[FeedbackMessage] = []
    with imaplib.IMAP4_SSL(cfg.imap.host, cfg.imap.port) as conn:
        conn.login(cfg.imap.username, cfg.imap.password)
        conn.select(cfg.imap.mailbox, readonly=True)
        since_s = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        uids: set[bytes] = set()
        for sender in cfg.feedback_senders:
            typ, data = conn.search(None, "SINCE", since_s, "FROM", f'"{sender}"', "SUBJECT", '"Consumer M&A Digest"')
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        for uid in sorted(uids, key=int):
            typ, data = conn.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            mid = (msg.get("Message-ID") or f"uid-{uid.decode()}").strip()
            if mid in seen_ids or not is_feedback(msg, cfg.feedback_senders):
                continue
            body = _body_text(msg)
            if body:
                results.append(FeedbackMessage(mid, parseaddr(msg["From"])[1], str(msg["Subject"]),
                                               str(msg.get("Date", "")), body))
    log.info("Found %d new feedback email(s)", len(results))
    return results
