"""Maintains a single pinned, auto-updating Slack message in the bot's Jira alerts
channel (SLACK_JIRA_CHANNEL_ID) listing all open Jira tickets whose Communication Link
is 'Facebook'.

Pattern mirrors services/maintenance_service.py: store the message (channel, ts) in Mongo
once, then chat_update it in place on every refresh; pin it once on first post.
"""

import os
import logging
from datetime import datetime
from pathlib import Path

import pytz
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from services.jira_service import get_open_facebook_issues
from services.mongo_db_service import get_slack_pin_sync, save_slack_pin_sync

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

_slack_client = WebClient(token=os.getenv('SLACK_TOKEN'))
SLACK_JIRA_CHANNEL_ID = os.getenv('SLACK_JIRA_CHANNEL_ID')
JIRA_BASE_URL = os.getenv('JIRA_BASE_URL', '').rstrip('/')

_PIN_KEY = 'facebook_open'
_MAX_SECTION_CHARS = 2900  # Slack hard-limits a section's text to 3000 chars


def _now_str() -> str:
    return datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M %Z')


def _issue_line(issue: dict) -> str:
    key = issue.get('key', '')
    url = f'{JIRA_BASE_URL}/browse/{key}' if JIRA_BASE_URL else key
    status = issue.get('status', '')
    summary = (issue.get('summary', '') or '').strip()
    if len(summary) > 100:
        summary = summary[:97] + '...'
    return f'• <{url}|{key}> — {summary} _({status})_'


def _build_message(issues: list) -> tuple[str, list]:
    """Builds (fallback_text, blocks) for the pinned message, chunking long bodies."""
    header = f'*Open Facebook tickets ({len(issues)})*'
    blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': header}}]

    if not issues:
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': '_No open Facebook tickets right now._'}})
        body_text = 'No open Facebook tickets right now.'
    else:
        lines = [_issue_line(it) for it in issues]
        body_text = '\n'.join(lines)
        # Chunk lines into section blocks that stay under Slack's per-section limit.
        chunk = ''
        for line in lines:
            if chunk and len(chunk) + len(line) + 1 > _MAX_SECTION_CHARS:
                blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': chunk}})
                chunk = line
            else:
                chunk = f'{chunk}\n{line}' if chunk else line
        if chunk:
            blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': chunk}})

    blocks.append({
        'type': 'context',
        'elements': [{'type': 'mrkdwn', 'text': f'Auto-updated · {_now_str()}'}],
    })
    fallback = f'Open Facebook tickets ({len(issues)})\n{body_text}'
    return fallback, blocks


# Fingerprint of the last text written to the pin: the 15-min refresh only calls Slack when the content
# actually changed (otherwise it would rewrite the same message ~96 times a day). In-memory, so the first
# refresh after a restart always writes once.
_last_fingerprint = None


def refresh_facebook_pin() -> None:
    """Refreshes (or first-time posts + pins) the Facebook-open-tickets message."""
    if not SLACK_JIRA_CHANNEL_ID:
        logger.warning('[FacebookPin] SLACK_JIRA_CHANNEL_ID not set — skipping refresh')
        return
    try:
        issues = get_open_facebook_issues()
        text, blocks = _build_message(issues)

        pin = get_slack_pin_sync(_PIN_KEY)
        ts = pin.get('slack_ts') if pin else None
        channel = pin.get('slack_channel') if pin else None

        global _last_fingerprint
        import hashlib, json as _json
        # Without the trailing "Auto-updated · <time>" context block: it changes on every build.
        content = [b for b in blocks if b.get('type') != 'context']
        fingerprint = hashlib.sha1(_json.dumps([text, content], sort_keys=True, default=str).encode()).hexdigest()

        if ts and channel:
            if fingerprint == _last_fingerprint:
                return   # nothing changed since the last write
            try:
                _slack_client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
                _last_fingerprint = fingerprint
                logger.info(f'[FacebookPin] Updated pinned message ({len(issues)} tickets)')
                return
            except SlackApiError as e:
                err = e.response.get('error') if e.response is not None else ''
                if err in ('message_not_found', 'cant_update_message'):
                    logger.warning(f'[FacebookPin] Stored message unusable ({err}) — reposting')
                else:
                    raise

        # First run, or the previous message is gone → post fresh and pin once.
        resp = _slack_client.chat_postMessage(channel=SLACK_JIRA_CHANNEL_ID, text=text, blocks=blocks)
        new_ts = resp.get('ts')
        new_channel = resp.get('channel', SLACK_JIRA_CHANNEL_ID)
        save_slack_pin_sync(_PIN_KEY, new_channel, new_ts)
        _last_fingerprint = fingerprint
        try:
            _slack_client.pins_add(channel=new_channel, timestamp=new_ts)
        except SlackApiError as e:
            err = e.response.get('error') if e.response is not None else ''
            if err != 'already_pinned':
                logger.warning(f'[FacebookPin] Could not pin message: {err}')
        logger.info(f'[FacebookPin] Posted and pinned new message ({len(issues)} tickets)')
    except Exception as e:
        logger.error(f'[FacebookPin] Failed to refresh pinned message: {e}')
