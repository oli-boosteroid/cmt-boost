"""
One switch for "Reddit is refusing us".

When Reddit answers 401/403 to everything (a network-security block of the app, a flagged account),
repeating the requests only makes it worse and floods the error channel. This module is the single
place that knows the state:

  * mark_down()  - called by the snapshot worker on a refusal. All Reddit loops in the bot then stand
                   down (they check is_down()), and exactly one probe is allowed per RECHECK_SECONDS.
  * mark_up()    - called on the first successful pass. Everything resumes by itself.

The state is mirrored to Mongo (console_config key "reddit_status") so the console can show its Reddit
sections as "on maintenance" and bring them back automatically, and so a bot restart does not start
hammering Reddit again in the middle of a block.
"""
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

RECHECK_SECONDS = 3600          # while down: one probe per hour

_lock = threading.Lock()
_state = {"down": False, "since": None, "next_try": 0.0, "loaded": False, "reason": ""}


def _col():
    from services.mongo_db_service import sync_db
    return sync_db["console_config"]


def _load():
    """Read the persisted state once per process (so a restart keeps honouring an active block)."""
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        doc = (_col().find_one({"key": "reddit_status"}) or {}).get("value") or {}
        if doc.get("down"):
            _state.update(down=True, since=doc.get("since"), reason=doc.get("reason") or "",
                          next_try=float(doc.get("next_try_ts") or 0))
    except Exception as e:
        logger.debug("[RedditGate] state not loaded: %s", type(e).__name__)


def _save():
    try:
        now = datetime.utcnow()
        value = {
            "down": _state["down"], "since": _state["since"], "reason": _state["reason"],
            "checked_at": now.replace(microsecond=0).isoformat() + "Z",
            "next_try_ts": _state["next_try"],
            "next_check_at": (datetime.utcfromtimestamp(_state["next_try"]).replace(microsecond=0).isoformat() + "Z")
            if _state["down"] and _state["next_try"] else "",
        }
        _col().update_one({"key": "reddit_status"}, {"$set": {"key": "reddit_status", "value": value}}, upsert=True)
    except Exception as e:
        logger.debug("[RedditGate] state not saved: %s", type(e).__name__)


def is_down() -> bool:
    """True while Reddit is refusing us. Loops that are not the probe just wait."""
    with _lock:
        _load()
        return bool(_state["down"])


def may_try() -> bool:
    """For the probing worker: True when it may touch Reddit now (always when up; once an hour when down)."""
    with _lock:
        _load()
        return (not _state["down"]) or time.time() >= _state["next_try"]


def mark_down(reason: str = "") -> None:
    with _lock:
        _load()
        first = not _state["down"]
        _state["down"] = True
        _state["reason"] = (reason or "")[:120]
        _state["next_try"] = time.time() + RECHECK_SECONDS
        if first:
            _state["since"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        _save()
    if first:
        logger.error("[RedditGate] Reddit is refusing requests (%s). All Reddit loops are paused; "
                     "one check per hour until it answers again.", reason)
    else:
        logger.info("[RedditGate] Reddit still refuses (%s); next check in %d min", reason, RECHECK_SECONDS // 60)


def mark_up() -> None:
    with _lock:
        _load()
        was_down = _state["down"]
        if not was_down:
            return
        _state.update(down=False, since=None, next_try=0.0, reason="")
        _save()
    logger.warning("[RedditGate] Reddit answers again. Reddit loops resumed.")
