"""
Discord -> console snapshots.

Writes two collections the console reads (Forum + Mentions tabs). Called from the async Discord
event handlers, so writes go through asyncio.to_thread (sync_db is pymongo/blocking). The console
and bot meet only in Mongo - same rendezvous as discord_tickets.

Collections (sync_db):
  discord_forum_posts  -- one doc per forum thread (title, author, native tags, category, messages)
  discord_mentions     -- one doc per message that @-mentions a moderator

Docs are shaped to match the console's frontend so mapping is near-identity. Snowflake ids are
stored as STRINGS (JS loses precision > 2**53).
"""
import asyncio
import logging

from services.mongo_db_service import sync_db

logger = logging.getLogger(__name__)

_forum_col = sync_db["discord_forum_posts"]
_mention_col = sync_db["discord_mentions"]
_config_col = sync_db["console_config"]


def _forum_category(parent_id) -> str:
    """Map a forum channel id -> console category (Suggestions / Help Centre / Discussion) using the
    admin-configured map in console_config (key='discord_forums', value={category: channel_id})."""
    try:
        doc = _config_col.find_one({"key": "discord_forums"}) or {}
        mapping = doc.get("value") or {}
        for cat, cid in mapping.items():
            if str(cid) == str(parent_id):
                return cat
    except Exception as e:
        logger.warning("[DiscordSnap] forum category lookup failed: %s", e)
    return ""


def _msg(author, is_user, content, when):
    return {"author": author, "is_user": bool(is_user),
            "content": content or "",
            "timestamp": when.strftime("%H:%M") if when else "",
            "ts": when.isoformat() if when else ""}


def _forum_doc(thread, starter):
    owner = getattr(thread, "owner", None)
    author = (owner.display_name if owner else None) or getattr(starter.author, "display_name", "") if starter else ""
    tags = []
    try:
        tags = [t.name for t in getattr(thread, "applied_tags", []) if getattr(t, "name", None)]
    except Exception as _e:
        logger.debug("ignored: %s", _e)
    created = getattr(thread, "created_at", None)
    return {
        "_id": str(thread.id), "id": str(thread.id), "thread_id": str(thread.id),
        "channel_id": str(thread.parent_id), "guild_id": str(thread.guild.id),
        "title": thread.name, "author": author,
        "author_id": str(getattr(thread, "owner_id", "") or ""),
        "tags": tags, "forum": _forum_category(thread.parent_id),
        "messages": [_msg(author, True, getattr(starter, "content", ""), created)] if starter else [],
        "ts": created.isoformat() if created else "",
        "created_at": created.isoformat() if created else "",
    }


async def snapshot_forum_post(thread, starter):
    """Upsert a forum thread (its starter message) into discord_forum_posts."""
    try:
        doc = _forum_doc(thread, starter)
        await asyncio.to_thread(_forum_col.update_one, {"_id": doc["_id"]},
                                {"$setOnInsert": {"messages": doc["messages"]},
                                 "$set": {k: v for k, v in doc.items() if k != "messages"}},
                                True)
    except Exception as e:
        logger.error("[DiscordSnap] forum snapshot failed for %s: %s", getattr(thread, "id", "?"), e)


async def append_forum_message(message):
    """Append a reply to an existing forum thread doc (keeps the console thread up to date)."""
    try:
        thread = message.channel
        is_user = not getattr(message.author, "bot", False)
        entry = _msg(getattr(message.author, "display_name", getattr(message.author, "name", "")),
                     is_user, message.content, message.created_at)
        await asyncio.to_thread(
            _forum_col.update_one, {"_id": str(thread.id)},
            {"$push": {"messages": entry}})
    except Exception as e:
        logger.error("[DiscordSnap] forum append failed: %s", e)


async def snapshot_mention(message, email=None):   # email: ignored (kept for old callers)
    """Upsert a moderator-mention message into discord_mentions (status defaults to 'new')."""
    try:
        doc = {
            "_id": str(message.id), "id": str(message.id), "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "channel_name": getattr(message.channel, "name", ""),
            "guild_id": str(message.guild.id),
            "author": message.author.name, "author_id": str(message.author.id),
            "content": message.content or "",
            "ts": message.created_at.isoformat() if message.created_at else "",
        }
        await asyncio.to_thread(_mention_col.update_one, {"_id": doc["_id"]},
                                {"$setOnInsert": {"status": "new"}, "$set": doc}, True)
    except Exception as e:
        logger.error("[DiscordSnap] mention snapshot failed for %s: %s", getattr(message, "id", "?"), e)
