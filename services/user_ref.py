"""
Refer to a Boosteroid user by the internal User ID instead of the email address.

Personal-data rule: tickets, Jira comments and work chats identify a user by "User ID", not by email.
These helpers turn an address into "User ID 9444330" through the cloud Operations API (see
services/cloud_api.py) right before a text leaves the bot.

We store no email addresses: an address a user gives us is looked up once (cached in memory for a few
hours) and only the User ID is kept, in the users_emails directory.
"""
import logging
import re
import time

from services import cloud_api

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_CACHE_TTL = 6 * 3600
_cache = {}   # lower-cased email -> (expires_at, user_id or None)


SESSION_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MANUAL_NOTE = ("The User ID could not be looked up automatically (the cloud admin did not answer). "
               "Please add the User ID by hand.")


def _cache_get(key):
    hit = _cache.get(key)
    return hit if hit and hit[0] > time.time() else None


def _cache_put(key, uid):
    if len(_cache) > 5000:
        _cache.clear()
    _cache[key] = (time.time() + _CACHE_TTL, uid)


def resolve_email(email: str):
    """(user_id, status) for an email. status: "found" | "not_found" | "error".

    Order: in-memory cache -> cloud API (we keep no addresses, so the API is the only source). A "not
    found" is remembered for a few hours as well."""
    key = (email or "").strip().lower()
    if not key:
        return None, "not_found"
    hit = _cache_get("e:" + key)
    if hit:
        return hit[1], ("found" if hit[1] else "not_found")
    if not cloud_api.configured():
        return None, "error"
    user, err = cloud_api.find_user(email=key)
    uid = (user or {}).get("user_id")
    if err and err != "user not found":
        logger.warning("[UserRef] User ID lookup failed: %s", err)
        return None, "error"                    # transient: not cached, asked again next time
    _cache_put("e:" + key, str(uid) if uid else None)
    return (str(uid), "found") if uid else (None, "not_found")


def known_user_id(email: str):
    """User ID for an email only from what this process already looked up - never asks the cloud API, so
    it is cheap enough for every redaction."""
    key = (email or "").strip().lower()
    hit = _cache_get("e:" + key) if key else None
    return hit[1] if hit and hit[1] else None


def resolve_session(session_id: str):
    """(user_id, status) for a streaming session ID: the session's owner, through the cloud API."""
    key = (session_id or "").strip().lower()
    hit = _cache_get("s:" + key)
    if hit:
        return hit[1], ("found" if hit[1] else "not_found")
    if not cloud_api.configured():
        return None, "error"
    uid, err = cloud_api.session_owner(key)
    if err and err != "session not found":
        logger.warning("[UserRef] session owner lookup failed: %s", err)
        return None, "error"
    _cache_put("s:" + key, str(uid) if uid else None)
    return (str(uid), "found") if uid else (None, "not_found")


def resolve(value: str):
    """(user_id, status) for whatever a user gave us: an email, a session ID or the User ID itself.
    status "invalid" means it is none of those."""
    value = (value or "").strip()
    if re.fullmatch(r"\d{1,12}", value):
        return value, "found"
    if SESSION_RE.match(value):
        return resolve_session(value)
    if EMAIL_RE.fullmatch(value):
        return resolve_email(value)
    return None, "invalid"


def user_id_for_email(email: str):
    """User ID for an email, or None when it cannot be resolved."""
    return resolve_email(email)[0]


def label_for_email(email: str):
    """'User ID 9444330' for a resolvable email, else None."""
    uid = user_id_for_email(email)
    return ("User ID %s" % uid) if uid else None


def replace_emails(text: str) -> str:
    """Replace every email address in `text` with its 'User ID N' label. An address that cannot be
    resolved is replaced with "[email removed]"."""
    if not text or "@" not in text:
        return text or ""

    def sub(m):
        # An address we cannot turn into an ID is removed, never passed on.
        return label_for_email(m.group(0)) or "[email removed]"

    return EMAIL_RE.sub(sub, text)


def remember_user_id(user_id, email: str = "", ref: dict = None) -> None:
    """Store a resolved User ID in the user directory for the person `ref` names on a platform
    ({"reddit_username": ...} / {"discord_user_id": ...} / {"facebook_author": ...}); the record is
    created if it does not exist yet. `email` is accepted for older callers and ignored: no address is
    ever stored."""
    try:
        from datetime import datetime
        from services.mongo_db_service import sync_db
        col = sync_db["users_emails"]
        uid = str(int(user_id))
        now = datetime.utcnow()
        done = {"cloud_user_id": uid, "cloud_user_id_at": now, "cloud_user_id_status": "found"}
        ref = ref or {}
        for field in ("reddit_username", "discord_user_id", "facebook_author"):
            val = ref.get(field)
            if not val:
                continue
            keys = [val]
            if field == "discord_user_id":
                keys = [str(val)] + ([int(val)] if str(val).isdigit() else [])
            platform = {"reddit_username": "Reddit", "discord_user_id": "Discord", "facebook_author": "Facebook"}[field]
            extra = {"platform": platform, "updated_at": now}
            if field == "reddit_username":
                extra["url"] = "https://www.reddit.com/user/%s" % val
            # Reddit names differ only by case between writers: update every record of the account.
            match = ({field: {"$regex": "^" + re.escape(str(val)) + "$", "$options": "i"}}
                     if field == "reddit_username" else {field: {"$in": keys}})
            if col.update_many(match, {"$set": dict(done, **extra)}).matched_count == 0:
                col.insert_one(dict(done, **extra, **{field: keys[-1], "created_at": now}))
    except Exception as e:
        logger.warning("[UserRef] could not store the User ID: %s", type(e).__name__)


def ref_from_link(link: str) -> dict:
    """Whose conversation a Communication Link points to, as a platform identity:
    {"reddit_username": ...} or {"discord_user_id": ...}; {} when it cannot be told.

    Understands a profile link directly, and resolves chat links through our own data (no Reddit or
    Discord request): a Discord ticket channel -> the ticket's user, a Reddit modmail conversation ->
    its participant, a Reddit post -> its author."""
    link = link or ""
    m = re.search(r"reddit\.com/u(?:ser)?/([^/?#\s]+)", link, re.IGNORECASE)
    if m:
        return {"reddit_username": m.group(1)}
    m = re.search(r"discord(?:app)?\.com/users/(\d+)", link, re.IGNORECASE)
    if m:
        return {"discord_user_id": m.group(1)}
    try:
        from services.mongo_db_service import sync_db
        m = re.search(r"discord(?:app)?\.com/channels/\d+/(\d+)", link, re.IGNORECASE)
        if m:
            cid = m.group(1)
            t = sync_db["discord_tickets"].find_one({"channel_id": {"$in": [cid, int(cid)]}}, {"user_id": 1},
                                                    sort=[("created_at", -1)])
            return {"discord_user_id": str(t["user_id"])} if t and t.get("user_id") else {}
        convo = re.search(r"reddit\.com/mail/[^/]+/(\w+)", link, re.IGNORECASE)
        post = re.search(r"reddit\.com/r/[^/]+/comments/(\w+)", link, re.IGNORECASE)
        if convo or post:
            from services.reddit_snapshot import _snap_db
            if convo:
                d = _snap_db["reddit_modmail_snapshot"].find_one({"convo_id": convo.group(1)}, {"participant": 1})
                name = (d or {}).get("participant")
            else:
                d = _snap_db["reddit_posts_snapshot"].find_one({"post_id": post.group(1)}, {"author": 1})
                name = (d or {}).get("author")
            if name and name not in ("[deleted]", "unknown"):
                return {"reddit_username": name}
    except Exception as e:
        logger.debug("[UserRef] link owner not resolved: %s", type(e).__name__)
    return {}
