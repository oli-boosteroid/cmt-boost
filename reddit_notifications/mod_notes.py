"""Reddit moderator-note + modmail-state helpers (synchronous, praw-based).

Used from Slack button handlers (Bolt thread) and scheduled jobs (schedule thread) -
i.e. plain sync contexts - so this deliberately uses praw (sync), not the asyncpraw
client that runs the live tracker in its own event loop. Same moderator credentials.

Reddit's Mod Notes API has no edit - "updating" a note is delete + recreate.

Rate limiting: every Reddit write is followed by REDDIT_WRITE_DELAY seconds, and all
bulk maintenance runs under a single lock so writes never overlap. praw's own
ratelimit_seconds=900 provides an extra 429 safety net.
"""

import os
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

import praw
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

SUBREDDIT_NAME = os.getenv('SUBREDDIT_NAME')
JIRA_BASE_URL = os.getenv('JIRA_BASE_URL', '').rstrip('/')
REDDIT_WRITE_DELAY = float(os.getenv('REDDIT_WRITE_DELAY', '3'))            # seconds between Reddit writes
INITIAL_DELAY_SECONDS = int(os.getenv('REDDIT_MAINTENANCE_INITIAL_DELAY', '600'))  # 10 min post-deploy delay
STALE_IN_PROGRESS_DAYS = int(os.getenv('REDDIT_STALE_INPROGRESS_DAYS', '30'))
INACTIVE_ARCHIVE_DAYS = int(os.getenv('REDDIT_INACTIVE_ARCHIVE_DAYS', '5'))  # auto-archive convos idle this long

# One lock for all bulk maintenance so reconcile and archive never write concurrently.
_maintenance_lock = threading.Lock()

_reddit = None
_subreddit = None


def _get_subreddit():
    global _reddit, _subreddit
    if _subreddit is None:
        _reddit = praw.Reddit(
            client_id=os.getenv('REDDIT_CLIENT_ID'),
            client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
            user_agent=os.getenv('REDDIT_USER_AGENT'),
            username=os.getenv('REDDIT_USERNAME'),
            password=os.getenv('REDDIT_PASSWORD'),
            ratelimit_seconds=900,
        )
        _subreddit = _reddit.subreddit(SUBREDDIT_NAME)
    return _subreddit


def _throttle():
    time.sleep(REDDIT_WRITE_DELAY)


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ── Low-level mod-note operations ─────────────────────────────────────────────



def create_note(redditor: str, text: str) -> str | bool | None:
    """DISABLED. Automated user-level Reddit mod notes (email / Jira / maintenance annotations) are no
    longer written to Reddit; that information now lives only in the console. Kept as a no-op returning
    None ('not created') so existing callers still behave. Notes a console user types by hand go through
    post_internal_reply, not this."""
    return None


def delete_note(redditor: str, note_id: str) -> bool:
    try:
        _get_subreddit().mod.notes.delete(note_id=note_id, redditor=redditor)
        _throttle()
        return True
    except Exception as e:
        logger.error(f'[Reddit] Failed to delete mod note {note_id} for u/{redditor}: {e}')
        return False


# ── Modmail conversation state ────────────────────────────────────────────────

def _devvit_highlight(convo_id: str, want: bool):
    """While the API is blocked the highlight goes through the Devvit app. None = use the API."""
    try:
        from services.console_actions_worker import _via_devvit
        if not _via_devvit():
            return None
        import time as _t
        from reddit_notifications.devvit_inbox import send_reddit_command
        ok, err = send_reddit_command("hl:%s:%d" % (convo_id, int(_t.time())), "modmail_highlight",
                                      conversationId=convo_id, highlight=want)
        if not ok:
            logger.error(f'[Reddit] Devvit highlight relay failed for convo {convo_id}: {err}')
        return ok
    except Exception as e:
        logger.error(f'[Reddit] Devvit highlight failed for convo {convo_id}: {e}')
        return False


def highlight_convo(convo_id: str) -> bool:
    via = _devvit_highlight(convo_id, True)
    if via is not None:
        return via
    try:
        _get_subreddit().modmail(convo_id).highlight()
        _throttle()
        return True
    except Exception as e:
        logger.error(f'[Reddit] Failed to highlight modmail convo {convo_id}: {e}')
        return False


def unhighlight_convo(convo_id: str) -> bool:
    via = _devvit_highlight(convo_id, False)
    if via is not None:
        return via
    try:
        _get_subreddit().modmail(convo_id).unhighlight()
        _throttle()
        return True
    except Exception as e:
        logger.error(f'[Reddit] Failed to unhighlight modmail convo {convo_id}: {e}')
        return False


def archive_convo(convo_id: str) -> bool:
    try:
        _get_subreddit().modmail(convo_id).archive()
        _throttle()
        try:   # the console reads Done from the snapshot: reflect the archive there right away
            from services.reddit_snapshot import mark_archived
            mark_archived(convo_id)
        except Exception as e:
            logger.warning(f'[Reddit] snapshot archive mark failed for {convo_id}: {e}')
        return True
    except Exception as e:
        logger.error(f'[Reddit] Failed to archive modmail convo {convo_id}: {e}')
        return False


def post_internal_reply(convo_id: str, text: str) -> bool:
    """Posts a 'Mods only' (internal) reply into a modmail conversation. `internal` is keyword-only."""
    try:
        _get_subreddit().modmail(convo_id).reply(body=text, internal=True)
        _throttle()
        return True
    except Exception as e:
        logger.error(f'[Reddit] Failed to post mods-only reply to convo {convo_id}: {e}')
        return False


def post_public_reply(convo_id: str, text: str) -> bool:
    """Posts a PUBLIC reply (visible to the user) into a modmail conversation, AS THE COMMUNITY
    (author_hidden=True hides the individual moderator behind the subreddit name)."""
    try:
        _get_subreddit().modmail(convo_id).reply(body=text, internal=False, author_hidden=True)
        _throttle()
        return True
    except Exception as e:
        logger.error(f'[Reddit] Failed to post public reply to convo {convo_id}: {e}')
        return False


def reply_to_post(post_id: str, text: str) -> bool:
    """Posts a public comment reply on a subreddit submission (from the community account).
    `post_id` is the bare Reddit submission id (e.g. 'abc123'). Returns True on success."""
    try:
        _get_subreddit()  # ensure the shared praw client (`_reddit`) is initialised
        comment = _reddit.submission(id=post_id).reply(body=text)
        # Post as the community: distinguish the comment as a moderator (best-effort - a distinguish
        # failure must not fail the reply, which already succeeded).
        try:
            comment.mod.distinguish(how='yes')
        except Exception as de:
            logger.warning(f'[Reddit] Could not distinguish post reply on {post_id}: {de}')
        _throttle()
        return True
    except Exception as e:
        logger.error(f'[Reddit] Failed to reply to post {post_id}: {e}')
        return False


def _name_of(participant) -> str | None:
    if participant is None:
        return None
    if isinstance(participant, dict):
        return participant.get('name')
    return getattr(participant, 'name', None)


def get_convo_participant(convo_id: str) -> str | None:
    """Resolves the Reddit username of a modmail conversation's participant, or None. Reads the stored
    snapshot first (works while Reddit blocks the API); asks Reddit only when it is not there."""
    try:
        from services.reddit_snapshot import modmail_col
        doc = modmail_col.find_one({"convo_id": convo_id}, {"participant": 1}) or {}
        if doc.get("participant"):
            return doc["participant"]
    except Exception as e:
        logger.debug(f'[Reddit] snapshot participant lookup failed for {convo_id}: {e}')
    try:
        from services import reddit_gate
        if reddit_gate.is_down():
            return None
    except Exception as _e:
        logger.debug("ignored: %s", _e)
    try:
        convo = _get_subreddit().modmail(convo_id)
        for attr in ('participant', 'user'):
            name = _name_of(getattr(convo, attr, None))
            if name:
                return name
        return None
    except Exception as e:
        logger.error(f'[Reddit] Failed to resolve participant for convo {convo_id}: {e}')
        return None


# ── High-level flows (Slack button + Communication-Link auto-link) ────────────

def link_jira_ticket(redditor: str, issue_key: str, convo_id: str = None) -> bool:
    """Links a Jira ticket to a Reddit user: the same record the console Link Jira Ticket writes
    (reddit_mod_notes.jira_convos). Nothing is written to Reddit (no mod note, no highlight). Idempotent.

    Safe to call from the Slack Bolt handler thread or scheduled threads (sync). Rate-limited.
    """
    from services.mongo_db_service import (
        get_reddit_mod_note_sync,
        set_reddit_mod_note_jira_sync,
        set_reddit_mod_note_jira_convo_sync,
        set_reddit_mod_note_convo_sync,
    )
    existing = get_reddit_mod_note_sync(redditor)
    already = bool(existing) and (issue_key in (existing.get('jira_notes') or {}) or issue_key in (existing.get('jira_convos') or {}))
    if already:
        if convo_id and (existing.get('jira_convos') or {}).get(issue_key) != convo_id:
            set_reddit_mod_note_jira_convo_sync(redditor, issue_key, convo_id)
        if convo_id and not existing.get('convo_id'):
            set_reddit_mod_note_convo_sync(redditor, convo_id)
        logger.info(f'[Reddit] Jira {issue_key} already linked to u/{redditor} - console tracking updated')
        return True

    # Record the link in Mongo (note_id=None: no Reddit note). Keeps jira_convos/jira_done for the console.
    set_reddit_mod_note_jira_sync(redditor, issue_key, None, convo_id=convo_id)
    if convo_id:
        set_reddit_mod_note_convo_sync(redditor, convo_id)
    logger.info(f'[Reddit] Linked Jira {issue_key} to u/{redditor} (console only, nothing on Reddit)')
    return True


def auto_link_jira_from_convo(issue_key: str, convo_id: str) -> bool:
    """Communication-Link auto-link: resolve the convo's participant, then link the Jira ticket.

    Mirrors the Discord auto-link - called when a Jira ticket's Communication Link is a
    mod.reddit.com modmail URL.
    """
    redditor = get_convo_participant(convo_id)
    if not redditor:
        logger.warning(f'[Reddit] Auto-link: could not resolve participant for convo {convo_id} ({issue_key})')
        return False
    return link_jira_ticket(redditor, issue_key, convo_id=convo_id)


# ── Bulk maintenance (scheduled + delayed initial run) ────────────────────────



def _reconcile_jira_notes() -> None:
    from services.mongo_db_service import (
        get_all_reddit_mod_notes_sync,
        mark_reddit_mod_note_jira_done_sync,
        remove_reddit_mod_note_highlighted_convo_sync,
        clear_reddit_mod_note_legacy_highlight_sync,
    )
    from services.jira_service import is_issue_done

    for doc in get_all_reddit_mod_notes_sync():
        user = doc.get('reddit_user')
        jira_convos = doc.get('jira_convos') or {}
        jira_done = dict(doc.get('jira_done') or {})
        # A link now only populates jira_convos (no Reddit note); include legacy jira_notes keys too.
        keys_all = set(jira_convos.keys()) | set((doc.get('jira_notes') or {}).keys())
        if not user or not keys_all:
            continue

        for issue_key in list(keys_all):
            if jira_done.get(issue_key):
                continue
            try:
                if is_issue_done(issue_key):
                    # Mark Done in Mongo only (no Reddit note) so highlighted convos can auto-unhighlight.
                    mark_reddit_mod_note_jira_done_sync(user, issue_key, None)
                    jira_done[issue_key] = True
                    logger.info(f'[Reddit] Marked Jira {issue_key} Done (console) for u/{user}')
            except Exception as e:
                logger.error(f'[Reddit] Error reconciling Jira {issue_key} for u/{user}: {e}')

        # Per-conversation unhighlight: clear a convo's highlight once every Jira ticket
        # linked from THAT conversation is Done. (Highlight is a visual marker, not a note.)
        for c in list(doc.get('highlighted_convos') or []):
            keys = [k for k, cv in jira_convos.items() if cv == c]
            if keys and all(jira_done.get(k) for k in keys):
                if unhighlight_convo(c):
                    remove_reddit_mod_note_highlighted_convo_sync(user, c)
                    logger.info(f'[Reddit] Unhighlighted convo {c} for u/{user} - all its Jira tickets Done')

        # Backward-compat: legacy single-field highlight (docs created before per-conversation).
        if doc.get('highlighted') and doc.get('convo_id') and all(jira_done.get(k) for k in keys_all):
            legacy_convo = doc.get('convo_id')
            if unhighlight_convo(legacy_convo):
                clear_reddit_mod_note_legacy_highlight_sync(user)
                logger.info(f'[Reddit] Unhighlighted legacy convo {legacy_convo} for u/{user} — all Jira tickets Done')


def _reddit_blocked(job: str) -> bool:
    """Background jobs stand down while Reddit refuses us (services/reddit_gate). Hitting it anyway
    only produced 403 errors every run and could prolong the block. They resume by themselves once
    the snapshot worker's hourly probe sees Reddit answer again."""
    try:
        from services import reddit_gate
        if reddit_gate.is_down():
            logger.debug('[Reddit] %s skipped: Reddit is refusing requests (reddit_gate)', job)
            return True
    except Exception as _e:
        logger.debug("ignored: %s", _e)
    return False


def reconcile_reddit_notes(include_jira: bool = True) -> None:
    """Idempotent: ensures email notes exist for known emails and (optionally) marks Done Jira notes.

    Email and Jira reconcile are isolated in their own try/except so a failure in one never
    blocks the other.
    The initial post-deploy run is email-only (include_jira=False) - see _delayed_initial_run.
    """
    if _reddit_blocked('reconcile_reddit_notes'):
        return
    if not _maintenance_lock.acquire(blocking=False):
        logger.info('[Reddit] Maintenance already running — skipping reconcile tick')
        return
    try:
        if include_jira:
            try:
                _reconcile_jira_notes()
            except Exception as e:
                logger.error(f'[Reddit] jira-note reconcile error: {e}')
    finally:
        _maintenance_lock.release()


def archive_stale_in_progress(days: int = None) -> int:
    """Archives modmail conversations in 'In Progress' whose last activity is older than `days`."""
    days = days if days is not None else STALE_IN_PROGRESS_DAYS
    if _reddit_blocked('archive_stale_in_progress'):
        return 0
    if not _maintenance_lock.acquire(blocking=False):
        logger.info('[Reddit] Maintenance already running — skipping archive_stale_in_progress')
        return 0
    archived = 0
    scanned = 0
    try:
        sub = _get_subreddit()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for convo in sub.modmail.conversations(state='inprogress', limit=None):
            scanned += 1
            last = _parse_dt(getattr(convo, 'last_updated', None))
            if last is None or last > cutoff:
                continue
            if archive_convo(convo.id):
                archived += 1
                logger.info(f'[Reddit] Archived stale modmail convo {convo.id} (last activity {last.isoformat()})')
        logger.info(f'[Reddit] archive_stale_in_progress done — scanned {scanned}, archived {archived}')
        return archived
    except Exception as e:
        logger.error(f'[Reddit] archive_stale_in_progress error (scanned {scanned}, archived {archived}): {e}')
        return archived
    finally:
        _maintenance_lock.release()


def archive_inactive_convos(days: int = None) -> int:
    """Auto-archive modmail conversations with NO activity for `days` (default 5) - the Reddit side
    of the console's History rule. Covers 'new' (never answered) and 'inprogress' conversations,
    but NEVER highlighted ones (those are actively tracked, e.g. a linked Jira ticket still open).
    Any new message updates last_updated, so a convo only gets archived once it's truly idle; if it
    comes back, a mod can unarchive it. Rate-limited (REDDIT_WRITE_DELAY between writes) under the
    shared maintenance lock so it never overlaps other bulk writes."""
    days = days if days is not None else INACTIVE_ARCHIVE_DAYS
    if _reddit_blocked('archive_inactive_convos'):
        return 0
    if not _maintenance_lock.acquire(blocking=False):
        logger.info('[Reddit] Maintenance already running — skipping archive_inactive_convos')
        return 0
    archived = 0
    scanned = 0
    # Highlighted convos are actively tracked (e.g. an open linked Jira) - never auto-archive them.
    # Check both the live flag AND our own tracked set, so we skip them even if the listing omits it.
    highlighted = set()
    try:
        from services.mongo_db_service import get_all_reddit_mod_notes_sync
        for doc in get_all_reddit_mod_notes_sync():
            for cid in (doc.get('highlighted_convos') or []):
                highlighted.add(cid)
    except Exception as e:
        logger.warning(f'[Reddit] could not load highlighted convos (will rely on live flag): {e}')
    try:
        sub = _get_subreddit()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        seen = set()
        for state in ('new', 'inprogress'):                 # open, non-archived conversations
            for convo in sub.modmail.conversations(state=state, limit=None):
                if convo.id in seen:
                    continue
                seen.add(convo.id)
                scanned += 1
                if convo.id in highlighted or getattr(convo, 'is_highlighted', False):
                    continue                                  # never touch highlighted/tracked convos
                last = _parse_dt(getattr(convo, 'last_updated', None))
                if last is None or last > cutoff:            # still active within the window
                    continue
                if archive_convo(convo.id):
                    archived += 1
                    logger.info(f'[Reddit] Auto-archived inactive convo {convo.id} (last activity {last.isoformat()})')
        logger.info(f'[Reddit] archive_inactive_convos done — scanned {scanned}, archived {archived}')
        return archived
    except Exception as e:
        logger.error(f'[Reddit] archive_inactive_convos error (scanned {scanned}, archived {archived}): {e}')
        return archived
    finally:
        _maintenance_lock.release()


def _delayed_initial_run() -> None:
    logger.info(f'[Reddit] Mod-note maintenance starts in {INITIAL_DELAY_SECONDS}s (post-deploy delay)')
    time.sleep(INITIAL_DELAY_SECONDS)
    logger.info('[Reddit] Starting initial mod-note maintenance (email backfill + In Progress cleanup)')
    # Email-only at startup: a Jira-Done reconcile here would churn through every already-closed
    # ticket at once (rewriting notes = spam). Jira reconcile runs on the 30-min schedule instead.
    reconcile_reddit_notes(include_jira=False)
    archive_stale_in_progress()
    archive_inactive_convos()   # first pass: archive convos idle >5d (console History rule)


def start_reddit_maintenance() -> None:
    """Spawns the delayed (10 min) initial backfill + In Progress cleanup in a daemon thread."""
    threading.Thread(target=_delayed_initial_run, daemon=True, name='reddit-maintenance-initial').start()
