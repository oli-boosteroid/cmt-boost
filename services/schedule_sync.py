"""
Team schedule sync (bot side) - via a Google Apps Script Web App on the schedule spreadsheet.

Read-mirror the monthly schedule tabs into sync_db.team_schedule (one doc per month) so the console can
show them, and forward admin edits (set a cell) + "create new month" back to the sheet. NOT tied to the
reminders / on-shift automation - it is just the team's viewable, editable schedule.

Web App actions (see services/schedule_apps_script.gs):
  GET  ?token&action=read                 -> { ok, months:{ "<tab>":{members,days:[{weekday,date,shifts}]}}, order:[...] }
  POST {token, action:"set", month, date, member, value}
  POST {token, action:"create_month", month, year, members:[...]}

Config:
  webapp_url  - console_config key "schedule" (Admin > Schedule), or env SCHEDULE_WEBAPP_URL.
  secret      - env SCHEDULE_SECRET (falls back to KNOWN_ISSUES_SECRET so one secret can serve both).
"""
import logging
import os
from services.apps_script_url import safe_webapp_url
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

SYNC_INTERVAL = 86400  # once a day (edits + "Sync now" refresh on demand; keep it light)
_lock = threading.Lock()
_started = False


def _db():
    from services.db import main_db
    sync_db = main_db()
    return sync_db


def _webapp_url():
    doc = _db()["console_config"].find_one({"key": "schedule"}) or {}
    return safe_webapp_url(((doc.get("value") or {}).get("webapp_url") or os.getenv("SCHEDULE_WEBAPP_URL", "") or "").strip(), "schedule")


def _secret():
    return (os.getenv("SCHEDULE_SECRET", "") or os.getenv("KNOWN_ISSUES_SECRET", "") or "").strip()


def configured():
    return bool(_webapp_url() and _secret())


_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December"]


def _allowed_months():
    """Previous, current and next month names (e.g. 'August 2026'), matching the console's window."""
    from datetime import date
    today = date.today()
    out = set()
    for off in (-1, 0, 1):
        m = today.month - 1 + off  # 0-based
        y = today.year
        while m < 0:
            m += 12
            y -= 1
        while m > 11:
            m -= 12
            y += 1
        out.add("%s %d" % (_MONTH_NAMES[m], y))
    return out


def _read_json(r):
    try:
        return r.json(), None
    except Exception:
        body = (r.text or "").strip().replace("\n", " ")
        hint = ""
        if body[:1] == "<" or "sign in" in body.lower():
            hint = (" — the web app returned HTML, not JSON. Re-deploy with Access = 'Anyone' and use the "
                    "/exec URL (Deploy -> Manage deployments).")
        return None, "HTTP %s: %s%s" % (r.status_code, body[:160], hint)


def sync_once():
    url, secret = _webapp_url(), _secret()
    if not url:
        return False, "webapp_url not configured"
    if not secret:
        return False, "SCHEDULE_SECRET not set on the bot"
    try:
        r = requests.get(url, params={"token": secret, "action": "read"}, timeout=25)
    except Exception as e:
        return False, "read request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return False, "read failed %s" % err
    if not (isinstance(data, dict) and data.get("ok")):
        return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")
    months = data.get("months") or {}
    order = data.get("order") or list(months.keys())
    # Keep the DB lean: mirror only the previous, current and next month (the console never shows more).
    # The full history stays in the Google Sheet.
    allowed = _allowed_months()
    col = _db()["team_schedule"]
    seen = set()
    for i, name in enumerate(order):
        if allowed and name not in allowed:
            continue
        m = months.get(name) or {}
        col.update_one({"_id": name},
                       {"$set": {"month": name, "order_index": i,
                                 "members": m.get("members") or [], "days": m.get("days") or [],
                                 "synced_at": datetime.utcnow().isoformat()}},
                       upsert=True)
        seen.add(name)
    # Drop months no longer in the window (or deleted/renamed in the sheet).
    for doc in col.find({}, {"_id": 1}):
        if doc["_id"] not in seen:
            col.delete_one({"_id": doc["_id"]})
    return True, "%d months (kept %d)" % (len(order), len(seen))


def _post(payload):
    url, secret = _webapp_url(), _secret()
    if not url:
        return False, "webapp_url not configured"
    if not secret:
        return False, "SCHEDULE_SECRET not set on the bot"
    payload = dict(payload)
    payload["token"] = secret
    try:
        r = requests.post(url, json=payload, timeout=25)
    except Exception as e:
        return None, "request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return None, err
    return data, None


def set_cell(month, date, member, value, member_index=None):
    data, err = _post({"action": "set", "month": month, "date": date, "member": member,
                       "member_index": member_index, "value": value})
    if err:
        return False, err
    if isinstance(data, dict) and data.get("ok"):
        try:
            sync_once()
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        return True, "set"
    return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")


def create_month(month, year, members):
    data, err = _post({"action": "create_month", "month": month, "year": year, "members": members or []})
    if err:
        return False, err
    if isinstance(data, dict) and data.get("ok"):
        try:
            sync_once()
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        return True, data.get("name") or "created"
    return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")


def _loop():
    time.sleep(20)
    while True:
        try:
            if configured():
                with _lock:
                    sync_once()
        except Exception as e:
            logger.error("[Schedule] sync loop error: %s", e)
        time.sleep(SYNC_INTERVAL)


def start_schedule_sync():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="schedule-sync").start()
    logger.info("[Schedule] sync scheduler started (every %ds, Apps Script Web App)", SYNC_INTERVAL)
