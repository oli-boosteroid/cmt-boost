"""
Per-user notification delivery (the bot side).

The CMT Console lets each team member subscribe to notification types (new Reddit post, new Discord
ticket, new modmail message, ...) and choose channels: Telegram DM, Slack DM, and Web Push (which
also covers installed-PWA mobile). Settings live in Mongo (`console_notify_prefs`, written by the
console). This module reads them and fans one event out to every subscribed user's enabled + linked
channels, honoring each user's per-weekday time window (Europe/Kiev).

Runs inside the bot process, which holds the Telegram/Slack tokens and the VAPID private key. Every
send is best-effort and isolated: a failure on one channel never blocks the others, and a failure
for one user never blocks the rest.
"""
import os
import json
import logging
from datetime import datetime, timezone, timedelta

from services.mongo_db_service import sync_db

logger = logging.getLogger(__name__)

_prefs_col = sync_db["console_notify_prefs"]
_push_col = sync_db["console_push_subs"]
_users_col = sync_db["console_users"]
_feed_col = sync_db["console_notify_feed"]   # in-app notification center (the console bell)

NOTIFY_TYPES = ["reddit_post", "reddit_modmail", "reddit_modmail_msg",
                "discord_ticket", "discord_msg", "discord_forum", "discord_mod_mention",
                "jira_cm", "jira_urg_review", "jira_l1",
                "feedback_new", "leave_request", "ki_suggest"]

# Admin-only events: default OFF, delivered only to admins who explicitly enable them, and hidden
# from non-admins' notification centre (admin notifications for new feedback / leave requests).
_ADMIN_EVENT_TYPES = {"feedback_new", "leave_request", "ki_suggest"}


def _kyiv_now():
    """Current Europe/Kiev time (UTC+2, +3 during DST). Mirrors the console clock so a user's
    schedule means the same wall-clock time they set in the UI."""
    now = datetime.now(timezone.utc)
    y = now.year

    def last_sunday(month):
        d = datetime(y, month, 31, tzinfo=timezone.utc)
        while d.weekday() != 6:
            d -= timedelta(days=1)
        return d

    dst = last_sunday(3).replace(hour=1) <= now < last_sunday(10).replace(hour=1)
    return now + timedelta(hours=3 if dst else 2)


def _within_schedule(prefs, when=None):
    when = when or _kyiv_now()
    sched = prefs.get("schedule") or {}
    if not sched:
        return True                       # no schedule at all -> allow

    def _mins(s):
        try:
            h, m = str(s).split(":"); return int(h) * 60 + int(m)
        except Exception:
            return None

    def _slot(day):
        s = sched.get(str(day % 7))
        if not s or not s.get("on", True):
            return None
        a, b = _mins(s.get("start") or "00:00"), _mins(s.get("end") or "23:59")
        return (a, b) if (a is not None and b is not None) else None

    cur = when.hour * 60 + when.minute
    # TODAY's window. For an overnight slot (start>end, e.g. 22:00-06:00) today only contributes its
    # evening part (start -> midnight); the morning part belongs to the NEXT calendar day.
    today = _slot(when.weekday())
    if today:
        a, b = today
        if a <= b:
            if a <= cur <= b:
                return True
        elif cur >= a:
            return True
    # An overnight window that STARTED YESTERDAY carrying into this morning (yesterday start -> today end).
    prev = _slot(when.weekday() - 1)
    if prev:
        a, b = prev
        if a > b and cur <= b:
            return True
    return False


_SCHED_DAYKEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]   # Python weekday() -> work schedule key
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]


def _hm_mins(s):
    try:
        h, m = str(s).split(":"); return int(h) * 60 + int(m)
    except Exception:
        return None


def _within_profile_hours(user, when):
    """True if `when` is inside the member's PROFILE working hours (console_users.schedule, keyed mon..sun;
    overnight shifts ok). Used when a user picked 'Follow my working hours' - the notify prefs then hold no
    custom schedule, so the profile shift is the gate. No profile schedule at all -> allow (don't over-block)."""
    sched = (user or {}).get("schedule") or {}
    if not sched:
        return True

    def _slot(i):
        s = sched.get(_SCHED_DAYKEYS[i % 7])
        if not s or not s.get("on", True):
            return None
        a, b = _hm_mins(s.get("start") or "09:00"), _hm_mins(s.get("end") or "18:00")
        return (a, b) if (a is not None and b is not None) else None

    cur = when.hour * 60 + when.minute
    t = _slot(when.weekday())
    if t:
        a, b = t
        if (a <= b and a <= cur < b) or (a > b and cur >= a):
            return True
    p = _slot(when.weekday() - 1)   # overnight window that started yesterday, spilling into this morning
    if p:
        a, b = p
        if a > b and cur <= b:
            return True
    return False


def _schedule_ok(prefs, user, when):
    """Which schedule gates notifications: the profile working hours when 'Follow my working hours' is on,
    otherwise the custom per-day notify schedule stored in the prefs."""
    if prefs.get("use_working_hours"):
        return _within_profile_hours(user or {}, when)
    return _within_schedule(prefs, when)


def _today_cal_cell(member, when):
    """The member's calendar value (VACATION / SICK LEAVE / a shift / "") for `when`'s date, Europe/Kyiv."""
    if not member:
        return ""
    monthkey = _MONTH_NAMES[when.month - 1] + " " + str(when.year)
    doc = sync_db["team_schedule"].find_one({"$or": [{"month": monthkey}, {"_id": monthkey}]}) or {}
    today = "%02d.%02d.%04d" % (when.day, when.month, when.year)
    for d in (doc.get("days") or []):
        if d.get("date") == today:
            return (d.get("shifts") or {}).get(member, "")
    return ""


def _user_available_now(user_email, when=None):
    """False when the member must NOT be pinged right now: they are on Vacation/Sick/Offline today (per the
    calendar, resolved through their profile's schedule_member binding), OR inside their Testing window (a
    block of the shift when they are off the console). True the rest of the time (Duty / normal hours)."""
    when = when or _kyiv_now()
    user = _users_col.find_one({"email": user_email}) or {}
    member = user.get("schedule_member") or user.get("display_name") or user.get("name")
    cell = str(_today_cal_cell(member, when) or "").strip().upper()
    if cell in ("VACATION", "SICK LEAVE", "SICK", "OFFLINE"):
        return False
    slot = (user.get("schedule") or {}).get(_SCHED_DAYKEYS[when.weekday()]) or {}
    a, b = _hm_mins(slot.get("test_start")), _hm_mins(slot.get("test_end"))
    if a is not None and b is not None and a != b:
        cur = when.hour * 60 + when.minute
        if (a <= cur < b) if a < b else (cur >= a or cur < b):
            return False   # inside the Testing window
    return True


def _send_telegram(chat_id, title, body, url):
    try:
        from telegram_bot.sender import send_to_user
        text = title + (("\n" + body) if body else "") + (("\n" + url) if url else "")
        return bool(send_to_user(int(chat_id), {"text": text}))
    except Exception as e:
        logger.warning("[UserNotify] telegram send failed: %s", e)
        return False


def _send_slack(slack_id, title, body, url):
    try:
        from services.mongo_db_service import client as slack_client
        text = "*%s*" % title + (("\n%s" % body) if body else "") + (("\n%s" % url) if url else "")
        slack_client.chat_postMessage(channel=slack_id, text=text)   # a user id opens a DM
        return True
    except Exception as e:
        logger.warning("[UserNotify] slack send failed: %s", e)
        return False


def _send_webpush(user_email, title, body, url):
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        logger.info("[UserNotify] pywebpush not installed - web push skipped")
        return 0
    priv = os.getenv("VAPID_PRIVATE_KEY")
    if not priv:
        return 0
    claims_sub = os.getenv("VAPID_SUBJECT", "mailto:admin@cmtconsole.xyz")
    payload = json.dumps({"title": title, "body": body or "", "url": url or "/console/"})
    sent = 0
    for sub in _push_col.find({"user_email": user_email}):
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": sub.get("keys", {})},
                data=payload,
                vapid_private_key=priv,
                vapid_claims={"sub": claims_sub},
            )
            sent += 1
            if sub.get("fails"):
                _push_col.update_one({"_id": sub["_id"]}, {"$unset": {"fails": ""}})
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):        # subscription gone -> prune it
                _push_col.delete_one({"_id": sub["_id"]})
            else:
                # A subscription that keeps failing (e.g. 400 from a browser that dropped it): warn once,
                # drop it after 5 failures in a row instead of a warning on every notification.
                fails = int(sub.get("fails") or 0) + 1
                if fails >= 5:
                    _push_col.delete_one({"_id": sub["_id"]})
                    logger.info("[UserNotify] removed a push subscription after %d failed sends (HTTP %s)", fails, code)
                else:
                    _push_col.update_one({"_id": sub["_id"]}, {"$set": {"fails": fails}})
                    if fails == 1:
                        logger.warning("[UserNotify] webpush failed (HTTP %s); will drop the subscription if it keeps failing", code)
        except Exception as e:
            logger.warning("[UserNotify] webpush error: %s", e)
    return sent


def _deliver(prefs, title, body, url):
    """Send to every enabled + linked channel for one user's prefs doc. Returns a per-channel result
    map (True / device-count / False) so callers like the test button can report what actually went."""
    out = {}
    channels = prefs.get("channels") or {}
    email = prefs.get("user_email")
    user = _users_col.find_one({"email": email}) or {}
    if channels.get("telegram") and user.get("telegram_chat_id"):
        out["telegram"] = _send_telegram(user["telegram_chat_id"], title, body, url)
    if channels.get("slack") and user.get("slack_id"):
        out["slack"] = _send_slack(user["slack_id"], title, body, url)
    if channels.get("webpush"):
        out["webpush"] = _send_webpush(email, title, body, url)
    return out


def notify_user(user_email, title, body="", url="", force=False):
    """Deliver to a single user. force=True ignores the master switch + schedule (used by the
    'send test notification' button) but still respects which channels are enabled. Returns the
    per-channel result map (empty if nothing was attempted)."""
    prefs = _prefs_col.find_one({"user_email": user_email}) or {"user_email": user_email}
    if not force:
        if not prefs.get("enabled"):
            return {}
        when = _kyiv_now()
        user = _users_col.find_one({"email": user_email}) or {}
        if not _schedule_ok(prefs, user, when) or not _user_available_now(user_email, when):
            return {}
    return _deliver(prefs, title, body, url)


def _console_link(route):
    """Build a full console URL from a console hash route (e.g. '#/modmail/<id>'). Team notifications
    link to the chat INSIDE the console, not to the social network."""
    base = os.getenv("CONSOLE_URL", "https://cmtconsole.xyz/console/")
    if not route:
        return base
    return (base if base.endswith("/") else base + "/") + route if not route.startswith("#") else base + route


def notify_event(event_type, title, body="", url="", route="", feed=True):
    """Fan a team event out to every user subscribed to `event_type`, each within their own
    schedule + channels. `route` is a console hash route (e.g. '#/modmail/<id>') - when present the
    Telegram/Slack/push link and the in-app bell both point at the chat inside the console rather than
    the social network. `feed=False` skips the bell insert (used when the caller already wrote it, e.g.
    the console's admin alerts). Called from the bot's Reddit / Discord loops. Never raises."""
    if event_type not in NOTIFY_TYPES:
        return
    link = _console_link(route) if route else (url or "")
    # In-app notification center: log every event once (independent of push channels/schedule) so the
    # console bell shows what should have arrived even if the user has no Telegram/Slack/push linked.
    if feed:
        try:
            _feed_col.insert_one({"type": event_type, "title": title, "body": body or "",
                                  "url": url or "", "route": route or "", "created_at": datetime.utcnow(),
                                  "admin_only": event_type in _ADMIN_EVENT_TYPES})
        except Exception as e:
            logger.warning("[UserNotify] feed insert failed: %s", e)
    try:
        when = _kyiv_now()   # compute the clock once, not per user
        _admin_evt = event_type in _ADMIN_EVENT_TYPES
        for prefs in _prefs_col.find({"enabled": True}):
            email = prefs.get("user_email")
            user = _users_col.find_one({"email": email}) or {}
            is_admin = user.get("role") == "admin"
            if _admin_evt and not is_admin:   # admin-only events never go to non-admins
                continue
            default_on = is_admin if _admin_evt else True   # admin events default ON for admins
            if not (prefs.get("types") or {}).get(event_type, default_on):
                continue
            # Admin events (feedback, leave requests, Known Issues suggestions) always reach admins: admins
            # have no off hours / vacation for these. Team events keep the schedule + availability gates.
            if not _admin_evt:
                if not _schedule_ok(prefs, user, when):   # outside working hours (profile) / custom notify schedule
                    continue
                if not _user_available_now(email, when):   # muted during Testing / Vacation / Sick -> Duty only
                    continue
            try:
                _deliver(prefs, title, body, link)
            except Exception as e:
                logger.warning("[UserNotify] deliver to %s failed: %s", prefs.get("user_email"), e)
    except Exception as e:
        logger.error("[UserNotify] notify_event(%s) error: %s", event_type, e)
