"""
Auto-link: a team member writes a Jira comment with a link to a chat -> the ticket is linked to that chat
in the console by itself, the same way as a manual "Link Jira Ticket" (services/jira_chat_link.py), but
without posting anything to Jira. The comment's author is told it happened (personal console bell entry
+ their own Telegram / Slack / push).

Only a CLEAR chat link counts, nothing else (no User ID / session guessing):
  * Discord ticket   https://discord.com/channels/<guild>/<channel>
  * Reddit ModMail   https://mod.reddit.com/mail/<folder>/<convo>
  * Reddit post      https://www.reddit.com/r/<sub>/comments/<post>
  * console          https://cmtconsole.xyz/console/#/discord/<ticket>, #/modmail/<convo>, #/posts/<post>
A link that does not resolve to a chat we know is ignored.

Whose comments: only console users (console_users.jira_account_id). The bot's own Jira account (the account
of JIRA_API_TOKEN) is excluded COMPLETELY: every comment the console / Slack / Discord posts comes from it, so
comments typed by hand from that account are excluded too. If the bot cannot tell its own accountId, the run
is skipped rather than risk treating its comments as a human's.

No duplicates:
  1. each comment is handled once: jira_autolink_seen {_id: "<key>|<comment id>"} is inserted BEFORE
     acting (a duplicate-key error = already handled), so overlapping sweeps / restarts are safe;
  2. each ticket-chat pair is linked once: jira_chat_link claims atomically and only a NEW link runs the
     Discord / Reddit side effects and the notification (already linked by anyone = nothing happens);
  3. one comment with several chats -> one notification listing them.
Only comments created in the last MAX_AGE are looked at, so switching this on never links old history.

Runs on the bot VM from the Jira index recent sweep (services/jira_so_index.sync_recent), which already
reads the recently updated SO tickets every 10 minutes: no extra Jira search. Best-effort, never raises.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MAX_AGE = timedelta(hours=3)          # comments older than this are history, never auto-linked
SEEN_TTL_SEC = 60 * 24 * 3600

_DISCORD_RE = re.compile(r"discord(?:app)?\.com/channels/\d+/(\d{5,25})")
_MODMAIL_RE = re.compile(r"mod\.reddit\.com/mail/(?:[a-z]+/)?([a-z0-9]{3,12})(?![a-z0-9])", re.I)
_POST_RE = re.compile(r"reddit\.com/r/[^/\s\"|\]]+/comments/([a-z0-9]{3,12})(?![a-z0-9])", re.I)
# console deep link; (?![\w/]) keeps #/discord/forum/<id> and #/discord/mentions/<id> out
_CONSOLE_RE = re.compile(r"https?://[^\s\"|\]]+?#/(discord|modmail|posts)/([A-Za-z0-9]+)(?![\w/])")


def _db():
    from services.mongo_db_service import sync_db
    return sync_db


def ensure_indexes():
    try:
        _db()["jira_autolink_seen"].create_index("created_at", expireAfterSeconds=SEEN_TTL_SEC, background=True)
    except Exception as e:
        logger.debug("[AutoLink] index create skipped: %s", e)


def _text_of(body):
    """Comment body as text. v3 search returns ADF (JSON): every link is still a plain string inside it
    (text, link mark href, inlineCard url), so the JSON dump is searched as is."""
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, ensure_ascii=False)
    except Exception:
        return ""


def _parse_time(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) - \
            _tz_offset(str(s))
    except Exception:
        return None


def _tz_offset(s):
    """Jira times end in +0200 / -0500 / Z."""
    m = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
    if not m:
        return timedelta(0)
    d = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return d if m.group(1) == "+" else -d


def chats_in(text):
    """[(kind, value)] of the chat links in `text`, in order, without repeats.
    kind: discord_channel | discord_ticket | modmail | post."""
    out = []

    def add(x):
        if x not in out:
            out.append(x)
    for m in _DISCORD_RE.finditer(text):
        add(("discord_channel", m.group(1)))
    for m in _MODMAIL_RE.finditer(text):
        add(("modmail", m.group(1).lower()))
    for m in _POST_RE.finditer(text):
        add(("post", m.group(1).lower()))
    for m in _CONSOLE_RE.finditer(text):
        kind, ref = m.group(1), m.group(2)
        if kind == "discord":
            if re.fullmatch(r"[0-9a-f]{24}", ref):
                add(("discord_ticket", ref))
            elif ref.isdigit():
                add(("discord_channel", ref))     # notification links use the channel id
        else:
            add(("modmail" if kind == "modmail" else "post", ref.lower()))
    return out


def _team_authors():
    """{jira accountId: {"email", "alias"}} for console users with a linked Jira account."""
    out = {}
    try:
        for u in _db()["console_users"].find({"jira_account_id": {"$nin": [None, ""]}},
                                             {"jira_account_id": 1, "email": 1, "alias": 1}):
            acc = (u.get("jira_account_id") or "").strip()
            if acc and u.get("email"):
                out[acc] = {"email": u["email"], "alias": (u.get("alias") or u["email"].split("@")[0]).strip()}
    except Exception as e:
        logger.warning("[AutoLink] console users read failed: %s", e)
    return out


def _resolve_and_link(kind, ref, key, who):
    """Link one chat. Returns (label, console route) when a NEW link was made, else None."""
    from services import jira_chat_link as L
    db = _db()
    if kind in ("discord_channel", "discord_ticket"):
        if kind == "discord_ticket":
            from bson import ObjectId
            t = db["discord_tickets"].find_one({"_id": ObjectId(ref)})
        else:
            t = db["discord_tickets"].find_one({"channel_id": {"$in": [ref, int(ref)]}}, sort=[("created_at", -1)])
        if not t or t.get("status") == "closed":     # a closed ticket is not pulled back into the Jira category
            return None
        if L.link_discord(t, key, who):
            return ("Discord ticket #%s" % (t.get("ticket_number") or ""), "#/discord/%s" % t["_id"])
        return None
    if kind == "modmail":
        if L.link_modmail(ref, key, who):
            return ("Reddit ModMail %s" % ref, "#/modmail/%s" % ref)
        return None
    if kind == "post":
        try:
            from services.reddit_snapshot import _snap_db
            known = _snap_db["reddit_posts_snapshot"].find_one({"post_id": ref}, {"_id": 1})
        except Exception:
            known = None
        if known and L.link_post(ref, key, who):
            return ("Reddit post %s" % ref, "#/posts/%s" % ref)
    return None


def _notify(author, key, summary, linked):
    from services.jira_service import JIRA_BASE_URL
    what = ", ".join(label for label, _r in linked)
    title = "Auto-linked %s to %s" % (key, what)
    body = "From your Jira comment on %s%s." % (key, (": " + summary) if summary else "")
    route = linked[0][1]
    db = _db()
    try:
        db["console_notify_feed"].insert_one({"type": "jira_autolink", "to_email": author["email"], "title": title,
                                              "body": body, "url": "%s/browse/%s" % (JIRA_BASE_URL, key) if JIRA_BASE_URL else "",
                                              "route": route, "created_at": datetime.utcnow()})
    except Exception as e:
        logger.warning("[AutoLink] feed insert failed for %s: %s", key, e)
    try:
        from services.user_notify_service import notify_user, _console_link
        notify_user(author["email"], title, body, _console_link(route), force=True)
    except Exception as e:
        logger.warning("[AutoLink] notify %s failed for %s: %s", author["email"], key, e)
    try:
        for label, _r in linked:
            db["console_activity"].insert_one({"user": author["alias"], "action": "auto-linked Jira %s (Jira comment)" % key,
                                               "target": label, "at": datetime.utcnow()})
    except Exception as e:
        logger.debug("[AutoLink] activity write failed: %s", e)


def _all_comments(key):
    from services.jira_service import JIRA_BASE_URL, _auth_headers
    import requests
    out, start = [], 0
    for _ in range(10):
        r = requests.get(f"{JIRA_BASE_URL}/rest/api/2/issue/{key}/comment",
                         params={"startAt": start, "maxResults": 100}, headers=_auth_headers(), timeout=15)
        if r.status_code != 200:
            break
        d = r.json() or {}
        page = d.get("comments") or []
        out.extend(page)
        start += len(page)
        if not page or start >= int(d.get("total") or 0):
            break
    return out


def process_issues(issues):
    """`issues`: raw issues from the recent sweep's search (fields summary, status, comment).
    Returns how many new links were made."""
    from pymongo.errors import DuplicateKeyError
    if not issues:
        return 0
    try:
        from services.jira_service import get_my_account_id
        me = get_my_account_id()
    except Exception:
        me = ""
    if not me:
        logger.warning("[AutoLink] own Jira accountId unknown, auto-link skipped this run")
        return 0
    team = _team_authors()
    team.pop(me, None)                       # the bot's own account: never
    if not team:
        return 0
    cutoff = datetime.now(timezone.utc) - MAX_AGE
    seen, made = _db()["jira_autolink_seen"], 0
    for it in issues:
        key = (it.get("key") or "").upper()
        f = it.get("fields") or {}
        if not key or key.startswith("URG-"):
            continue
        if ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done":
            continue
        cf = f.get("comment") or {}
        comments = cf.get("comments") or []
        if int(cf.get("total") or 0) > len(comments):
            try:
                comments = _all_comments(key) or comments
            except Exception as e:
                logger.debug("[AutoLink] full comment read for %s failed: %s", key, e)
        for c in comments:
            aid = ((c.get("author") or {}).get("accountId") or "")
            if aid == me or aid not in team:
                continue
            created = _parse_time(c.get("created"))
            if not created or created < cutoff:
                continue
            chats = chats_in(_text_of(c.get("body")))
            if not chats:
                continue
            try:
                seen.insert_one({"_id": "%s|%s" % (key, c.get("id")), "created_at": datetime.utcnow()})
            except DuplicateKeyError:
                continue                     # handled by an earlier sweep
            author = team[aid]
            who = {"author_email": author["email"], "author_name": author["alias"]}
            linked = []
            for kind, ref in chats:
                try:
                    res = _resolve_and_link(kind, ref, key, who)
                except Exception as e:
                    logger.warning("[AutoLink] %s -> %s %s failed: %s", key, kind, ref, e)
                    res = None
                if res:
                    linked.append(res)
            if linked:
                made += len(linked)
                logger.info("[AutoLink] %s linked to %s from comment %s by %s", key,
                            ", ".join(l for l, _r in linked), c.get("id"), author["alias"])
                _notify(author, key, f.get("summary") or "", linked)
    return made
