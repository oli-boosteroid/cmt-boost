"""
Image-ratelimit whitelist.

A Discord user added here is exempt from the image-spam ratelimit (the alert to the meeting group)
for WHITELIST_DAYS. Backed by Mongo so it survives restarts and is shared by every process that
checks it. Written from the Telegram "Add to whitelist" button; read in the Discord image-spam
detector alongside the moderator exemption.
"""
import logging
from datetime import datetime, timedelta

from services.mongo_db_service import sync_db

logger = logging.getLogger(__name__)

_col = sync_db["image_ratelimit_whitelist"]
WHITELIST_DAYS = 2


def add_image_whitelist(user_id, username="", added_by="", days=WHITELIST_DAYS):
    """Whitelist a Discord user for `days` (default 2). Idempotent per user id; returns expiry."""
    now = datetime.utcnow()
    expires = now + timedelta(days=days)
    try:
        _col.update_one(
            {"_id": str(user_id)},
            {"$set": {"discord_user_id": int(user_id), "username": username or "",
                      "added_by": added_by or "", "added_at": now, "expires_at": expires}},
            upsert=True,
        )
    except Exception as e:
        logger.error("[Whitelist] add failed for %s: %s", user_id, e)
    return expires


def is_image_whitelisted(user_id, names=()) -> bool:
    """True if the user is exempt from the image ratelimit:
      * on the PERMANENT list the admin keeps in the console (console_config image_whitelist_discord,
        matched by id or any of the user's names, case-insensitive; cached 60s), or
      * on a 2-day entry added from the Telegram alert button that hasn't expired yet."""
    try:
        from services.identity_service import get_image_whitelist_discord
        perm = get_image_whitelist_discord()
        if perm:
            keys = {str(user_id)} | {str(n).strip().lower().lstrip("@") for n in (names or ()) if n}
            if keys & perm:
                return True
    except Exception as e:
        logger.warning("[Whitelist] permanent list check failed: %s", e)
    try:
        d = _col.find_one({"_id": str(user_id)}, {"expires_at": 1})
        return bool(d and d.get("expires_at") and d["expires_at"] > datetime.utcnow())
    except Exception as e:
        logger.warning("[Whitelist] check failed for %s: %s", user_id, e)
        return False
