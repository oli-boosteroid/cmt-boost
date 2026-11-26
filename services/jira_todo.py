"""
The Jira To-Do buttons - ONE implementation for Slack and the console.

Slack buttons (handlers/action_handlers.py) call run() directly; the console queues a `jira_todo`
action that the worker (services/console_actions_worker.py) passes to run(). Whichever side acts,
the result is the same everywhere: the Jira comment / transition, the console marker (handled or
dismissed, so the console To-Do drops the card), and the Slack card is deleted.

  op            what it does
  forward       comment "Передано" (SO, CM In Progress), ticket stays in its status
  close         comment "Передано, закрыто" + move to Done
  stale_close   comment "Ответа нет, закрыто" + move to Done (the "stuck 5 days" card)
  urg_done      URG in Review: this user is transferred; the ticket goes to Done after the last one
  dismiss       hide the card without any Jira change (it comes back when the ticket re-enters the status)

Two people pressing the same button at once (Slack + console, or two consoles): the first claims the
action in `jira_todo_claims` (unique _id per ticket + cycle), the second gets {"already": True} and
does nothing - no second comment, no second transition.
"""
import logging
from datetime import datetime

from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

# Jira comment texts the team posts (kept in Russian on purpose).
COMMENTS = {"forward": "Передано", "close": "Передано, закрыто", "stale_close": "Ответа нет, закрыто"}
OPS = ("forward", "close", "stale_close", "urg_done", "dismiss")
_CLAIM_DAYS = 14


def _db():
    from services.db import main_db
    return main_db()


def _claims():
    col = _db()["jira_todo_claims"]
    try:
        col.create_index("created_at", expireAfterSeconds=_CLAIM_DAYS * 86400)
    except Exception as e:
        logger.debug("[JiraTodo] claims index: %s", e)
    return col


def _cycle(key):
    """The console's status_since for the ticket: it changes each time the ticket re-enters a status,
    so a claim (and a handled / dismissed marker) only covers the current round."""
    from services import jira_store
    return jira_store.cycle(_db(), key)


def _claim(op, key, rid, cycle, by):
    group = "done" if op in ("close", "stale_close") else op      # Closed and No-reply-closed are one outcome
    try:
        _claims().insert_one({"_id": "%s|%s|%s|%s" % (group, key, rid or "", cycle), "op": op, "by": by,
                              "created_at": datetime.utcnow()})
        return True
    except DuplicateKeyError:
        return False


def _delete_slack_cards(key, clicked=None):
    """Delete the ticket's Slack cards: the CM / URG notification and the "stuck" card (+ the clicked one)."""
    from jira_notifications.jira_notifications import slack_client, delete_slack_notification
    delete_slack_notification(key)
    doc = _db()["jira_tickets"].find_one({"issue_key": key}, {"slack_stale_channel": 1, "slack_stale_ts": 1}) or {}
    cards = []
    if doc.get("slack_stale_channel") and doc.get("slack_stale_ts"):
        cards.append((doc["slack_stale_channel"], doc["slack_stale_ts"]))
    if clicked and clicked[0] and clicked[1]:
        cards.append(clicked)
    for channel, ts in cards:
        try:
            slack_client.chat_delete(channel=channel, ts=ts)
        except Exception as e:
            if "message_not_found" not in str(e):
                logger.warning("[JiraTodo] Slack card delete failed for %s: %s", key, e)


def _comment(key, text):
    """Post the team comment unless the newest comment is already from the user's side (out-user) -
    the one rule for Slack and the console. post_jira_comment itself never repeats our own last comment."""
    from services.jira_service import post_jira_comment, is_last_comment_from_out_user
    if is_last_comment_from_out_user(key):
        logger.info("[JiraTodo] %s: last comment is from an out-user, %r not posted", key, text)
        return False
    return bool(post_jira_comment(key, text))


def _to_done(key):
    from services.jira_service import transition_to_done
    ok = transition_to_done(key)
    if ok:
        _db()["jira_tickets"].update_one({"issue_key": key}, {"$set": {"status": "Done"}})
    return ok


def run(op, key, rid="", since="", by="", clicked=None):
    """Do one To-Do action. `since` is the cycle the console showed (status_since / URG updated);
    `clicked` = (channel, ts) of the Slack card that was pressed. Returns a result dict."""
    key = (key or "").strip()
    if op not in OPS or not key:
        return {"ok": False, "error": "bad op or key"}
    from services.mongo_db_service import mark_console_jira_handled_sync, mark_console_jira_dismissed_sync

    if op == "dismiss":
        if not since:
            since = _cycle(key)
            if not since and key.upper().startswith("URG"):
                from services.jira_service import get_issue_updated
                since = get_issue_updated(key)
        mark_console_jira_dismissed_sync(key, since)
        _delete_slack_cards(key, clicked)
        logger.info("[JiraTodo] %s dismissed by %s", key, by)
        return {"ok": True}

    if op == "urg_done":
        # URG tickets are not in the console's SO store: both sides use Jira's own `updated` as the round.
        from services.jira_service import get_issue_updated
        cycle = get_issue_updated(key) or since
    else:
        cycle = since or _cycle(key)
    if not _claim(op, key, rid, cycle, by):
        logger.info("[JiraTodo] %s %s by %s skipped: already done by someone else", op, key, by)
        _delete_slack_cards(key, clicked)
        return {"ok": True, "already": True}

    if op == "urg_done":
        left = 0
        if rid:
            from services.urg_recipients import mark_transferred, delete_slack
            left = mark_transferred(key, rid, by=by)
            delete_slack(key, rid)
        if left:
            if clicked:
                _delete_slack_cards_only_clicked(clicked)
            logger.info("[JiraTodo] URG %s: user %s transferred by %s, %d left", key, rid, by, left)
            return {"ok": True, "left": left}
        _to_done(key)
        mark_console_jira_handled_sync(key)
        _delete_slack_cards(key, clicked)
        logger.info("[JiraTodo] URG %s moved to Done by %s", key, by)
        return {"ok": True, "left": 0, "status": "Done"}

    posted = _comment(key, COMMENTS[op])
    status = ""
    if op in ("close", "stale_close"):
        status = "Done" if _to_done(key) else ""
    mark_console_jira_handled_sync(key)
    _delete_slack_cards(key, clicked)
    logger.info("[JiraTodo] %s %s by %s (comment %s)", op, key, by, "posted" if posted else "skipped")
    return {"ok": True, "comment_skipped": not posted, "status": status}


def _delete_slack_cards_only_clicked(clicked):
    from jira_notifications.jira_notifications import slack_client
    try:
        slack_client.chat_delete(channel=clicked[0], ts=clicked[1])
    except Exception as e:
        if "message_not_found" not in str(e):
            logger.warning("[JiraTodo] Slack card delete failed: %s", e)
