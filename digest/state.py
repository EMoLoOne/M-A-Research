"""Small JSON state files committed back to the repo after each run."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .config import DIGESTS_DIR, RUN_STATE_PATH, SENT_DEALS_PATH


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_run_state() -> dict[str, Any]:
    return _load(RUN_STATE_PATH, {})


def save_run_state(state: dict[str, Any]) -> None:
    _save(RUN_STATE_PATH, state)


def recent_sent_deals(today: date, days: int) -> list[dict]:
    cutoff = (today - timedelta(days=days)).isoformat()
    return [d for d in _load(SENT_DEALS_PATH, {"deals": []})["deals"] if d.get("date", "") >= cutoff]


def record_sent_deals(today: date, deals: list[dict], keep_days: int = 60) -> None:
    data = _load(SENT_DEALS_PATH, {"deals": []})
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    kept = [d for d in data["deals"] if d.get("date", "") >= cutoff and d.get("date") != today.isoformat()]
    kept.extend(
        {"date": today.isoformat(), "acquirer": d["acquirer"], "target": d["target"]} for d in deals
    )
    _save(SENT_DEALS_PATH, {"deals": kept})


def save_digest(day: date, payload: dict) -> None:
    """Archive what was sent so feedback like 'D3 -' can be resolved to a deal later."""
    _save(DIGESTS_DIR / f"{day.isoformat()}.json", payload)


def load_recent_digests(today: date, days: int = 14) -> dict[str, dict]:
    out = {}
    for i in range(days):
        d = today - timedelta(days=i)
        payload = _load(DIGESTS_DIR / f"{d.isoformat()}.json", None)
        if payload:
            out[d.isoformat()] = payload
    return out
