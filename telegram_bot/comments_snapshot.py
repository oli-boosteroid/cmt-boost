"""
Telegram -> console snapshot of USER messages in the linked discussion group.

The bot already receives every group message (Group Privacy is off in @BotFather). This handler
snapshots messages written by real users in the configured discussion group (comments under channel
posts + general chat) into sync_db.telegram_messages, which the console's Telegram tab reads.

Excluded (never snapshotted):
  - the auto-forwarded channel posts themselves (is_automatic_forward / sender_chat)
  - messages sent "as a chat/channel" (sender_chat set)
  - messages from chat ADMINS (moderators) - cached via getChatAdministrators

Reply happens "as the group" (anonymous admin) via MTProto (see telegram_bot/mtproto.py); the doc
stores chat_id + message_id so the reply can thread under the user's message.

Env: TG_COMMENTS_GROUP_ID = the discussion group id (add the bot to that group).
"""
import logging
import os
import time
from datetime import datetime

from telegram.ext import MessageHandler, filters

from services.mongo_db_service import sync_db
from services import blob_store

logger = logging.getLogger(__name__)

_col = sync_db["telegram_messages"]
# Per-message photos (base64) live on the separate blob store, not the main cluster, so multi-MB images
# never bloat its RAM working set. Short TTL so they never pile up. The console reads them via blob_store.
_img_col = blob_store.blob_db["telegram_images"]
_admins = {"ids": set(), "at": 0.0}
_ADMIN_TTL = 600            # refresh the admin list every 10 min
_MAX_IMG = 4 * 1024 * 1024  # skip anything bigger than ~4MB
_IMG_TTL_DAYS = 14          # photos auto-expire after two weeks (Mongo TTL) - don't hoard storage


def _group_id() -> int:
    try:
        return int(os.getenv("TG_COMMENTS_GROUP_ID", "0") or 0)
    except (TypeError, ValueError):
        return 0


async def _admin_ids(context, chat_id) -> set:
    now = time.time()
    if now - _admins["at"] < _ADMIN_TTL and _admins["ids"]:
        return _admins["ids"]
    try:
        members = await context.bot.get_chat_administrators(chat_id)
        _admins["ids"] = {m.user.id for m in members}
        _admins["at"] = now
    except Exception as e:
        logger.warning("[TGSnap] getChatAdministrators failed: %s", e)
    return _admins["ids"]


async def _store_photo(context, doc_id, photo):
    """Download a Telegram photo and stash it (base64) under the message id so the console can serve it -
    Telegram file URLs are token-scoped and expire, so we can't just link. A short TTL prunes them (below)."""
    try:
        if getattr(photo, "file_size", 0) and photo.file_size > _MAX_IMG:
            return
        tgfile = await context.bot.get_file(photo.file_id)
        data = await tgfile.download_as_bytearray()
        if not data or len(data) > _MAX_IMG:
            return
        import base64
        _img_col.update_one(
            {"_id": doc_id},
            {"$set": {"b64": base64.b64encode(bytes(data)).decode("ascii"),
                      "mime": "image/jpeg", "at": datetime.utcnow()}},
            upsert=True)
    except Exception as e:
        logger.warning("[TGSnap] photo download failed for %s: %s", doc_id, e)


async def snapshot_group_message(update, context):
    m = update.effective_message
    chat = update.effective_chat
    gid = _group_id()
    if not m or not chat or not gid or chat.id != gid:
        return
    # Skip channel posts / anything posted as a chat/channel (not a real user message).
    if getattr(m, "is_automatic_forward", False) or getattr(m, "sender_chat", None):
        return
    user = m.from_user
    if not user or user.is_bot:
        return

    text = (m.text or m.caption or "").strip()

    # Moderators (chat admins): we never snapshot their messages as user messages, BUT if a mod replies
    # in Telegram directly to a tracked user message, record that reply on the user's doc - so the console
    # shows it was answered (and what/who), the same shape as a reply sent from the console. A mod message
    # that isn't a reply to a user message is ignored (nothing to attribute it to).
    if user.id in await _admin_ids(context, chat.id):
        reply_to = getattr(m, "reply_to_message", None)
        if reply_to and text and not getattr(reply_to, "is_automatic_forward", False):
            target_id = "%s_%s" % (chat.id, reply_to.message_id)
            try:
                res = _col.update_one(
                    {"_id": target_id},
                    {"$set": {"answered": True, "reply": text, "answered_at": datetime.utcnow(),
                              "answered_by": user.full_name, "answered_via": "telegram"}})
                if res.matched_count:
                    logger.info("[TGSnap] recorded a moderator's Telegram reply on %s", target_id)
            except Exception as e:
                logger.warning("[TGSnap] mod-reply record failed for %s: %s", target_id, e)
        return

    # Largest photo size, if the user attached an image. Image-only messages (no caption) are kept too.
    photo = m.photo[-1] if getattr(m, "photo", None) else None
    if not text and not photo:
        return

    # A comment under a channel post replies to the auto-forwarded post; general chat does not.
    reply = getattr(m, "reply_to_message", None)
    is_comment = bool(reply and getattr(reply, "is_automatic_forward", False))
    post_title = ""
    if is_comment:
        post_title = ((reply.text or reply.caption or "").strip().splitlines() or [""])[0][:120]

    doc = {
        "_id": f"{chat.id}_{m.message_id}", "id": f"{chat.id}_{m.message_id}",
        "chat_id": chat.id, "message_id": m.message_id,
        "user": user.full_name, "username": user.username or "",
        "user_id": str(user.id),
        "text": text,
        "time": m.date.strftime("%H:%M") if m.date else "",
        "ts": m.date.isoformat() if m.date else "",
        "is_comment": is_comment, "post_title": post_title,
        "has_image": bool(photo),
    }
    try:
        # Nothing about the user is copied here: the console reads the User ID from the directory by user_id.
        _col.update_one({"_id": doc["_id"]}, {"$setOnInsert": {"answered": False}, "$set": doc}, upsert=True)
        if photo:
            await _store_photo(context, doc["_id"], photo)
    except Exception as e:
        logger.error("[TGSnap] snapshot failed for %s: %s", doc["_id"], e)


def register(app):
    """Add the group-message snapshot handler. Runs before the catch-all (group=-2) and does not
    stop propagation. No-op if TG_COMMENTS_GROUP_ID is unset."""
    if not _group_id():
        logger.info("[TGSnap] not registered (TG_COMMENTS_GROUP_ID unset)")
        return
    try:   # auto-expire stashed photos so the collection can't grow without bound
        _img_col.create_index("at", expireAfterSeconds=_IMG_TTL_DAYS * 86400)
    except Exception as e:
        logger.warning("[TGSnap] image TTL index failed: %s", e)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP, snapshot_group_message),
        group=-2)
    logger.info("[TGSnap] registered for group %s", _group_id())
