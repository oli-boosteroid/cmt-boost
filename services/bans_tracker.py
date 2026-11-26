"""
Bans from the Telegram ban channel -> Mongo `bans` -> console Bans tab.

The ban channel gets one message per banned user:

    Banned user: 10416938            (old messages say "Banned email: ..." and are ignored)

    Warns:
    1. Date: 13 November, 2026 21:04:54
    Severity Level: Critical
    Details: Russia
    Warned by: r.yevsenov
    Session: 01a1d62a-81ab-7087-871f-27d4e9c144ae

A Critical warn bans at once; two Medium/High warns ban too. The bot reads the channel with the team's
Telethon user client (telegram_bot/mtproto.py, a regular bot cannot read channel history): on start it
backfills everything after the last stored message, then handles new / edited messages live.

Old-format bans (an email instead of the User ID) are not stored.

Env: BANS_TG_CHANNEL - the channel's id (-100...) or @username; the MTProto account must be a member.
"""
import asyncio
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_FIELD_RE = re.compile(r"^\s*(Date|Severity Level|Details|Warned by|Session)\s*:\s*(.*)$", re.I)
_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


PARSER_VERSION = 3   # bump after changing parse_ban(): the next start re-reads the whole channel


def main_db_meta():
    from services.db import main_db
    return main_db()["bans_meta"]


def _col():
    from services.db import main_db
    return main_db()["bans"]


def _parse_date(value: str):
    value = (value or "").strip()
    for fmt in ("%d %B, %Y %H:%M:%S", "%d %B %Y %H:%M:%S", "%d %B, %Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def parse_ban(text: str) -> dict | None:
    """Ban message text -> {user, warns: [...]}; None when it is not a ban message. Image lists are skipped."""
    if not text:
        return None
    # Telegram bold around the labels only ("**Banned user:** ..."); a masked email keeps its own "**".
    text = re.sub(r"\*\*(?=[A-Za-z ]+:)|(?<=:)\*\*", "", text)
    m = re.search(r"Banned (?:user|email)\s*:\s*(\S+)", text, re.I)
    if not m:
        return None
    user = m.group(1).strip().rstrip(".,")
    warns, cur, in_images = [], None, False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"^Images\s*:", s, re.I):
            in_images = True
            continue
        if in_images:
            continue
        num = re.match(r"^(\d+)\.\s*(.*)$", s)
        if num:
            cur = {"n": int(num.group(1))}
            warns.append(cur)
            s = num.group(2)
            if not s:
                continue
        f = _FIELD_RE.match(s)
        if f and cur is not None:
            key = {"date": "date", "severity level": "severity", "details": "details",
                   "warned by": "warned_by", "session": "session"}[f.group(1).lower()]
            cur[key] = f.group(2).strip()
    for w in warns:
        w["at"] = _parse_date(w.get("date", ""))
        w["severity"] = (w.get("severity") or "").strip().title()
    return {"user": user, "warns": warns}


def _doc(channel_id, msg) -> dict | None:
    parsed = parse_ban(getattr(msg, "message", "") or "")
    if not parsed:
        return None
    user = parsed["user"]
    doc = {
        "_id": "%s:%s" % (channel_id, msg.id),
        "channel_id": str(channel_id), "msg_id": msg.id,
        "posted_at": msg.date.replace(tzinfo=None) if getattr(msg, "date", None) else datetime.utcnow(),
        "warns": parsed["warns"],
        "severity": max((w.get("severity") or "" for w in parsed["warns"]),
                        key=lambda s: _SEVERITY_ORDER.get(s.lower(), 0), default=""),
        "warned_by": sorted({w.get("warned_by") for w in parsed["warns"] if w.get("warned_by")}),
        "sessions": [w.get("session") for w in parsed["warns"] if w.get("session")],
        "reason": (parsed["warns"][-1].get("details") if parsed["warns"] else "") or "",
        "synced_at": datetime.utcnow(),
    }
    if "@" in user:
        # Old format (an email instead of the User ID): stored only when this process already knows the
        # address's User ID; otherwise skipped. The address itself is never stored.
        uid = None
        if "*" not in user:
            from services.user_ref import known_user_id
            uid = known_user_id(user)
        if not uid:
            return None
        doc.update(user_id=str(uid), needs_user_id=False, user_id_from="file")
        return doc
    doc.update(user_id=re.sub(r"\D", "", user) or None, needs_user_id=False)
    return doc


def _store(channel_id, messages) -> int:
    from pymongo import ReplaceOne
    ops = []
    for msg in messages:
        d = _doc(channel_id, msg)
        if d:
            ops.append(ReplaceOne({"_id": d["_id"]}, d, upsert=True))
    if ops:
        _col().bulk_write(ops, ordered=False)
    return len(ops)


async def _run(client, channel_ref):
    from telethon import events
    ref = int(channel_ref) if str(channel_ref).lstrip("-").isdigit() else channel_ref
    logger.info("[Bans] connecting to ban channel %s", channel_ref)
    try:
        entity = await client.get_entity(ref)
    except Exception:
        await client.get_dialogs()          # a numeric id resolves only after the dialogs are cached
        entity = await client.get_entity(ref)
    logger.info("[Bans] reading channel '%s'", getattr(entity, "title", channel_ref))
    cid = getattr(entity, "id", channel_ref)
    col = _col()
    col.create_index([("posted_at", -1)])
    col.create_index("user_id")
    removed = col.delete_many({"needs_user_id": True}).deleted_count   # old email-format bans are ignored
    if removed:
        logger.info("[Bans] removed %d old email-format ban(s)", removed)
    meta = main_db_meta()
    state = meta.find_one({"_id": str(cid)}) or {}
    if state.get("parser") != PARSER_VERSION:
        min_id = 0      # the parser changed: read the whole channel again
        logger.info("[Bans] parser v%s: re-reading the whole channel", PARSER_VERSION)
    else:
        last = col.find_one({"channel_id": str(cid)}, sort=[("msg_id", -1)])
        min_id = max(state.get("last_read") or 0, last["msg_id"] if last else 0)
        logger.info("[Bans] reading only messages after #%s", min_id)
    batch, total, seen, skipped, last_read = [], 0, 0, 0, min_id
    async for msg in client.iter_messages(entity, min_id=min_id, reverse=True):
        batch.append(msg)
        seen += 1
        last_read = max(last_read, msg.id)
        body = getattr(msg, "message", "") or ""
        if body and not parse_ban(body):
            skipped += 1
            if skipped <= 8:
                from services.redact import redact_emails
                logger.info("[Bans] not recognised as a ban (msg %s): %r", msg.id, redact_emails(body[:160]))
        if len(batch) >= 50:
            total += _store(cid, batch)
            batch = []
            logger.info("[Bans] backfill: %d message(s) read, %d ban(s) stored so far", seen, total)
    total += _store(cid, batch)
    meta.update_one({"_id": str(cid)}, {"$set": {"parser": PARSER_VERSION, "last_read": last_read}}, upsert=True)
    logger.info("[Bans] backfill done: %d message(s) read, %d ban(s) stored, %d text message(s) not recognised, "
                "now listening for new ones", seen, total, skipped)

    @client.on(events.NewMessage(chats=entity))
    @client.on(events.MessageEdited(chats=entity))
    async def _on_message(event):
        try:
            meta.update_one({"_id": str(cid)}, {"$max": {"last_read": event.message.id}}, upsert=True)
            if _store(cid, [event.message]):
                logger.info("[Bans] ban message %s stored", event.message.id)
        except Exception as e:
            logger.error("[Bans] could not store message %s: %s", getattr(event.message, "id", "?"), e)


def start_bans_tracker() -> None:
    channel = os.getenv("BANS_TG_CHANNEL", "").strip()
    if not channel:
        logger.info("[Bans] BANS_TG_CHANNEL not set, ban tracking is off")
        return
    import threading
    from telegram_bot import mtproto
    logger.info("[Bans] tracker starting, waiting for the MTProto client")

    def _wait_and_start():
        if not mtproto._ready.wait(timeout=300):
            logger.error("[Bans] MTProto client is not ready, ban tracking not started")
            return
        fut = asyncio.run_coroutine_threadsafe(_run(mtproto._client, channel), mtproto._loop)
        try:
            fut.result(timeout=1800)
        except Exception as e:
            logger.error("[Bans] tracker failed to start: %s", e)

    threading.Thread(target=_wait_and_start, daemon=True, name="bans-tracker").start()
