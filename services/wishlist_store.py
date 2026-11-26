"""
Our own game wishlist: every request from Discord, Slack and the console lands here (no external wishlist API).

  wishlist_games     one row per game + platform (key "title|platform"): total requests, per-source counts,
                     status (open / problematic / rejected / added) with a note and its history.
  wishlist_requests  one row per request: who asked, where (discord / slack / console / telegram), when, link.
  wishlist_weekly    the Monday summary of the previous week (Mon-Sun, Kyiv time), one row per week.

The console reads these collections directly (Wishlist tab) and changes a status through the
wishlist_set_status action, which the bot worker runs here.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import pytz
from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from services.game_title import norm_game_title
from utils.general_utils import clean_title

logger = logging.getLogger(__name__)

STATUSES = ("open", "problematic", "rejected", "added")
STATUS_LABELS = {"open": "Open", "problematic": "Problematic", "rejected": "Rejected", "added": "Added"}
SOURCES = ("discord", "slack", "console", "telegram")
KYIV = pytz.timezone("Europe/Kiev")
SUMMARY_HOUR = int(os.getenv("WISHLIST_SUMMARY_HOUR", "9"))      # Monday, Kyiv time
TOP_N = 10


def _db():
    from services.db import main_db
    return main_db()


def games_col():
    return _db()["wishlist_games"]


def requests_col():
    return _db()["wishlist_requests"]


def weekly_col():
    return _db()["wishlist_weekly"]


def ensure_indexes():
    games_col().create_index("key", unique=True)
    games_col().create_index([("status", 1), ("requests", -1)])
    requests_col().create_index([("created_at", -1)])
    requests_col().create_index([("game_key", 1), ("created_at", -1)])
    requests_col().create_index("legacy_id", sparse=True)


def game_key(title, platform) -> str:
    return "%s|%s" % (norm_game_title(clean_title(str(title or ""))), str(platform or "Other").strip().lower())


def _source(source):
    return source if source in SOURCES else "console"


def add_request(title, platform, source, requestor="", author="", url="", now=None) -> dict:
    """Record one request and return the game row after it (requests = new total, created = first request)."""
    title = clean_title(str(title or "")).strip()
    platform = str(platform or "Other").strip() or "Other"
    if not title:
        raise ValueError("game title required")
    source = _source(source)
    now = now or datetime.utcnow()
    key = game_key(title, platform)
    game = games_col().find_one_and_update(
        {"key": key},
        {"$setOnInsert": {"key": key, "title": title, "platform": platform, "status": "open",
                          "first_requested_at": now, "created_at": now},
         "$set": {"last_requested_at": now},
         "$inc": {"requests": 1, "by_source." + source: 1}},
        upsert=True, return_document=ReturnDocument.AFTER)
    requests_col().insert_one({"game_key": key, "title": game.get("title") or title, "platform": platform,
                               "source": source, "requestor": requestor or "", "author": author or "",
                               "url": url or "", "created_at": now})
    game["created"] = game.get("requests") == 1
    return game


def request_note(game, who, manual=False) -> str:
    """Slack text for a new request, the same for every source."""
    title, platform, total = game.get("title"), game.get("platform"), game.get("requests", 1)
    status = game.get("status") or "open"
    if status in ("problematic", "rejected"):
        reason = (" (%s)" % game["status_note"]) if game.get("status_note") else ""
        return "Wishlist request for *%s* (%s) - marked %s%s - by %s (%s total)" % (
            title, platform, STATUS_LABELS[status].lower(), reason, who, total)
    if status == "added":
        return "Wishlist request for *%s* (%s) - already marked added - by %s (%s total)" % (title, platform, who, total)
    if game.get("created"):
        verb = "manually added to" if manual else "added to"
        return "New game %s Wishlist: *%s* (%s) - requested by %s (1 total)" % (verb, title, platform, who)
    return "Additional wishlist request for *%s* (%s) - requested by %s (%s total)" % (title, platform, who, total)


def set_status(key, status, note="", by="", now=None) -> dict:
    if status not in STATUSES:
        raise ValueError("unknown status: %s" % status)
    now = now or datetime.utcnow()
    note = str(note or "").strip()[:500] if status != "open" else ""
    game = games_col().find_one_and_update(
        {"key": key},
        {"$set": {"status": status, "status_note": note, "status_by": by or "", "status_at": now},
         "$push": {"status_history": {"$each": [{"status": status, "note": note, "by": by or "", "at": now}],
                                      "$slice": -50}}},
        return_document=ReturnDocument.AFTER)
    if not game:
        raise ValueError("game not found")
    logger.info("[Wishlist] %s (%s) -> %s by %s", game.get("title"), game.get("platform"), status, by)
    return game


# ---------------------------------------------------------------------------
# Weekly summary
# ---------------------------------------------------------------------------

def week_bounds(day_kyiv):
    """(monday 00:00, next monday 00:00) of the week holding day_kyiv, as naive UTC datetimes."""
    d = day_kyiv.date() if isinstance(day_kyiv, datetime) else day_kyiv
    monday = d - timedelta(days=d.weekday())
    start = KYIV.localize(datetime(monday.year, monday.month, monday.day))
    end = KYIV.localize(datetime.combine(monday + timedelta(days=7), datetime.min.time()))
    return (start.astimezone(pytz.utc).replace(tzinfo=None), end.astimezone(pytz.utc).replace(tzinfo=None), monday)


def weekly_summary(start, end) -> dict:
    rows = list(requests_col().aggregate([
        {"$match": {"created_at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": "$game_key", "n": {"$sum": 1}, "title": {"$last": "$title"},
                    "platform": {"$last": "$platform"}}},
        {"$sort": {"n": -1, "title": 1}}]))
    by_source = {r["_id"]: r["n"] for r in requests_col().aggregate([
        {"$match": {"created_at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": "$source", "n": {"$sum": 1}}}])}
    keys = [r["_id"] for r in rows]
    meta = {g["key"]: g for g in games_col().find({"key": {"$in": keys}},
                                                  {"key": 1, "status": 1, "requests": 1, "first_requested_at": 1})}
    firsts = [meta.get(k, {}).get("first_requested_at") for k in keys]
    new_games = sum(1 for f in firsts if f and start <= f < end)
    top = [{"key": r["_id"], "title": r["title"], "platform": r["platform"], "requests": r["n"],
            "total": meta.get(r["_id"], {}).get("requests", r["n"]),
            "status": meta.get(r["_id"], {}).get("status", "open")} for r in rows[:TOP_N]]
    problematic = [{"key": r["_id"], "title": r["title"], "platform": r["platform"], "requests": r["n"]}
                   for r in rows if meta.get(r["_id"], {}).get("status") == "problematic"]
    return {"requests": sum(r["n"] for r in rows), "games": len(rows), "new_games": new_games,
            "by_source": by_source, "top": top, "problematic": problematic}


def summary_text(week_start_date, s) -> str:
    end = week_start_date + timedelta(days=6)
    head = "*Wishlist - week %s - %s*" % (week_start_date.strftime("%d %b"), end.strftime("%d %b %Y"))
    if not s["requests"]:
        return head + "\nNo wishlist requests this week."
    src = ", ".join("%s %s" % (k, v) for k, v in sorted(s["by_source"].items(), key=lambda kv: -kv[1]))
    lines = [head, "Requests: *%s* for %s game(s), %s new (%s)" % (s["requests"], s["games"], s["new_games"], src),
             "", "*Top requested:*"]
    for i, t in enumerate(s["top"], 1):
        flag = "" if t["status"] == "open" else " [%s]" % STATUS_LABELS.get(t["status"], t["status"])
        lines.append("%s. %s (%s) - %s this week, %s total%s" % (i, t["title"], t["platform"], t["requests"], t["total"], flag))
    if s["problematic"]:
        lines += ["", "*Requested but problematic:* " + ", ".join(
            "%s (%s) x%s" % (p["title"], p["platform"], p["requests"]) for p in s["problematic"])]
    return "\n".join(lines)


def post_weekly_summary(now=None, force=False):
    """On Monday from SUMMARY_HOUR (Kyiv), summarise the previous week once: save it and post it to Slack."""
    now_kyiv = (now or datetime.now(pytz.utc)).astimezone(KYIV)
    if not force and (now_kyiv.weekday() != 0 or now_kyiv.hour < SUMMARY_HOUR):
        return None
    start, end, monday = week_bounds(now_kyiv - timedelta(days=7))
    week_id = monday.isoformat()
    s = weekly_summary(start, end)
    doc = dict(s, _id=week_id, week_start=start, week_end=end, created_at=datetime.utcnow(), slack=False)
    try:
        weekly_col().insert_one(doc)          # the claim: one summary per week even with several workers
    except DuplicateKeyError:
        if not force:
            return None
        weekly_col().replace_one({"_id": week_id}, doc)
    channel = os.getenv("SLACK_WISHLIST_NOTIFICATIONS_CHANNEL_ID") or os.getenv("SLACK_FLOOD_CHANNEL_ID")
    if channel:
        try:
            from services.mongo_db_service import client as slack
            slack.chat_postMessage(channel=channel, text=summary_text(monday, s))
            weekly_col().update_one({"_id": week_id}, {"$set": {"slack": True}})
        except Exception as e:
            logger.error("[Wishlist] weekly summary Slack post failed: %s", e)
            weekly_col().update_one({"_id": week_id}, {"$set": {"slack_error": str(e)[:300]}})
    logger.info("[Wishlist] weekly summary %s: %s requests, %s games", week_id, s["requests"], s["games"])
    return doc


# ---------------------------------------------------------------------------
# Housekeeping: catalog check + one-time import of the old counters
# ---------------------------------------------------------------------------

def mark_added_from_catalog():
    """Open / problematic games that now exist in the Boosteroid catalog become "added"."""
    from services import boosteroid_catalog_service as catalog
    if not catalog.catalog_loaded():
        return 0
    n = 0
    for g in games_col().find({"status": {"$in": ["open", "problematic"]}}, {"key": 1, "title": 1}):
        if catalog.find_in_catalog(g.get("title") or ""):
            set_status(g["key"], "added", "Found in the Boosteroid catalog", by="catalog")
            n += 1
    return n


def import_legacy():
    """Once: seed wishlist_games from the old all-sources counter and the Discord submissions."""
    meta = _db()["wishlist_meta"]
    if meta.find_one({"_id": "legacy_import"}):
        return 0
    logger.info("[Wishlist] importing the old wishlist counters (one time)")
    first, batch = {}, []
    done = {d["legacy_id"] for d in requests_col().find({"legacy_id": {"$exists": True}}, {"legacy_id": 1})}
    for s in _db()["wishlist_submissions"].find({"added_to_wishlist": True, "title": {"$ne": None}}):
        key = game_key(s.get("title"), s.get("entity_type"))
        at = s.get("added_at") or s.get("submitted_at")
        if at and (key not in first or at < first[key]):
            first[key] = at
        if s["_id"] not in done:
            batch.append({"game_key": key, "title": clean_title(s.get("title") or ""),
                          "platform": s.get("entity_type") or "Other", "source": "discord",
                          "requestor": s.get("requestor_discord_name") or "", "author": "",
                          "url": s.get("url") or "", "created_at": at, "legacy_id": s["_id"]})
        if len(batch) >= 500:
            requests_col().insert_many(batch, ordered=False)
            batch = []
    if batch:
        requests_col().insert_many(batch, ordered=False)
    ops = []
    for c in _db()["wishlist_counts"].find():
        title = clean_title(str(c.get("title") or "")).strip()
        if not title:
            continue
        platform = c.get("entity_type") or "Other"
        key, count = game_key(title, platform), int(c.get("count") or 0)
        at = first.get(key)
        ops.append(UpdateOne(
            {"key": key},
            {"$setOnInsert": {"key": key, "title": title, "platform": platform, "status": "open",
                              "first_requested_at": at, "created_at": datetime.utcnow(), "last_requested_at": at},
             "$max": {"requests": count, "by_source.legacy": count}},
            upsert=True))
    for i in range(0, len(ops), 500):
        games_col().bulk_write(ops[i:i + 500], ordered=False)
    n = len(ops)
    meta.insert_one({"_id": "legacy_import", "at": datetime.utcnow(), "games": n})
    logger.info("[Wishlist] imported %s game(s) from the old wishlist counters", n)
    return n


def _loop():
    last_catalog_day = None
    while True:
        try:
            import_legacy()
            post_weekly_summary()
            today = datetime.now(KYIV).date()
            if last_catalog_day != today:
                n = mark_added_from_catalog()
                if n:
                    logger.info("[Wishlist] %s game(s) found in the catalog, marked added", n)
                last_catalog_day = today
        except Exception as e:
            logger.error("[Wishlist] housekeeping failed: %s", e)
        time.sleep(600)


def start():
    try:
        ensure_indexes()
    except Exception as e:
        logger.error("[Wishlist] index setup failed: %s", e)
    threading.Thread(target=_loop, daemon=True, name="wishlist").start()
