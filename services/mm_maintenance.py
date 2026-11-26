"""
Game / server maintenances driven by the Mattermost admin events.

services/mattermost_events.py stores every admin-service message in `mm_events`. For each NEW maintenance
event this module does the following:
  start (game / server)  -> Slack message in the games / servers maintenance channel, record in
                            `active_maintenances` (console Maintenance tab, Discord/Reddit "!maintenance"),
                            for a game: a row in the maintenance Google Sheet;
  end                    -> Slack "finished" message with the duration, record removed.
A game whose name carries "[Brazil]", or a regional maintenance in Brazil, is a Brazil-only maintenance
(type "brazil"), exactly like the webhook's "[Brazil]" rule. Other regional maintenances only stay in the
Events feed, as before.

End-of-maintenance messages to subscribed users (Discord / Reddit) are not flagged at once: they wait
mattermost_events.NOTIFY_DELAY_SECONDS (games 1 h, servers 30 min) and go out only if
maintenance_really_ended() still says yes (the game was not removed and did not go into maintenance again).

Safety:
  - every event is applied at most once (`maint_applied` flag, set atomically);
  - events older than the moment this module was first switched on are never applied, so the first start
    (which back-reads up to MM_BACKFILL_HOURS) does not replay old maintenances into Slack;
  - MM_MAINTENANCE=0 switches the whole thing off (the Events feed keeps working).
"""
import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

NOTIFY_CHECK_SECONDS = 60


def enabled() -> bool:
    return os.getenv("MM_MAINTENANCE", "1") != "0"


def _mm():
    from services import mattermost_events
    return mattermost_events


def _utc(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _since():
    """The moment maintenance handling was first switched on (stored once). Older events are history."""
    col = _mm().cursor_col
    doc = col.find_one({"_id": "maintenance_since"})
    if not doc:
        now = datetime.now(timezone.utc)
        col.update_one({"_id": "maintenance_since"}, {"$setOnInsert": {"at": now}}, upsert=True)
        doc = col.find_one({"_id": "maintenance_since"}) or {"at": now}
    return _utc(doc["at"])


def _duration(secs) -> str | None:
    if secs is None:
        return None
    secs = max(0, int(secs))
    h, m = divmod(secs // 60, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{secs}s"


def _target(ev):
    """(entity, store_type, slack_type) for an event, or None when it is not a tracked maintenance."""
    from services.maintenance_names import split_brazil
    cat = ev.get("category")
    if cat == "maintenance" and ev.get("game"):
        clean, brazil = split_brazil(ev["game"])
        return clean, ("brazil" if brazil else "game"), "game"
    if cat == "regional" and ev.get("game") and "brazil" in str(ev.get("region") or "").lower():
        clean, _ = split_brazil(ev["game"])
        return clean, "brazil", "game"
    if cat == "server" and ev.get("server"):
        return ev["server"].strip(), "server", "server"
    return None


def _send_slack(entity, is_started, slack_type, duration, event_dt):
    from services.maintenance_service import send_maintenance_message
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(send_maintenance_message(entity, is_started, slack_type, duration, event_dt))
    finally:
        loop.close()


def apply(post_id: str) -> str:
    """Apply one stored maintenance event (idempotent). Returns what was done, for logs and tests."""
    if not enabled():
        return "off"
    col = _mm().events_col
    ev = col.find_one({"post_id": post_id})
    if not ev or ev.get("kind") not in ("start", "end") or ev.get("maint_applied"):
        return "skip"
    target = _target(ev)
    if not target:
        return "not tracked"
    if _utc(ev.get("created_at")) is None or _utc(ev["created_at"]) < _since():
        col.update_one({"_id": ev["_id"]}, {"$set": {"maint_applied": "history"}})
        return "history"
    # Claim: only one thread/process applies an event.
    if not col.find_one_and_update({"_id": ev["_id"], "maint_applied": {"$exists": False}},
                                   {"$set": {"maint_applied": True, "maint_applied_at": datetime.now(timezone.utc)}}):
        return "skip"
    from services.mongo_db_service import add_active_maintenance, remove_active_maintenance, get_active_maintenance
    entity, store_type, slack_type = target
    event_dt = _utc(ev["created_at"]).strftime("%Y-%m-%d %H:%M UTC")
    if ev["kind"] == "start":
        if get_active_maintenance(entity, store_type):
            return "already active"
        slack_ts, slack_channel = _send_slack(entity, True, slack_type, None, event_dt)
        add_active_maintenance(entity, store_type, slack_ts=slack_ts, slack_channel=slack_channel)
        if slack_type == "game":
            try:
                from services.maintenance_sheet_sync import push_maintenance_start
                threading.Thread(target=push_maintenance_start,
                                 args=(entity, "Only for Brazil" if store_type == "brazil" else "All playgrounds", None),
                                 daemon=True, name="maint-sheet").start()
            except Exception as e:
                logger.error(f"[MMMaintenance] sheet append failed to start for '{entity}': {e}")
        logger.info(f"[MMMaintenance] {store_type} maintenance started: {entity}")
        return "started"
    # end
    was_active = get_active_maintenance(entity, store_type) is not None
    if was_active or ev.get("category") != "regional":
        _send_slack(entity, False, slack_type, _duration(ev.get("duration_s")), event_dt)
    remove_active_maintenance(entity, store_type)
    if store_type == "brazil":
        remove_active_maintenance(entity, "game")       # a record from before the Brazil split
    elif store_type == "game":
        remove_active_maintenance(entity, "brazil")     # a game moved to Brazil by hand in the console
    col.update_one({"_id": ev["_id"]}, {"$set": {"notify_pending": True}})
    logger.info(f"[MMMaintenance] {store_type} maintenance ended: {entity} (user notifications after the delay)")
    return "ended"


def process_due_notifications(now=None) -> int:
    """Flag subscriptions for maintenance ends whose delay has passed and that really ended."""
    if not enabled():
        return 0
    mm = _mm()
    from services.maintenance_notify_service import on_maintenance_ended
    now = now or datetime.now(timezone.utc)
    done = 0
    for ev in mm.events_col.find({"notify_pending": True}):
        if _utc(mm.notify_due_at(ev)) > now:
            continue
        target = _target(ev)
        if not mm.events_col.find_one_and_update({"_id": ev["_id"], "notify_pending": True},
                                                 {"$set": {"notify_pending": False}}):
            continue
        if not target or not mm.maintenance_really_ended(ev):
            mm.events_col.update_one({"_id": ev["_id"]}, {"$set": {"notify_result": "skipped"}})
            logger.info(f"[MMMaintenance] no end message for {ev.get('game') or ev.get('server')}: "
                        "removed or back in maintenance within the delay")
            continue
        entity, store_type, _ = target
        n = on_maintenance_ended(entity, store_type)
        if store_type == "game":
            n += on_maintenance_ended(entity, "brazil")
        elif store_type == "brazil":
            n += on_maintenance_ended(entity, "game")
        mm.events_col.update_one({"_id": ev["_id"]}, {"$set": {"notify_result": "flagged:%d" % n}})
        done += 1
    return done


def _notify_loop():
    while True:
        try:
            process_due_notifications()
        except Exception as e:
            logger.error(f"[MMMaintenance] notification check failed: {e}")
        time.sleep(NOTIFY_CHECK_SECONDS)


def start():
    if not enabled():
        logger.info("[MMMaintenance] off (MM_MAINTENANCE=0)")
        return
    try:
        _since()
    except Exception as e:
        logger.error(f"[MMMaintenance] could not read the switch-on time: {e}")
    threading.Thread(target=_notify_loop, daemon=True, name="mm-maintenance-notify").start()
    logger.info("[MMMaintenance] maintenances follow the Mattermost admin events")
