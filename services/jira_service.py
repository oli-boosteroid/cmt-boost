import os
import re
import logging
from base64 import b64encode
from enum import Enum
from pathlib import Path

import requests
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

JIRA_BASE_URL = os.getenv('JIRA_BASE_URL', '').rstrip('/')
JIRA_API_EMAIL = os.getenv('JIRA_API_EMAIL', '')
JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN', '')
JIRA_COMMUNICATION_LINK_FIELD = os.getenv('JIRA_COMMUNICATION_LINK_FIELD', 'communication_link')
JIRA_PROJECT_KEY = os.getenv('JIRA_PROJECT_KEY', 'SO')

# URG (Urgent Request) board - duplicate detection uses two text custom fields
JIRA_URG_PROJECT_KEY = os.getenv('JIRA_URG_PROJECT_KEY', 'URG')
JIRA_URG_APP_IDS_FIELD = os.getenv('JIRA_URG_APP_IDS_FIELD', 'customfield_10410')
JIRA_URG_PLAYGROUNDS_FIELD = os.getenv('JIRA_URG_PLAYGROUNDS_FIELD', 'customfield_10411')

# Jira accountIds whose presence as last commenter means we skip posting a new comment
JIRA_OUT_USER_IDS: set[str] = {
    s.strip() for s in os.getenv('JIRA_OUT_USERS', '').split(',') if s.strip()
}
JIRA_L1_TAGS_FIELD = 'customfield_10209'


class JiraL1Tags(Enum):
    """Values of the L1 Tags custom field (Name: option ID)."""
    NATIVE_DESKTOP = "10111"
    NATIVE_ANDROID = "10112"
    WEBCLIENT      = "10113"
    WEB_TV         = "10114"
    GAME           = "10115"
    LAUNCHER       = "10120"
    SUBSCRIPTION   = "10116"
    STREAM         = "10117"
    WEB_BACKEND    = "10118"
    TBD            = "10119"


# Maps the lowercase string used in Slack modal options → JiraL1Tags enum member
L1_TAG_NAME_TO_ENUM: dict[str, JiraL1Tags] = {
    "native_desktop": JiraL1Tags.NATIVE_DESKTOP,
    "native_android": JiraL1Tags.NATIVE_ANDROID,
    "webclient":      JiraL1Tags.WEBCLIENT,
    "web_tv":         JiraL1Tags.WEB_TV,
    "game":           JiraL1Tags.GAME,
    "launcher":       JiraL1Tags.LAUNCHER,
    "subscription":   JiraL1Tags.SUBSCRIPTION,
    "stream":         JiraL1Tags.STREAM,
    "web_backend":    JiraL1Tags.WEB_BACKEND,
    "tbd":            JiraL1Tags.TBD,
}


def _auth_headers() -> dict:
    token = b64encode(f'{JIRA_API_EMAIL}:{JIRA_API_TOKEN}'.encode()).decode()
    return {
        'Authorization': f'Basic {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def _configured() -> bool:
    if not JIRA_BASE_URL or not JIRA_API_EMAIL or not JIRA_API_TOKEN:
        logger.warning('[Jira] API credentials not configured (JIRA_BASE_URL / JIRA_API_EMAIL / JIRA_API_TOKEN)')
        return False
    return True


_MY_JIRA_ACCOUNT_ID = None


def get_my_account_id() -> str:
    """accountId of the Jira account the bot posts as (JIRA token owner). Cached after first lookup."""
    global _MY_JIRA_ACCOUNT_ID
    if _MY_JIRA_ACCOUNT_ID is not None:
        return _MY_JIRA_ACCOUNT_ID
    _MY_JIRA_ACCOUNT_ID = ''
    if not _configured():
        return ''
    try:
        resp = requests.get(f'{JIRA_BASE_URL}/rest/api/2/myself', headers=_auth_headers(), timeout=10)
        if resp.status_code == 200:
            _MY_JIRA_ACCOUNT_ID = (resp.json() or {}).get('accountId', '') or ''
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch own accountId: {e}')
    return _MY_JIRA_ACCOUNT_ID


def post_jira_comment(issue_key: str, text: str, dedupe: bool = True) -> bool:
    if not _configured():
        return False
    # Anti-duplicate: never post the same comment twice in a row from our own account (double button
    # press, retried worker action, Slack + console both firing, etc.). Covers EVERY caller since all
    # Jira comments go through here. Best-effort - on any check error we fall through and post normally.
    if dedupe:
        try:
            url = f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment?orderBy=-created&maxResults=1"
            resp = requests.get(url, headers=_auth_headers(), timeout=10)
            if resp.status_code == 200:
                comments = resp.json().get("comments", [])
                if comments and (comments[0].get("body") or "").strip() == (text or "").strip():
                    my_id = get_my_account_id()
                    author_id = (comments[0].get("author") or {}).get("accountId", "")
                    if not my_id or author_id == my_id:
                        logger.info(f'[Jira] Skipped duplicate comment on {issue_key} (identical to our last): "{text}"')
                        return True   # already there -> treat as success
        except Exception as e:
            logger.warning(f'[Jira] dedupe check failed for {issue_key}: {e}')
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment'
        resp = requests.post(url, json={'body': text}, headers=_auth_headers(), timeout=10)
        resp.raise_for_status()
        logger.info(f'[Jira] Posted comment to {issue_key}: "{text}"')
        return True
    except Exception as e:
        logger.error(f'[Jira] Failed to post comment to {issue_key}: {e}')
        return False


def get_last_comment(issue_key: str) -> str:
    """Returns the body of the latest comment on the issue, or '' if none/error."""
    if not _configured():
        return ""
    try:
        url = f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment?orderBy=-created&maxResults=1"
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return ""
        comments = resp.json().get("comments", [])
        return comments[0].get("body", "") if comments else ""
    except Exception as e:
        logger.error(f"[Jira] Failed to fetch last comment for {issue_key}: {e}")
        return ""


def get_jira_issue_status(issue_key: str) -> str:
    """Returns the current status name of the Jira issue, or '' on error."""
    if not _configured():
        return ''
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}?fields=status'
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return ''
        return resp.json().get('fields', {}).get('status', {}).get('name', '')
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch status for {issue_key}: {e}')
        return ''


def is_issue_done(issue_key: str) -> bool:
    """True if the issue's status is in the 'Done' status category (closed/resolved/done)."""
    if not _configured():
        return False
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}?fields=status'
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return False
        status = resp.json().get('fields', {}).get('status', {}) or {}
        category = (status.get('statusCategory') or {}).get('key', '')
        return category == 'done'
    except Exception as e:
        logger.error(f'[Jira] Failed to check done state for {issue_key}: {e}')
        return False


def get_last_comment_author_id(issue_key: str) -> str:
    """Returns the accountId of the last comment's author, or '' if none/error."""
    if not _configured():
        return ''
    try:
        url = f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment?orderBy=-created&maxResults=1"
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return ''
        comments = resp.json().get('comments', [])
        if not comments:
            return ''
        return comments[0].get('author', {}).get('accountId', '')
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch last comment author for {issue_key}: {e}')
        return ''


def is_last_comment_from_out_user(issue_key: str) -> bool:
    """Returns True if the last Jira comment was written by a JIRA_OUT_USERS account."""
    if not JIRA_OUT_USER_IDS:
        return False
    return get_last_comment_author_id(issue_key) in JIRA_OUT_USER_IDS


def transition_to_done(issue_key: str) -> bool:
    if not _configured():
        return False
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/transitions'
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        resp.raise_for_status()
        transitions = resp.json().get('transitions', [])

        done_id = None
        for t in transitions:
            if t.get('name', '').lower() == 'done':
                done_id = t['id']
                break

        if not done_id:
            available = [t.get('name') for t in transitions]
            logger.warning(f'[Jira] No "Done" transition for {issue_key}. Available: {available}')
            return False

        resp = requests.post(
            url,
            json={'transition': {'id': done_id}},
            headers=_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f'[Jira] Transitioned {issue_key} to Done')
        return True
    except Exception as e:
        logger.error(f'[Jira] Failed to transition {issue_key} to Done: {e}')
        return False


def get_issue_communication_link(issue_key: str) -> str:
    """Returns the value of the Communication Link custom field, or '' if absent/error."""
    if not _configured():
        return ''
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}?fields={JIRA_COMMUNICATION_LINK_FIELD}'
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return ''
        return resp.json().get('fields', {}).get(JIRA_COMMUNICATION_LINK_FIELD, '') or ''
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch communication link for {issue_key}: {e}')
        return ''


def get_issue_updated(issue_key: str) -> str:
    """Returns the issue's Jira `updated` timestamp string, or ''. This is exactly what the console's
    manual Sync stores as status_since (_scan_jira_team), so mirroring a transition with this value
    keeps the console's cache + its 'handled' matching consistent."""
    if not _configured():
        return ''
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}?fields=updated'
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return ''
        return resp.json().get('fields', {}).get('updated', '') or ''
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch updated for {issue_key}: {e}')
        return ''


# URG "Update result" custom field (the resolution chosen when a URG ticket reaches Review). Jira
# normally returns the option's display `value`; the id map is a fallback if only the id comes back.
JIRA_URG_UPDATE_RESULT_FIELD = os.getenv('JIRA_URG_UPDATE_RESULT_FIELD', 'customfield_10478')
_URG_UPDATE_RESULT_BY_ID = {
    '10300': 'Fixed',
    '10335': 'Disabled',
    '10301': 'Started maintenance',
    '10302': 'No replication',
}


class JiraURGTicketType(Enum):
    """
    Values of the URG ticket type field (customfield_10610) for Jira URG tickets (Name: ID)
    """

    Update = "10368"
    Verification = "10369"


JIRA_URG_TICKET_TYPE_FIELD = 'customfield_10610'


def urg_ticket_type_enabled() -> bool:
    """Verification URG tickets are allowed only while the admin switch in the console Urgent Request form
    (console_config urg_verification) is on. Update is always sent: the field is required in Jira."""
    try:
        from services.mongo_db_service import sync_db
        doc = sync_db['console_config'].find_one({'key': 'urg_verification'}) or {}
        return bool((doc.get('value') or {}).get('enabled'))
    except Exception:
        return False


def get_issue_update_result(issue_key: str) -> str:
    """Returns the URG 'Update result' field (customfield_10478) as a human-readable label
    (e.g. 'Fixed'), or '' if absent/error. Surfaced when a URG ticket reaches Review, in the To-Do
    and in the linked-chat system note (Discord + Reddit)."""
    if not _configured():
        return ''
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}?fields={JIRA_URG_UPDATE_RESULT_FIELD}'
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return ''
        val = resp.json().get('fields', {}).get(JIRA_URG_UPDATE_RESULT_FIELD)
        if isinstance(val, dict):
            return val.get('value') or _URG_UPDATE_RESULT_BY_ID.get(str(val.get('id') or ''), '')
        if isinstance(val, str):
            return val
        return ''
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch update result for {issue_key}: {e}')
        return ''


def get_recently_updated_issues(minutes: int = 6) -> list:
    """Returns issues in the project updated in the last N minutes (reporter only, no comments)."""
    if not _configured():
        return []
    try:
        jql = f'project = {JIRA_PROJECT_KEY} AND updated >= "-{minutes}m"'
        url = f'{JIRA_BASE_URL}/rest/api/3/search/jql'
        resp = requests.get(
            url,
            params={'jql': jql, 'fields': 'key,summary,reporter', 'maxResults': 50},
            headers=_auth_headers(),
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error(f'[Jira] get_recently_updated_issues: {resp.status_code} {resp.text[:200]}')
            return []
        return resp.json().get('issues', [])
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch recently updated issues: {e}')
        return []


def _split_field_tokens(val) -> list[str]:
    """Normalizes an app_ids / playgrounds custom-field value into a lowercase token list.
    Handles text (comma/newline/slash/semicolon-separated), a select option, or a list."""
    if val is None:
        return []
    if isinstance(val, dict):
        parts = [val.get('value') or val.get('name') or '']
    elif isinstance(val, list):
        parts = []
        for v in val:
            parts.append((v.get('value') or v.get('name') or '') if isinstance(v, dict) else str(v))
    else:
        parts = re.split(r'[,\n/;]+', str(val))
    return [p.strip().lower() for p in parts if p and p.strip()]


def find_recent_urg_duplicate(app_ids, playgrounds, app_name: str = '', minutes: int = 60) -> str | None:
    """Returns the key of a URG ticket created in the last `minutes` that matches the request (same
    playground AND a shared app_id OR the same game), or None. Used to avoid duplicate urgent requests -
    the caller adds the user to the existing ticket instead of creating a new one.

    Checks the local store FIRST (immediately consistent), because Jira's search index lags a few
    seconds/minutes behind creation and console-created URG tickets don't fill the app_ids/playgrounds
    custom fields the JQL below reads - both of which let duplicates slip through. Falls back to JQL to
    also catch tickets created manually in Jira (which won't be in the local store)."""
    try:
        from services.mongo_db_service import find_recent_urg_local
        local_key = find_recent_urg_local(app_ids, playgrounds, app_name=app_name, minutes=minutes)
        if local_key:
            return local_key
    except Exception as e:
        logger.error(f'[Jira] URG local dedup check failed: {e}')
    if not _configured():
        return None
    app_set = {str(a).strip().lower() for a in (app_ids or []) if str(a).strip()}
    pg_set = {str(p).strip().lower() for p in (playgrounds or []) if str(p).strip()}
    if not app_set or not pg_set:
        return None
    want_pairs = {(a, p) for a in app_set for p in pg_set}
    # Any OPEN URG ticket counts, whoever made it (our bot, the L1 bot, by hand) and however old: not Done
    # and no Update result yet = the problem is not fixed, so the new user is added to it as a comment.
    # Tickets from other sources may not fill our app_ids / playgrounds fields, so the title
    # "Game [ID: 1121] - Allentown/Paris" is parsed as well.
    try:
        jql = f'project = {JIRA_URG_PROJECT_KEY} AND statusCategory != Done ORDER BY created DESC'
        url = f'{JIRA_BASE_URL}/rest/api/3/search/jql'
        resp = requests.get(
            url,
            params={'jql': jql, 'maxResults': 100,
                    'fields': f'key,summary,status,{JIRA_URG_APP_IDS_FIELD},{JIRA_URG_PLAYGROUNDS_FIELD},{JIRA_URG_UPDATE_RESULT_FIELD}'},
            headers=_auth_headers(),
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error(f'[Jira] find_recent_urg_duplicate: {resp.status_code} {resp.text[:200]}')
            return None
        for issue in resp.json().get('issues', []):
            fields = issue.get('fields', {})
            if fields.get(JIRA_URG_UPDATE_RESULT_FIELD):
                continue   # already has a result (Fixed / Disabled / ...) -> not "still broken"
            if str(((fields.get('status') or {}).get('name') or '')).strip().lower() == 'review':
                continue
            cand_apps = set(_split_field_tokens(fields.get(JIRA_URG_APP_IDS_FIELD)))
            cand_pgs = set(_split_field_tokens(fields.get(JIRA_URG_PLAYGROUNDS_FIELD)))
            summ = str(fields.get('summary') or '')
            m = re.search(r'\[\s*ID\s*:\s*(\d+)\s*\]', summ, re.I)
            if m:
                cand_apps.add(m.group(1).lower())
            if ' - ' in summ:
                for pg in summ.rsplit(' - ', 1)[1].split('/'):
                    if pg.strip():
                        cand_pgs.add(pg.strip().lower())
            if want_pairs & {(a, p) for a in cand_apps for p in cand_pgs}:
                return issue.get('key')
        return None
    except Exception as e:
        logger.error(f'[Jira] Failed to search URG duplicates: {e}')
        return None


def _comm_link_is_facebook(value) -> bool:
    """True if a Communication Link field value represents 'Facebook'.

    Handles a select option ({'value': 'Facebook'}), a multi-select (list), and plain
    text, matching case-insensitively so field-type differences don't matter.
    """
    if not value:
        return False
    if isinstance(value, dict):
        candidate = value.get('value') or value.get('name') or ''
    elif isinstance(value, list):
        return any(_comm_link_is_facebook(v) for v in value)
    else:
        candidate = str(value)
    return 'facebook' in candidate.strip().lower()


def get_open_facebook_issues() -> list:
    """Returns open issues (statusCategory != Done) whose Communication Link is 'Facebook'.

    The field value is filtered in Python (see _comm_link_is_facebook) so it works whether
    the custom field is a select option or plain text - avoiding JQL custom-field pitfalls.
    Each item: {'key', 'summary', 'status'}.
    """
    if not _configured():
        return []
    field = JIRA_COMMUNICATION_LINK_FIELD
    jql = f'project = {JIRA_PROJECT_KEY} AND statusCategory != Done ORDER BY updated DESC'
    url = f'{JIRA_BASE_URL}/rest/api/3/search/jql'
    results = []
    next_token = None
    pages = 0
    try:
        while True:
            params = {
                'jql': jql,
                'fields': f'key,summary,status,{field}',
                'maxResults': 100,
            }
            if next_token:
                params['nextPageToken'] = next_token
            resp = requests.get(url, params=params, headers=_auth_headers(), timeout=20)
            if resp.status_code != 200:
                logger.error(f'[Jira] get_open_facebook_issues: {resp.status_code} {resp.text[:200]}')
                break
            data = resp.json()
            for issue in data.get('issues', []):
                fields = issue.get('fields', {})
                if _comm_link_is_facebook(fields.get(field)):
                    results.append({
                        'key': issue.get('key', ''),
                        'summary': fields.get('summary', '') or '',
                        'status': (fields.get('status') or {}).get('name', ''),
                    })
            next_token = data.get('nextPageToken')
            pages += 1
            if data.get('isLast') or not next_token or pages >= 20:
                break
        return results
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch open Facebook issues: {e}')
        return results


def get_recent_comments(issue_key: str, max_results: int = 20) -> list:
    """Returns the most recent comments for an issue, newest first."""
    if not _configured():
        return []
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment'
        resp = requests.get(
            url,
            params={'orderBy': '-created', 'maxResults': max_results},
            headers=_auth_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        return resp.json().get('comments', [])
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch comments for {issue_key}: {e}')
        return []


def get_issue_reporter(issue_key: str) -> dict | None:
    """Returns reporter info {'accountId': str, 'displayName': str} or None."""
    if not _configured():
        return None
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}?fields=reporter'
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return None
        reporter = resp.json().get('fields', {}).get('reporter') or {}
        if not reporter:
            return None
        return {
            'accountId': reporter.get('accountId', ''),
            'displayName': reporter.get('displayName', ''),
        }
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch reporter for {issue_key}: {e}')
        return None


def set_issue_communication_link(issue_key: str, link: str) -> bool:
    """Updates the Communication Link custom field on a Jira issue."""
    if not _configured():
        return False
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}'
        resp = requests.put(
            url,
            json={'fields': {JIRA_COMMUNICATION_LINK_FIELD: link}},
            headers=_auth_headers(),
            timeout=10,
        )
        if resp.status_code == 400:
            # The field is not plain text on this project (URG), or is not on its edit screen. Ask Jira
            # for the field's real shape and retry once; if the issue simply has no such field, say so
            # once at INFO instead of raising an error for every ticket.
            meta = requests.get(url + '/editmeta', headers=_auth_headers(), timeout=10)
            field = ((meta.json() or {}).get('fields') or {}).get(JIRA_COMMUNICATION_LINK_FIELD) if meta.ok else None
            if field is None:
                logger.info(f'[Jira] {issue_key}: the Communication Link field is not editable on this project, skipped')
                return False
            schema = field.get('schema') or {}
            if schema.get('type') == 'option' or (schema.get('type') == 'array' and schema.get('items') == 'option'):
                low = (link or '').lower()
                name = 'Discord' if 'discord' in low else 'Reddit' if 'reddit' in low else 'Facebook' if 'facebook' in low else ''
                allowed = [o.get('value') for o in (field.get('allowedValues') or [])]
                if name not in allowed:
                    logger.info(f'[Jira] {issue_key}: Communication Link is a list ({allowed}) with no match, skipped')
                    return False
                value = {'value': name} if schema.get('type') == 'option' else [{'value': name}]
                resp = requests.put(url, json={'fields': {JIRA_COMMUNICATION_LINK_FIELD: value}},
                                    headers=_auth_headers(), timeout=10)
            if resp.status_code == 400:
                logger.error(f'[Jira] Failed to set communication link for {issue_key}: {resp.text[:300]}')
                return False
        resp.raise_for_status()
        logger.info(f'[Jira] Updated communication link for {issue_key}')
        return True
    except Exception as e:
        logger.error(f'[Jira] Failed to set communication link for {issue_key}: {e}')
        return False


_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
_DISCORD_CHANNEL_RE = re.compile(r'discord\.com/channels/\d+/(\d+)')
# Accepts mod.reddit.com, www.reddit.com and bare reddit.com modmail links.
# The \b before "reddit" anchors to a subdomain dot or a slash, so "notreddit.com" won't match.
_REDDIT_MODMAIL_RE = re.compile(r'\breddit\.com/mail/[^/]+/(\w+)')


def get_emails_from_all_comments(issue_key: str) -> list:
    """Returns deduplicated list of emails found across all comments on the issue."""
    if not _configured():
        return []
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment'
        resp = requests.get(url, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return []
        emails, seen = [], set()
        for comment in resp.json().get('comments', []):
            for email in _EMAIL_RE.findall(comment.get('body', '')):
                if email not in seen:
                    seen.add(email)
                    emails.append(email)
        return emails
    except Exception as e:
        logger.error(f'[Jira] Failed to fetch emails from comments for {issue_key}: {e}')
        return []


def parse_discord_channel_id(url: str) -> int | None:
    """Extracts channel_id from a Discord channel URL, or None if not a Discord URL."""
    m = _DISCORD_CHANNEL_RE.search(url or '')
    return int(m.group(1)) if m else None


def parse_reddit_convo_id(url: str) -> str | None:
    """Extracts the modmail conversation id from a mod.reddit.com/mail/.../<id> URL, else None."""
    m = _REDDIT_MODMAIL_RE.search(url or '')
    return m.group(1) if m else None


_AUTHOR_LINE_RE = re.compile(r'^\s*\*Author:\*\s*\[~accountId:([^\]\s]+)\]\s*\n?', re.IGNORECASE)
_USER_ID_LINE_RE = re.compile(r'(?im)^([ \t]*user[ \t]*id[ \t]*:?[ \t]*)(\d{3,12})[ \t]*$')
_ADMIN_SESSIONS_URL = ('https://admin.cloud.boosteroid.com/admin/filament/streaming/'
                       'streaming-user-sessions?filters%5Buser_id%5D%5Bvalue%5D=')


def _split_author_line(description: str):
    """(description without the leading "*Author:* [~accountId:X]" line, X) - or (description, None)
    when the author is not a Jira account mention (then the line stays)."""
    m = _AUTHOR_LINE_RE.match(description or '')
    if not m:
        return description, None
    return (description[m.end():]).lstrip('\n'), m.group(1)


def _user_ids_in_text(text: str, link: bool = True) -> str:
    """Personal-data rule for anything written to Jira: an email becomes "User ID N" (resolved through
    our directory, then the cloud API; an address that cannot be resolved is removed), and a plain
    "User ID: N" line becomes a link to that user's sessions in the cloud admin."""
    if not text:
        return text or ''
    try:
        from services.user_ref import replace_emails
        text = replace_emails(text)
    except Exception as e:
        logger.warning(f'[Jira] email -> User ID replacement skipped: {type(e).__name__}')
    if link:
        text = _USER_ID_LINE_RE.sub(lambda m: f'[User ID {m.group(2)}|{_ADMIN_SESSIONS_URL}{m.group(2)}]', text)
    return text


def create_jira_issue(
    summary: str,
    description: str,
    project_key: str = None,
    l1_tags: list[JiraL1Tags] | None = None,
    issue_type: str = 'Task',
    extra_fields: dict | None = None,
) -> str | None:
    """Creates a Jira issue and returns its key (e.g. 'SO-1234'), or None on failure.

    project_key - board to create on (defaults to JIRA_PROJECT_KEY, i.e. 'SO').
    issue_type  - Jira issue type name (defaults to 'Task').
    l1_tags     - list of JiraL1Tags enum members to set on customfield_10209.
    """
    if not _configured():
        return None
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue'
        # One place for every ticket the bot creates (Slack modals and the console alike):
        #  - the "*Author:* [~accountId:...]" first line becomes the issue's Reporter,
        #  - emails become "User ID N", and "User ID: N" lines link to the cloud admin.
        original_description = description
        description, reporter_id = _split_author_line(description)
        description = _user_ids_in_text(description)
        summary = _user_ids_in_text(summary, link=False)
        fields: dict = {
            'project': {'key': project_key or JIRA_PROJECT_KEY},
            'summary': summary,
            'description': description,
            'issuetype': {'name': issue_type},
        }
        if l1_tags:
            fields[JIRA_L1_TAGS_FIELD] = [{'id': tag.value} for tag in l1_tags]
        if extra_fields:
            fields.update(extra_fields)
        if reporter_id:
            fields['reporter'] = {'accountId': reporter_id}
        resp = requests.post(url, json={'fields': fields}, headers=_auth_headers(), timeout=15)
        if resp.status_code == 400 and reporter_id:
            # Reporter not settable here (permission / screen): keep the Author line instead.
            logger.warning(f'[Jira] Could not set Reporter on create ({resp.text[:200]}); keeping the Author line')
            fields.pop('reporter', None)
            fields['description'] = _user_ids_in_text(original_description)
            resp = requests.post(url, json={'fields': fields}, headers=_auth_headers(), timeout=15)
        resp.raise_for_status()
        issue_key = resp.json().get('key')
        logger.info(f'[Jira] Created issue {issue_key}: {summary!r}')
        try:
            from services.jira_so_index import upsert as _index_ticket
            _index_ticket(issue_key, summary, 'Backlog', source='created')
        except Exception as _e:
            logger.warning("ignored: %s", _e)
        return issue_key
    except Exception as e:
        logger.error(f'[Jira] Failed to create issue "{summary}": {e}')
        return None


def attach_file_to_jira(issue_key: str, content: bytes, filename: str, mimetype: str) -> bool:
    """Attaches a file to an existing Jira issue. Returns True on success."""
    if not _configured():
        return False
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/attachments'
        token = b64encode(f'{JIRA_API_EMAIL}:{JIRA_API_TOKEN}'.encode()).decode()
        # X-Atlassian-Token: no-check is required by Jira Cloud to bypass XSRF protection
        # Content-Type is intentionally omitted - requests sets it automatically for multipart
        headers = {
            'Authorization': f'Basic {token}',
            'X-Atlassian-Token': 'no-check',
        }
        resp = requests.post(
            url,
            files={'file': (filename, content, mimetype)},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        logger.info(f'[Jira] Attached "{filename}" to {issue_key}')
        return True
    except Exception as e:
        logger.error(f'[Jira] Failed to attach "{filename}" to {issue_key}: {e}')
        return False


def find_jira_user_by_email(email: str) -> str | None:
    """Searches Jira users by email and returns accountId (Atlassian Cloud), or None."""
    if not _configured() or not email:
        return None
    try:
        url = f'{JIRA_BASE_URL}/rest/api/2/user/search'
        resp = requests.get(url, params={'query': email}, headers=_auth_headers(), timeout=10)
        if resp.status_code != 200:
            return None
        users = resp.json()
        # Prefer exact email match
        for user in users:
            if user.get('emailAddress', '').lower() == email.lower():
                return user.get('accountId')
        return users[0].get('accountId') if users else None
    except Exception as e:
        logger.error(f'[Jira] Failed to find user by email {email}: {e}')
        return None
