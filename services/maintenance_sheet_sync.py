"""
Maintenance sheet sync (bot side) - append ONE row to a Google Sheet when a game maintenance starts,
via a Google Apps Script Web App (same pattern as known_issues / game_cards / schedule).

APPEND-ONLY. It never reads, edits or deletes existing rows - the sheet is hand-maintained, so we only
add a new row at the start of a game maintenance and never touch anything else.

Column mapping (the Apps Script owns the exact layout / merges):
  A:C  Game Name          <- game
  D:E  Launcher           <- left empty (the console doesn't know the launcher)
  F:G  Status             <- "Ready to work" (default at start)
  H:I  Reason             <- left empty (filled by hand later)
  J:K  ETA                <- left empty (filled by hand later)
  L    additional comment <- left empty
  M    Maintenance in     <- "All playgrounds" (general) | "Only for Brazil" (Brazil-only)
  N    start time         <- Kyiv "dd.mm.yyyy HH:MM:SS"

Config: webapp_url in console_config key "maintenance_sheet" (or env MAINTENANCE_SHEET_WEBAPP_URL);
secret in env MAINTENANCE_SHEET_SECRET (falls back to KNOWN_ISSUES_SECRET).
"""
import logging
import os
from services.apps_script_url import safe_webapp_url
from datetime import datetime

import pytz
import requests

logger = logging.getLogger(__name__)

_KYIV = pytz.timezone("Europe/Kyiv")


def _db():
    from services.db import main_db
    sync_db = main_db()
    return sync_db


def _webapp_url():
    doc = _db()["console_config"].find_one({"key": "maintenance_sheet"}) or {}
    return safe_webapp_url(((doc.get("value") or {}).get("webapp_url") or os.getenv("MAINTENANCE_SHEET_WEBAPP_URL", "") or "").strip(), "maintenance_sheet")


def _secret():
    return (os.getenv("MAINTENANCE_SHEET_SECRET", "") or os.getenv("KNOWN_ISSUES_SECRET", "") or "").strip()


def configured():
    return bool(_webapp_url() and _secret())


def _fmt_started(started):
    """Format a datetime / ISO string as Kyiv 'dd.mm.yyyy HH:MM:SS'; defaults to now on anything odd."""
    dt = None
    if isinstance(started, datetime):
        dt = started
    elif started:
        try:
            dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        except Exception:
            dt = None
    if dt is None:
        dt = datetime.utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.utc)
    return dt.astimezone(_KYIV).strftime("%d.%m.%Y %H:%M:%S")


def _read_json(r):
    try:
        return r.json(), None
    except Exception:
        body = (r.text or "").strip().replace("\n", " ")
        hint = " - web app returned HTML, not JSON. Re-deploy with Access = 'Anyone', use the /exec URL." if body[:1] == "<" else ""
        return None, "HTTP %s: %s%s" % (r.status_code, body[:160], hint)


def push_maintenance_start(game, maintenance_in, started):
    """Append one row for a game maintenance that just started. `started` may be a datetime or ISO
    string. Best-effort; returns (ok, err). Silently no-ops (ok=True) when not configured, so callers
    never fail just because the sheet integration isn't set up."""
    game = (game or "").strip()
    if not game:
        return False, "game required"
    url, secret = _webapp_url(), _secret()
    if not (url and secret):
        logger.debug("[MaintSheet] not configured; skipping append for %r", game)
        return True, None
    payload = {"token": secret, "action": "add", "game": game, "status": "Ready to work",
               "maintenance_in": maintenance_in or "", "started_at": _fmt_started(started)}
    try:
        r = requests.post(url, json=payload, timeout=30)
    except Exception as e:
        logger.error("[MaintSheet] request failed for %r: %s", game, e)
        return False, "request failed: %s" % e
    data, err = _read_json(r)
    if err:
        logger.error("[MaintSheet] append failed for %r: %s", game, err)
        return False, err
    if data.get("error"):
        logger.error("[MaintSheet] web app error for %r: %s", game, data.get("error"))
        return False, str(data.get("error"))
    logger.info("[MaintSheet] appended row for %r (%s)", game, maintenance_in)
    return True, None
