"""
Team forms shared by Slack and the console (one function per action, the console's look and fields).

  submit_report        Community / Weekly report: saved in console_reports (so it shows in the
                       console Reports from either side) and posted to the Slack reports channel.
  submit_game_proposal a game for the wishlist: our own store (services/wishlist_store.py) + the wishlist
                       channel note (+ the learned Discord URL mapping when there is one).
  submit_user_proposal a user proposal (Feature / Payment Method / Feedback / ...): proposals API.

Slack handlers (handlers/modal_handlers.py) call these directly; the console queues `report`,
`add_suggestion` and `add_proposal`, which the worker passes here.
"""
import logging
import os
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

REPORT_TITLES = {"daily": "Community Report", "weekly": "Weekly Report"}
# The console's field labels (web/js/app.js modalReport / modalWeekly) - Slack uses the same ones.
REPORT_FIELDS = {
    "daily": ["Issues summary", "Sent posts to TG", "Testing sessions count", "Testing sessions summary",
              "Crypto Activity", "Day Summary"],
    "weekly": ["Important/Interesting Technical Problems", "Massive Non-Technical Requests", "Created Jira Tickets",
               "Cryptoactivity", "Other"],
}


def _db():
    from services.db import main_db
    return main_db()


def _slack():
    from services.mongo_db_service import client
    return client


def report_text(rtype, author_name, fields):
    title = "%s - %s" % (REPORT_TITLES.get(rtype, "Report"), author_name or "")
    body = "\n".join("*%s:* %s" % (label, value or "-") for label, value in fields)
    return title, body


def submit_report(rtype, fields, author_email="", author_name=""):
    """fields = [[label, value], ...] in the console's order. Returns (result, error)."""
    rtype = rtype if rtype in REPORT_TITLES else "daily"
    fields = [[str(l), str(v or "")] for l, v in (fields or [])]
    doc = {"type": rtype, "date": datetime.utcnow().strftime("%Y-%m-%d"), "fields": fields,
           "created_at": datetime.utcnow(), "author_email": author_email or "", "author_name": author_name or ""}
    rid = _db()["console_reports"].insert_one(doc).inserted_id
    title, body = report_text(rtype, author_name, fields)
    channel = os.getenv("SLACK_REPORTS_CHANNEL_ID")
    if not channel:
        return {"ok": True, "id": str(rid), "slack": False}, "SLACK_REPORTS_CHANNEL_ID not configured (report saved)"
    try:
        _slack().chat_postMessage(channel=channel, text="*%s*\n%s" % (title, body))
    except Exception as e:
        logger.error("[Forms] report Slack post failed: %s", e)
        return {"ok": True, "id": str(rid), "slack": False}, "saved, but the Slack post failed: %s" % e
    logger.info("[Forms] %s report from %s saved and posted", rtype, author_name)
    return {"ok": True, "id": str(rid), "slack": True, "title": title, "body": body}, None


def _post_proposal(data):
    url = os.getenv("SLACK_PROPOSAL_API_URL")
    if not url:
        return "SLACK_PROPOSAL_API_URL not configured"
    try:
        r = requests.post(url, json=data, headers={"Content-Type": "application/json"}, timeout=10)
    except Exception as e:
        return "proposals API unreachable: %s" % e
    if r.status_code != 200:
        return "proposals API %s: %s" % (r.status_code, (r.text or "")[:200])
    return None


def submit_game_proposal(entity, entity_type, author, discord_url="", tg_msg_id="", source="slack"):
    """A game for the wishlist (Slack "Gaming proposal" / console "Add suggestion"). Returns (result, error)."""
    from services import wishlist_store
    entity = (entity or "").strip()
    entity_type = (entity_type or "Other").strip()
    if not entity:
        return None, "entity required"
    if tg_msg_id:
        source = "telegram"
    try:
        game = wishlist_store.add_request(entity, entity_type, source, author=author, url=discord_url)
    except Exception as e:
        logger.error("[Forms] wishlist store failed: %s", e)
        return None, "wishlist store failed: %s" % e
    if discord_url:      # opened from a Discord wishlist alert with an unknown URL: learn the mapping
        from services.mongo_db_service import save_url_mapping_sync
        save_url_mapping_sync(discord_url, entity, entity_type, author)
    channel = os.getenv("SLACK_WISHLIST_NOTIFICATIONS_CHANNEL_ID") or os.getenv("SLACK_FLOOD_CHANNEL_ID")
    if channel:
        try:
            _slack().chat_postMessage(channel=channel, text=wishlist_store.request_note(game, author, manual=True))
        except Exception as e:
            logger.error("[Forms] wishlist channel note failed: %s", e)
    if tg_msg_id:       # added from a Telegram message: the card shows "Added to wishlist: X"
        try:
            _db()["telegram_messages"].update_one({"_id": tg_msg_id}, {"$set": {"wishlist_added": entity}})
        except Exception as e:
            logger.warning("[Forms] telegram wishlist mark failed: %s", e)
    total = game.get("requests", 1)
    logger.info("[Forms] game proposal %r (%s) by %s via %s, %s total", entity, entity_type, author, source, total)
    return {"ok": True, "entity": entity, "total": total, "status": game.get("status"), "key": game.get("key")}, None


def submit_user_proposal(entity, entity_type, author):
    """A user proposal (Feature / Payment Method / Feedback / Gamepad / Proposal). Returns (result, error)."""
    entity = (entity or "").strip()
    if not entity:
        return None, "entity required"
    err = _post_proposal({"type": "user", "entity": entity, "entity_type": (entity_type or "Proposal"),
                          "author": author, "requestor": ""})
    if err:
        return None, err
    logger.info("[Forms] user proposal (%s) by %s", entity_type, author)
    return {"ok": True, "entity": entity}, None
