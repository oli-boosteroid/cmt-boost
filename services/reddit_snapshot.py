"""
Reddit snapshot - the bot side of making the console's Reddit tabs real.

Snapshots the LAST YEAR of Reddit modmail threads and posts into Mongo collections the
console reads:
  - `reddit_modmail_snapshot`  (one doc per modmail conversation)
  - `reddit_posts_snapshot`    (one doc per subreddit post)
  - `_snapshot_cursors`        (resumable backfill cursors)

Stored in a SEPARATE Mongo (env `SNAPSHOT_MONGODB_URI`) so the volume never bloats the bot's
main Atlas; falls back to `MONGODB_URI` if unset. The bot only WRITES here (this task); the
console only READS. No other bot logic touches this database.

Deliberately GENTLE so it never trips Reddit rate limits or starves the live modmail tracker
(shared creds): each cycle refreshes the newest items and backfills ONE older page, capping the
expensive per-conversation message fetches and pausing between them. The one-year backfill runs
once (cursor -> DONE); after that only the recent-refresh runs, so the DB keeps growing on its own
as new modmail/posts arrive.
"""
import os
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from services.redact import redact_emails   # users' emails are never shown in the console

logger = logging.getLogger(__name__)

from services import db as _shared   # one shared client per cluster for the whole process
_snap_uri = _shared.snapshot_uri()
_snap_db = _shared.snapshot_db()
modmail_col = _snap_db["reddit_modmail_snapshot"]
posts_col = _snap_db["reddit_posts_snapshot"]
cursor_col = _snap_db["_snapshot_cursors"]

SNAPSHOT_SECONDS = 300       # cycle every 5 minutes
BACKFILL_DAYS = 365          # collect the last year, then stop backfilling
RECENT_LIMIT = 50            # refresh this many newest each cycle (cheap metadata)
BACKFILL_LIMIT = 75          # advance the year backfill by this many per cycle
MSG_FETCH_PER_CYCLE = 20     # cap on the EXPENSIVE per-conversation message fetch each cycle
MSG_FETCH_SLEEP = 1.0        # pause between message fetches so we never burst the Reddit API
MSG_BODY_MAX = 10000         # cap stored message body length (Reddit's comment max; effectively full)
POST_BODY_MAX = 40000        # cap stored post self-text (Reddit's self-post max; effectively full)
# Lightweight fast pass (option 2): surface INCOMING user messages in ~1 min instead of up to 5,
# WITHOUT running the whole heavy cycle. One cheap listing of the newest convos + targeted message
# fetches only for the few that actually changed. Kept tiny so it never starves the live modmail
# tracker (all three share one praw instance / account rate-limit).
FAST_SECONDS = 60            # lightweight incoming-detection cadence
FAST_LIMIT = 15              # newest convos to scan per fast pass (one listing call, cheap metadata)
FAST_MSG_CAP = 8             # max targeted message re-fetches per fast pass
_DONE = "__DONE__"

# The main cycle, the fast pass and mirror-on-send all touch the SAME cached praw instance
# (reddit_notifications.mod_notes._get_subreddit); praw's session + rate-limiter are not safe for
# concurrent use, so serialize every Reddit-touching section on this lock.
_reddit_lock = threading.Lock()

_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _post_images(post):
    """Best-effort list of image URLs on a post: direct image posts + Reddit galleries."""
    urls = []
    url = getattr(post, "url", "") or ""
    base = url.lower().split("?", 1)[0]
    if url and (base.endswith(_IMG_EXT) or "i.redd.it/" in url.lower()):
        urls.append(url)
    if getattr(post, "is_gallery", False):
        meta = getattr(post, "media_metadata", None)
        if isinstance(meta, dict):
            for m in meta.values():
                s = (m or {}).get("s") or {}
                u = s.get("u") or s.get("gif")
                if u:
                    urls.append(u.replace("&amp;", "&"))
    out, seen = [], set()
    for u in urls:
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out[:10]


def _cutoff():
    return datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)


def _parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _name_of(x):
    if x is None:
        return None
    if isinstance(x, dict):
        return x.get("name")
    return getattr(x, "name", None)


def _get_cursor(key):
    d = cursor_col.find_one({"key": key})
    return (d or {}).get("value")


def _set_cursor(key, value):
    cursor_col.update_one({"key": key}, {"$set": {"key": key, "value": value}}, upsert=True)


def _messages_of(convo):
    """Read a conversation's messages (triggers a per-conversation Reddit fetch)."""
    out = []
    for m in convo.messages:
        a = getattr(m, "author", None)
        author = None
        if a is not None:
            author = getattr(a, "name", None) or (a.get("name") if isinstance(a, dict) else None)
        body = getattr(m, "body_markdown", None) or getattr(m, "body", "") or ""
        out.append({
            "id": getattr(m, "id", ""),
            "author": author or "Anonymous",
            "body": redact_emails(body[:MSG_BODY_MAX]),
            "is_internal": bool(getattr(m, "is_internal", False)),
            "date": getattr(m, "date", None),
        })
    return out


class _Budget:
    def __init__(self, cap):
        self.left = cap


def _snapshot_convos(sub, state, limit, after, budget, cutoff=None, tag_state=True, archived_ids=None):
    """List conversations (metadata is cheap) and fetch messages for new/changed ones, capped by
    budget. Returns (ids, done). done=True when a conversation older than cutoff is reached.

    tag_state=False (the fast pass) lists the newest "all" inbox but does NOT write the `state`
    field, so it never clobbers the in-progress/archived tag the main cycle applied (the console
    derives `_archived` from `state`; overwriting it would flip a Done convo back to active)."""
    ids, done = [], False
    kwargs = {"state": state, "limit": limit, "sort": "recent"}
    if after:
        # praw's Modmail.conversations() has no `after` kwarg (it leaks into ListingGenerator and
        # crashes); page via query params instead - same pattern as _snapshot_posts_page / sub.new().
        kwargs["params"] = {"after": after}
    for convo in sub.modmail.conversations(**kwargs):
        try:
            last = getattr(convo, "last_updated", None)
            if cutoff:
                dt = _parse_dt(last)
                if dt and dt < cutoff:
                    done = True
                    break  # sorted recent->old, everything past here is older than the window
            cid = convo.id
            # Egress-friendly existence check: DON'T pull the heavy `messages` back from Mongo -
            # only the tiny fields needed to decide whether to (re)fetch messages.
            existing = modmail_col.find_one({"convo_id": cid}, {"last_updated": 1, "num_messages": 1})
            has_msgs = bool(existing) and int(existing.get("num_messages") or 0) > 0
            need_msgs = (not existing) or (not has_msgs) or (existing.get("last_updated") != last)
            set_fields = {
                "convo_id": cid,
                "subject": getattr(convo, "subject", "") or "",
                "participant": _name_of(getattr(convo, "participant", None)),
                "synced_at": datetime.utcnow().isoformat(),
            }
            if tag_state:
                # ModmailConversation has no reliable state/is_archived attribute - the listing we
                # fetched from is the only signal. The "all" listing also holds archived convos, so it
                # tags those "archived" (from the archived pass that ran first) instead of flipping them
                # back to active between passes.
                set_fields["state"] = "archived" if (archived_ids is not None and cid in archived_ids) else state
            if need_msgs and budget.left > 0:
                msgs = _messages_of(convo)
                budget.left -= 1
                time.sleep(MSG_FETCH_SLEEP)
                set_fields["messages"] = msgs
                set_fields["num_messages"] = len(msgs)
                # last_updated is only advanced together with the messages it describes: if the budget
                # skips this convo, the next pass still sees it as changed and fetches it then.
                set_fields["last_updated"] = last
                # Preview + activity + status hints all come from the PUBLIC (non-internal) messages so an
                # internal "[Console] ..." / "[System] ..." note never becomes the list preview, the last
                # author, or the activity time. This matches the real-time writer
                # (redit_notifications._snap_write_convo) so last_body stays consistent no matter which
                # writer touched the convo last (last_updated on Reddit also moves on non-message mod
                # actions, so the console uses these message-derived fields instead).
                public = [m for m in msgs if not m.get("is_internal")]
                tail = public[-1] if public else None
                set_fields["last_body"] = (tail["body"][:200] if tail else "")
                set_fields["last_activity"] = (tail["date"] if tail else last)
                set_fields["mod_replied"] = any(m["author"] != set_fields["participant"] for m in public)
                set_fields["last_public_author"] = tail["author"] if tail else None
            elif not existing:
                # new convo we won't fetch this cycle: seed the count from the listing metadata
                set_fields["num_messages"] = int(getattr(convo, "num_messages", 0) or 0)
            elif not need_msgs:
                set_fields["last_updated"] = last
            # when messages aren't (re)fetched we leave the stored `messages` untouched (no read, no overwrite)
            modmail_col.update_one({"convo_id": cid}, {"$set": set_fields}, upsert=True)
            ids.append(cid)
        except Exception as e:
            logger.error("[RedditSnapshot] convo %s failed: %s", getattr(convo, "id", "?"), e)
    return ids, done


def snapshot_modmail():
    from reddit_notifications.mod_notes import _get_subreddit
    sub = _get_subreddit()
    budget = _Budget(MSG_FETCH_PER_CYCLE)
    # 1) refresh the newest. Fetch per-state so each convo's status is correct - there is no reliable
    #    per-conversation state attribute, so the listing is the signal. The "all" pass fetches
    #    messages (budgeted); the in-progress/archived passes only re-tag the state of convos they
    #    contain (same last_updated -> no extra message fetch), so they cost almost nothing.
    # Archived first, so the "all" pass (which also lists archived convos) writes each state once and
    # a convo never reads as active between passes, even if a later listing fails.
    archived, _ = _snapshot_convos(sub, "archived", RECENT_LIMIT, None, budget)   # -> resolved
    _snapshot_convos(sub, "all", RECENT_LIMIT, None, budget, archived_ids=set(archived))   # baseline -> waiting_user
    _snapshot_convos(sub, "inprogress", RECENT_LIMIT, None, budget)   # -> in_progress
    # 2) backfill one older page of the last year, unless already complete
    after = _get_cursor("modmail_archived_after")
    if after == _DONE:
        return
    page, done = _snapshot_convos(sub, "archived", BACKFILL_LIMIT, after, budget, cutoff=_cutoff())
    _set_cursor("modmail_archived_after", _DONE if (done or not page) else page[-1])
    logger.info("[RedditSnapshot] modmail: backfilled %s, done=%s, msg budget left %s",
                len(page), done or not page, budget.left)


def mark_archived(convo_id):
    """Write-through for an archive done by the bot (console Done/Archive, auto-archive): the console
    shows Done from `state`, so set it now instead of waiting for the next cycle's archived listing
    (which only covers the newest RECENT_LIMIT archived convos)."""
    if not convo_id:
        return False
    try:
        modmail_col.update_one({"convo_id": convo_id},
                               {"$set": {"state": "archived", "synced_at": datetime.utcnow().isoformat()}})
        return True
    except Exception as e:
        logger.error("[RedditSnapshot] mark_archived %s failed: %s", convo_id, e)
        return False


def mirror_sent_reply(convo_id, author, body, is_internal=False):
    """Write-through (option 1): the instant the bot POSTS a modmail reply to reddit.com, mirror it
    straight into the snapshot so EVERY console session (not just the sender's optimistic bubble) and
    a reload see it at once, instead of waiting up to 5 min for the next cycle. Costs ZERO extra
    Reddit calls - the text is already in hand. The next cycle re-fetches this convo (posting bumped
    its last_updated on Reddit) and REPLACES `messages` with Reddit's authoritative copy, which
    contains this same reply -> no duplicate, no gap. Called by the console_actions worker.

    Note: we intentionally do NOT touch num_messages (avoids a false "unread" bump for our own
    message); the console builds the transcript from the `messages` array we push here, and the LIST
    row reads last_activity / last_public_author / mod_replied / last_body, which we set for a public
    reply so the row shows "Waiting on user" and sorts to the top immediately."""
    if not convo_id or not body:
        return False
    now = datetime.now(timezone.utc).isoformat()
    msg = {
        "id": "sent-" + str(int(time.time() * 1000)),
        "author": author or "CMT",
        "body": str(body)[:MSG_BODY_MAX],
        "is_internal": bool(is_internal),
        "date": now,
    }
    set_fields = {"last_updated": now, "synced_at": datetime.utcnow().isoformat()}
    if not is_internal:   # a mods-only internal note must not flip the public status/preview
        set_fields["mod_replied"] = True
        set_fields["last_public_author"] = msg["author"]
        set_fields["last_activity"] = now
        set_fields["last_body"] = msg["body"][:200]
    try:
        modmail_col.update_one(
            {"convo_id": convo_id},
            {"$push": {"messages": msg}, "$set": set_fields, "$setOnInsert": {"convo_id": convo_id}},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.error("[RedditSnapshot] mirror_sent_reply %s failed: %s", convo_id, e)
        return False


def fast_refresh_incoming():
    """Lightweight fast pass (option 2): detect INCOMING user messages quickly. One cheap listing of
    the newest FAST_LIMIT convos (metadata only); for any whose last_updated advanced past what we
    stored, targeted-fetch just those messages (capped at FAST_MSG_CAP, paced by MSG_FETCH_SLEEP).
    Does NOT re-tag `state` (tag_state=False) and never backfills, so it stays tiny and never resets
    an in-progress/archived convo. Serialized with the main cycle via _reddit_lock by the caller."""
    from reddit_notifications.mod_notes import _get_subreddit
    sub = _get_subreddit()
    budget = _Budget(FAST_MSG_CAP)
    ids, _ = _snapshot_convos(sub, "all", FAST_LIMIT, None, budget, tag_state=False)
    return len(ids)


def _snapshot_posts_page(sub, after, limit, cutoff_ts=None):
    """Snapshot post METADATA only. Comments are written in real time by the Reddit comment stream
    (reddit_notifications.track_comments), so `$setOnInsert` seeds empty comments/has_reply and we
    never overwrite what the stream already stored."""
    ids, done = [], False
    params = {"after": after} if after else None
    for post in sub.new(limit=limit, params=params):
        try:
            if cutoff_ts and getattr(post, "created_utc", 0) < cutoff_ts:
                done = True
                break
            pid = post.id
            posts_col.update_one({"post_id": pid}, {
                "$set": {
                    "post_id": pid,
                    "title": getattr(post, "title", "") or "",
                    "author": str(post.author) if getattr(post, "author", None) else "[deleted]",
                    "subreddit": "r/" + str(post.subreddit),
                    "url": "https://www.reddit.com" + getattr(post, "permalink", ""),
                    "body": redact_emails((getattr(post, "selftext", "") or "")[:POST_BODY_MAX]),
                    "images": _post_images(post),
                    "created": datetime.utcfromtimestamp(post.created_utc).isoformat() if getattr(post, "created_utc", None) else None,
                    "num_comments": getattr(post, "num_comments", 0),
                    "flair": (getattr(post, "link_flair_text", "") or ""),   # user post flair (Suggestion/Help/...) 
                    "synced_at": datetime.utcnow().isoformat(),
                },
                "$setOnInsert": {"comments": [], "has_reply": False},
            }, upsert=True)
            ids.append("t3_" + pid)
        except Exception as e:
            logger.error("[RedditSnapshot] post %s failed: %s", getattr(post, "id", "?"), e)
    return ids, done


def snapshot_posts():
    from reddit_notifications.mod_notes import _get_subreddit
    sub = _get_subreddit()
    _snapshot_posts_page(sub, None, RECENT_LIMIT)   # refresh newest
    after = _get_cursor("posts_after")
    if after == _DONE:
        return
    cutoff_ts = _cutoff().timestamp()
    page, done = _snapshot_posts_page(sub, after, BACKFILL_LIMIT, cutoff_ts=cutoff_ts)
    _set_cursor("posts_after", _DONE if (done or not page) else page[-1])
    logger.info("[RedditSnapshot] posts: backfilled %s, done=%s", len(page), done or not page)


# ─── Reddit account health: "flagged as a bot" detection ─────────────────────
# When Reddit flags the bot account (the "verify you're not a robot" check), every API call starts
# returning 401/403 and nothing recovers until someone logs in and passes the check by hand. These
# loops call Reddit every minute, so they double as the canary: a sustained 401/403 streak with no
# successful call raises ONE alert in the TG meeting group (tagging the configured person), repeats it
# while still broken, and posts a short note once calls succeed again. Transient errors (network,
# 429 rate limits, 5xx) neither count nor reset the streak.
# Settings live in the CONSOLE (Admin -> "Reddit - flagged account alert"), stored in console_config
# key `reddit_flag_alert`; read here with a 60s cache so edits apply without a restart.
_FLAG_DEFAULTS = {"enabled": True, "alert_minutes": 10, "min_errors": 5, "realert_hours": 3.0, "tag": "o.teleshko"}
_flag_cfg = {"at": 0.0, "val": None}


def _flag_config() -> dict:
    now = time.time()
    if _flag_cfg["val"] is not None and now - _flag_cfg["at"] < 60:
        return _flag_cfg["val"]
    val = dict(_FLAG_DEFAULTS)
    try:
        from services.mongo_db_service import sync_db
        doc = (sync_db["console_config"].find_one({"key": "reddit_flag_alert"}) or {}).get("value") or {}
        val["enabled"] = bool(doc.get("enabled", True))
        for key, cast in (("alert_minutes", float), ("min_errors", int), ("realert_hours", float)):
            try:
                if doc.get(key) not in (None, ""):
                    val[key] = cast(doc[key])
            except (TypeError, ValueError):
                pass
        if "tag" in doc:
            val["tag"] = str(doc.get("tag") or "").strip()
    except Exception as e:
        logger.warning("[RedditSnapshot] flag-alert config unavailable, using defaults: %s", e)
    _flag_cfg.update(at=now, val=val)
    return val


_health_lock = threading.Lock()
_auth_fail_since = None     # epoch when the current 401/403 streak started
_auth_fail_count = 0
_flag_alerted_at = None     # epoch of the last flagged alert in this streak (None = not alerted)


def _is_auth_block(exc) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    msg = str(exc)
    return ("403 HTTP" in msg) or ("401 HTTP" in msg) or ("invalid_grant" in msg)


def _flag_notify(text_html: str) -> None:
    try:
        from telegram_bot.sender import send_to_meet_group_html
        send_to_meet_group_html(text_html)
    except Exception as e:
        logger.error("[RedditSnapshot] flag alert send failed: %s", e)


def _reddit_health_ok() -> None:
    global _auth_fail_since, _auth_fail_count, _flag_alerted_at
    with _health_lock:
        was_alerted = _flag_alerted_at is not None
        _auth_fail_since, _auth_fail_count, _flag_alerted_at = None, 0, None
    if was_alerted:
        logger.info("[RedditSnapshot] Reddit API recovered after a flagged-account alert")
        _flag_notify("Reddit API is responding again - the bot account is working.")


def _reddit_health_fail(exc) -> None:
    global _auth_fail_since, _auth_fail_count, _flag_alerted_at
    if not _is_auth_block(exc):
        return
    cfg = _flag_config()
    now = time.time()
    with _health_lock:
        if _auth_fail_since is None:
            _auth_fail_since = now
        _auth_fail_count += 1
        streak_min = (now - _auth_fail_since) / 60
        due = (cfg["enabled"] and _auth_fail_count >= cfg["min_errors"] and streak_min >= cfg["alert_minutes"] and
               (_flag_alerted_at is None or now - _flag_alerted_at >= cfg["realert_hours"] * 3600))
        if due:
            _flag_alerted_at = now
        count = _auth_fail_count
    if not due:
        return
    # While Reddit is blocking the whole API app (reddit_gate down, Nov 2026) every call is a 403, so this
    # "account flagged, pass the captcha" alert fires every few hours and is misleading - the account is
    # fine, the app is blocked. Muted for the duration of the block (the console shows the block anyway).
    try:
        from services import reddit_gate
        if reddit_gate.is_down():
            logger.info("[RedditSnapshot] flagged-account alert muted: Reddit API block in effect (reddit_gate)")
            return
    except Exception as _e:
        logger.debug("ignored: %s", _e)
    import html as _html
    try:
        from services.reminders_service import mention_html_for, on_shift_mentions_html
        tag = (mention_html_for(cfg["tag"]) if cfg["tag"] else "") or on_shift_mentions_html()
    except Exception:
        tag = ""
    acct = os.getenv("REDDIT_USERNAME", "") or "the bot account"
    dur = f"{int(streak_min)} min" if streak_min < 120 else f"{streak_min / 60:.1f} h"
    text = (f"Reddit API has been rejecting {_html.escape(acct)} (HTTP 401/403) for {dur} "
            f"({count} failed calls) and it is not recovering on its own. Reddit has most likely "
            f"flagged the account as a bot: log in to the account on reddit.com and pass the "
            f"\"I'm not a robot\" check.")
    logger.warning("[RedditSnapshot] Reddit account likely flagged: %s failed calls over %s", count, dur)
    _flag_notify((tag + "\n" if tag else "") + text)


# While Reddit refuses us (401/403 - a network-security block or a flagged account) every further request
# only prolongs the refusal and logs another error. services/reddit_gate holds the
# shared switch: after a refusal every Reddit loop in the bot stands down and this worker makes one
# probe per hour. The first successful pass switches everything back on.
def _run_reddit(label, fn):
    """Run one Reddit-touching pass under the shared lock and feed the result to the health tracker."""
    from services import reddit_gate
    if not reddit_gate.may_try():
        return                                   # standing down, no request is made
    try:
        with _reddit_lock:
            fn()
    except Exception as e:
        if _is_auth_block(e):
            reddit_gate.mark_down(str(e)[:80])
        else:
            logger.error("[RedditSnapshot] %s loop error: %s", label, e)
        _reddit_health_fail(e)
        return
    reddit_gate.mark_up()
    _reddit_health_ok()


def _loop():
    logger.info("[RedditSnapshot] worker started (every %ss, db=%s, window=%sd)",
                SNAPSHOT_SECONDS, _snap_db.name, BACKFILL_DAYS)
    while True:
        _run_reddit("modmail", snapshot_modmail)
        _run_reddit("posts", snapshot_posts)
        time.sleep(SNAPSHOT_SECONDS)


def _fast_loop():
    logger.info("[RedditSnapshot] fast incoming pass started (every %ss, newest %s)",
                FAST_SECONDS, FAST_LIMIT)
    # Stagger off the main cycle's start so the two rarely queue on the lock at the same instant.
    time.sleep(FAST_SECONDS)
    while True:
        _run_reddit("fast", fast_refresh_incoming)
        time.sleep(FAST_SECONDS)


def start_reddit_snapshot():
    t = threading.Thread(target=_loop, daemon=True, name="reddit-snapshot")
    t.start()
    tf = threading.Thread(target=_fast_loop, daemon=True, name="reddit-snapshot-fast")
    tf.start()
    return t
