"""
Slack alerts for the Facebook group feed collected by the CMT browser extension.

The console (Railway) stores what the extension sends in `fb_posts` / `fb_dms` with slack_sent=False;
this job (VM, every minute) posts each new one to Slack and flips the flag.
New post -> author, text, link. New chat message -> only the fact (who, link), no message text.

Env: FACEBOOK_SLACK_CHANNEL_ID (falls back to SLACK_JIRA_CHANNEL_ID).
"""
import logging
import os

from slack_sdk import WebClient

from services.db import main_db
from services.redact import redact_emails

logger = logging.getLogger(__name__)

_slack = WebClient(token=os.getenv("SLACK_TOKEN"))


def _channel():
    return os.getenv("FACEBOOK_SLACK_CHANNEL_ID") or os.getenv("SLACK_JIRA_CHANNEL_ID")


def _esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify_facebook_feed():
    ch = _channel()
    if not ch:
        return
    db = main_db()
    try:
        for p in db["fb_posts"].find({"slack_sent": False}).sort("first_seen", 1).limit(20):
            body = redact_emails(p.get("text") or "")
            if len(body) > 600:
                body = body[:597] + "..."
            who = _esc(p.get("author") or "Unknown")
            if p.get("author_url"):
                who = "<%s|%s>" % (p["author_url"], who)
            text = "*New Facebook post* by %s\n%s\n<%s|Open post>" % (who, _esc(body) or "_(no text)_", p.get("url", ""))
            _slack.chat_postMessage(channel=ch, text=text, unfurl_links=False, unfurl_media=False)
            db["fb_posts"].update_one({"_id": p["_id"]}, {"$set": {"slack_sent": True}})
        for d in db["fb_dms"].find({"slack_sent": False}).sort("first_seen", 1).limit(20):
            name = _esc(d.get("name") or "someone")
            link = ("<%s|Open chat>" % d["url"]) if d.get("url") else ""
            _slack.chat_postMessage(channel=ch, text="*New Facebook message* from %s %s" % (name, link),
                                    unfurl_links=False, unfurl_media=False)
            db["fb_dms"].update_one({"_id": d["_id"]}, {"$set": {"slack_sent": True}})
    except Exception as e:
        logger.error("[FB feed] Slack notify failed: %s", e)
