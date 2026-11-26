"""
Reddit events from our Devvit app (cmthelper) -> the usual Slack alerts, notifications and console data.

The Devvit app runs on Reddit's servers and receives the subreddit's events even while Reddit blocks our
API app. It POSTs them to the console (cmt-console api/routes/devvit_ingest.py), which queues them in
Mongo `devvit_inbox`. This task drains that queue inside the Reddit event loop and turns each event into
the same post / comment / ModMail-conversation shape the API trackers produce, so it goes through the
unchanged handlers in redit_notifications (handle_post, handle_comment, _process_modmail_convo).

Seen-tracking in those handlers dedupes against the API streams and the RSS fallback, so running all of
them side by side never sends anything twice.

Event shapes (what the app sends as `data`):
  post:    {post: PostV2, author: {name}, subreddit: {name}}
  comment: {comment: CommentV2, author: {name}, post: PostV2}
  modmail: {conversation: ConversationData (with messages)}   - fetched in full by the app
"""
import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace

logger = logging.getLogger(__name__)

POLL_SECONDS = 5


def _strip(thing_id: str, prefix: str = "") -> str:
    """'t3_abc' -> 'abc', 'ModmailConversation_3v29uj' -> '3v29uj'."""
    s = str(thing_id or "")
    if prefix and s.startswith(prefix):
        return s[len(prefix):]
    return s.split("_", 1)[-1] if "_" in s else s


def _epoch(value) -> float | None:
    """Devvit times come as epoch seconds or milliseconds (numbers) or ISO strings."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def to_post(data: dict):
    p = data.get("post") or {}
    flair = (p.get("linkFlair") or {}).get("text") or ""
    permalink = p.get("permalink") or ""
    return SimpleNamespace(
        id=_strip(p.get("id")), title=p.get("title") or "",
        author=(data.get("author") or {}).get("name") or "[deleted]",
        permalink=permalink, selftext=p.get("selftext") or "", created_utc=_epoch(p.get("createdAt")),
        subreddit=(data.get("subreddit") or {}).get("name") or "", url="https://www.reddit.com" + permalink,
        num_comments=p.get("numComments"), is_gallery=False,
        link_flair_text=flair, link_flair_richtext=[],
    )


def to_comment(data: dict):
    c = data.get("comment") or {}
    return SimpleNamespace(
        id=_strip(c.get("id")), author=(data.get("author") or {}).get("name") or "[deleted]",
        body=c.get("body") or "", created_utc=_epoch(c.get("createdAt")),
        permalink=c.get("permalink") or "", link_id=c.get("postId") or "", parent_id=c.get("parentId") or "",
        distinguished=None, is_submitter=False,
    )


def to_convo(data: dict):
    """ConversationData -> an object shaped like asyncpraw's ModmailConversation (what the tracker uses)."""
    conv = data.get("conversation") or {}

    async def _load():   # already complete: the app fetched the whole conversation
        return None

    msgs = []
    for m in sorted((conv.get("messages") or {}).values(), key=lambda m: m.get("date") or ""):
        author = (m.get("author") or {}).get("name")
        msgs.append(SimpleNamespace(
            id=_strip(m.get("id"), "ModmailMessage_"),
            author=SimpleNamespace(name=author) if author else None,
            body_markdown=m.get("bodyMarkdown") or m.get("body") or "",
            is_internal=bool(m.get("isInternal")), date=m.get("date"), distinguished=None,
        ))
    participant = (conv.get("participant") or {}).get("name")
    return SimpleNamespace(
        id=_strip(conv.get("id"), "ModmailConversation_"), subject=conv.get("subject") or "",
        participant=SimpleNamespace(name=participant) if participant else None,
        last_updated=conv.get("lastUpdated"), messages=msgs, load=_load,
        state=str(conv.get("state") or "").lower(),
    )


async def track_devvit_inbox(ignored_users_list: list) -> None:
    from reddit_notifications.redit_notifications import (_db, _snap_modmail_col, handle_post, handle_comment,
                                                          _process_modmail_convo)
    inbox = _db["devvit_inbox"]
    try:
        await inbox.create_index("expire_at", expireAfterSeconds=0)   # handled events auto-delete after 14 days
        await inbox.create_index([("status", 1), ("received_at", 1)])
        await inbox.create_index("relay_key", sparse=True)
        await _db["devvit_chunks"].create_index("expire_at", expireAfterSeconds=0)   # half-received events
        await _db["devvit_chunks"].create_index("eid")
    except Exception as e:
        logger.warning("[Devvit] inbox indexes not ensured: %s", e)
    logger.info("[Devvit] inbox consumer started")
    while True:
        try:
            ev = await inbox.find_one_and_update({"status": "new"},
                                                 {"$set": {"status": "processing", "picked_at": datetime.utcnow()}},
                                                 sort=[("received_at", 1)])
            if not ev:
                await asyncio.sleep(POLL_SECONDS)
                continue
            kind, data, error = ev.get("kind"), ev.get("data") or {}, None
            try:
                if kind == "post":
                    await handle_post(to_post(data), ignored_users_list)
                elif kind == "comment":
                    await handle_comment(to_comment(data), ignored_users_list)
                elif kind == "modmail":
                    convo = to_convo(data)
                    await _process_modmail_convo(convo, ignored_users_list)
                    # Without the API's snapshot pass, the app's copy is the source of the state: e.g. a user
                    # writing into an archived conversation un-archives it on Reddit.
                    if convo.id and convo.state:
                        cur = await _snap_modmail_col.find_one({"convo_id": convo.id}, {"last_updated": 1})
                        if not (cur and str(cur.get("last_updated") or "") > str(convo.last_updated or "")):
                            await _snap_modmail_col.update_one({"convo_id": convo.id}, {"$set": {"state": convo.state}}, upsert=True)
                else:
                    error = "unknown kind %r" % kind
            except Exception as e:
                error = "%s: %s" % (type(e).__name__, e)
                logger.error("[Devvit] %s event failed: %s", kind, error)
            await inbox.update_one({"_id": ev["_id"]}, {"$set": {"status": "error" if error else "done",
                                                                  "error": error, "done_at": datetime.utcnow()}})
        except Exception as e:
            logger.error("[Devvit] inbox consumer error: %s", e)
            await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Slack relay: the Devvit app posts events into a private Slack channel (slack.com is on Devvit's global
# fetch allowlist, our own domain would need Reddit's approval). main.py's Slack event handlers pass every
# message here first; a valid relay message is queued into devvit_inbox and the handlers stop there.
# ---------------------------------------------------------------------------
RELAY_PREFIX = "cmt-devvit:v1:"


def _queue_event(kind: str, payload: str, sig: str, key: str, quiet: bool = False) -> None:
    """Verify the HMAC over the whole base64 payload, check the subreddit, queue it into devvit_inbox."""
    import base64
    import hashlib
    import hmac
    import json
    import os
    from datetime import timedelta
    secret = os.getenv("DEVVIT_SECRET", "")
    good = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        # Re-reading old history (catch-up) meets early test messages that Slack split: not worth a warning.
        (logger.debug if quiet else logger.warning)("[Devvit] relay message with a bad signature dropped")
        return
    msg = json.loads(base64.b64decode(payload).decode("utf-8"))
    if msg.get("kind") != kind or kind not in ("post", "comment", "modmail") or not isinstance(msg.get("data"), dict):
        logger.warning("[Devvit] malformed relay message dropped")
        return
    # Only our subreddit (SUBREDDIT_NAME) and test subreddits listed in DEVVIT_ALLOWED_SUBREDDITS reach
    # Slack / the console; events from any other install of the app are dropped.
    allowed = {x.strip().lower().removeprefix("r/") for x in
               (os.getenv("SUBREDDIT_NAME", "") + "," + os.getenv("DEVVIT_ALLOWED_SUBREDDITS", "")).split(",") if x.strip()}
    sub = str(msg.get("sub") or "").lower()
    if sub not in allowed:
        (logger.debug if quiet else logger.info)("[Devvit] %s event from r/%s ignored (not an allowed subreddit)", kind, sub or "?")
        return
    from services.db import main_db
    now = datetime.utcnow()
    # Slack retries an event it thinks was not acknowledged: keyed so a repeat is a no-op.
    main_db()["devvit_inbox"].update_one(
        {"relay_key": key},
        {"$setOnInsert": {"kind": kind, "data": msg["data"], "status": "new", "received_at": now,
                          "relay_key": key, "expire_at": now + timedelta(days=14)}},
        upsert=True)


def ingest_slack_relay(event: dict) -> bool:
    """True when `event` is a Devvit relay message (then queued, held for its other parts, or dropped).

    v2 (current): cmt-devvit:v2:<kind>:<hmac>:<event id>:<part>:<parts>:<base64 chunk>
        Slack splits long messages, so the app sends each event in parts of <= 3500 characters; the parts
        are collected in Mongo `devvit_chunks` and the HMAC is checked over the joined payload.
    v1 (old):     cmt-devvit:v1:<kind>:<hmac>:<base64 JSON>
    """
    import os
    from datetime import timedelta

    text = (event or {}).get("text") or ""
    channel = os.getenv("DEVVIT_RELAY_CHANNEL_ID", "")
    if not channel or event.get("channel") != channel:
        return False   # outside the relay channel the prefixes are just text, not ours to handle
    if text.startswith(("cmt-devvit-cmd:", "cmt-devvit-ack:")):
        if text.startswith("cmt-devvit-ack:v1:"):
            _handle_ack(text)
        return True   # our own commands / the app's acks: never handled as a normal message
    if not text.startswith("cmt-devvit:"):
        return False
    if not os.getenv("DEVVIT_SECRET", ""):
        logger.warning("[Devvit] relay message ignored: DEVVIT_SECRET is not set on the bot")
        return True
    try:
        if text.startswith("cmt-devvit:v2:"):
            _, _, kind, sig, eid, part, parts, chunk = text.split(":", 7)
            part, parts = int(part), int(parts)
            from services.db import main_db
            col = main_db()["devvit_chunks"]
            now = datetime.utcnow()
            col.update_one({"_id": "%s:%d" % (eid, part)},
                           {"$set": {"eid": eid, "part": part, "chunk": chunk.strip(), "at": now,
                                     "expire_at": now + timedelta(hours=6)}}, upsert=True)
            got = list(col.find({"eid": eid}, {"part": 1, "chunk": 1}).sort("part", 1))
            if len(got) < parts:
                return True   # wait for the other parts
            payload = "".join(g["chunk"] for g in got[:parts])
            col.delete_many({"eid": eid})
            _queue_event(kind, payload, sig, "v2:" + eid, quiet=bool(event.get("_catchup")))
        elif text.startswith("cmt-devvit:v1:"):
            _, _, kind, sig, payload = text.split(":", 4)
            _queue_event(kind, payload.strip(), sig, "v1:" + str(event.get("ts")), quiet=bool(event.get("_catchup")))
    except Exception as e:
        logger.error("[Devvit] relay message could not be queued: %s", e)
    return True


def catch_up_relay(max_hours: int = 24) -> int:
    """Re-read the relay channel's history and queue what the live Slack events missed (the bot was
    restarting, Slack gave up retrying, ...). Safe to repeat: every event is keyed (relay_key), parts are
    re-collected, so an already queued event is a no-op. Runs at startup and every few minutes."""
    import os
    import time as _time
    from slack_sdk import WebClient
    channel = os.getenv("DEVVIT_RELAY_CHANNEL_ID", "")
    if not channel or not os.getenv("DEVVIT_SECRET") or not os.getenv("SLACK_TOKEN"):
        return 0
    from services.db import main_db
    cfg = main_db()["console_config"]
    cur = cfg.find_one({"key": "devvit_relay_cursor"}) or {}
    floor = _time.time() - max_hours * 3600
    oldest = max(float(cur.get("ts") or 0) - 600, floor)   # 10 min overlap: parts may arrive out of order
    client = WebClient(token=os.getenv("SLACK_TOKEN"))
    prev = float(cur.get("ts") or 0)
    seen, newest, cursor = 0, prev, None
    while True:
        resp = client.conversations_history(channel=channel, oldest=str(oldest), limit=200, cursor=cursor)
        for m in reversed(resp.get("messages") or []):
            text = m.get("text") or ""
            fresh_ack = text.startswith("cmt-devvit-ack:") and prev and float(m.get("ts") or 0) > prev
            if text.startswith("cmt-devvit:") or fresh_ack:
                ingest_slack_relay({"text": text, "channel": channel, "ts": m.get("ts"), "_catchup": True})
                seen += 1
            newest = max(newest, float(m.get("ts") or 0))
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    if newest:
        cfg.update_one({"key": "devvit_relay_cursor"}, {"$set": {"key": "devvit_relay_cursor", "ts": newest}}, upsert=True)
    return seen


# ---------------------------------------------------------------------------
# Commands back to Reddit (console replies) while the API is blocked: the bot writes a signed command
# into the relay channel, the Devvit app's per-minute job runs it on Reddit and answers with a signed ack.
#   command: cmt-devvit-cmd:v1:<hmac>:<base64 JSON {id, op, sub, ...}>
#   ack:     cmt-devvit-ack:v1:<hmac>:<base64 JSON {id, ok, error?}>
# ---------------------------------------------------------------------------
CMD_PREFIX = "cmt-devvit-cmd:v1:"
ACK_PREFIX = "cmt-devvit-ack:v1:"


def relay_configured() -> bool:
    import os
    return bool(os.getenv("DEVVIT_RELAY_CHANNEL_ID") and os.getenv("DEVVIT_SECRET") and os.getenv("SLACK_TOKEN"))


def send_reddit_command(cmd_id: str, op: str, **fields) -> tuple[bool, str | None]:
    """Queue one Reddit action for the Devvit app (runs within ~1 minute). Returns (queued, error)."""
    import base64
    import hashlib
    import hmac
    import json
    import os
    from slack_sdk import WebClient
    # DEVVIT_COMMAND_SUBREDDIT overrides the target only for testing on a test subreddit.
    sub = os.getenv("DEVVIT_COMMAND_SUBREDDIT") or os.getenv("SUBREDDIT_NAME", "")
    cmd = {"id": str(cmd_id), "op": op, "sub": sub, **fields}
    payload = base64.b64encode(json.dumps(cmd, ensure_ascii=False).encode("utf-8")).decode("ascii")
    sig = hmac.new(os.getenv("DEVVIT_SECRET", "").encode(), payload.encode(), hashlib.sha256).hexdigest()
    try:
        WebClient(token=os.getenv("SLACK_TOKEN")).chat_postMessage(
            channel=os.getenv("DEVVIT_RELAY_CHANNEL_ID"), text=f"{CMD_PREFIX}{sig}:{payload}")
        logger.info("[Devvit] %s queued for the app (%s)", op, cmd_id)
        return True, None
    except Exception as e:
        return False, "could not queue the command for the Reddit app: %s" % e


_acks_seen = set()


def _handle_ack(text: str) -> None:
    """The app's result for a command: on failure, flip the console action to error so the operator sees it."""
    import base64
    import hashlib
    import hmac
    import json
    import os
    try:
        sig, payload = text[len(ACK_PREFIX):].split(":", 1)
        good = hmac.new(os.getenv("DEVVIT_SECRET", "").encode(), payload.strip().encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            logger.warning("[Devvit] ack with a bad signature dropped")
            return
        ack = json.loads(base64.b64decode(payload).decode("utf-8"))
        aid = str(ack.get("id"))
        if aid in _acks_seen:          # the same ack read live and again by the catch-up: handle once
            return
        _acks_seen.add(aid)
        if len(_acks_seen) > 5000:
            _acks_seen.clear()
        if ack.get("ok"):
            logger.info("[Devvit] command %s done on Reddit", ack.get("id"))
            return
        logger.error("[Devvit] command %s failed on Reddit: %s", ack.get("id"), ack.get("error"))
        from bson import ObjectId
        from services.db import main_db
        try:
            main_db()["console_actions"].update_one(
                {"_id": ObjectId(str(ack.get("id")))},
                {"$set": {"status": "error", "error": "Reddit app: %s" % (ack.get("error") or "failed")}})
        except Exception as _e:
            logger.debug("ignored: %s", _e)
    except Exception as e:
        logger.error("[Devvit] ack could not be read: %s", e)
