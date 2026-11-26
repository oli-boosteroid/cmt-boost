import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient

from services.jira_service import (
    get_emails_from_all_comments,
    get_issue_communication_link,
    get_jira_issue_status,
    get_last_comment,
    parse_discord_channel_id,
    parse_reddit_convo_id,
)
from services.mongo_db_service import (
    get_stale_cm_tickets_sync,
    mark_jira_ticket_needs_postpone_sync,
    save_jira_slack_notification_sync,
    set_jira_discord_channel_sync,
    update_jira_stale_notified_sync,
    upsert_jira_ticket_sync,
)
from services.user_ref import label_for_email, replace_emails

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

SLACK_JIRA_NOTIFICATIONS_CHANNEL_ID = os.getenv('SLACK_JIRA_NOTIFICATIONS_CHANNEL_ID', '')
SLACK_JIRA_CHANNEL_ID = os.getenv('SLACK_JIRA_CHANNEL_ID', '')
SLACK_TOKEN = os.getenv('SLACK_TOKEN', '')
JIRA_BASE_URL = os.getenv('JIRA_BASE_URL', '').rstrip('/')

# Maps alias → Slack user ID for @mentions  (same format as SLACK_USER_IDS elsewhere)
# Format: SLACK_USER_IDS=o.teleshko:U08ABC123,m.yeromenko:U09XYZ456
_SLACK_USER_ID_MAP: dict[str, str] = {}
for _su_entry in os.getenv('SLACK_USER_IDS', '').split(','):
    if ':' in _su_entry:
        _su_alias, _su_id = _su_entry.strip().split(':', 1)
        if _su_alias and _su_id:
            _SLACK_USER_ID_MAP[_su_alias.strip()] = _su_id.strip()

# Maps Jira accountId → display alias for our team members
# Format: JIRA_OUR_USER_IDS=prefix:uuid:alias,...  (Atlassian Cloud accountId = "prefix:uuid")
# Fallback: accountId:alias (for non-prefix formats)
OUR_JIRA_USER_MAP: dict[str, str] = {}
for _pair in os.getenv('JIRA_OUR_USER_IDS', '').split(','):
    _parts = _pair.strip().split(':', 2)
    if len(_parts) == 3 and _parts[0]:
        _account_id = f'{_parts[0]}:{_parts[1]}'
        OUR_JIRA_USER_MAP[_account_id] = _parts[2]
    elif len(_parts) == 2 and _parts[0]:
        OUR_JIRA_USER_MAP[_parts[0]] = _parts[1]
OUR_JIRA_ACCOUNT_IDS: set[str] = set(OUR_JIRA_USER_MAP.keys())

CM_IN_PROGRESS = 'CM In Progress'
L1_IN_PROGRESS = 'L1 In Progress'
BACKLOG = 'Backlog'
STALE_DAYS = 5

# URG (Urgent Request) board - separate lifecycle from SO
JIRA_URG_PROJECT_KEY = os.getenv('JIRA_URG_PROJECT_KEY', 'URG')
URG_REVIEW_STATUS = os.getenv('JIRA_URG_REVIEW_STATUS', 'Review')


def _is_urg_key(issue_key: str) -> bool:
    return issue_key.upper().startswith(f'{JIRA_URG_PROJECT_KEY.upper()}-')

slack_client = WebClient(token=SLACK_TOKEN)

# Matches messages like:
# "John Doe transitioned a Task from Review → CM In Progress"
# "John Doe transitioned a Bug from *Backlog* → *CM In Progress*"
_TRANSITION_RE = re.compile(
    r'transitioned .+ from .+?[^\w\s]+\s*\*?(?P<to_status>[A-Z][^*\n]+?)\*?\s*$',
    re.MULTILINE,
)

# Matches Jira issue key like SO-14117 or PROJ-123 (with optional Slack link)
_ISSUE_KEY_RE = re.compile(r'\b([A-Z][A-Z0-9]+-\d+)\b')

# Matches Slack hyperlink: <https://...|TEXT>
_SLACK_LINK_RE = re.compile(r'<(https?://[^|>]+)\|([^>]+)>')


def clean_transition_summary(s: str) -> str:
    """The Slack line is "<who> transitioned <title> from <A> → <B>": keep only the ticket title."""
    import re as _re
    s = (s or '').strip(' ·-–—')
    m = _re.search(r'transitioned\s+(.+?)\s+from\s+.+?(?:→|->|⟶|➡)\s*.+$', s)
    return (m.group(1) if m else s).strip(' ·-–—')


def _parse_jira_transition(text: str) -> dict | None:
    """
    Parses a Jira Slack integration message.
    Returns {'issue_key', 'summary', 'to_status', 'url'} or None.
    """
    match = _TRANSITION_RE.search(text)
    if not match:
        return None

    to_status = match.group('to_status').strip()

    # Extract issue key and URL
    issue_key = ''
    url = ''
    # Only a link to the ISSUE counts (".../browse/KEY" or a link whose text is the key). Slack also
    # auto-links words like "Battle.net" in the summary, so the first link is not necessarily the issue.
    for link_match in _SLACK_LINK_RE.finditer(text):
        key_in_link = _ISSUE_KEY_RE.search(link_match.group(2) or '') or _ISSUE_KEY_RE.search(link_match.group(1) or '')
        if key_in_link and ('/browse/' in link_match.group(1) or _ISSUE_KEY_RE.search(link_match.group(2) or '')):
            url = link_match.group(1) if '/browse/' in link_match.group(1) else ''
            issue_key = key_in_link.group(1)
            break

    if not issue_key:
        key_match = _ISSUE_KEY_RE.search(text)
        if key_match:
            issue_key = key_match.group(1)

    if not issue_key:
        return None

    # Summary: everything after "ISSUE-KEY " on that line
    summary = ''
    for line in text.splitlines():
        plain = _SLACK_LINK_RE.sub(lambda m: m.group(2), line).strip()
        if issue_key in plain:
            summary = plain.replace(issue_key, '').strip(' -–—')
            break
    summary = clean_transition_summary(summary)

    return {
        'issue_key': issue_key,
        'to_status': to_status,
        'summary': summary,
        'url': url,
    }


def _transferred_button(issue_key: str, channel: str, ts: str) -> dict:
    return {
        'type': 'actions',
        'elements': [
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': 'Forwarded'},
                'style': 'primary',
                'action_id': 'jira_transferred',
                'value': f'{issue_key}:{channel}:{ts}',
            },
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': 'Closed'},
                'style': 'danger',
                'action_id': 'jira_transferred_closed',
                'value': f'{issue_key}:{channel}:{ts}',
            },
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': 'Dismiss'},
                'action_id': 'jira_dismiss',
                'value': f'{channel}:{ts}',
            },
        ],
    }


def _closed_button(issue_key: str, channel: str, ts: str) -> dict:
    return {
        'type': 'actions',
        'elements': [
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': 'Closed'},
                'style': 'danger',
                'action_id': 'jira_stale_closed',
                'value': f'{issue_key}:{channel}:{ts}',
            },
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': 'Dismiss'},
                'action_id': 'jira_dismiss',
                'value': f'{channel}:{ts}',
            },
        ],
    }


def _format_duration(since) -> str:
    now = datetime.utcnow()
    delta = now - since.replace(tzinfo=None) if since.tzinfo else now - since
    total_seconds = int(delta.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    if days > 0:
        return f'{days}d {hours}h'
    minutes = (total_seconds % 3600) // 60
    return f'{hours}h {minutes}m'


def _comment_user_ids(issue_key: str) -> list:
    """'User ID N' labels for the addresses found in the ticket's comments. An address without
    an account is left out: an email never goes to Slack."""
    labels = []
    for email in get_emails_from_all_comments(issue_key):
        label = label_for_email(email)
        if label and label not in labels:
            labels.append(label)
    return labels


def _send_cm_in_progress_notification(issue_key: str, summary: str, url: str, communication_link: str = None) -> None:
    link = f'<{url}|{issue_key}>' if url else issue_key
    text = f'*Ticket moved to CM In Progress*\n{link} — {summary}'

    last_comment = get_last_comment(issue_key)
    if last_comment:
        text += f'\n\n*Last comment:*\n{replace_emails(last_comment[:500])}'

    if communication_link is None:
        communication_link = get_issue_communication_link(issue_key)
    if communication_link:
        text += f'\n\n*Communication link:* {communication_link}'

    users = _comment_user_ids(issue_key)
    if users:
        text += f'\n\n*Additional users in comments:* {", ".join(users)}'

    try:
        resp = slack_client.chat_postMessage(
            channel=SLACK_JIRA_CHANNEL_ID,
            text=text,
            blocks=[
                {
                    'type': 'section',
                    'text': {'type': 'mrkdwn', 'text': text},
                }
            ],
        )
        ts = resp['ts']
        channel = resp['channel']
        slack_client.chat_update(
            channel=channel,
            ts=ts,
            text=text,
            blocks=[
                {
                    'type': 'section',
                    'text': {'type': 'mrkdwn', 'text': text},
                },
                _transferred_button(issue_key, channel, ts),
            ],
        )
        save_jira_slack_notification_sync(issue_key, channel, ts)
        logger.info(f'[Jira] Sent CM In Progress notification for {issue_key}')
    except Exception as e:
        logger.error(f'[Jira] Failed to send CM In Progress notification for {issue_key}: {e}')

    # Per-user console notifications (jira_cm): fan the CM handoff out to every team member subscribed
    # to it, on their own channels + schedule. Best-effort on a daemon thread so a slow send never
    # blocks this Slack event handler; notify_event itself never raises.
    try:
        import threading
        from services.user_notify_service import notify_event
        body = f'{issue_key}: {summary}' if summary else issue_key
        threading.Thread(target=notify_event, args=('jira_cm', 'Jira handoff to CM', body, url or '', f'#/jira/{issue_key}'),
                         daemon=True, name='jira-cm-notify').start()
    except Exception as e:
        logger.debug(f'[Jira] jira_cm user notify failed to start for {issue_key}: {e}')


def _urg_transferred_button(issue_key: str, rid: str = '') -> dict:
    # channel + ts come from the interaction payload in the handler, so this button ships
    # INSIDE the original message (no fragile post-then-update that intermittently drops the
    # buttons from Jira notifications).
    return {
        'type': 'actions',
        'elements': [
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': 'Done'},
                'style': 'primary',
                'action_id': 'urg_transferred',
                'value': f'{issue_key}|{rid}' if rid else issue_key,
            },
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': 'Dismiss'},
                'action_id': 'urg_dismiss',
                'value': issue_key,
            },
        ],
    }


def _slack_id_for_jira_account(account_id: str) -> str | None:
    """Maps a Jira accountId of one of our team members → their Slack user id."""
    if not account_id:
        return None
    try:
        from services.identity_service import get_jira_our_user_map, get_slack_user_ids
        alias = get_jira_our_user_map().get(account_id)
        if alias:
            return get_slack_user_ids().get(alias)
    except Exception:
        alias = OUR_JIRA_USER_MAP.get(account_id)
        if alias:
            return _SLACK_USER_ID_MAP.get(alias)
    return None


def delete_slack_notification(issue_key: str) -> bool:
    """Delete the stored CM/URG Slack notification for an issue - mirrors the Slack 'Передано' button
    (handlers.action_handlers.handle_jira_transferred) so a transfer done from the console keeps Slack
    in sync. Best-effort: a missing ts is a no-op, not an error."""
    if not slack_client:
        return False
    try:
        from services.mongo_db_service import get_jira_slack_notification_sync
        channel, ts = get_jira_slack_notification_sync(issue_key)
        if not channel or not ts:
            return False
        slack_client.chat_delete(channel=channel, ts=ts)
        logger.info(f'[Jira] Deleted Slack notification for {issue_key} (console transfer)')
        return True
    except Exception as e:
        if 'message_not_found' in str(e):   # already deleted: nothing to do
            logger.debug(f'[Jira] Slack notification for {issue_key} already gone')
        else:
            logger.warning(f'[Jira] Failed to delete Slack notification for {issue_key}: {e}')
        return False


def _urg_creator_mention(issue_key: str) -> str:
    """`<@slack_id>` of the CM who created the URG ticket (empty string if unknown).
    Prefers the creator stored at ticket-creation time; falls back to the Jira reporter."""
    slack_id = None
    try:
        from services.mongo_db_service import get_jira_ticket_creator_sync
        slack_id = get_jira_ticket_creator_sync(issue_key)
    except Exception:
        slack_id = None
    if not slack_id:
        try:
            from services.jira_service import get_issue_reporter
            reporter = get_issue_reporter(issue_key) or {}
            slack_id = _slack_id_for_jira_account(reporter.get('accountId', ''))
        except Exception:
            slack_id = None
    return f'<@{slack_id}>' if slack_id else ''


def _urg_ticket_is_ours(issue_key: str) -> bool:
    """True if the URG ticket was raised by our team, so we only alert on our own urgent
    requests - not every ticket that lands in the URG Review column. Signal: created via our
    Slack modal (creator stored), or the Jira reporter is one of our team members."""
    try:
        from services.mongo_db_service import get_jira_ticket_creator_sync
        if get_jira_ticket_creator_sync(issue_key):
            return True
    except Exception as _e:
        logger.warning("ignored: %s", _e)
    try:
        from services.jira_service import get_issue_reporter
        acc = (get_issue_reporter(issue_key) or {}).get('accountId', '')
        if acc:
            try:
                from services.identity_service import get_jira_our_user_map
                our_map = get_jira_our_user_map()
            except Exception:
                our_map = OUR_JIRA_USER_MAP
            if acc in our_map:
                return True
    except Exception as _e:
        logger.warning("ignored: %s", _e)
    return False


def _send_urg_review_notification(issue_key: str, summary: str, url: str, communication_link: str = None,
                                  update_result: str = '') -> None:
    link = f'<{url}|{issue_key}>' if url else issue_key
    creator = _urg_creator_mention(issue_key)
    header = '*Urgent request in Review*'
    if creator:
        header += f' (created by {creator})'
    text = f'{header}\n{link} - {summary}'

    last_comment = get_last_comment(issue_key)
    if last_comment:
        text += f'\n\n*Last comment:*\n{replace_emails(last_comment[:500])}'

    if communication_link is None:
        communication_link = get_issue_communication_link(issue_key)
    if communication_link:
        text += f'\n\n*Communication link:* {communication_link}'

    # One message per user to tell (main user + every "Additional user" we added). The ticket goes to
    # Done only when ALL of them are transferred (see handlers.action_handlers.handle_urg_transferred).
    try:
        from services.urg_recipients import fetch_recipients, set_slack, close_own
        all_recs = fetch_recipients(issue_key)
        # "My Own Ticket" users (a CM's own session): no one to reply to -> transferred automatically and
        # the creator gets the result. If nobody else is waiting, the ticket goes straight to Done.
        own_n, own_left = close_own(issue_key, summary, url, update_result, last_comment or '')
        if own_n and not own_left:
            from services.jira_service import transition_to_done
            transition_to_done(issue_key)
            try:
                from services.mongo_db_service import mark_console_jira_handled_sync
                mark_console_jira_handled_sync(issue_key)
            except Exception as _e:
                logger.warning("ignored: %s", _e)
            logger.info(f'[Jira] URG {issue_key}: own ticket closed automatically at Review')
            return
        recs = [r for r in all_recs if not r.get('transferred') and not r.get('own')]
        if recs and not any(r.get('ours') for r in recs):
            recs[0]['ours'] = True   # our ticket but the reporter is not mapped: at least the main user
            from services.urg_recipients import _col
            _col().update_one({'_id': recs[0]['_id']}, {'$set': {'ours': True}})
        recs = [r for r in recs if r.get('ours')]
    except Exception as e:
        logger.error(f'[Jira] URG recipients failed for {issue_key}: {e}')
        recs = []
    if len(recs) > 1:
        for i, rc in enumerate(recs, 1):
            who = f"\n\n*User {i} of {len(recs)}:* {rc.get('email') or 'no email'}"
            if rc.get('link'):
                who += f"\n*Communication link:* {rc['link']}"
            body = text.split('\n\n*Communication link:*')[0] + who
            try:
                resp = slack_client.chat_postMessage(
                    channel=SLACK_JIRA_CHANNEL_ID, text=body,
                    blocks=[{'type': 'section', 'text': {'type': 'mrkdwn', 'text': body}},
                            _urg_transferred_button(issue_key, rc['rid'])])
                set_slack(issue_key, rc['rid'], resp['channel'], resp['ts'])
            except Exception as e:
                logger.error(f'[Jira] Failed to send URG Review notification for {issue_key} ({rc["rid"]}): {e}')
        logger.info(f'[Jira] Sent {len(recs)} URG Review notifications for {issue_key}')
        recs = None
    try:
        if recs is None:
            raise StopIteration
        # Post message AND button in a single call - the button carries only the issue key,
        # and the handler reads channel/ts from the interaction payload, so nothing can drop
        # the button between a post and a follow-up update.
        resp = slack_client.chat_postMessage(
            channel=SLACK_JIRA_CHANNEL_ID,
            text=text,
            blocks=[
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
                _urg_transferred_button(issue_key, recs[0]['rid'] if recs else ''),
            ],
        )
        save_jira_slack_notification_sync(issue_key, resp['channel'], resp['ts'])
        if recs:
            from services.urg_recipients import set_slack
            set_slack(issue_key, recs[0]['rid'], resp['channel'], resp['ts'])
        logger.info(f'[Jira] Sent URG Review notification for {issue_key}')
    except StopIteration:
        pass
    except Exception as e:
        logger.error(f'[Jira] Failed to send URG Review notification for {issue_key}: {e}')

    # Per-user console notifications (jira_urg_review): fan the URG Review handoff out to subscribed
    # team members. Best-effort on a daemon thread so a slow send never blocks this Slack handler.
    try:
        import threading
        from services.user_notify_service import notify_event
        body = f'{issue_key}: {summary}' if summary else issue_key
        threading.Thread(target=notify_event, args=('jira_urg_review', 'Urgent request in Review', body, url or '', '#/inbox'),
                         daemon=True, name='jira-urg-notify').start()
    except Exception as e:
        logger.debug(f'[Jira] jira_urg_review user notify failed to start for {issue_key}: {e}')


# ── System notes into the linked conversation ────────────────────────
# When a ticket linked to a Reddit modmail convo / Discord ticket changes status, drop an internal
# "system" note into that conversation so the team sees the ticket's progress inside the chat. Reddit
# mods-only notes are mirrored into the snapshot and Discord internal notes are stored on the ticket,
# so these show up in the console too. All best-effort - a failure here never affects the Slack flow.
def _system_note_text(issue_key: str, to_status: str, url: str, update_result: str = '') -> str:
    if _is_urg_key(issue_key) and to_status == URG_REVIEW_STATUS:
        base = f'[System] Urgent request {issue_key} is in Review. Please notify the user'
        if update_result:   # the resolution chosen on the URG ticket (Fixed / Disabled / ...)
            base += f'\nUpdate result: {update_result}'
    else:
        base = f'[System] Linked Jira {issue_key} moved to {to_status}'
    return base + (f'\n{url}' if url else '')


def _notify_linked_reddit(issue_key: str, note: str, communication_link: str) -> None:
    # Console-ONLY mods-only note: we do NOT write to reddit.com (automated Reddit notes stay
    # disabled). Instead we record the note in `reddit_convo_notes`; the console merges it into the
    # modmail transcript (api/routes/console_api.py::reddit_modmail_detail) so it shows in the chat and
    # survives snapshot rebuilds. The linked convo is taken from the ticket's Communication Link.
    convo_id = parse_reddit_convo_id(communication_link) if communication_link else None   # imported at module top
    if not convo_id:
        return
    try:
        from services.mongo_db_service import sync_db
        col = sync_db['reddit_convo_notes']
        now = datetime.utcnow()
        # Skip an exact duplicate from a redelivered Slack event within the last 2 minutes.
        if col.find_one({'convo_id': convo_id, 'body': note, 'created_utc': {'$gte': now - timedelta(minutes=2)}}):
            return
        col.insert_one({
            'convo_id': convo_id,
            'issue_key': issue_key,
            'author': 'System',
            'body': note,
            'is_internal': True,
            'date': now.replace(microsecond=0).isoformat() + 'Z',
            'created_utc': now,
        })
        logger.info(f'[Jira] Console note recorded for Reddit convo {convo_id} ({issue_key})')
    except Exception as e:
        logger.warning(f'[Jira] reddit console note failed for {issue_key}: {e}')


def _notify_linked_discord(issue_key: str, note: str) -> None:
    import asyncio
    from services.mongo_db_service import sync_db
    from discord_notifications.discord_notifications import get_running_discord_bot
    try:
        # Match the fields the auto-link actually sets: jira_ticket_key (primary scalar)
        # and jira_ticket_keys (array of all linked keys). The legacy 'jira_key' field is
        # never written by the bot, so querying it alone found nothing.
        tickets = list(sync_db['discord_tickets'].find({
            '$or': [
                {'jira_ticket_key': issue_key},
                {'jira_ticket_keys': issue_key},
                {'jira_key': issue_key},
            ]
        }))
    except Exception as e:
        logger.warning(f'[Jira] discord ticket lookup failed for {issue_key}: {e}')
        return
    if not tickets:
        return
    bot = get_running_discord_bot()
    if not bot or getattr(bot, 'loop', None) is None or bot.is_closed():
        logger.warning('[Jira] discord bot not ready for system note')
        return
    cog = bot.get_cog('ModMail')
    if not cog:
        return
    for t in tickets:
        try:
            fut = asyncio.run_coroutine_threadsafe(
                cog.send_console_reply(t, note, 'System', True), bot.loop)
            fut.result(timeout=30)
            logger.info(f'[Jira] System note posted to Discord ticket {t.get("_id")} for {issue_key}')
        except Exception as e:
            logger.warning(f'[Jira] system note to discord ticket {t.get("_id")} failed: {e}')


# In-memory dedupe so a redelivered Slack event doesn't double-post the chat note. Covers BOTH
# Discord and Reddit (Reddit also has a persistent Mongo backstop for cross-restart dupes). Per-process,
# which is fine - handle_jira_channel_message runs only on the bot.
_LAST_LINKED_NOTE = {}   # issue_key -> (to_status, datetime)


def _notify_linked_conversations(issue_key: str, to_status: str, url: str, communication_link: str,
                                 update_result: str = '') -> None:
    prev = _LAST_LINKED_NOTE.get(issue_key)
    now = datetime.utcnow()
    if prev and prev[0] == to_status and (now - prev[1]) < timedelta(minutes=2):
        return   # same transition seen <2 min ago (redelivered event) -> don't double-notify
    _LAST_LINKED_NOTE[issue_key] = (to_status, now)
    note = _system_note_text(issue_key, to_status, url, update_result)
    try:
        _notify_linked_reddit(issue_key, note, communication_link)
    except Exception as e:
        logger.warning(f'[Jira] linked Reddit notify failed for {issue_key}: {e}')
    try:
        _notify_linked_discord(issue_key, note)
    except Exception as e:
        logger.warning(f'[Jira] linked Discord notify failed for {issue_key}: {e}')


def handle_jira_channel_message(event: dict) -> None:
    """
    Called from the Slack message event handler.
    Filters messages from the jira-notifications channel and processes transitions.
    """
    if event.get('channel') != SLACK_JIRA_NOTIFICATIONS_CHANNEL_ID:
        return
    if not SLACK_JIRA_CHANNEL_ID:
        return

    text = event.get('text', '')
    if not text:
        for attachment in event.get('attachments', []):
            text += ' ' + attachment.get('text', '') + ' ' + attachment.get('fallback', '')
        text = text.strip()

    if not text:
        return

    parsed = _parse_jira_transition(text)
    if not parsed:
        return

    issue_key = parsed['issue_key']
    to_status = parsed['to_status']
    summary = parsed['summary']
    url = parsed['url'] or (f'{JIRA_BASE_URL}/browse/{issue_key}' if JIRA_BASE_URL else '')

    # Always upsert to keep status_since accurate
    upsert_jira_ticket_sync(issue_key, to_status, summary, url)
    # Title index for the console's "Link Jira Ticket" search (every ticket we see, with its status).
    try:
        from services.jira_so_index import upsert as _index_ticket
        _index_ticket(issue_key, summary, to_status, source='slack')
    except Exception as e:
        logger.debug(f'[Jira] title index failed for {issue_key}: {e}')
    # Mirror the transition into the CONSOLE's own store so the console Jira tab shows it LIVE (no
    # manual Sync). status_since must be Jira's `updated` (what the console Sync + its 'handled'
    # matching use), so we fetch it. Best-effort - a mirror failure must never block the Slack path.
    try:
        from services.jira_service import get_issue_updated
        from services.mongo_db_service import mirror_console_jira_ticket_sync
        mirror_console_jira_ticket_sync(issue_key, to_status, summary, url, get_issue_updated(issue_key))
    except Exception as e:
        logger.debug(f'[Jira] console mirror failed for {issue_key}: {e}')

    # Always store Discord channel ID from Communication Link (enables early auto-link)
    communication_link = get_issue_communication_link(issue_key)
    if communication_link:
        discord_channel_id = parse_discord_channel_id(communication_link)
        if discord_channel_id:
            set_jira_discord_channel_sync(issue_key, discord_channel_id)
        # Reddit modmail auto-link (mirrors the Discord auto-link above)
        reddit_convo_id = parse_reddit_convo_id(communication_link)
        if reddit_convo_id:
            try:
                from reddit_notifications.mod_notes import auto_link_jira_from_convo
                auto_link_jira_from_convo(issue_key, reddit_convo_id)
            except Exception as e:
                logger.error(f'[Jira] Reddit auto-link failed for {issue_key}: {e}')

    # System note + review alert fire only when the ticket reaches its "review" column
    # (URG: Review, SO: CM In Progress). For URG we act ONLY on our own tickets (raised by our
    # team) so we don't alert on every unrelated ticket that lands in the URG Review column.
    is_urg = _is_urg_key(issue_key)
    review_status = URG_REVIEW_STATUS if is_urg else CM_IN_PROGRESS
    reached_review = (to_status == review_status)
    urg_ours = _urg_ticket_is_ours(issue_key) if (is_urg and reached_review) else False

    # For a URG ticket reaching Review, read its "Update result" (customfield_10478) so the resolution
    # (Fixed / Disabled / Started maintenance / No replication) is shown in the linked-chat system note.
    update_result = ''
    if is_urg and reached_review:
        try:
            from services.jira_service import get_issue_update_result
            update_result = get_issue_update_result(issue_key)
        except Exception as e:
            logger.debug(f'[Jira] update result fetch failed for {issue_key}: {e}')

    # Mods-only system note into the linked Reddit convo / Discord ticket on EVERY status change of a
    # linked ticket, so the team sees the ticket's progress inside the chat - not only when it
    # reaches the review column. Self-gates: a no-op when nothing is linked to this issue key.
    _notify_linked_conversations(issue_key, to_status, url, communication_link, update_result=update_result)

    if is_urg:
        # URG (Urgent Request) board - its own Review → Передано → Done lifecycle, our tickets only.
        if reached_review and urg_ours:
            _send_urg_review_notification(issue_key, summary, url, communication_link=communication_link,
                                          update_result=update_result)
    else:
        # SO board flow (gated to non-URG so a URG ticket never triggers these).
        if to_status == L1_IN_PROGRESS:
            # Users our team promised to update (User IDs / sessions / chats in our comments + linked chats):
            # one To-Do entry each in the console + one notification to whoever is on shift.
            try:
                import threading
                from services.jira_l1_followups import handle as _l1_followups
                threading.Thread(target=_l1_followups, args=(issue_key, summary, url, communication_link or ''),
                                 daemon=True, name='jira-l1-followups').start()
            except Exception as e:
                logger.warning(f'[Jira] L1 follow-ups failed to start for {issue_key}: {e}')

        if to_status == CM_IN_PROGRESS:
            _send_cm_in_progress_notification(issue_key, summary, url, communication_link=communication_link)

        if to_status == BACKLOG:
            # Ticket moved back to Backlog - Discord bot should re-enter jira-waiting mode
            mark_jira_ticket_needs_postpone_sync(issue_key)


def check_and_notify_stale_tickets() -> None:
    """Checks MongoDB for tickets stuck in CM In Progress for 5+ days and notifies."""
    if not SLACK_JIRA_CHANNEL_ID:
        return
    try:
        stale = get_stale_cm_tickets_sync(older_than_days=STALE_DAYS)
        now = datetime.utcnow()
        for doc in stale:
            last_notified = doc.get('last_stale_notified')
            if last_notified:
                age = now - last_notified.replace(tzinfo=None) if last_notified.tzinfo else now - last_notified
                if age < timedelta(hours=6):
                    continue

            issue_key = doc['issue_key']

            live_status = get_jira_issue_status(issue_key)
            if live_status and live_status != CM_IN_PROGRESS:
                logger.info(f'[Jira] Skipping stale alert for {issue_key} — live status is "{live_status}", not CM In Progress')
                upsert_jira_ticket_sync(issue_key, live_status, doc.get('summary', ''), doc.get('url', ''))
                continue

            summary = doc.get('summary', '')
            url = doc.get('url', '')
            status_since = doc.get('status_since')
            duration = _format_duration(status_since) if status_since else '5d+'

            link = f'<{url}|{issue_key}>' if url else issue_key
            text = (
                f'⏰ *Ticket stuck in CM In Progress*\n'
                f'{link} — {summary}\n'
                f'In this column for *{duration}*'
            )
            try:
                resp = slack_client.chat_postMessage(
                    channel=SLACK_JIRA_CHANNEL_ID,
                    text=text,
                    blocks=[{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}],
                )
                ts = resp['ts']
                channel = resp['channel']
                slack_client.chat_update(
                    channel=channel,
                    ts=ts,
                    text=text,
                    blocks=[
                        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}},
                        _closed_button(issue_key, channel, ts),
                    ],
                )
                update_jira_stale_notified_sync(issue_key, channel, ts)
                logger.info(f'[Jira] Sent stale alert for {issue_key} ({duration})')
            except Exception as e:
                logger.error(f'[Jira] Failed to send stale alert for {issue_key}: {e}')
    except Exception as e:
        logger.error(f'[Jira] Error in stale ticket check: {e}')
