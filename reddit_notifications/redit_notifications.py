import asyncio
from pathlib import Path
import aiohttp
import certifi
from asyncprawcore import RequestException, AsyncPrawcoreException, TooManyRequests
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.web.async_client import AsyncWebClient
import os
import asyncpraw
import logging
import ssl
from slack_sdk.errors import SlackApiError
import random
from datetime import datetime, timezone

import re
from motor.motor_asyncio import AsyncIOMotorClient
from services.mongo_db_service import async_get_email_by_reddit_username, async_save_reddit_user_email
from services.user_notify_service import notify_event   # per-user notification fan-out (console prefs)
from services.redact import redact_emails, user_id_from, link_user_ids_slack   # emails hidden, User IDs linked


_notify_tasks = set()   # keep strong refs so the fire-and-forget tasks aren't GC'd before they run


def _notify_bg(event_type, title, body="", url="", route=""):
    """Fire a per-user notification without blocking the async loop (sends are sync HTTP). `route` is a
    console hash route so the notification links to the chat inside the console."""
    try:
        t = asyncio.create_task(asyncio.to_thread(notify_event, event_type, title, body, url, route))
        _notify_tasks.add(t)
        t.add_done_callback(_notify_tasks.discard)
    except Exception as _e:
        logger.debug("ignored: %s", _e)


def _preview(text, limit=140):
    """A short one-line preview of a message body for notifications (like the Slack alert)."""
    s = " ".join(str(text or "").split())
    return (s[:limit] + "…") if len(s) > limit else s

_EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
_SESSION_ID_REGEX = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

#Slack clients
client = WebClient(token=os.getenv('SLACK_TOKEN'))
async_client = AsyncWebClient(token=os.getenv('SLACK_TOKEN'))

SLACK_MODMAIL_CHANNEL = os.getenv('SLACK_MODMAIL_CHANNEL')
SLACK_REDDIT_COMMENTS_CHANNEL = os.getenv('SLACK_REDDIT_COMMENTS_CHANNEL')
SLACK_REDDIT_POSTS_CHANNEL = os.getenv('SLACK_REDDIT_POSTS_CHANNEL')

logger = logging.getLogger(__name__)

# MongoDB (async)
# Same Motor client as services.mongo_db_service (both are used only from this Reddit event loop).
from services.mongo_db_service import async_client as _mongo_client
_db = _mongo_client[os.getenv('MONGODB_DATABASE', 'reddit_posts')]
_seen_posts_col = _db['seen_reddit_posts']
_seen_comments_col = _db['seen_reddit_comments']
_seen_modmail_col = _db['seen_reddit_modmail']
_reddit_mod_notes_col = _db['reddit_mod_notes']

# Snapshot DB (what the console reads). The streams below mirror FULL content here in real time,
# right when the Slack notification fires - so posts/comments/modmail history show up in the console
# with no extra Reddit API calls (the content is already in hand).
# TLS only for an Atlas (mongodb+srv) address: a plain mongodb:// server (a proxy, or the Mongo on the VM)
# answers without TLS, and forcing it there fails the handshake (UNEXPECTED_EOF).
_snap_uri = os.getenv('SNAPSHOT_MONGODB_URI') or os.getenv('MONGODB_URI')
_snap_client = AsyncIOMotorClient(
    _snap_uri, **({'tlsCAFile': certifi.where()} if (_snap_uri or '').startswith('mongodb+srv://') else {}))
_snap_db = _snap_client[os.getenv('SNAPSHOT_MONGODB_DATABASE', 'reddit_snapshots')]
_snap_posts_col = _snap_db['reddit_posts_snapshot']
_snap_modmail_col = _snap_db['reddit_modmail_snapshot']

_OUR_REDDIT_USER = (os.getenv('REDDIT_USERNAME') or '').strip().lower()

# Cache of our team's Reddit usernames (lowercased): the bot account plus every console user's linked
# reddit_username (profile Identity), so a human mod's comment
# counts as a team reply even when they forget to click Reddit's "distinguish as moderator" shield.
_mods_cache = {"at": 0.0, "names": set()}
_MODS_TTL = 300  # refresh at most every 5 minutes (config rarely changes)



def _note_manual_user_id(convo_id):
    """Console-only system note on a ModMail chat: the User ID lookup failed, add it by hand. Written
    to reddit_convo_notes (shown in the console transcript, never posted to Reddit). One per chat/day."""
    try:
        from datetime import datetime, timedelta
        from services.mongo_db_service import sync_db
        from services.user_ref import MANUAL_NOTE
        col = sync_db['reddit_convo_notes']
        now = datetime.utcnow()
        if col.find_one({'convo_id': convo_id, 'body': MANUAL_NOTE, 'created_utc': {'$gte': now - timedelta(days=1)}}):
            return
        col.insert_one({'convo_id': convo_id, 'author': 'System', 'body': MANUAL_NOTE, 'is_internal': True,
                        'date': now.replace(microsecond=0).isoformat() + 'Z', 'created_utc': now})
    except Exception as e:
        logging.getLogger(__name__).warning('[Reddit] could not add the manual User ID note: %s', type(e).__name__)

async def _get_reddit_mods():
    import time
    now = time.time()
    if _mods_cache["at"] > 0 and (now - _mods_cache["at"]) < _MODS_TTL:
        return _mods_cache["names"]
    names = set()
    if _OUR_REDDIT_USER:
        names.add(_OUR_REDDIT_USER)
    try:
        async for u in _db['console_users'].find(
                {"reddit_username": {"$nin": [None, ""]}}, {"reddit_username": 1}):
            ru = str(u.get("reddit_username") or "").strip().lower()
            if ru:
                names.add(ru)
    except Exception as e:
        logger.warning("[Reddit] could not load console_users reddit names: %s", e)
    _mods_cache["names"] = names
    _mods_cache["at"] = now
    return names


def _comment_is_team(author, distinguished, mods=None):
    """A real community reply: our mod account, any known team Reddit mod, or a moderator-distinguished
    comment - never AutoMod. `mods` is the lowercased set from _get_reddit_mods()."""
    if author == 'AutoModerator':
        return False
    a = (author or '').strip().lower()
    if _OUR_REDDIT_USER and a == _OUR_REDDIT_USER:
        return True
    if mods and a in mods:
        return True
    return distinguished == 'moderator'


_IMG_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp')


def _post_images(post):
    """Best-effort list of image URLs on a post: direct image posts + Reddit galleries."""
    urls = []
    url = getattr(post, 'url', '') or ''
    base = url.lower().split('?', 1)[0]
    if url and (base.endswith(_IMG_EXT) or 'i.redd.it/' in url.lower()):
        urls.append(url)
    if getattr(post, 'is_gallery', False):
        meta = getattr(post, 'media_metadata', None)
        if isinstance(meta, dict):
            for m in meta.values():
                s = (m or {}).get('s') or {}
                u = s.get('u') or s.get('gif')
                if u:
                    urls.append(u.replace('&amp;', '&'))
    out, seen = [], set()
    for u in urls:
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out[:10]


def _snap_bg(coro):
    """Run a snapshot write as a background task with a timeout, so a slow/unreachable snapshot Mongo
    can NEVER block or stall the Slack-notification path. The Slack alerts (modmail/comments/posts)
    are business-critical and must always go out even if the console mirror is down."""
    async def _run():
        try:
            await asyncio.wait_for(coro, timeout=15)
        except Exception as e:
            logger.warning(f'[Reddit] snapshot bg write dropped: {e}')
    try:
        asyncio.create_task(_run())
    except RuntimeError:
        coro.close()


async def _snap_upsert_post(post):
    """Mirror a post's metadata into the snapshot DB (comments are added by the comment stream)."""
    try:
        await _snap_posts_col.update_one(
            {'post_id': post.id},
            {'$set': {
                'post_id': post.id,
                'title': getattr(post, 'title', '') or '',
                'author': str(post.author) if getattr(post, 'author', None) else '[deleted]',
                'subreddit': 'r/' + str(post.subreddit),
                'url': 'https://www.reddit.com' + getattr(post, 'permalink', ''),
                'body': redact_emails((getattr(post, 'selftext', '') or '')[:40000]),
                'images': _post_images(post),
                'created': datetime.utcfromtimestamp(post.created_utc).isoformat() if getattr(post, 'created_utc', None) else None,
                'synced_at': datetime.utcnow().isoformat(),
                **({'num_comments': post.num_comments} if getattr(post, 'num_comments', None) is not None else {}),
            }, '$setOnInsert': {'comments': [], 'has_reply': False}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[Reddit] snapshot post write failed: {e}')


async def _snap_append_comment(comment):
    """Append a streamed comment to its post's snapshot doc; flag has_reply only when WE/the
    community replied (never AutoMod / other users)."""
    try:
        link = getattr(comment, 'link_id', '') or ''
        pid = link.split('_', 1)[-1] if link else None
        if not pid:
            return
        a = getattr(comment, 'author', None)
        author = str(a) if a else '[deleted]'
        distinguished = getattr(comment, 'distinguished', None)
        team = _comment_is_team(author, distinguished, await _get_reddit_mods())
        parent_id = getattr(comment, 'parent_id', '') or ''
        cdoc = {
            'id': comment.id,
            'author': author,
            'body': redact_emails((getattr(comment, 'body', '') or '')[:10000]),
            'created': datetime.utcfromtimestamp(comment.created_utc).isoformat() if getattr(comment, 'created_utc', None) else None,
            'distinguished': distinguished,
            'is_submitter': bool(getattr(comment, 'is_submitter', False)),
            'team': team,
            'parent_id': parent_id,                                       # for the console Reddit Comments tab
            'permalink': getattr(comment, 'permalink', '') or '',
            'reply_to_mod': False,                                        # set True below when it replies to us
        }
        # Idempotent: ensure the post doc exists, then push the comment ONLY if that id is not
        # already stored. This lets us mirror every comment (incl. our mods / ignored accounts)
        # from the top of the stream without risking duplicate pushes on a stream reconnect.
        await _snap_posts_col.update_one(
            {'post_id': pid},
            {'$setOnInsert': {'post_id': pid, 'comments': [], 'has_reply': False}},
            upsert=True,
        )
        set_fields = {'synced_at': datetime.utcnow().isoformat()}
        if cdoc['created']:
            set_fields['last_comment_at'] = cdoc['created']   # real last activity for the console (S/#1)
        if team:
            set_fields['has_reply'] = True
        # Item W6: only a USER's reply to one of OUR mod comments should count as "new" and pull a
        # post out of Ignored in the console - not a plain top-level comment. Detect it: this comment
        # is NOT ours, and its parent (t1_<id>) is one of our team comments already in the snapshot.
        # We also stamp reply_to_mod + the parent's author/body onto the comment itself so the console
        # Reddit Comments tab can show each reply next to the comment of ours it answered.
        if not team and cdoc['created'] and parent_id.startswith('t1_'):
            parent_cid = parent_id.split('_', 1)[-1]
            parent_doc = await _snap_posts_col.find_one(
                {'post_id': pid, 'comments.id': parent_cid}, {'comments.$': 1})
            pc = (parent_doc or {}).get('comments') or []
            if pc and pc[0].get('team'):
                cdoc['reply_to_mod'] = True
                cdoc['parent_author'] = pc[0].get('author', '')
                cdoc['parent_body'] = (pc[0].get('body', '') or '')[:300]
                set_fields['last_user_reply_to_mod_at'] = cdoc['created']
        await _snap_posts_col.update_one(
            {'post_id': pid, 'comments.id': {'$ne': comment.id}},
            {'$push': {'comments': cdoc}, '$set': set_fields},
        )
    except Exception as e:
        logger.error(f'[Reddit] snapshot comment write failed: {e}')


async def _snap_write_convo(convo):
    """Mirror a modmail conversation's FULL message thread into the snapshot DB. track_modmail has
    already loaded the whole convo for Slack, so this is free - and it gives the console real message
    history for active convos. Does NOT touch `state`; the snapshot's per-state passes own that."""
    try:
        _p = getattr(convo, 'participant', None)
        _pname = getattr(_p, 'name', None) if _p else None
        # The user's own email in their messages is shown as their User ID (the one stored for the account).
        _puid = user_id_from(await async_get_email_by_reddit_username(_pname)) if _pname else ''
        msgs = []
        for m in convo.messages:
            au = getattr(m, 'author', None)
            _aname = (getattr(au, 'name', None) if au else None) or 'Anonymous'
            msgs.append({
                'id': getattr(m, 'id', ''),
                'author': _aname,
                'body': redact_emails((getattr(m, 'body_markdown', None) or getattr(m, 'body', '') or '')[:10000],
                                      _puid if _aname == _pname else ''),
                'is_internal': bool(getattr(m, 'is_internal', False)),
                'date': getattr(m, 'date', None),
            })
        # An older copy of the thread (a Devvit event caught up late) must not overwrite a newer one.
        new_upd = str(getattr(convo, 'last_updated', None) or '')
        cur = await _snap_modmail_col.find_one({'convo_id': convo.id}, {'last_updated': 1, 'num_messages': 1})
        if cur and new_upd and str(cur.get('last_updated') or '') > new_upd and (cur.get('num_messages') or 0) >= len(msgs):
            return
        p = getattr(convo, 'participant', None)
        participant_name = getattr(p, 'name', None) if p else None
        # Lightweight status hints so the console can derive a convo's status in the LIST (which
        # omits `messages`) without opening it: whether a mod has publicly replied, and who sent the
        # last public (non-internal) message.
        public = [m for m in msgs if not m['is_internal']]
        mod_replied = any(m['author'] != participant_name for m in public)
        last_public_author = public[-1]['author'] if public else None
        # Activity + preview + count are PUBLIC-only, so an automated internal note (e.g. "[System]
        # Linked to Jira ..." or a "[Console] Archived - X" audit note) never becomes the list preview
        # or makes a convo look freshly active/unread in the console. Without last_body here the field
        # was left frozen at whatever the 5-min cycle last wrote (often an internal note).
        last_public_date = public[-1]['date'] if public else getattr(convo, 'last_updated', None)
        last_public_body = (public[-1]['body'] if public else '')[:200]
        await _snap_modmail_col.update_one(
            {'convo_id': convo.id},
            {'$set': {
                'convo_id': convo.id,
                'subject': getattr(convo, 'subject', '') or '',
                'participant': participant_name,
                'last_updated': getattr(convo, 'last_updated', None),
                'num_messages': len(msgs),
                'num_public_messages': len(public),
                'messages': msgs,
                'mod_replied': mod_replied,
                'last_public_author': last_public_author,
                'last_activity': last_public_date,
                'last_body': last_public_body,
                'synced_at': datetime.utcnow().isoformat(),
            }},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[Reddit] snapshot modmail write failed: {e}')


async def _is_seen_post(post_id: str) -> bool:
    return await _seen_posts_col.find_one({'post_id': post_id}, {'_id': 1}) is not None

async def _mark_seen_post(post_id: str):
    await _seen_posts_col.update_one(
        {'post_id': post_id},
        {'$setOnInsert': {'post_id': post_id, 'created_at': datetime.now(timezone.utc)}},
        upsert=True
    )

async def _is_seen_comment(comment_id: str) -> bool:
    return await _seen_comments_col.find_one({'comment_id': comment_id}, {'_id': 1}) is not None

async def _mark_seen_comment(comment_id: str):
    await _seen_comments_col.update_one(
        {'comment_id': comment_id},
        {'$setOnInsert': {'comment_id': comment_id, 'created_at': datetime.now(timezone.utc)}},
        upsert=True
    )

async def _is_seen_modmail_message(convo_id: str, message_id: str) -> bool:
    doc = await _seen_modmail_col.find_one({'convo_id': convo_id}, {'message_ids': 1})
    if not doc:
        return False
    return message_id in doc.get('message_ids', [])

async def _mark_seen_modmail_message(convo_id: str, message_id: str):
    await _seen_modmail_col.update_one(
        {'convo_id': convo_id},
        {
            '$addToSet': {'message_ids': message_id},
            '$set': {'updated_at': datetime.now(timezone.utc)},
            '$setOnInsert': {'convo_id': convo_id},
        },
        upsert=True
    )


async def handle_post(post, ignored_users_list):
    """One new post: mirror it into the console snapshot, then the Slack alert + per-user
    notification (unless muted / already sent). Used by the API stream and the RSS fallback."""

    author = str(post.author) if post.author else "[deleted]"

    # Mirror EVERY post into the snapshot (the console shows all activity). The
    # upsert is idempotent, so it is safe before the ignore/seen checks.
    _snap_bg(_snap_upsert_post(post))

    # The ignore list only mutes the bot's Slack alert - the console already has the post.
    if author in ignored_users_list:
        logger.info(f"[Reddit] Muting Slack alert for post from {author}")
        return

    if await _is_seen_post(post.id):
        logger.info(f"[Reddit] Post already sent: {post.title}")
        return

    logger.info(f"[Reddit] New post: {post.title}")
    _notify_bg("reddit_post", "New Reddit post",
               _preview(redact_emails(f"u/{author}: {post.title}" + (f" - {post.selftext}" if getattr(post, 'selftext', '') else ""))),
               url=f"https://www.reddit.com{post.permalink}", route=f"#/posts/{post.id}")

    try:
        email = await async_get_email_by_reddit_username(author)
        email_text = link_user_ids_slack((email if str(email).startswith("User ID") else f"Email: {email}") if email else "No User ID on file")

        action_buttons = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Add information"},
                "action_id": "add_information_button",
                "style": "primary",
                "value": f"https://www.reddit.com{post.permalink}",
            }
        ]
        if not email:
            action_buttons.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "Add Email"},
                "action_id": "add_reddit_user_email_button",
                "value": author,
            })

        flair_text = post.link_flair_text or ""
        flair_richtext = post.link_flair_richtext or []
        flair_richtext_str = " ".join(
            part.get("t", "") for part in flair_richtext if part.get("e") == "text"
        )
        if "Suggestion" in flair_text or "Suggestion" in flair_richtext_str:
            action_buttons.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "Add Gaming Proposal"},
                "action_id": "open_gaming_proposal_button",
                "style": "primary",
            })

        await async_client.chat_postMessage(
            channel=SLACK_REDDIT_POSTS_CHANNEL,
            text=f"New post: {post.title}\nAuthor: u/{author} | {email_text}\nLink: https://www.reddit.com{post.permalink}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"New post: {post.title}\n"
                            f"Author: u/{author} | {email_text}\n"
                            f"Link: https://www.reddit.com{post.permalink}"
                        ),
                    }
                },
                {
                    "type": "actions",
                    "elements": action_buttons,
                }
            ]
        )
        await _mark_seen_post(post.id)
        logger.info("[Reddit] Message sent to Slack")
    except SlackApiError as e:
        logger.error(f"[Reddit] Slack error: {e}")


async def handle_comment(comment, ignored_users_list):
    """One new comment: mirror it into the console snapshot, then the Slack alert (unless muted,
    already sent or our own team's). Used by the API stream and the RSS fallback."""
    author = str(comment.author)

    # Mirror EVERY comment into the snapshot, including our moderators and any ignored
    # accounts (the console must show all post comments). The mirror is
    # idempotent, so doing it before the seen/ignore checks is safe.
    _snap_bg(_snap_append_comment(comment))

    # The ignore list only mutes the bot's Slack alert - the console already has the
    # comment from the mirror above.
    if author in ignored_users_list:
        logger.info(f'[Reddit] Muting Slack alert for comment from {author}')
        return

    if await _is_seen_comment(comment.id):
        logger.info(f"[Reddit] Comment already sent: {comment.id}")
        return

    # Slack tracks ALL subreddit comments again (restored). We skip only our own team/mod
    # comments - there's no point pinging ourselves about our own replies. The narrower
    # "someone replied to one of OUR comments" case now has its own console tab (Reddit
    # Comments), fed by the reply_to_mod flag the snapshot stamps above.
    if _comment_is_team(author, getattr(comment, 'distinguished', None), await _get_reddit_mods()):
        await _mark_seen_comment(comment.id)
        return

    _cbody = redact_emails(comment.body)
    comment_preview = _cbody[:100] + '...' if len(_cbody) > 100 else _cbody
    logger.info(f"[Reddit] New subreddit comment by {author}: {comment_preview}")
    try:
        await async_client.chat_postMessage(
            channel=SLACK_REDDIT_COMMENTS_CHANNEL,
            text=f"New comment by {author}: {comment_preview}\n\nLink: https://www.reddit.com{comment.permalink}"
        )
        await _mark_seen_comment(comment.id)
    except SlackApiError as e:
        logger.error(f"[Reddit] Slack error while posting comment: {e}")


# Posts tracking
async def track_posts(subreddit, ignored_users_list):
    retry_delay = 60
    max_retry_delay = 600
    logger.info("[Reddit] Posts tracking has been started...")

    while True:
        # Reddit is refusing the bot (see services/reddit_gate): wait quietly; the snapshot worker
        # probes once an hour and clears the flag when Reddit answers again.
        from services import reddit_gate
        if reddit_gate.is_down():
            await asyncio.sleep(120)
            continue
        try:
            async for post in subreddit.stream.submissions(skip_existing=True):
                await handle_post(post, ignored_users_list)

            retry_delay = 60

        except TooManyRequests:
            jitter = random.uniform(0, 30)
            wait_time = min(retry_delay + jitter, max_retry_delay)
            logger.warning(f"[Reddit] Rate limit hit (429). Waiting {wait_time:.0f} seconds...")
            await asyncio.sleep(wait_time)
            retry_delay = min(retry_delay * 2, max_retry_delay)

        except (RequestException, AsyncPrawcoreException, SlackApiError) as e:
            logger.error(f"[Reddit] Error in posts tracking: {e}. Retrying in 60 sec...")
            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"[Reddit] Unexpected error in posts tracking: {e}. Restarting in 60 sec...")
            await asyncio.sleep(60)


# Comments tracking
async def track_comments(subreddit, ignored_users_list):
    retry_delay = 60
    max_retry_delay = 600

    logger.info("[Reddit] Comments tracking has been started...")
    while True:
        # Reddit is refusing the bot (see services/reddit_gate): wait quietly; the snapshot worker
        # probes once an hour and clears the flag when Reddit answers again.
        from services import reddit_gate
        if reddit_gate.is_down():
            await asyncio.sleep(120)
            continue
        try:
            logger.info("[Reddit] Starting comments stream...")

            async for comment in subreddit.stream.comments(skip_existing=True):
                await handle_comment(comment, ignored_users_list)

            logger.warning("[Reddit] Comments stream ended unexpectedly. Restarting in 30 seconds...")
            await asyncio.sleep(30)
            retry_delay = 60

        except TooManyRequests:
            jitter = random.uniform(0, 30)
            wait_time = min(retry_delay + jitter, max_retry_delay)
            logger.warning(f'[Reddit] Rate limit hit (429) in comments. Waiting {wait_time:.0f} seconds...')
            await asyncio.sleep(wait_time)
            retry_delay = min(retry_delay * 2, max_retry_delay)

        except (RequestException, AsyncPrawcoreException) as e:
            logger.error(f'[Reddit] Error in comments tracking: {e}. Retrying in 60 sec...')
            await asyncio.sleep(60)
            retry_delay = 60

        except Exception as e:
            logger.error(f'[Reddit] Unexpected error in comments tracking: {e}. Retrying in 60 sec...')
            await asyncio.sleep(60)
            retry_delay = 60


# Detection inside "Mods only" (internal) modmail messages
_JIRA_KEY_REGEX = re.compile(r'\bSO-\d+\b')


def _participant_name(convo):
    p = getattr(convo, 'participant', None)
    if p is None:
        return None
    if isinstance(p, dict):
        return p.get('name')
    return getattr(p, 'name', None)


def _resolve_participant_sync(convo_id: str):
    from reddit_notifications.mod_notes import get_convo_participant
    return get_convo_participant(convo_id)


def _maybe_subscribe_maintenance(participant: str, convo_id: str, body: str):
    """Sync (run in a thread): if the internal message names an active game/server maintenance,
    create a tracking mod note + a Reddit maintenance-end subscription for this conversation."""
    from services.mongo_db_service import get_active_maintenances
    from services.maintenance_notify_service import normalize, subscribe_reddit_maintenance

    body_norm = normalize(body)
    for mtype in ('game', 'server'):
        for entity in get_active_maintenances(mtype):
            ent_norm = normalize(entity)
            if ent_norm and ent_norm in body_norm:
                subscribe_reddit_maintenance(entity, mtype, convo_id, 'en', participant=participant)
                logger.info(f'[Reddit] Subscribed convo {convo_id} to {mtype} maintenance end for "{entity}"')
                return


async def _handle_internal_modmail_message(convo, convo_id, message):
    """Detects Jira keys and active-maintenance game names in a Mods-only (internal) message."""
    try:
        body = message.body_markdown or ''
        participant = _participant_name(convo) or await asyncio.to_thread(_resolve_participant_sync, convo_id)
        if not participant:
            return

        jira_match = _JIRA_KEY_REGEX.search(body)
        if jira_match:
            from reddit_notifications.mod_notes import link_jira_ticket
            issue_key = jira_match.group(0)
            await asyncio.to_thread(link_jira_ticket, participant, issue_key, convo_id)
            logger.info(f'[Reddit] Linked Jira {issue_key} from mods-only comment in convo {convo_id}')

        await asyncio.to_thread(_maybe_subscribe_maintenance, participant, convo_id, body)
    except Exception as e:
        logger.error(f'[Reddit] Failed to handle internal modmail message in convo {convo_id}: {e}')


# Modmail tracking


async def _process_modmail_convo(convo, ignored_users_list):
    """Process ONE modmail conversation: mirror it to the snapshot and send Slack alerts for its new
    public messages. Fully isolated - any error here is logged and swallowed so a single bad convo
    (or a transient Mongo/Reddit/Slack hiccup) can never kill the whole tracker (which would silently
    stop all Slack alerts until the next restart). A message is marked "seen" only AFTER it is handled
    (internal/ignored) or its Slack alert is actually sent, so a failed send is retried, never lost."""
    try:
        await asyncio.wait_for(convo.load(), timeout=45)
        convo_id = convo.id
        _snap_bg(_snap_write_convo(convo))   # mirror the FULL thread into snapshot (background)

        for message in convo.messages:
            try:
                message_id = message.id
                author = message.author.name if message.author else "Anonymous"
                is_internal = getattr(message, 'is_internal', False)

                if await _is_seen_modmail_message(convo_id, message_id):
                    continue

                # Mods-only (internal) messages are written by moderators, who are all on
                # the ignore list. Detect Jira keys / active-maintenance names BEFORE the
                # ignored-author skip, otherwise they would never be processed. These
                # messages are not forwarded to the Slack modmail channel.
                if is_internal:
                    await _handle_internal_modmail_message(convo, convo_id, message)
                    await _mark_seen_modmail_message(convo_id, message_id)
                    continue

                # A public reply written by OUR side - the bot account used for console/Slack replies
                # (e.g. u/HelperBoosteroid) or any known team moderator - is an OUTGOING message. It is
                # still mirrored to the snapshot above (so the console shows it), but must NEVER raise a
                # Slack alert or a per-user notification: that would ping the team about their own reply.
                if _comment_is_team(author, getattr(message, 'distinguished', None), await _get_reddit_mods()):
                    await _mark_seen_modmail_message(convo_id, message_id)
                    continue

                if author in ignored_users_list:
                    await _mark_seen_modmail_message(convo_id, message_id)
                    continue

                modmail_url = f"https://mod.reddit.com/mail/all/{convo_id}"

                # Auto-detect email in message body
                # A user we already hold a User ID for is not looked up again (no cloud API request).
                existing_value = await async_get_email_by_reddit_username(author)
                if not str(existing_value or "").startswith("User ID"):
                    detected_email = _EMAIL_REGEX.search(message.body_markdown)
                    detected_session = None if detected_email else _SESSION_ID_REGEX.search(message.body_markdown)
                    detected = detected_email or detected_session
                    if detected:
                        from services.mongo_db_service import ident_status
                        _uid, _st = await asyncio.to_thread(ident_status, detected.group(0))
                        if _st == "found":
                            await async_save_reddit_user_email(author, _uid)
                            logger.info(f"[Reddit] Auto-saved User ID for u/{author}")
                        elif _st == "error":
                            # Never store the address: leave a mods-only note in the console chat instead.
                            await asyncio.to_thread(_note_manual_user_id, convo_id)

                email = await async_get_email_by_reddit_username(author)
                email_text = link_user_ids_slack((email if str(email).startswith("User ID") else f"Email: {email}") if email else "No User ID on file")

                section_text = (
                    f"New ModMail message!\n"
                    f"Subject: {convo.subject}\n"
                    f"Author: u/{author} | {email_text}\n"
                    f"Message: {link_user_ids_slack(redact_emails(message.body_markdown[:200], user_id_from(email)))}\n"
                    f"URL: {modmail_url}"
                )

                blocks = [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": section_text},
                    }
                ]

                action_elements = []
                if not email:
                    action_elements.append({
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Add Email"},
                        "action_id": "add_reddit_user_email_button",
                        "value": author,
                    })

                # Hide "Add Jira Ticket" only once THIS conversation already has a linked
                # ticket - the same user can have separate tickets across different
                # conversations, so the check is per-conversation, not per-user.
                mod_note = await _reddit_mod_notes_col.find_one(
                    {'reddit_user': author}, {'jira_convos': 1}
                )
                # jira_convos maps issue_key -> convo_id (string). Guard against malformed/legacy
                # values (e.g. a dict) so a bad doc can't raise "unhashable type" and block alerts.
                jira_convos = (mod_note or {}).get('jira_convos') or {}
                linked_convos = {v for v in jira_convos.values() if isinstance(v, str)}
                if convo_id not in linked_convos:
                    action_elements.append({
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Add Jira Ticket"},
                        "action_id": "add_reddit_jira_ticket_button",
                        "value": f"{author}:{convo_id}",
                    })

                if action_elements:
                    blocks.append({"type": "actions", "elements": action_elements})

                await async_client.chat_postMessage(
                    channel=SLACK_MODMAIL_CHANNEL,
                    text=section_text,
                    blocks=blocks,
                )
                # Mark seen ONLY after the Slack alert actually went out, so a failed send is retried.
                await _mark_seen_modmail_message(convo_id, message_id)
                # Per-user notifications fire AFTER mark-seen so a Slack-send retry can't re-notify.
                _mm_first = bool(convo.messages) and message.id == convo.messages[0].id
                _notify_bg("reddit_modmail" if _mm_first else "reddit_modmail_msg",
                           "New Reddit ModMail" if _mm_first else "New ModMail reply",
                           _preview(redact_emails(f"u/{author}: {convo.subject} - {message.body_markdown}", user_id_from(email))),
                           url=modmail_url, route=f"#/modmail/{convo_id}")
                logger.info("[Reddit] New ModMail message was sent to Slack")
            except TooManyRequests:
                raise   # rate limited -> back off in the tracker loop (message stays unseen, retried)
            except Exception as e:
                # Any other per-message error (Slack reject, Mongo blip, bad data): log and move to the
                # next message. The message stays unseen so a transient failure is retried next cycle,
                # and one bad message can never stall the whole tracker.
                logger.error(f"[Reddit] Failed to process a modmail message in convo {getattr(convo, 'id', '?')}: {e}", exc_info=True)
                continue
        # An email in the thread may have just been resolved to a User ID above: write the snapshot again
        # so the console shows "[email hidden] (User ID n)" instead of the bare "[email hidden]".
        if any('@' in (getattr(m, 'body_markdown', '') or '') for m in convo.messages):
            _snap_bg(_snap_write_convo(convo))
    except TooManyRequests:
        raise   # propagate to the tracker loop's backoff handler
    except (RequestException, AsyncPrawcoreException) as e:
        # convo.load() / listing network error - skip this convo, the tracker keeps going.
        logger.warning(f"[Reddit] Skipping modmail convo {getattr(convo, 'id', '?')} (load error): {e}")
    except Exception as e:
        logger.error(f"[Reddit] Failed to process modmail convo {getattr(convo, 'id', '?')}: {e}")


async def track_modmail(subreddit, ignored_users_list):
    retry_delay = 60
    max_retry_delay = 600
    logger.info("[Reddit] Modmail tracking has been started...")

    while True:
        # Reddit is refusing the bot (see services/reddit_gate): wait quietly; the snapshot worker
        # probes once an hour and clears the flag when Reddit answers again.
        from services import reddit_gate
        if reddit_gate.is_down():
            await asyncio.sleep(120)
            continue
        try:
            async for convo in subreddit.modmail.conversations(limit=10):
                await _process_modmail_convo(convo, ignored_users_list)

            retry_delay = 60
            await asyncio.sleep(30)

        except TooManyRequests:
            jitter = random.uniform(0, 30)
            wait_time = min(retry_delay + jitter, max_retry_delay)
            logger.warning(f'[Reddit] Rate limit hit (429) in modmail. Waiting {wait_time:.0f} seconds...')
            await asyncio.sleep(wait_time)
            retry_delay = min(retry_delay * 2, max_retry_delay)

        except (RequestException, AsyncPrawcoreException, SlackApiError) as e:
            logger.error(f"[Reddit] Error in modmail tracking: {e}. Retrying in 60 sec...")
            await asyncio.sleep(60)

        except Exception as e:
            # Catch-all so the tracker NEVER dies silently (which stops all Slack alerts until the
            # next restart while the console keeps updating via the separate snapshot worker).
            logger.error(f"[Reddit] Unexpected error in modmail tracking: {e}. Retrying in 60 sec...", exc_info=True)
            await asyncio.sleep(60)


async def start_tracking(ignored_users_str):
    ignored_users_list = [u.strip() for u in ignored_users_str.split(',')] if ignored_users_str else []

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
        reddit = asyncpraw.Reddit(
            client_id=os.getenv('REDDIT_CLIENT_ID'),
            client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
            user_agent=os.getenv('REDDIT_USER_AGENT'),
            username=os.getenv('REDDIT_USERNAME'),
            password=os.getenv('REDDIT_PASSWORD'),
            requestor_kwargs={"session": session},
            ratelimit_seconds=900
        )

        subreddit_name = os.getenv('SUBREDDIT_NAME')
        subreddit = await reddit.subreddit(subreddit_name)

        logger.info(f'[Reddit] Successfully connected to: {subreddit_name}')

        from reddit_notifications.devvit_inbox import track_devvit_inbox
        await asyncio.gather(
            track_posts(subreddit, ignored_users_list),
            track_comments(subreddit, ignored_users_list),
            track_modmail(subreddit, ignored_users_list),
            # RSS fallback (rss_fallback.py) is switched off: Devvit covers posts, comments and ModMail.
            # Events from our Devvit app (ModMail, posts, comments), queued by the console.
            track_devvit_inbox(ignored_users_list),
            return_exceptions=True
        )
