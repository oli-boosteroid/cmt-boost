"""
Mattermost -> console Events feed.

Listens to the Boosteroid Admin API notifications channel in Mattermost over a websocket
(instant) and catches up over REST (no mattermostdriver), keeps only prod events from the admin service, turns each one into a
short English line and stores it in Mongo `mm_events` for the console Events tab.

Catch-up: the timestamp of the last processed post is kept in `mm_cursor`. On every
(re)connect the bot first asks REST for posts `since` that moment, so whatever was posted while
the bot or the connection was down is picked up before live events continue. Writes are upserts by post id, so re-reading the
same post is harmless.

Env:
    MM_URL            https://mattermost.example.com   (no trailing slash needed)
    MM_TOKEN          personal access token, OR
    MM_LOGIN + MM_PASSWORD  a regular account (used when personal tokens are disabled on the server;
                      the bot logs in and logs in again when the session expires)
    MM_CHANNEL_ID     optional; otherwise looked up by MM_TEAM + MM_CHANNEL
    MM_TEAM           team name (slug), used only for the channel lookup
    MM_CHANNEL        channel name, default "cloud-boosteroid"
    MM_SAFETY_SYNC_SECONDS  extra REST sync while connected, default 900 (15 min)
    MM_BACKFILL_HOURS first run only: how far back to read, default 168 (7 days)
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from pymongo import UpdateOne

logger = logging.getLogger(__name__)

MM_URL = (os.getenv("MM_URL") or "").rstrip("/")
MM_TOKEN = os.getenv("MM_TOKEN") or ""
MM_LOGIN = os.getenv("MM_LOGIN") or ""
MM_PASSWORD = os.getenv("MM_PASSWORD") or ""
_session = {"token": MM_TOKEN}
MM_CHANNEL_ID = os.getenv("MM_CHANNEL_ID") or ""
MM_TEAM = os.getenv("MM_TEAM") or ""
MM_CHANNEL = os.getenv("MM_CHANNEL") or "cloud-boosteroid"
SAFETY_SYNC_SECONDS = int(os.getenv("MM_SAFETY_SYNC_SECONDS") or 900)
BACKFILL_HOURS = int(os.getenv("MM_BACKFILL_HOURS") or 168)

# Every relevant message carries this in its attachment footer.
ADMIN_FOOTER = "admin.cloud.boosteroid.com"

from services import db as _shared   # one shared client per cluster for the whole process
_db = _shared.main_db()
events_col = _db["mm_events"]
cursor_col = _db["mm_cursor"]

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_PROD = r"MerchantID:\s*1\b"
_APP = r'Application\s+"(?P<game>.+?)"\s*\[ID:\s*(?P<gid>\d+)\]'

RE_STATUS = re.compile(_PROD + r"\s+" + _APP + r"\s+change status from\s+(?P<src>\w+)\s+to:?\s*(?P<dst>\w+)", re.I)
RE_LOCAL = re.compile(_PROD + r"\s+" + _APP + r"\s+[*_]{0,2}(?P<act>START|FINISH)[*_]{0,2}\s+local MAINTENANCE in:\s*(?P<region>[^\n]+)", re.I)
RE_NEW_CARD = re.compile(r'NEW!\s*Game\s+"(?P<game>.+?)"\s+becoming alive', re.I)
RE_PLAYGROUND = re.compile(r'Playground\s+"(?P<server>.+?)"\s+change status to:?\s*(?P<dst>\w+)', re.I)
RE_BULK = re.compile(_PROD + r".*Bulk update:\s*(?P<rest>.+)", re.I | re.S)


MAINT_STATUSES = {"maintenance", "unavailable"}
# The only events we store: added / removed games, new game cards, maintenances (game, regional, server).
KEEP_CATEGORIES = {"new_game", "removed", "game_card", "maintenance", "regional", "server"}
# Bump this whenever the parsing / pairing rules change (categories, texts, what is hidden). Stored
# events are re-parsed once on the next start after a bump, never on an ordinary restart.
PARSER_VERSION = 4
# A maintenance that ends within this many seconds is a status flip-flop, not a real one: both its
# start and end are hidden from the console.
MIN_MAINT_SECONDS = 60
# Removing a game that is in maintenance comes in as two events: "maintenance -> active" and then
# "enabled -> disabled". That "end" is not a real back-online. Any maintenance end followed by the
# game's removal within this window is treated as part of the removal.
REMOVAL_GRACE_SECONDS = 300


def _title(status: str) -> str:
    return status.strip().capitalize()


def parse_event(text: str) -> dict | None:
    """Text of one admin-service message -> {category, text, ...} or None if not interesting."""
    if not text:
        return None

    m = RE_STATUS.search(text)
    if m:
        game, gid = m["game"].strip(), int(m["gid"])
        src, dst = m["src"].lower(), m["dst"].lower()
        base = {"game": game, "game_id": gid, "status_from": src, "status_to": dst}
        # "unavailable" is a maintenance too (same meaning for us).
        if dst in MAINT_STATUSES and src not in MAINT_STATUSES:
            return {**base, "category": "maintenance", "kind": "start", "text": f"{game} went into maintenance"}
        if src in MAINT_STATUSES and dst not in MAINT_STATUSES:
            return {**base, "category": "maintenance", "kind": "end", "text": f"{game} is out of maintenance"}
        if src == "disabled" and dst in ("enabled", "active"):
            return {**base, "category": "new_game", "text": f"{game} added to the service"}
        if dst == "disabled":
            return {**base, "category": "removed", "text": f"{game} removed from the service"}
        return {**base, "category": "status", "text": f"{game}: {_title(src)} to {_title(dst)}"}

    m = RE_LOCAL.search(text)
    if m:
        game, region = m["game"].strip(), m["region"].strip()
        start = m["act"].upper() == "START"
        return {
            "category": "regional", "kind": "start" if start else "end",
            "game": game, "game_id": int(m["gid"]), "region": region,
            "text": f"{game} went into maintenance in {region}" if start
                    else f"{game} is out of maintenance in {region}",
        }

    m = RE_NEW_CARD.search(text)
    if m:
        game = m["game"].strip()
        return {"category": "game_card", "game": game, "text": f"New game card: {game}"}

    m = RE_PLAYGROUND.search(text)
    if m:
        server, dst = m["server"].strip(), m["dst"].lower()
        if dst == "inactive":
            return {"category": "server", "kind": "start", "server": server, "status_to": dst,
                    "text": f"{server} server is down"}
        if dst == "active":
            return {"category": "server", "kind": "end", "server": server, "status_to": dst,
                    "text": f"{server} server is back up"}
        return {"category": "server", "server": server, "status_to": dst,
                "text": f"{server} server: {_title(dst)}"}

    m = RE_BULK.search(text)
    if m:
        first = m["rest"].strip().splitlines()[0][:200]
        return {"category": "bulk", "text": f"Bulk update: {first}"}

    return None


def post_text(post: dict) -> str:
    """Message body + attachment texts, joined. The admin service sends everything as attachments."""
    parts = [post.get("message") or ""]
    for a in (post.get("props") or {}).get("attachments") or []:
        for k in ("pretext", "title", "text", "footer"):
            if a.get(k):
                parts.append(str(a[k]))
    return "\n".join(p for p in parts if p)


_NOISE = re.compile(r"^(INFO|WARNING|WARN|ERROR|CRITICAL|DEBUG|Boostore|Streaming|AdminID:.*|AdminEmail:.*|.*admin\.cloud\.boosteroid\.com.*)$", re.I)


def _admin_email(text: str) -> str:
    m = re.search(r"AdminEmail:\s*\[?([^\s\]\(]+@[^\s\]\(]+)", text)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Mattermost REST
# ---------------------------------------------------------------------------

def _login() -> None:
    r = requests.post(f"{MM_URL}/api/v4/users/login",
                      json={"login_id": MM_LOGIN, "password": MM_PASSWORD}, timeout=30)
    r.raise_for_status()
    _session["token"] = r.headers.get("Token") or ""
    logger.info("[MMEvents] Logged in to Mattermost")


def _get(path: str, **params):
    if not _session["token"] and MM_LOGIN:
        _login()
    for attempt in (1, 2):
        r = requests.get(f"{MM_URL}/api/v4{path}", params=params,
                         headers={"Authorization": f"Bearer {_session['token']}"}, timeout=30)
        if r.status_code == 401 and MM_LOGIN and attempt == 1:
            _login()   # session expired
            continue
        r.raise_for_status()
        return r.json()


def _resolve_channel_id() -> str:
    if MM_CHANNEL_ID:
        return MM_CHANNEL_ID
    teams = [MM_TEAM] if MM_TEAM else [t["name"] for t in _get("/users/me/teams")]
    for team in teams:
        try:
            return _get(f"/teams/name/{team}/channels/name/{MM_CHANNEL}")["id"]
        except requests.HTTPError:
            continue
    raise RuntimeError(f"Mattermost channel '{MM_CHANNEL}' not found in teams {teams}")


def _store_posts(posts: list) -> int:
    """Interesting posts -> upserted into mm_events (by post id). Returns stored count."""
    ops, evs = [], []
    for p in posts:
        if p.get("delete_at"):
            continue
        text = post_text(p)
        if ADMIN_FOOTER not in text:
            continue
        ev = parse_event(text)
        if not ev or ev.get("category") not in KEEP_CATEGORIES:
            continue   # not one of the events we track
        ev.update({
            "post_id": p["id"],
            "created_at": datetime.fromtimestamp(p["create_at"] / 1000, tz=timezone.utc),
            "admin_email": _admin_email(text),
            "raw": text[:2000],
        })
        ops.append(UpdateOne({"post_id": p["id"]}, {"$set": ev}, upsert=True))
        evs.append(ev)
    if ops:
        events_col.bulk_write(ops, ordered=False)
        for p in sorted(posts, key=lambda x: x.get("create_at", 0)):
            _pair_maintenance(p["id"])
        for ev in evs:
            if ev.get("category") == "removed":
                _supersede_end_by_removal(ev)
        _apply_to_catalog(evs)
        _apply_maintenances(evs)
    return len(ops)


def _apply_maintenances(evs: list) -> None:
    """Game / server maintenances follow these events (Slack, active list, user notices): see
    services/mm_maintenance.py. Oldest first; each event is applied once."""
    from services import mm_maintenance
    for ev in sorted(evs, key=lambda e: e.get("created_at")):
        if ev.get("category") in ("maintenance", "regional", "server") and ev.get("kind") in ("start", "end"):
            try:
                mm_maintenance.apply(ev["post_id"])
            except Exception as e:
                logger.error(f"[MMEvents] Maintenance handling failed for post {ev.get('post_id')}: {e}")


def _apply_to_catalog(evs: list) -> None:
    """Keep the Boosteroid game catalog in sync: added games are fetched and stored, removed ones
    dropped. Oldest first, so an add-then-remove (or the reverse) ends in the right state."""
    for ev in sorted(evs, key=lambda e: e.get("created_at")):
        gid = ev.get("game_id")
        if gid is None:
            continue
        try:
            from services import boosteroid_catalog_service as catalog
            if ev.get("category") == "new_game":
                catalog.upsert_game(gid)
            elif ev.get("category") == "removed":
                catalog.remove_game(gid)
        except Exception as e:
            logger.error(f"[MMEvents] Catalog update for game {gid} failed: {e}")


def _maint_key(ev: dict) -> dict | None:
    """What identifies one maintenance: the game, the game in a region, or the server."""
    cat = ev.get("category")
    if cat == "maintenance" and ev.get("game_id") is not None:
        return {"category": "maintenance", "game_id": ev["game_id"]}
    if cat == "regional" and ev.get("game_id") is not None:
        return {"category": "regional", "game_id": ev["game_id"], "region": ev.get("region")}
    if cat == "server" and ev.get("server"):
        return {"category": "server", "server": ev["server"]}
    return None


def _pair_maintenance(post_id: str) -> None:
    """An end event finds the latest open start of the same maintenance: both get the duration."""
    ev = events_col.find_one({"post_id": post_id})
    if not ev or ev.get("kind") != "end" or ev.get("started_at"):
        return
    key = _maint_key(ev)
    if not key:
        return
    start = events_col.find_one(
        {**key, "kind": "start", "created_at": {"$lte": ev["created_at"]}, "ended_at": None},
        sort=[("created_at", -1)])
    if not start:
        return
    secs = int((ev["created_at"] - start["created_at"]).total_seconds())
    flip = secs < MIN_MAINT_SECONDS   # on and off within a minute -> not a real maintenance, hide both
    events_col.update_one({"_id": ev["_id"]}, {"$set": {"started_at": start["created_at"], "duration_s": secs, "ignored": flip}})
    events_col.update_one({"_id": start["_id"]}, {"$set": {"ended_at": ev["created_at"], "duration_s": secs, "ignored": flip}})


def _supersede_end_by_removal(removed: dict) -> None:
    """A game removed right after its maintenance "ended": that end was part of the removal, not a
    real back-online. Hide it (and its start) from the console and mark it so notifications skip it."""
    gid, at = removed.get("game_id"), removed.get("created_at")
    if gid is None or at is None:
        return
    from datetime import timedelta
    q = {"category": "maintenance", "kind": "end", "game_id": gid,
         "created_at": {"$gte": at - timedelta(seconds=REMOVAL_GRACE_SECONDS), "$lte": at}}
    for end in events_col.find(q):
        events_col.update_one({"_id": end["_id"]}, {"$set": {"ignored": True, "ended_by_removal": True}})
        if end.get("started_at") is not None:
            # The maintenance itself is over (the game is gone): keep its start out of "Active" and
            # out of the finished list, the Removed event tells the story.
            events_col.update_many({"category": "maintenance", "kind": "start", "game_id": gid,
                                    "created_at": end["started_at"]},
                                   {"$set": {"ignored": True, "ended_by_removal": True}})
        logger.info(f"[MMEvents] Maintenance end of game {gid} was part of its removal - hidden")


# End-of-maintenance notifications to users wait this long after the end event, then send only if
# maintenance_really_ended() still says yes (no removal, no new maintenance in the meantime).
NOTIFY_DELAY_SECONDS = {"maintenance": 3600, "regional": 3600, "server": 1800}   # games 1 h, servers 30 min


def notify_due_at(end_ev: dict):
    """When an end-of-maintenance notification for this end event may be sent (created_at + delay)."""
    from datetime import timedelta
    return end_ev["created_at"] + timedelta(seconds=NOTIFY_DELAY_SECONDS.get(end_ev.get("category"), 3600))


def maintenance_really_ended(end_ev: dict) -> bool:
    """For end-of-maintenance notifications to users. Call it at notify_due_at(end_ev) or later.
    False when, within the delay after the end:
      - the game was removed from the service (its "end" was only the first step of the removal), or
      - the same game / region / server went into maintenance (or down) again.
    Either way telling users "it is back" would be wrong."""
    from datetime import timedelta
    if end_ev.get("ignored") or end_ev.get("ended_by_removal"):
        return False
    key = _maint_key(end_ev)
    at = end_ev.get("created_at")
    if not key or at is None:
        return True
    until = at + timedelta(seconds=NOTIFY_DELAY_SECONDS.get(end_ev.get("category"), 3600))
    window = {"$gt": at, "$lte": until}
    if end_ev.get("game_id") is not None and events_col.find_one(
            {"category": "removed", "game_id": end_ev["game_id"], "created_at": {"$gte": at, "$lte": until}}):
        return False
    if events_col.find_one({**key, "kind": "start", "created_at": window}):
        return False
    return True


def _reprocess_if_rules_changed() -> None:
    """Re-parse stored events only when PARSER_VERSION is newer than the one they were parsed with."""
    state = cursor_col.find_one({"_id": "parser_version"}) or {}
    if state.get("version") == PARSER_VERSION:
        return
    n = reprocess_all()
    cursor_col.update_one({"_id": "parser_version"}, {"$set": {"version": PARSER_VERSION}}, upsert=True)
    logger.info(f"[MMEvents] Parsing rules updated to v{PARSER_VERSION}: re-parsed {n} stored event(s)")


def reprocess_all() -> int:
    """Re-parse every stored event from its raw text and redo the maintenance pairing, so parser
    changes (e.g. "unavailable" = maintenance, hiding sub-minute flip-flops) apply to old events too."""
    docs = list(events_col.find({"raw": {"$exists": True}}, {"raw": 1, "post_id": 1}).sort("created_at", 1))
    drop = []
    for d in docs:
        ev = parse_event(d["raw"])
        if not ev or ev.get("category") not in KEEP_CATEGORIES:
            drop.append(d["_id"])   # stored before we narrowed down what to track
            continue
        ev.setdefault("kind", None)
        events_col.update_one({"_id": d["_id"]}, {
            "$set": ev,
            "$unset": {"started_at": "", "ended_at": "", "duration_s": "", "ignored": "", "ended_by_removal": ""}})
    if drop:
        events_col.delete_many({"_id": {"$in": drop}})
    for d in docs:
        if d["_id"] not in drop:
            _pair_maintenance(d["post_id"])
    for d in docs:
        if d["_id"] not in drop:
            ev = events_col.find_one({"_id": d["_id"]}) or {}
            if ev.get("category") == "removed":
                _supersede_end_by_removal(ev)
    return len(docs)


def _move_cursor(channel_id: str, ts: int) -> None:
    # $max: the cursor only ever moves forward, whichever path (websocket / catch-up) saw the post.
    cursor_col.update_one({"_id": channel_id}, {"$max": {"since": int(ts)}}, upsert=True)


def poll_once(channel_id: str) -> int:
    """Catch-up: reads every post since the cursor over REST, stores the interesting ones."""
    cur = cursor_col.find_one({"_id": channel_id})
    if cur:
        since = int(cur["since"])
    else:
        since = int((time.time() - BACKFILL_HOURS * 3600) * 1000)

    data = _get(f"/channels/{channel_id}/posts", since=since)
    posts = sorted((data.get("posts") or {}).values(), key=lambda p: p.get("create_at", 0))
    n = _store_posts(posts)
    newest = max([since] + [max(int(p.get("create_at") or 0), int(p.get("update_at") or 0)) for p in posts])
    # `since` overlaps a little on the server side; the upsert by post_id makes that harmless.
    _move_cursor(channel_id, newest)
    return n


def _ws_url() -> str:
    return MM_URL.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/v4/websocket"


def _listen(channel_id: str) -> None:
    """One websocket session: connect, catch up over REST, then handle live posts until it drops."""
    import websocket   # websocket-client

    if not _session["token"] and MM_LOGIN:
        _login()
    try:
        ws = websocket.create_connection(
            _ws_url(), header=[f"Authorization: Bearer {_session['token']}"], timeout=30)
    except websocket.WebSocketBadStatusException as e:
        if getattr(e, "status_code", 0) == 401 and MM_LOGIN:
            _session["token"] = ""   # expired session -> fresh login on the next attempt
        raise

    try:
        # Connected first, caught up second: anything posted in between waits in the socket,
        # so nothing falls into a gap.
        n = poll_once(channel_id)
        logger.info(f"[MMEvents] Websocket connected, caught up {n} event(s)")
        last_sync = time.time()
        ws.settimeout(60)
        while True:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                raw = None
            if raw:
                msg = json.loads(raw)
                if msg.get("event") == "posted" and (msg.get("broadcast") or {}).get("channel_id") == channel_id:
                    post = json.loads((msg.get("data") or {}).get("post") or "{}")
                    if post.get("id"):
                        if _store_posts([post]):
                            logger.info(f"[MMEvents] Live event stored ({post['id']})")
                        _move_cursor(channel_id, post.get("create_at") or 0)
            # Safety net: a quiet REST sync now and then, in case a websocket event was ever dropped.
            if time.time() - last_sync > SAFETY_SYNC_SECONDS:
                poll_once(channel_id)
                last_sync = time.time()
    finally:
        try:
            ws.close()
        except Exception as _e:
            logger.debug("ignored: %s", _e)


def _loop() -> None:
    channel_id, delay = None, 5
    while True:
        started = time.time()
        try:
            if not channel_id:
                channel_id = _resolve_channel_id()
                events_col.create_index("post_id", unique=True)
                events_col.create_index([("created_at", -1)])
                try:
                    _reprocess_if_rules_changed()
                except Exception as e:
                    logger.error(f"[MMEvents] Re-parse failed: {e}")
                logger.info(f"[MMEvents] Watching channel {MM_CHANNEL} ({channel_id})")
            _listen(channel_id)
        except Exception as e:
            logger.error(f"[MMEvents] Websocket dropped: {e}")
        # Reconnect with backoff (reset after a session that lived a while).
        delay = 5 if time.time() - started > 300 else min(delay * 2, 300)
        time.sleep(delay)


def start() -> None:
    if not (MM_URL and (MM_TOKEN or (MM_LOGIN and MM_PASSWORD))):
        logger.info("[MMEvents] Mattermost credentials not set, Mattermost events are off")
        return
    threading.Thread(target=_loop, daemon=True, name="mm-events").start()
    from services import mm_maintenance
    mm_maintenance.start()
