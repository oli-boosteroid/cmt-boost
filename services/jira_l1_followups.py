"""
"Ticket reached L1 In Progress -> tell the users we promised to update."

When an SO ticket moves to **L1 In Progress** we look at who is waiting on it:
  * comments written by OUR team (Jira accounts linked in the console profiles) that carry a
    Boosteroid User ID, a streaming session ID, or a link to a Discord ticket / Reddit modmail chat;
  * chats linked to the ticket in our own data (Discord tickets with the key, Reddit modmail convos linked
    to it) and the ticket's Communication Link.

Each user / chat becomes one To-Do entry in the console (`jira_l1_todo`, one document per ticket+target,
cleared with "Done"), and the people on shift get ONE notification listing them all (event `jira_l1`,
delivered through the normal per-user channels, which already respect working hours, Testing and leave).

Accidental moves are filtered: the bot waits CONFIRM_DELAY_SEC (1 min) after the transition and only acts
if Jira still shows the ticket in L1 In Progress.

For a user found in one of our comments we remember that comment (id + author), so the console's "Done"
can answer "Передано" under exactly that comment. Done never changes the ticket's status.

Best-effort: runs in its own thread from the Slack transition handler and never raises.
"""
import time
import logging
import re
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# "User ID: 123", "User ID 123", "user_id=123", "[User ID 123|https://...]" (our own Jira link format)
_UID_RE = re.compile(r"(?i)\buser[\s_]*id\b\W{0,4}(\d{3,12})\b")
_SESSION_RE = re.compile(r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b")
_DISCORD_RE = re.compile(r"discord\.com/channels/\d+/(\d+)")
_REDDIT_RE = re.compile(r"\breddit\.com/mail/[^/\s]+/(\w+)")

CONFIRM_DELAY_SEC = 60   # protection against an accidental status change: act only if it is still there
L1_STATUS = "L1 In Progress"

_recent = {}          # issue_key -> datetime of the last run (a redelivered Slack event must not double-notify)
_recent_lock = threading.Lock()


def _col():
    from services.mongo_db_service import sync_db
    return sync_db["jira_l1_todo"]


def _status_and_comments(issue_key):
    """(status, comments) with ONE Jira request (issue fields status + comment). ("", None) on error."""
    from services.jira_service import JIRA_BASE_URL, _auth_headers, _configured
    import requests
    if not _configured():
        return "", None
    try:
        r = requests.get(f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}", params={"fields": "status,comment"},
                         headers=_auth_headers(), timeout=15)
        if r.status_code != 200:
            return "", None
        f = (r.json() or {}).get("fields") or {}
        return ((f.get("status") or {}).get("name") or ""), ((f.get("comment") or {}).get("comments") or [])
    except Exception as e:
        logger.warning("[L1] issue read for %s failed: %s", issue_key, type(e).__name__)
        return "", None


def _all_comments(issue_key):
    return _status_and_comments(issue_key)[1] or []


def _targets_from_text(text, added_by, out, comment_id="", author_id=""):
    """Add every user / chat found in `text`. The FIRST comment that mentions a target is kept as its
    source (comments are read oldest first)."""
    src = {"by": added_by or "", "comment_id": str(comment_id or ""), "author_id": author_id or ""}
    for uid in _UID_RE.findall(text or ""):
        out.setdefault(("uid", uid), dict(src))
    for sid in _SESSION_RE.findall(text or ""):
        out.setdefault(("session", sid.lower()), dict(src))
    for ch in _DISCORD_RE.findall(text or ""):
        out.setdefault(("discord", ch), dict(src))
    for cid in _REDDIT_RE.findall(text or ""):
        out.setdefault(("reddit", cid), dict(src))
    # Console deep links (https://cmtconsole.xyz/console/#/discord/<ticket>, #/modmail/<convo>): the same
    # parser the auto-link uses, mapped to the same targets as the social links above.
    try:
        from services.jira_autolink import chats_in
        for kind, ref in chats_in(text or ""):
            if kind == "modmail":
                out.setdefault(("reddit", ref), dict(src))
            elif kind == "discord_channel":
                out.setdefault(("discord", ref), dict(src))
            elif kind == "discord_ticket":
                ch = _channel_of_ticket(ref)
                if ch:
                    out.setdefault(("discord", ch), dict(src))
    except Exception as e:
        logger.debug("[L1] console link parse failed: %s", e)


def _channel_of_ticket(ticket_id):
    """Discord channel id (str) of a discord_tickets _id from a console link, or ""."""
    try:
        from bson import ObjectId
        from services.mongo_db_service import sync_db
        t = sync_db["discord_tickets"].find_one({"_id": ObjectId(ticket_id)}, {"channel_id": 1})
        return str(t["channel_id"]) if t and t.get("channel_id") else ""
    except Exception:
        return ""


def collect_targets(issue_key, communication_link="", comments=None):
    """{(kind, value): {by, comment_id, author_id}} for everyone waiting on this ticket.
    kind: uid | session | discord | reddit. comment_id is empty for chats found outside our comments."""
    out = {}
    try:
        from services.identity_service import get_jira_our_user_map
        ours = dict(get_jira_our_user_map() or {})
    except Exception:
        ours = {}
    try:
        from services.jira_service import get_my_account_id
        me = get_my_account_id()
        if me:
            ours.setdefault(me, "CMT bot")   # comments the bot posted for an operator (console / Slack)
    except Exception as _e:
        logger.warning("[jira_l1_followups.py:98] ignored: %s", _e)
    for c in (comments if comments is not None else _all_comments(issue_key)):
        author = (c.get("author") or {})
        aid = author.get("accountId", "")
        if aid not in ours:
            continue
        _targets_from_text(c.get("body") or "", ours.get(aid) or author.get("displayName", ""), out,
                           comment_id=c.get("id", ""), author_id=aid)
    if communication_link:
        _targets_from_text(communication_link, "Communication Link", out)
    try:
        from services.mongo_db_service import sync_db
        for t in sync_db["discord_tickets"].find(
                {"$or": [{"jira_ticket_key": issue_key}, {"jira_ticket_keys": issue_key}]},
                {"channel_id": 1, "status": 1}):
            if t.get("channel_id") and t.get("status") != "closed":
                out.setdefault(("discord", str(t["channel_id"])), {"by": "linked chat", "comment_id": "", "author_id": ""})
        for u in sync_db["jira_user_links"].find({"issue_key": issue_key}, {"user_id": 1}):   # Quick Actions > Link Ticket
            if u.get("user_id"):
                out.setdefault(("uid", str(u["user_id"])), {"by": "linked user", "comment_id": "", "author_id": ""})
        for n in sync_db["reddit_mod_notes"].find({"jira_convos.%s" % issue_key: {"$exists": True}},
                                                  {"jira_convos": 1}):
            cid = (n.get("jira_convos") or {}).get(issue_key)
            if cid:
                out.setdefault(("reddit", str(cid)), {"by": "linked chat", "comment_id": "", "author_id": ""})
    except Exception as e:
        logger.debug("[L1] linked chat lookup failed for %s: %s", issue_key, e)
    return out


_KIND_LABEL = {"uid": "User ID %s", "session": "Session %s", "discord": "Discord ticket (channel %s)",
               "reddit": "Reddit modmail %s"}


def _label(kind, value):
    v = value[:8] if kind == "session" else value
    return _KIND_LABEL.get(kind, "%s") % v


def handle(issue_key, summary="", url="", communication_link=""):
    """Entry point from the transition handler (call in a thread)."""
    now = datetime.utcnow()
    with _recent_lock:
        last = _recent.get(issue_key)
        if last and now - last < timedelta(minutes=2):
            return
        _recent[issue_key] = now
    try:
        # Protection against an accidental move: wait, then confirm with Jira that it is still in L1.
        if CONFIRM_DELAY_SEC:
            time.sleep(CONFIRM_DELAY_SEC)
        live, comments = _status_and_comments(issue_key)   # status re-check + comments in one request
        if live and live != L1_STATUS:
            logger.info("[L1] %s left L1 In Progress within %ds (now %r) - treated as accidental, skipped",
                        issue_key, CONFIRM_DELAY_SEC, live)
            return
        now = datetime.utcnow()
        targets = collect_targets(issue_key, communication_link, comments)
        if not targets:
            logger.info("[L1] %s reached L1 In Progress - no users/chats to notify", issue_key)
            return
        col = _col()
        for (kind, value), src in targets.items():
            col.update_one({"_id": "%s|%s|%s" % (issue_key, kind, value)}, {
                "$set": {"issue_key": issue_key, "summary": summary or "", "url": url or "", "kind": kind,
                         "value": value, "label": _label(kind, value), "added_by": src.get("by", ""),
                         "comment_id": src.get("comment_id", ""), "comment_author": src.get("author_id", ""),
                         "entered_at": now, "done": False},
                "$unset": {"done_by": "", "done_at": "", "replied_at": "", "reply_id": ""},
            }, upsert=True)
        lines = ["- %s%s" % (_label(k, v), (" (added by %s)" % s["by"]) if s.get("by") and s["by"] != "linked chat" else "")
                 for (k, v), s in targets.items()]
        title = "%s moved to L1 In Progress - %d user(s) to update" % (issue_key, len(targets))
        body = ((summary + "\n") if summary else "") + "\n".join(lines)
        from services.user_notify_service import notify_event
        notify_event("jira_l1", title, body, url or "", "#/inbox")
        logger.info("[L1] %s: %d target(s) queued in To-Do and notified", issue_key, len(targets))
    except Exception as e:
        logger.warning("[L1] follow-ups for %s failed: %s", issue_key, e)
