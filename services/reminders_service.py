"""
Reminders / SLA-nudge scheduler (bot side).

Nudges the team when work sits too long. Config lives in console_config (key='reminders', set from the
Admin > Reminders panel). Key rule: elapsed time counts only while someone is on shift - each check
tick adds the interval to a per-item accumulator ONLY when a CM is on shift; when nobody is on shift the
timers pause. When an item's accumulated on-shift minutes cross its threshold, one reminder fires to the
Telegram work group (send_to_meet_group) and to each on-shift CM via their console notification settings.

"On shift" uses each member's working-hours schedule (console_users.schedule, keyed mon..sun), in
Europe/Kiev. Per-item accumulator state is in console_reminders_state (auto-cleared when the item is no
longer stale, and reset when the item's anchor timestamp changes, e.g. a reply arrives / FB re-checked).

Config keys (minutes): check_every_min, reddit_post_min, modmail_new_min, modmail_review_min,
facebook_min, jira_stuck_min, mention_min; flags: enabled, pause_no_shift, to_group, to_shift.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from services.mongo_db_service import sync_db
from services.user_notify_service import _kyiv_now

logger = logging.getLogger(__name__)

try:
    from services.mongo_db_service import snapshot_db
except Exception:                     # snapshots may share the same db in some setups
    snapshot_db = sync_db

_state_col = sync_db["console_reminders_state"]
_config_col = sync_db["console_config"]
_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_DEFAULTS = {"enabled": True, "pause_no_shift": True, "to_group": True, "to_shift": True,
             "check_every_min": 15, "repeat_every_min": 15, "reddit_post_min": 120, "modmail_new_min": 60,
             "modmail_review_min": 30, "facebook_min": 240, "jira_stuck_min": 120, "mention_min": 60}


def _config():
    try:
        doc = _config_col.find_one({"key": "reminders"}) or {}
        cfg = dict(_DEFAULTS)
        cfg.update(doc.get("value") or {})
        return cfg
    except Exception:
        return dict(_DEFAULTS)


def _mins(s):
    try:
        h, m = str(s).split(":"); return int(h) * 60 + int(m)
    except Exception:
        return None


def _on_shift(schedule, when):
    """True if `when` (Kyiv) falls in the member's working-hours schedule (keyed mon..sun; overnight ok)."""
    def slot(i):
        s = (schedule or {}).get(_ORDER[i % 7])
        if not s or not s.get("on", True):
            return None
        a, b = _mins(s.get("start") or "09:00"), _mins(s.get("end") or "18:00")
        return (a, b) if (a is not None and b is not None) else None
    cur = when.hour * 60 + when.minute
    t = slot(when.weekday())
    if t:
        a, b = t
        if (a <= b and a <= cur <= b) or (a > b and cur >= a):
            return True
    p = slot(when.weekday() - 1)          # overnight slot started yesterday
    if p:
        a, b = p
        if a > b and cur <= b:
            return True
    return False


def _on_shift_members(when):
    out = []
    try:
        # A CM inside their Testing window, or on Vacation/Sick today, is on shift but must NOT be pinged.
        from services.user_notify_service import _user_available_now
        for m in sync_db["console_users"].find({}):
            if (m.get("schedule") and _on_shift(m["schedule"], when)
                    and _user_available_now(m.get("email", ""), when)):
                out.append(m)
    except Exception as e:
        logger.warning("[Reminders] on-shift lookup failed: %s", e)
    return out


def _threshold(cfg, kind):
    return int(cfg.get({
        "reddit_post": "reddit_post_min", "modmail_new": "modmail_new_min",
        "modmail_review": "modmail_review_min", "facebook": "facebook_min",
        "jira": "jira_stuck_min", "mention": "mention_min",
    }.get(kind, "reddit_post_min"), 120))


_HISTORY_DAYS = 5


def _parse_iso(s):
    """Parse the ISO timestamps the console/snapshot store (tolerating a trailing Z, an offset, or
    fractional seconds)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "").split("+")[0].split(".")[0])
    except Exception:
        return None


def _any_dt(v):
    """datetime / ISO string (Z, +00:00, +0000, fractions) / epoch -> naive UTC datetime, else None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(v, (int, float)):
        return datetime.utcfromtimestamp(float(v))
    st = str(v).strip().replace(" ", "T")
    try:
        if st.endswith("Z"):
            st = st[:-1] + "+00:00"
        if len(st) > 5 and st[-5] in "+-" and st[-3] != ":":
            st = st[:-2] + ":" + st[-2:]
        d = datetime.fromisoformat(st)
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        try:
            return datetime.utcfromtimestamp(float(st))
        except Exception:
            return None


def _history_cutoff_dt():
    try:
        d = _config_col.find_one({"key": "history_cutoff"}) or {}
        return _parse_iso(d.get("value"))
    except Exception:
        return None


def _ignored_post_ids():
    try:
        return {d.get("post_id") for d in sync_db["console_post_flags"].find({"ignored": True}, {"post_id": 1})}
    except Exception:
        return set()


def _reddit_post_in_history(post, cutoff_dt, now):
    """Mirror the console's inHistory(): a post drops into History when its last activity is at/before
    the admin 'move all to History' cutoff, or it has had no activity for more than 5 days. Reminders
    must respect this so they never nudge about posts the team already archived (bug #12)."""
    act = _parse_iso(post.get("last_comment_at")) or _parse_iso(post.get("created"))
    if act is None:
        return False
    if cutoff_dt and act <= cutoff_dt:
        return True
    if (now - act) > timedelta(days=_HISTORY_DAYS):
        return True
    return False


def _activity_in_history(ts_value, cutoff_dt, now):
    """True if an item's last-activity ts is at/before the console History cutoff, or older than 5 days
    (mirrors the console's inHistory, which also applies to Discord mentions / Telegram / forum). Used
    so SLA reminders never nudge about items the team already archived. Unknown ts -> not history."""
    act = _parse_iso(ts_value)
    if act is None:
        return False
    if cutoff_dt and act <= cutoff_dt:
        return True
    if (now - act) > timedelta(days=_HISTORY_DAYS):
        return True
    return False


def _candidates(cfg):
    """Stale-eligible work items: {key, kind, label, url, anchor}. anchor = a stable string that changes
    when the item is refreshed (reply/ignore/check), so its timer resets. Each source is best-effort."""
    items = []
    now = datetime.utcnow()
    cutoff_dt = _history_cutoff_dt()
    ignored_ids = _ignored_post_ids()
    # Reddit posts: no reply, not ignored, and not already in the console's History (bug #12).
    try:
        for p in snapshot_db["reddit_posts_snapshot"].find(
                {}, {"post_id": 1, "title": 1, "url": 1, "has_reply": 1, "ignored": 1,
                     "created": 1, "last_comment_at": 1}):
            pid = p.get("post_id")
            if p.get("has_reply") or p.get("ignored") or pid in ignored_ids:
                continue
            if _reddit_post_in_history(p, cutoff_dt, now):
                continue
            items.append({"key": "post:" + str(pid), "kind": "reddit_post",
                          "label": "Reddit post: " + (p.get("title") or "")[:80],
                          "url": p.get("url") or "",
                          "anchor": str(p.get("last_comment_at") or p.get("created") or pid)})
    except Exception as e:
        logger.warning("[Reminders] reddit posts source failed: %s", e)
    # Discord mentions: still New AND not already in the console's History (fix: SLA spam of old,
    # already-archived mentions when reminders are first enabled).
    try:
        for m in sync_db["discord_mentions"].find({"status": "new"}):
            if _activity_in_history(m.get("ts"), cutoff_dt, now):
                continue
            g, c, mid = m.get("guild_id") or "", m.get("channel_id") or "", m.get("message_id") or ""
            murl = ("https://discord.com/channels/%s/%s/%s" % (g, c, mid)) if (g and c and mid) else ""
            items.append({"key": "mention:" + str(m["_id"]), "kind": "mention",
                          "label": "Discord mention in #" + (m.get("channel_name") or ""),
                          "url": murl, "anchor": str(m.get("ts") or m["_id"])})
    except Exception as e:
        logger.warning("[Reminders] discord mentions source failed: %s", e)
    # Jira handed to the community team (CM In Progress) and not yet answered - excluding tickets the
    # console already forwarded ("Передано", console_jira_handled), mirroring the To-Do (fix: no
    # reminder for Jira already handled in the console).
    try:
        jira_handled = {}
        try:
            for d in sync_db["console_jira_handled"].find({}, {"key": 1, "status_since": 1}):
                jira_handled[d.get("key", "")] = d.get("status_since") or ""
        except Exception as _e:
            logger.warning("ignored: %s", _e)
        jira_dismissed = {}
        try:
            for d in sync_db["console_jira_dismissed"].find({}, {"key": 1, "status_since": 1}):
                jira_dismissed[d.get("key", "")] = str(d.get("status_since") or "")
        except Exception as _e:
            logger.warning("ignored: %s", _e)
        for j in sync_db["jira_tickets"].find({"console": True, "status": "CM In Progress"}):
            if j.get("cm_awaiting_reply") is False:
                continue
            key = j.get("issue_key")
            since = str(j.get("jira_updated") or "")
            if key in jira_handled and str(jira_handled[key]) == since:
                continue   # forwarded from the console at the current status
            if key in jira_dismissed and jira_dismissed[key] == since:
                continue   # dismissed in the To-Do: hidden there until it re-enters the status, so no SLA
            items.append({"key": "jira:" + str(key), "kind": "jira",
                          "label": "Jira " + str(key) + " (CM In Progress)",
                          "url": j.get("url") or "", "anchor": str(j.get("jira_updated") or key)})
    except Exception as e:
        logger.warning("[Reminders] jira source failed: %s", e)
    # Facebook "group not checked" is NOT a Telegram SLA anymore - the console surfaces it as an unread
    # To-Do reminder instead (built client-side from the fb_group_check last-check time), so it is not a
    # candidate here.
    # Reddit ModMail: New (no mod reply yet) and Needs review (user wrote last), same rules as the
    # console's deriveRedditStatus. A manual console status wins until the user writes after it.
    overrides = {}
    try:
        for d in sync_db["console_convo_status"].find({}, {"convo_id": 1, "status": 1, "updated_at": 1}):
            if d.get("status"):
                overrides[str(d.get("convo_id"))] = d
        for c in snapshot_db["reddit_modmail_snapshot"].find(
                {"state": {"$ne": "archived"}},
                {"convo_id": 1, "subject": 1, "participant": 1, "mod_replied": 1, "last_public_author": 1,
                 "last_activity": 1, "last_updated": 1}):
            cid = str(c.get("convo_id") or "")
            ts = _any_dt(c.get("last_activity")) or _any_dt(c.get("last_updated"))
            # Same as the console's active list: an unknown time or 5+ days idle is History -> never nudged.
            if not cid or ts is None or _activity_in_history(ts.isoformat(), cutoff_dt, now):
                continue
            if c.get("mod_replied") is None:
                continue            # no reply info on this row (old record): the console cannot tell either
            user_last = c.get("last_public_author") == c.get("participant")
            ov = overrides.get(cid)
            if ov:
                act = _parse_iso(c.get("last_activity"))
                upd = ov.get("updated_at")
                stale = (ov.get("status") in ("in_progress", "waiting_user", "done") and user_last
                         and act is not None and isinstance(upd, datetime) and act > upd)
                if not stale:
                    continue        # a manual status (In progress / Waiting / Done / Needs review set by hand)
            if not c.get("mod_replied"):
                kind = "modmail_new"
            elif user_last:
                kind = "modmail_review"
            else:
                continue            # we replied last: waiting on the user
            items.append({"key": "modmail:" + cid, "kind": kind,
                          "label": "Reddit ModMail (%s): %s" % ("New" if kind == "modmail_new" else "Needs review",
                                                               (c.get("subject") or "")[:80]),
                          "url": "https://mod.reddit.com/mail/all/" + cid,
                          "anchor": kind + ":" + str(c.get("last_activity") or "")})
    except Exception as e:
        logger.warning("[Reminders] reddit modmail source failed: %s", e)
    # Discord ModMail tickets (open): New = nobody from the team answered yet, Needs review = the user
    # wrote last. Same holds as the console: a manual status, Postponed and a Jira wait stay quiet until
    # the user writes again.
    try:
        console_url = (os.getenv("CONSOLE_PUBLIC_URL") or "").rstrip("/")
        for t in sync_db["discord_tickets"].find(
                {"status": {"$ne": "closed"}},
                {"ticket_number": 1, "user_name": 1, "first_response_at": 1, "last_message_is_user": 1,
                 "last_message_ts": 1, "postponed_until": 1, "waiting_for_jira": 1, "jira_waiting_reply": 1}):
            tkey = str(t["_id"])
            last_ts = t.get("last_message_ts")
            last_dt = last_ts if isinstance(last_ts, datetime) else _parse_iso(last_ts)
            if last_dt is None or _activity_in_history(last_dt.isoformat(), cutoff_dt, now):
                continue
            user_last = bool(t.get("last_message_is_user"))
            kind = "modmail_new" if not t.get("first_response_at") else ("modmail_review" if user_last else None)
            ov = overrides.get(tkey)
            if ov:
                upd = ov.get("updated_at")
                if not (user_last and last_dt and isinstance(upd, datetime) and last_dt > upd):
                    continue        # manual status holds until the user writes after it
                kind = "modmail_review"
            elif (t.get("postponed_until") or t.get("waiting_for_jira") or t.get("jira_waiting_reply")) \
                    and not user_last:      # the cog stores a postpone as postponed_until (cleared when it ends)
                continue
            if not kind:
                continue            # we answered last: waiting on the user
            items.append({"key": "discord:" + tkey, "kind": kind,
                          "label": "Discord ModMail #%s (%s): %s" % (t.get("ticket_number") or "",
                                                                   "New" if kind == "modmail_new" else "Needs review",
                                                                   t.get("user_name") or ""),
                          "url": (console_url + "/console/#/discord/" + tkey) if console_url else "",
                          "anchor": kind + ":" + str(last_ts or "")})
    except Exception as e:
        logger.warning("[Reminders] discord modmail source failed: %s", e)
    return items


def _tg_mentions(members):
    """Inline Telegram mentions for the on-shift CM(s): <a href="tg://user?id=..">Name</a>. Works
    without a public @username - the person just needs to be in the group. Names are HTML-escaped."""
    import html as _html
    out = []
    for m in members or []:
        tid = m.get("telegram_chat_id") or m.get("telegram_id")
        if not tid:
            continue
        try:
            tid = int(str(tid).strip())
        except (TypeError, ValueError):
            continue
        name = m.get("name") or m.get("alias") or (m.get("email", "").split("@")[0]) or "on-shift"
        out.append('<a href="tg://user?id=%d">%s</a>' % (tid, _html.escape(str(name))))
    return out


def mention_html_for(identifier):
    """HTML @-mention for ONE console user, matched case-insensitively by email, email local part
    (e.g. "o.teleshko"), name or alias. Their Telegram must be linked in the console
    (console_users.telegram_chat_id). '' if not found / not linked."""
    ident = str(identifier or "").strip().lower().lstrip("@")
    if not ident:
        return ""
    try:
        for u in sync_db["console_users"].find({}, {"email": 1, "name": 1, "alias": 1,
                                                    "telegram_chat_id": 1, "telegram_id": 1}):
            email = str(u.get("email") or "").strip().lower()
            keys = {email, email.split("@")[0], str(u.get("name") or "").strip().lower(),
                    str(u.get("alias") or "").strip().lower()}
            if ident in keys:
                return " ".join(_tg_mentions([u]))
    except Exception as e:
        logger.warning("[Reminders] mention lookup for %s failed: %s", ident, e)
    return ""


def on_shift_mentions_html():
    """HTML @-mentions for whoever is on shift RIGHT NOW, for tagging in the Telegram work group;
    '' when nobody on shift is Telegram-linked. Shared so every group alert can tag on-shift CMs."""
    try:
        return " ".join(_tg_mentions(_on_shift_members(_kyiv_now())))
    except Exception:
        return ""


# What the "where to act" link says per source (the SLA points at the actual chat / mention / ticket).
_LINK_LABEL = {"reddit_post": "Open the post", "mention": "Open the mention",
               "jira": "Open the ticket", "facebook": "Open the group"}


def _deliver(cfg, item, threshold, on_shift_members):
    import html as _html
    title = "Reminder - %s" % item["label"]
    body = "No action for %d+ min of shift time." % threshold
    url = item.get("url") or ""
    if cfg.get("to_group", True):
        try:
            from telegram_bot.sender import send_to_meet_group_html
            tags = " ".join(_tg_mentions(on_shift_members))   # ALWAYS @-tag whoever is on shift now
            lines = [_html.escape(title), _html.escape(body)]
            if url:   # a direct link to where the SLA has to be actioned
                lines.append('<a href="%s">%s</a>' % (_html.escape(url, quote=True), _LINK_LABEL.get(item.get("kind"), "Open")))
            send_to_meet_group_html((tags + "\n" if tags else "") + "\n".join(lines))
        except Exception as e:
            logger.warning("[Reminders] meet-group send failed: %s", e)
    if cfg.get("to_shift", True):
        try:
            from services.user_notify_service import notify_user
            import os
            fallback = os.getenv("CONSOLE_URL", "https://cmtconsole.xyz/console/")
            for m in on_shift_members:
                if m.get("email"):
                    notify_user(m["email"], title, body, url or fallback)   # link straight to the item
        except Exception as e:
            logger.warning("[Reminders] per-CM notify failed: %s", e)


DIGEST_MAX_PER_HOUR = 3
DIGEST_MAX_LINES = 30
_DIGEST_ID = "__digest__"


def _deliver_digest(cfg, due, on_shift_members):
    """ONE message with everything that is past its SLA right now (all kinds together)."""
    import html as _html
    import os
    title = "SLA - %d item(s) not handled" % len(due)
    rows, plain = [], []
    for it, minutes, thr in due[:DIGEST_MAX_LINES]:
        label = _html.escape(it["label"])
        url = it.get("url") or ""
        rows.append(('- <a href="%s">%s</a>' % (_html.escape(url, quote=True), label) if url else "- " + label)
                    + " (%d+ min)" % thr)
        plain.append("- %s (%d+ min)" % (it["label"], thr))
    if len(due) > DIGEST_MAX_LINES:
        rows.append("... and %d more" % (len(due) - DIGEST_MAX_LINES))
        plain.append("... and %d more" % (len(due) - DIGEST_MAX_LINES))
    if cfg.get("to_group", True):
        try:
            from telegram_bot.sender import send_to_meet_group_html
            tags = " ".join(_tg_mentions(on_shift_members))   # ALWAYS @-tag whoever is on shift now
            send_to_meet_group_html((tags + "\n" if tags else "") + _html.escape(title) + "\n" + "\n".join(rows))
        except Exception as e:
            logger.warning("[Reminders] meet-group send failed: %s", e)
    if cfg.get("to_shift", True):
        try:
            from services.user_notify_service import notify_user
            console = os.getenv("CONSOLE_URL", "https://cmtconsole.xyz/console/")
            for m in on_shift_members:
                if m.get("email"):
                    notify_user(m["email"], title, "\n".join(plain), console)
        except Exception as e:
            logger.warning("[Reminders] per-CM notify failed: %s", e)


def _digest_allowed() -> bool:
    """At most DIGEST_MAX_PER_HOUR digests in any 60 minutes."""
    d = _state_col.find_one({"_id": _DIGEST_ID}) or {}
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    return len([t for t in (d.get("sent") or []) if isinstance(t, datetime) and t > hour_ago]) < DIGEST_MAX_PER_HOUR


def _digest_sent() -> None:
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    d = _state_col.find_one({"_id": _DIGEST_ID}) or {}
    sent = [t for t in (d.get("sent") or []) if isinstance(t, datetime) and t > hour_ago] + [datetime.utcnow()]
    _state_col.update_one({"_id": _DIGEST_ID}, {"$set": {"sent": sent}}, upsert=True)


def _check():
    cfg = _config()
    if not cfg.get("enabled", True):
        return
    interval = int(cfg.get("check_every_min", 15)) or 15
    repeat = int(cfg.get("repeat_every_min", 15)) or 15   # re-nudge every N on-shift minutes until done
    when = _kyiv_now()
    on_members = _on_shift_members(when)
    if cfg.get("pause_no_shift", True) and not on_members:
        return                              # nobody on shift -> timers paused
    cands = _candidates(cfg)
    live = set()
    states, due, fire = {}, [], False
    for it in cands:
        live.add(it["key"])
        thr = _threshold(cfg, it["kind"])
        st = _state_col.find_one({"_id": it["key"]})
        if not st or st.get("anchor") != it.get("anchor"):     # new item / refreshed -> reset its timer
            st = {"_id": it["key"], "minutes": 0, "last_reminded_min": None, "anchor": it.get("anchor")}
        st["minutes"] = int(st.get("minutes", 0)) + interval
        if st["minutes"] >= thr:
            due.append((it, st["minutes"], thr))
            last_fire = st.get("last_reminded_min")
            if last_fire is None or (st["minutes"] - last_fire) >= repeat:
                fire = True                 # newly overdue, or its repeat interval has passed
        states[it["key"]] = st
    # Everything overdue goes out together in ONE message, at most DIGEST_MAX_PER_HOUR per hour.
    if due and fire and _digest_allowed():
        due.sort(key=lambda x: -x[1])
        _deliver_digest(cfg, due, on_members)
        _digest_sent()
        for it, minutes, thr in due:
            states[it["key"]]["last_reminded_min"] = minutes
        logger.info("[Reminders] digest sent: %d overdue item(s)", len(due))
    for key, st in states.items():
        _state_col.update_one({"_id": key},
                              {"$set": {"minutes": st["minutes"], "last_reminded_min": st.get("last_reminded_min"),
                                        "anchor": st.get("anchor"), "updated_at": datetime.utcnow()}},
                              upsert=True)
    # Drop state for items that are no longer stale (resolved).
    try:
        _state_col.delete_many({"_id": {"$nin": list(live) + [_DIGEST_ID]}})
    except Exception as _e:
        logger.warning("ignored: %s", _e)


def _loop():
    logger.info("[Reminders] scheduler started")
    while True:
        try:
            _check()
        except Exception as e:
            logger.error("[Reminders] check failed: %s", e)
        time.sleep(max(60, int(_config().get("check_every_min", 15)) * 60))


def start_reminders_service():
    """Start the reminders scheduler on a daemon thread. Call once at bot startup."""
    t = threading.Thread(target=_loop, daemon=True, name="reminders-service")
    t.start()
    return t
