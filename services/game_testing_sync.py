"""
Game Testing sync (bot side): the weekly game-testing sheet ("26/10 - 30/10" tabs) via a Google Apps
Script Web App (services/game_testing_apps_script.gs).

- sync_once(): read the newest KEEP_WEEKS week tabs (columns, their dropdown answers, games and current
  answers) into sync_db.game_testing, one doc per week; drop older weeks.
- set_cell(): write one answer to the sheet, then update the same cell in Mongo so every console sees it.

Config: webapp_url in console_config key "game_testing" (Admin) or env GAME_TESTING_WEBAPP_URL; secret in
env GAME_TESTING_SECRET (falls back to GAME_CARDS_SECRET / KNOWN_ISSUES_SECRET).
"""
import logging
import os
from services.apps_script_url import safe_webapp_url
import re
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

FAST_INTERVAL = 60     # Thu/Fri (Kyiv): new games for testing usually arrive then, so poll every minute
KEEP_WEEKS = 3         # newest week tabs to read (each tab: Steam on top, Xbox below the gap)
# Automatic polls read only the newest week tab (the current week once it exists): older weeks do not
# change, and console edits update Mongo directly. All KEEP_WEEKS are read only by the console Sync
# button, or at startup when Mongo does not have them yet.
READ_TIMEOUT = (10, 120)   # connect, read seconds: the Apps Script can take a while on big sheets
MAX_BACKOFF = 600      # after failures wait 2, 4, 8 ... minutes, at most 10
WARN_AFTER = 3         # log a WARNING only when this many polls in a row failed (single timeouts are normal)
_WEEK_RE = re.compile(r"(\d{1,2}/\d{1,2})\s*-\s*(\d{1,2}/\d{1,2})")


def _week_label(name):
    m = _WEEK_RE.search(name or "")
    return ("%s - %s" % m.groups()) if m else name


def _poll_now():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        now = datetime.utcnow()
    return now.weekday() in (3, 4)   # Thursday, Friday
_lock = threading.Lock()
_started = False


def _db():
    from services.db import main_db
    sync_db = main_db()
    return sync_db


def _webapp_url():
    doc = _db()["console_config"].find_one({"key": "game_testing"}) or {}
    return safe_webapp_url(((doc.get("value") or {}).get("webapp_url") or os.getenv("GAME_TESTING_WEBAPP_URL", "") or "").strip(), "game_testing")


def _secret():
    return (os.getenv("GAME_TESTING_SECRET", "") or os.getenv("GAME_CARDS_SECRET", "")
            or os.getenv("KNOWN_ISSUES_SECRET", "") or "").strip()


def configured():
    return bool(_webapp_url() and _secret())


def _json(r):
    try:
        return r.json(), None
    except Exception:
        body = (r.text or "").strip().replace("\n", " ")
        hint = " - web app returned HTML, not JSON. Re-deploy with Access = 'Anyone', use the /exec URL." if body[:1] == "<" else ""
        return None, "HTTP %s: %s%s" % (r.status_code, body[:160], hint)


def sync_once(weeks=KEEP_WEEKS):
    """Read the newest `weeks` week tabs into Mongo (weeks=1: just the current week). Weeks older than the
    newest KEEP_WEEKS tabs are dropped from Mongo either way, using the full tab list the script returns."""
    url, secret = _webapp_url(), _secret()
    if not url:
        return False, "webapp_url not configured"
    if not secret:
        return False, "GAME_TESTING_SECRET not set on the bot"
    with _lock:
        try:
            r = requests.get(url, params={"token": secret, "weeks": weeks}, timeout=READ_TIMEOUT)
        except Exception as e:
            return False, "read request failed: %s" % e
        data, err = _json(r)
        if err:
            return False, "read failed %s" % err
        if not (isinstance(data, dict) and data.get("ok")):
            return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")
        got = data.get("weeks") or {}
        all_order = data.get("order") or []          # every week tab, newest first
        order = [w for w in all_order if w in got]   # the ones read now
        col = _db()["game_testing"]
        now = datetime.utcnow().isoformat()
        for name in order:
            w = got.get(name) or {}
            col.update_one({"_id": name}, {"$set": {"week": name, "week_label": _week_label(name),
                                                    "order_index": all_order.index(name), "columns": w.get("columns") or [],
                                                    "rows": w.get("rows") or [], "synced_at": now}}, upsert=True)
        # Keep the newest KEEP_WEEKS tabs (also the ones not re-read now), drop anything older.
        if all_order:
            col.delete_many({"_id": {"$nin": all_order[:KEEP_WEEKS]}})
            for i, name in enumerate(all_order[:KEEP_WEEKS]):
                col.update_one({"_id": name}, {"$set": {"order_index": i}})
        return True, "%d weeks" % len(order)


def set_cell(week, row, column, value, game=""):
    url, secret = _webapp_url(), _secret()
    if not (url and secret):
        return False, "game testing sheet not configured"
    try:
        r = requests.post(url, json={"token": secret, "action": "set", "week": week, "row": int(row),
                                     "col": column, "value": value, "game": game}, timeout=30)
    except Exception as e:
        return False, "request failed: %s" % e
    data, err = _json(r)
    if err:
        return False, err
    if not (isinstance(data, dict) and data.get("ok")):
        return False, (data.get("error") if isinstance(data, dict) else "bad response")
    _db()["game_testing"].update_one({"_id": week, "rows.row": int(row)}, {"$set": {"rows.$.values." + column: value}})
    return True, "ok"


def _loop():
    time.sleep(30)
    first, fails = True, 0
    while True:
        wait = FAST_INTERVAL
        if configured() and (first or _poll_now()):   # once at startup, then only on Thu/Fri
            # Startup fills all KEEP_WEEKS if Mongo is missing some; every other poll reads the current week only.
            full = first and _db()["game_testing"].count_documents({}) < KEEP_WEEKS
            first = False
            try:
                ok, msg = sync_once(KEEP_WEEKS if full else 1)
            except Exception as e:
                ok, msg = False, "error: %s" % e
            if ok:
                if fails >= WARN_AFTER:
                    logger.info("[GameTesting] sync recovered after %d failed polls", fails)
                fails = 0
            else:
                fails += 1
                # Back off so a slow sheet is not hammered every minute: 2, 4, 8 ... min, max 10.
                wait = min(MAX_BACKOFF, FAST_INTERVAL * (2 ** fails))
                log = logger.warning if fails >= WARN_AFTER else logger.info
                log("[GameTesting] sync failed (%d in a row, next try in %ds): %s", fails, wait, msg)
        time.sleep(wait)


def start_game_testing_sync():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="game-testing-sync").start()
