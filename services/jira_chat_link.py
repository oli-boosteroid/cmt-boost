"""
Link a Jira ticket to a console chat from the BOT side, exactly the way the console's "Link Jira Ticket"
does it (cmt-console api/routes/console_state.py: /discord-jira-link, /reddit-jira-link, /post-jira-link
and web/js/app.js linkJiraToChat). KEEP IN SYNC with those.

Every function claims the link atomically and returns True only when THIS call created it, so the
side effects (Discord channel note + category move, ModMail highlight) and any notification run at most
once per ticket-chat pair, whoever links first (an operator, !jira, or services/jira_autolink.py).

  link_discord(ticket, key, who)   -> discord_tickets.jira_ticket_key(s) + queued `discord_jira_link`
  link_modmail(convo_id, key, who) -> reddit_mod_notes.jira_convos.<key> + queued `reddit_highlight`
  link_post(post_id, key, who)     -> console_post_jira.issue_keys

`who` = {"author_email", "author_name"}: written on the queued actions like a console action.
"""
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def _db():
    from services.mongo_db_service import sync_db
    return sync_db


def _enqueue(atype, payload, who):
    doc = {"type": atype, "payload": payload, "status": "pending", "result": None, "error": None,
           "created_at": datetime.utcnow(), "claimed_at": None, "finished_at": None,
           "author_email": (who or {}).get("author_email", ""), "author_name": (who or {}).get("author_name", "")}
    try:
        _db()["console_actions"].insert_one(doc)
    except Exception as e:
        logger.warning("[JiraChatLink] could not enqueue %s: %s", atype, e)


def link_discord(ticket, issue_key, who=None):
    """Same as the console's /discord-jira-link. `ticket` is the discord_tickets doc."""
    key = (issue_key or "").upper()
    if not ticket or not _KEY_RE.match(key):
        return False
    col, oid, now = _db()["discord_tickets"], ticket["_id"], datetime.utcnow()
    # Primary = the ticket's first Jira link (channel moves to the Jira category); claimed atomically.
    primary = False
    if not key.startswith("URG-"):
        res = col.update_one(
            {"_id": oid, "$and": [{"$or": [{"jira_ticket_key": {"$exists": False}}, {"jira_ticket_key": {"$in": [None, ""]}}]},
                                  {"$or": [{"jira_ticket_keys": {"$exists": False}}, {"jira_ticket_keys": {"$size": 0}}]}]},
            {"$set": {"jira_ticket_key": key, "updated_at": now}, "$addToSet": {"jira_ticket_keys": key}})
        primary = res.modified_count == 1
    if not primary:
        res = col.update_one({"_id": oid, "jira_ticket_keys": {"$ne": key}, "jira_ticket_key": {"$ne": key}},
                             {"$addToSet": {"jira_ticket_keys": key}, "$set": {"updated_at": now}})
        if res.modified_count == 0:
            return False                     # already linked: nothing new to tell Discord
    _enqueue("discord_jira_link", {"ticket_id": str(oid), "issue_key": key, "primary": primary}, who)
    return True


def link_modmail(convo_id, issue_key, who=None):
    """Same as the console's /reddit-jira-link + the reddit_highlight it queues. A key already linked to
    ANOTHER convo of the same user is left alone (never moved by the bot)."""
    key = (issue_key or "").upper()
    convo_id = (convo_id or "").strip()
    if not convo_id or not _KEY_RE.match(key):
        return False
    try:
        from services.reddit_snapshot import _snap_db
        snap = _snap_db["reddit_modmail_snapshot"].find_one({"convo_id": convo_id}, {"participant": 1}) or {}
    except Exception as e:
        logger.warning("[JiraChatLink] modmail %s lookup failed: %s", convo_id, e)
        return False
    ru = (snap.get("participant") or "").strip()
    if not ru or ru in ("[deleted]", "unknown"):
        return False
    col, now = _db()["reddit_mod_notes"], datetime.utcnow()
    col.update_one({"reddit_user": ru}, {"$setOnInsert": {"reddit_user": ru}}, upsert=True)
    res = col.update_one({"reddit_user": ru, "jira_convos.%s" % key: {"$exists": False}},
                         {"$set": {"jira_convos.%s" % key: convo_id, "updated_at": now}})
    if res.modified_count == 0:
        return False
    doc = col.find_one({"reddit_user": ru}, {"highlighted_convos": 1}) or {}
    if convo_id not in (doc.get("highlighted_convos") or []):
        _enqueue("reddit_highlight", {"convo_id": convo_id, "reddit_user": ru, "highlight": True}, who)
    return True


def link_post(post_id, issue_key, who=None):
    """Same as the console's /post-jira-link."""
    key = (issue_key or "").upper()
    post_id = (post_id or "").strip()
    if not post_id or not _KEY_RE.match(key):
        return False
    col, now = _db()["console_post_jira"], datetime.utcnow()
    col.update_one({"post_id": post_id}, {"$setOnInsert": {"post_id": post_id, "created_at": now, "issue_keys": []}},
                   upsert=True)
    res = col.update_one({"post_id": post_id, "issue_keys": {"$ne": key}},
                         {"$addToSet": {"issue_keys": key},
                          "$set": dict({"updated_at": now}, **{k: v for k, v in (who or {}).items() if k in ("author_email", "author_name")})})
    return res.modified_count == 1

