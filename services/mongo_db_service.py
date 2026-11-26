import asyncio
import os
import re
from pathlib import Path
from dotenv import load_dotenv
import logging
from datetime import datetime, timedelta
import requests
import pytz
from slack_sdk import WebClient

from services import reddit_service

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

client = WebClient(token=os.environ['SLACK_TOKEN'])

logger = logging.getLogger(__name__)

# Sync client for Flask/Slack
from services import db as _shared   # one shared client per cluster for the whole process
sync_client = _shared.client()
sync_db = _shared.main_db()
sync_issues_collection = sync_db['issues']
sync_testing_sessions_collection = sync_db['testing_sessions']
sync_users_emails_collection = sync_db['users_emails']
sync_discord_tickets_collection = sync_db['discord_tickets']
sync_active_maintenances_collection = sync_db['active_maintenances']
sync_jira_tickets_collection = sync_db['jira_tickets']
sync_url_mappings_collection = sync_db['wishlist_url_mappings']
sync_slack_pins_collection = sync_db['slack_pins']
sync_reddit_mod_notes_collection = sync_db['reddit_mod_notes']
sync_maintenance_subscriptions_collection = sync_db['maintenance_subscriptions']
sync_urg_dedup_collection = sync_db['urg_dedup']   # local, immediately-consistent record of recent URG tickets
try:   # self-prune: a URG dedup record is useless once older than the dedup window (kept 1 day)
    sync_urg_dedup_collection.create_index('created_at', expireAfterSeconds=24 * 3600)
except Exception as _e:
    logger.warning("ignored: %s", _e)

# Mapping: alias -> Slack user ID
# Format: SLACK_USER_IDS=o.teleshko:U08ABC123,m.yeromenko:U09XYZ456
_slack_user_ids: dict[str, str] = {}
for _entry in os.getenv("SLACK_USER_IDS", "").split(","):
    if ":" in _entry:
        _alias, _sid = _entry.strip().split(":", 1)
        if _alias and _sid:
            _slack_user_ids[_alias.strip()] = _sid.strip()

# Async client (Motor)
from motor.motor_asyncio import AsyncIOMotorClient
# Motor (async) client: used only from the Reddit event loop (reddit_notifications shares it).
_async_uri = os.getenv('MONGODB_URI') or ''
async_client = AsyncIOMotorClient(_async_uri, **({'tlsCAFile': __import__('certifi').where()} if _async_uri.startswith('mongodb+srv://') else {}))
async_db = async_client[os.getenv('MONGODB_DATABASE', 'reddit_posts')]
async_issues_collection = async_db['issues']
async_users_emails_collection = async_db['users_emails']

SLACK_REPORT_API_URL = os.getenv('SLACK_REPORT_API_URL')
SLACK_REPORTS_CHANNEL = os.getenv('SLACK_REPORTS_CHANNEL_ID')
SUBREDDIT_NAME = os.getenv('SUBREDDIT_NAME')
# Console-only sentinel written by "dismiss the no-tag alert" (app.js NOTAG_MARK). It is NOT a real
# tag and must never appear in any tag report.
NO_TAG_SENTINEL = 'no-tag-needed'
def save_url_mapping_sync(url: str, game_name: str, entity_type: str, learned_by: str) -> None:
    """Upserts a learned URL → (game_name, entity_type) mapping for the wishlist bot."""
    try:
        sync_url_mappings_collection.update_one(
            {"url": url},
            {"$set": {
                "game_name": game_name,
                "entity_type": entity_type,
                "learned_by": learned_by,
                "learned_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        logger.info(f"[WishlistMapping] Saved URL mapping: {url} -> {game_name} ({entity_type})")
    except Exception as e:
        logger.error(f"[WishlistMapping] Failed to save URL mapping for {url}: {e}")


# Sync version for using in modal_handlers
def save_information_sync(issues_data):
    try:
        issues_data['created_at'] = datetime.utcnow()
        try:   # one schema with the console: the record knows its chat (services/issue_tags.py)
            from services import issue_tags
            from services.db import snapshot_db
            issue_tags.add_context(sync_db, snapshot_db(), issues_data)
        except Exception as e:
            logger.warning(f'[MongoDB] tag record chat not resolved: {e}')
        result = sync_issues_collection.insert_one(issues_data)
        logger.info(f'Issue saved to MongoDB: {result.inserted_id}')
        return result
    except Exception as e:
        logger.error(f'Issue not saved to MongoDB. Error: {e}.')
        return None



def collect_and_send_tags():
    tz = pytz.timezone('Europe/Kyiv')
    now = datetime.now(tz)

    end_time = now.replace(hour=6, minute=59, second=59, microsecond=0)
    start_time = (now - timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)

    if now.hour < 7:
        end_time = end_time - timedelta(days=1)
        start_time = start_time - timedelta(days=1)

    pipeline = [
        {"$match": {"created_at": {"$gte": start_time, "$lte": end_time}}},
        {"$project": {"tags": 1}},
        {"$unwind": "$tags"},
        {"$match": {"tags": {"$ne": NO_TAG_SENTINEL}}},   # drop the console 'no-tag' sentinel (not a real tag)
        {"$group": {"_id": "$tags", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]

    results = list(sync_issues_collection.aggregate(pipeline))
    tags_count = {res['_id']: res['count'] for res in results if res['count'] > 0}

    poor_quality_tags = ['Stream FPS', 'Game FPS', 'Bitrate', 'Latency', 'Session crash', 'Image quality']
    poor_quality_tags_count = 0

    if not tags_count:
        logger.info(f'No tags found in the period {start_time} - {end_time} to send.')
        return

    for tag in poor_quality_tags:
        if tag in tags_count:
            poor_quality_tags_count += tags_count[tag]

    if poor_quality_tags_count > 0:
        if 'Poor quality' in tags_count:
            tags_count['Poor quality'] += poor_quality_tags_count
        else:
            tags_count['Poor quality'] = poor_quality_tags_count

    sorted_tags = dict(sorted(tags_count.items(), key=lambda x: x[1], reverse=True))

    data = {
        "type": "tags",
        "data": sorted_tags,
    }

    headers = {"Content-Type": "application/json"}
    response = requests.post(SLACK_REPORT_API_URL, headers=headers, json=data)

    if response.status_code == 200:
        logger.info(f'Slack tags report sent: {response.text}, Data: {data}')
        try:
            client.chat_postMessage(
                channel=SLACK_REPORTS_CHANNEL,
                text=f'Tags report sent: {data}, Status: {response.status_code}',
            )
        except Exception as e:
            logger.error(f'Slack tags report sending confirmation error: {e}')
    else:
        logger.error(
            f'Slack report error. Status: {response.status_code}, Response: {response}, Response Text: {response.text}, Data: {data}')
        try:
            client.chat_postMessage(
                channel=SLACK_REPORTS_CHANNEL,
                text=f"Tags report didn't send. Response text: {response.text}, Data: {data}, Response: {response}",
            )
        except Exception as e:
            logger.error(f'Slack tags report(failed) sending confirmation error: {e}')


# Collect and send issues by platforms to L1
def collect_and_send_issues_by_platforms():
    tz = pytz.timezone('Europe/Kyiv')
    now = datetime.now(tz)
    last_24h = now - timedelta(hours=24)

    pipeline = [
        {"$match": {"created_at": {"$gte": last_24h}}},
        {"$project": {"platform": 1}},
        {"$unwind": "$platform"},
        {"$group": {"_id": "$platform", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]

    results = list(sync_issues_collection.aggregate(pipeline))
    issues_count = {res['_id']: res['count'] for res in results if res['count'] > 0}

    #Reddit posts count
    try:
        reddit_result = reddit_service.get_daily_window_posts(SUBREDDIT_NAME)
        reddit_count = reddit_result['count']

        if reddit_count > 0:
            issues_count['Reddit'] = reddit_count
            logger.info(f'Reddit posts found: {reddit_count}')
        else:
            logger.info('No Reddit posts found in the period')

    except Exception as e:
        logger.error(f'Error fetching Reddit posts: {e}')

    if not issues_count:
        logger.info('No issues by platforms found in the last 24h to send.')
        return

    # Facebook group posts: the posts the CMT browser extension collected (fb_posts, first_seen).
    try:
        fb_start, fb_end = _daily_window()
        fb_count = sync_db['fb_posts'].count_documents({
            'first_seen': {'$gte': fb_start, '$lte': fb_end}
        })
        if fb_count > 0:
            issues_count['Facebook'] = fb_count
            logger.info(f'[MongoDB] Facebook posts in daily window: {fb_count}')
    except Exception as e:
        logger.error(f'Error counting Facebook posts: {e}')

    if not issues_count:
        logger.info('No issues by platforms found in the last 24h to send.')
        return

    # Discord: moderator mentions + new forum posts in the window - the same records the console shows
    # (discord_mentions / discord_forum_posts; every Slack mention / forum alert has one).
    try:
        d_start, d_end = _daily_window()
        lo = d_start.astimezone(pytz.utc).strftime('%Y-%m-%dT%H:%M:%S')
        hi = d_end.astimezone(pytz.utc).strftime('%Y-%m-%dT%H:%M:%S.999999')
        mentions = sync_db['discord_mentions'].count_documents({'ts': {'$gte': lo, '$lte': hi}})
        forum = sync_db['discord_forum_posts'].count_documents({'created_at': {'$gte': lo, '$lte': hi}})
        if mentions + forum > 0:
            issues_count['Discord'] = mentions + forum
            logger.info(f"Discord in the daily window: {mentions + forum} (mentions: {mentions}, forums: {forum})")
    except Exception as e:
        logger.error(f'[MongoDB] Error fetching Discord notifications: {e}')

    if not issues_count:
        logger.info('[MongoDB] No issues by platforms found in the last 24h to send.')
        return

    total_issues = sum(issues_count.values())

    sorted_issues_count = dict(sorted(issues_count.items(), key=lambda x: x[1], reverse=True))

    data = {
        "type": "source_load",
        "data": {
            "total": total_issues,
            "sections": sorted_issues_count
        }
    }

    headers = {"Content-Type": "application/json"}
    response = requests.post(SLACK_REPORT_API_URL, headers=headers, json=data)

    if response.status_code == 200:
        logger.info(f'[MongoDB] Slack platform issues report sent: {response.text}, Data: {data}')
        try:
            client.chat_postMessage(
                channel=SLACK_REPORTS_CHANNEL,
                text=f'Platform issues report sent: {data}, Status: {response.status_code}',
            )
        except Exception as e:
            logger.error(f'[MongoDB] Slack platform issues report sending confirmation error: {e}')
    else:
        logger.error(f'[MongoDB] Slack platform issues report error. Status: {response.status_code}, Response: {response}, Response Text: {response.text}, Data: {data}')
        try:
            client.chat_postMessage(
                channel=SLACK_REPORTS_CHANNEL,
                text=f"Platform issues report didn't send. Response text: {response.text}, Data: {data}, Response: {response}",
            )
        except Exception as e:
            logger.error(f'[MongoDB] Slack platform issues report(failed) sending confirmation error: {e}')

def get_testing_session_by_id(mongo_id: str):
    try:
        from bson import ObjectId
        result = sync_testing_sessions_collection.find_one({'_id': ObjectId(mongo_id)})
        return result
    except Exception as e:
        logger.error(f'[MongoDB] Error getting testing session by id {mongo_id}: {e}')
        return None




# Collect and send sessions
def collect_and_send_testing_sessions():
    tz = pytz.timezone('Europe/Kyiv')
    now = datetime.now(tz)

    end_time = now.replace(hour=6, minute=59, second=59, microsecond=0)
    start_time = (now - timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)

    if now.hour < 7:
        end_time = end_time - timedelta(days=1)
        start_time = start_time - timedelta(days=1)

    time_filter = {"created_at": {"$gte": start_time, "$lte": end_time}}

    all_sessions = list(sync_testing_sessions_collection.find(time_filter))

    if not all_sessions:
        logger.info(f'[MongoDB] No sessions found in the period {start_time} - {end_time} to send.')
        return

    def _build_group(sessions: list) -> dict:
        """Build prod/stage sub-object from a list of session documents."""
        tags: dict = {}
        results: list = []
        problems = 0

        for s in sessions:
            if s.get('issue') == 'Yes':
                problems += 1
            for tag in s.get('testing_tags') or []:
                tags[tag] = tags.get(tag, 0) + 1

        tags = dict(sorted(tags.items(), key=lambda x: x[1], reverse=True))

        for s in sessions:
            result_text = s.get('result', '')
            if not result_text:
                continue

            if s.get('issue') == 'Yes':
                session_id = s.get('session_id', '')
                game = s.get('game', '')
                issue_tags = ', '.join(s.get('testing_tags') or [])
                entry = f'ID: {session_id}\nApplication: {game}\nIssue Tags: {issue_tags}\n\n   {result_text}'
            else:
                entry = result_text

            results.append(entry)

        return {
            'total': len(sessions),
            'problems': problems,
            'tags': tags,
            'results': results,
        }

    # Split by environment: "Prod" → prod, "Testing" → stage, unknown → prod (fallback)
    prod_sessions = [s for s in all_sessions if s.get('environment') == 'Prod' or not s.get('environment')]
    stage_sessions = [s for s in all_sessions if s.get('environment') == 'Testing']

    data = {
        "type": "testing",
        "data": {
            "prod": _build_group(prod_sessions),
            "stage": _build_group(stage_sessions),
        }
    }

    total_sessions = len(all_sessions)
    prod_count = len(prod_sessions)
    stage_count = len(stage_sessions)

    headers = {"Content-Type": "application/json"}
    response = requests.post(SLACK_REPORT_API_URL, headers=headers, json=data)

    if response.status_code == 200:
        logger.info(f'[MongoDB] Slack testing report sent. Data: {data}')
        try:
            prod_problems  = data['data']['prod']['problems']
            stage_problems = data['data']['stage']['problems']
            client.chat_postMessage(
                channel=SLACK_REPORTS_CHANNEL,
                text=(
                    f'Testing report sent.\n'
                    f'Total: {total_sessions} | '
                    f'Prod (problems): {prod_count} ({prod_problems}) | '
                    f'Stage (problems): {stage_count} ({stage_problems})'
                ),
            )
        except Exception as e:
            logger.error(f'[MongoDB] Testing report sending confirmation error: {e}')
    else:
        logger.error(
            f'[MongoDB] Testing sessions report error. Status: {response.status_code}, Response text: {response.text}, Data: {data}')
        try:
            client.chat_postMessage(
                channel=SLACK_REPORTS_CHANNEL,
                text=f"Testing sessions report didn't send. Status: {response.status_code}, Error: {response.text}",
            )
        except Exception as e:
            logger.error(f"[MongoDB] Testing sessions report(failed) sending confirmation error: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#   USER ID INSTEAD OF EMAIL (personal-data rule)
# ═══════════════════════════════════════════════════════════════════════════
_EMAIL_SHAPE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class IdentNotStored(Exception):
    """Raised by _ident when nothing may be written (unknown account, or the cloud admin is down)."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


def ident_status(value):
    """(user_id, status) for an email / session ID / User ID a user gave us. status is "found",
    "not_found", "error" (cloud admin unavailable - add the ID by hand) or "invalid"."""
    from services.user_ref import resolve
    return resolve(value)


def _ident(value) -> dict:
    """Fields to $set on a users_emails record for an identifier a user gave us.

    The value (email, session ID or the ID itself) is resolved to the Boosteroid User ID and ONLY the
    ID is stored, in plain text. An email address is never written, under any condition: if the account
    is unknown or the cloud admin cannot be reached, IdentNotStored is raised and the caller's write is
    skipped (callers tell the operator to add the ID by hand)."""
    uid, status = ident_status(value)
    if status != 'found' or not uid:
        raise IdentNotStored(status)
    return {'cloud_user_id': str(uid), 'cloud_user_id_at': datetime.utcnow(), 'cloud_user_id_status': 'found'}


def _ident_display(doc):
    """What to show for a stored record: 'User ID N', 'Account deleted' or None."""
    if not doc:
        return None
    if doc.get('cloud_user_id'):
        return 'User ID %s' % doc['cloud_user_id']
    if doc.get('cloud_user_id_status') == 'deleted':
        return 'Account deleted'
    return None

    

# ═══════════════════════════════════════════════════════════════════════════
#   REDDIT USER EMAIL FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
#   DISCORD USER EMAIL FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════



def get_email_by_discord_user_id_sync(user_id: int):
    try:
        doc = sync_users_emails_collection.find_one(
            {'discord_user_id': user_id}, {'cloud_user_id': 1, '_id': 0}
        )
        return _ident_display(doc)
    except Exception as e:
        logger.error(f'[MongoDB] Error getting email for Discord user {user_id}: {e}')
        return None


def save_discord_user_email_sync(user_id: int, username: str, email: str):
    try:
        now = datetime.utcnow()
        sync_users_emails_collection.update_one(
            {'discord_user_id': user_id},
            {
                '$set': {
                    **_ident(email),
                    'discord_username': username,
                    'platform': 'Discord',
                    'updated_at': now,
                },
                '$setOnInsert': {
                    'discord_user_id': user_id,
                    'created_at': now,
                },
            },
            upsert=True,
        )
        logger.info(f'[MongoDB] User ID saved for Discord user {username} ({user_id})')
    except IdentNotStored as e:
        logger.info(f'[MongoDB] User ID not resolved for Discord user {user_id} ({e.status}); nothing saved')
    except Exception as e:
        logger.error(f'[MongoDB] Error saving email for Discord user {user_id}: {e}')


async def async_get_email_by_reddit_username(username: str):
    try:
        doc = await async_users_emails_collection.find_one(
            {'reddit_username': username}, {'cloud_user_id': 1, '_id': 0}
        )
        return _ident_display(doc)
    except Exception as e:
        logger.error(f'[MongoDB] Error getting email for Reddit user {username}: {e}')
        return None


async def async_save_reddit_user_email(username: str, email: str):
    try:
        now = datetime.utcnow()
        await async_users_emails_collection.update_one(
            {'reddit_username': username},
            {
                '$set': {
                    **(await asyncio.to_thread(_ident, email)),
                    'platform': 'Reddit',
                    'url': f'https://www.reddit.com/user/{username}',
                    'updated_at': now,
                },
                '$setOnInsert': {
                    'reddit_username': username,
                    'created_at': now,
                },
            },
            upsert=True,
        )
        logger.info(f'[MongoDB] User ID saved for Reddit user {username}')
    except IdentNotStored as e:
        logger.info(f'[MongoDB] User ID not resolved for Reddit user {username} ({e.status}); nothing saved')
    except Exception as e:
        logger.error(f'[MongoDB] Error saving email for Reddit user {username}: {e}')


def get_email_by_reddit_username_sync(username: str):
    try:
        doc = sync_users_emails_collection.find_one(
            {'reddit_username': username}, {'cloud_user_id': 1, '_id': 0}
        )
        return _ident_display(doc)
    except Exception as e:
        logger.error(f'[MongoDB] Error getting email for Reddit user {username}: {e}')
        return None


def save_reddit_user_email_sync(username: str, email: str):
    try:
        now = datetime.utcnow()
        sync_users_emails_collection.update_one(
            {'reddit_username': username},
            {
                '$set': {
                    **_ident(email),
                    'platform': 'Reddit',
                    'url': f'https://www.reddit.com/user/{username}',
                    'updated_at': now,
                },
                '$setOnInsert': {
                    'reddit_username': username,
                    'created_at': now,
                },
            },
            upsert=True,
        )
        logger.info(f'[MongoDB] User ID saved for Reddit user {username}')
    except IdentNotStored as e:
        logger.info(f'[MongoDB] User ID not resolved for Reddit user {username} ({e.status}); nothing saved')
    except Exception as e:
        logger.error(f'[MongoDB] Error saving email for Reddit user {username}: {e}')


# ═══════════════════════════════════════════════════════════════════════════
#   REDDIT MOD-NOTE BOOKKEEPING (tracks note ids the bot manages on Reddit)
# ═══════════════════════════════════════════════════════════════════════════
#   Doc shape: {reddit_user, convo_id (legacy), email, email_note_id,
#               jira_notes: {ISSUE_KEY: note_id}, jira_done: {ISSUE_KEY: bool},
#               jira_convos: {ISSUE_KEY: convo_id}, highlighted_convos: [convo_id, ...]}
#   Note: a Reddit mod note is per-user, but Jira links are tracked per-conversation
#   via jira_convos, and highlighting is tracked per-conversation via highlighted_convos.

def get_reddit_mod_note_sync(reddit_user: str) -> dict | None:
    try:
        return sync_reddit_mod_notes_collection.find_one({'reddit_user': reddit_user})
    except Exception as e:
        logger.error(f'[MongoDB] get_reddit_mod_note_sync failed for {reddit_user}: {e}')
        return None


def set_reddit_mod_note_convo_sync(reddit_user: str, convo_id: str) -> None:
    try:
        sync_reddit_mod_notes_collection.update_one(
            {'reddit_user': reddit_user},
            {'$set': {'convo_id': convo_id, 'updated_at': datetime.utcnow()},
             '$setOnInsert': {'reddit_user': reddit_user}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[MongoDB] set_reddit_mod_note_convo_sync failed for {reddit_user}: {e}')


def add_reddit_mod_note_highlighted_convo_sync(reddit_user: str, convo_id: str) -> None:
    """Records that `convo_id` is currently highlighted for this user (per-conversation)."""
    try:
        sync_reddit_mod_notes_collection.update_one(
            {'reddit_user': reddit_user},
            {'$addToSet': {'highlighted_convos': convo_id},
             '$set': {'updated_at': datetime.utcnow()},
             '$setOnInsert': {'reddit_user': reddit_user}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[MongoDB] add_reddit_mod_note_highlighted_convo_sync failed for {reddit_user}: {e}')


def remove_reddit_mod_note_highlighted_convo_sync(reddit_user: str, convo_id: str) -> None:
    """Clears the highlighted flag for a single conversation of this user."""
    try:
        sync_reddit_mod_notes_collection.update_one(
            {'reddit_user': reddit_user},
            {'$pull': {'highlighted_convos': convo_id},
             '$set': {'updated_at': datetime.utcnow()}},
        )
    except Exception as e:
        logger.error(f'[MongoDB] remove_reddit_mod_note_highlighted_convo_sync failed for {reddit_user}: {e}')


def clear_reddit_mod_note_legacy_highlight_sync(reddit_user: str) -> None:
    """Clears the legacy single-field `highlighted` flag (pre per-conversation docs)."""
    try:
        sync_reddit_mod_notes_collection.update_one(
            {'reddit_user': reddit_user},
            {'$set': {'highlighted': False, 'updated_at': datetime.utcnow()}},
        )
    except Exception as e:
        logger.error(f'[MongoDB] clear_reddit_mod_note_legacy_highlight_sync failed for {reddit_user}: {e}')


def set_reddit_mod_note_jira_sync(reddit_user: str, issue_key: str, note_id: str, convo_id: str = None) -> None:
    try:
        fields = {
            f'jira_notes.{issue_key}': note_id,
            f'jira_done.{issue_key}': False,
            'updated_at': datetime.utcnow(),
        }
        if convo_id:
            fields[f'jira_convos.{issue_key}'] = convo_id
        sync_reddit_mod_notes_collection.update_one(
            {'reddit_user': reddit_user},
            {'$set': fields,
             '$setOnInsert': {'reddit_user': reddit_user}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[MongoDB] set_reddit_mod_note_jira_sync failed for {reddit_user}/{issue_key}: {e}')


def set_reddit_mod_note_jira_convo_sync(reddit_user: str, issue_key: str, convo_id: str) -> None:
    """Records which conversation a Jira ticket was linked from, without touching jira_done."""
    try:
        sync_reddit_mod_notes_collection.update_one(
            {'reddit_user': reddit_user},
            {'$set': {f'jira_convos.{issue_key}': convo_id, 'updated_at': datetime.utcnow()}},
        )
    except Exception as e:
        logger.error(f'[MongoDB] set_reddit_mod_note_jira_convo_sync failed for {reddit_user}/{issue_key}: {e}')


def mark_reddit_mod_note_jira_done_sync(reddit_user: str, issue_key: str, note_id: str) -> None:
    try:
        sync_reddit_mod_notes_collection.update_one(
            {'reddit_user': reddit_user},
            {'$set': {
                f'jira_notes.{issue_key}': note_id,
                f'jira_done.{issue_key}': True,
                'updated_at': datetime.utcnow(),
             }},
        )
    except Exception as e:
        logger.error(f'[MongoDB] mark_reddit_mod_note_jira_done_sync failed for {reddit_user}/{issue_key}: {e}')


def get_all_reddit_mod_notes_sync() -> list:
    try:
        return list(sync_reddit_mod_notes_collection.find({}))
    except Exception as e:
        logger.error(f'[MongoDB] get_all_reddit_mod_notes_sync failed: {e}')
        return []


# ═══════════════════════════════════════════════════════════════════════════
#   FACEBOOK AUTHOR EMAIL FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def get_email_by_facebook_author(author: str) -> list:
    """['User ID N'] for a Facebook author, or [] when the author has no User ID."""
    try:
        doc = sync_users_emails_collection.find_one({'facebook_author': author}, {'cloud_user_id': 1, '_id': 0})
        return ['User ID %s' % doc['cloud_user_id']] if doc and doc.get('cloud_user_id') else []
    except Exception as e:
        logger.error(f'[MongoDB] Error getting FB email for {author}: {e}')
        return []


def save_facebook_author_email_sync(author: str, email: str):
    try:
        now = datetime.utcnow()
        try:
            _f = _ident(email)
        except IdentNotStored as e:
            logger.info(f'[MongoDB] User ID not resolved for FB author {author} ({e.status}); nothing saved')
            return
        if _f.get('cloud_user_id'):
            # Resolved: keep the User ID for this author, never the address.
            sync_users_emails_collection.update_one(
                {'facebook_author': author},
                {'$set': dict(_f, platform='Facebook', updated_at=now),
                 '$setOnInsert': {'facebook_author': author, 'created_at': now}},
                upsert=True,
            )
            logger.info(f'[MongoDB] Saved FB User ID for {author}')
            return
    except Exception as e:
        logger.error(f'[MongoDB] Error saving FB email for {author}: {e}')


def _daily_window():
    """The report window: 07:00 yesterday -> 06:59:59 today, Kyiv time (aware datetimes)."""
    tz = pytz.timezone('Europe/Kyiv')
    now = datetime.now(tz)
    end = now.replace(hour=6, minute=59, second=59, microsecond=0)
    start = (now - timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    if now.hour < 7:
        end -= timedelta(days=1)
        start -= timedelta(days=1)
    return start, end


def collect_and_send_reports():
    logger.info('Collecting scheduled reports collection...')

    collect_and_send_tags()
    collect_and_send_issues_by_platforms()
    collect_and_send_testing_sessions()

    logger.info('Collecting scheduled reports collection complete.')


# ═══════════════════════════════════════════════════════════════════════════
#   CMT HELPER FUNCTIONS - Get user statistics for reminders
# ═══════════════════════════════════════════════════════════════════════════



def add_active_maintenance(
    entity: str,
    maintenance_type: str,
    slack_ts: str | None = None,
    slack_channel: str | None = None,
) -> None:
    try:
        entity = (entity or "").strip()   # normalize so a stray trailing space never makes the record un-removable
        now = datetime.utcnow()
        sync_active_maintenances_collection.update_one(
            {'entity': entity, 'type': maintenance_type},
            {
                '$set': {
                    'entity': entity,
                    'type': maintenance_type,
                    'started_at': now,
                    'slack_ts': slack_ts,
                    'slack_channel': slack_channel,
                },
            },
            upsert=True,
        )
        logger.info(f'[MongoDB] Active maintenance added: {maintenance_type} - {entity}')
    except Exception as e:
        logger.error(f'[MongoDB] Error adding active maintenance {entity}: {e}')


def remove_active_maintenance(entity: str, maintenance_type: str) -> None:
    try:
        import re
        target = (entity or "").strip()
        # Whitespace-tolerant match: some sources stored the entity with a stray leading/trailing space
        # (e.g. "Mistfall Hunter "), which made the record un-removable - the caller trims the name, so a
        # plain equality delete never matched and the maintenance "reappeared" after every end. Match on
        # the trimmed name and delete_many (in case a duplicate slipped in with different whitespace).
        pat = re.compile(r'^\s*' + re.escape(target) + r'\s*$')
        res = sync_active_maintenances_collection.delete_many({'entity': pat, 'type': maintenance_type})
        logger.info(f'[MongoDB] Active maintenance removed: {maintenance_type} - {target} ({res.deleted_count} doc(s))')
    except Exception as e:
        logger.error(f'[MongoDB] Error removing active maintenance {entity}: {e}')


def get_active_maintenance(entity: str, maintenance_type: str) -> dict | None:
    try:
        return sync_active_maintenances_collection.find_one({'entity': entity, 'type': maintenance_type})
    except Exception as e:
        logger.error(f'[MongoDB] Error getting active maintenance {entity}: {e}')
        return None


def update_maintenance_eta(entity: str, maintenance_type: str, eta: str) -> None:
    try:
        sync_active_maintenances_collection.update_one(
            {'entity': entity, 'type': maintenance_type},
            {'$set': {'eta': eta}},
        )
        logger.info(f'[MongoDB] ETA updated for maintenance {maintenance_type} - {entity}: {eta}')
    except Exception as e:
        logger.error(f'[MongoDB] Error updating ETA for maintenance {entity}: {e}')


def get_active_maintenances(maintenance_type: str) -> list:
    try:
        cursor = sync_active_maintenances_collection.find(
            {'type': maintenance_type},
            {'entity': 1, '_id': 0}
        )
        return [doc['entity'] for doc in cursor]
    except Exception as e:
        logger.error(f'[MongoDB] Error getting active maintenances ({maintenance_type}): {e}')
        return []


# ── Maintenance subscriptions (notify via Discord/Reddit on maintenance end) ──
#   Doc shape: {entity, entity_norm, type: game|server, target_type: discord|reddit,
#               target_id, language, reddit_convo_id?, reddit_user?, reddit_note_id?,
#               status: active|ended_pending|notified, created_at}

def create_maintenance_subscription_sync(entity, entity_norm, mtype, target_type, target_id,
                                         language, reddit_convo_id=None, reddit_user=None,
                                         reddit_note_id=None) -> bool:
    try:
        sync_maintenance_subscriptions_collection.insert_one({
            'entity': entity,
            'entity_norm': entity_norm,
            'type': mtype,
            'target_type': target_type,
            'target_id': str(target_id),
            'language': language,
            'reddit_convo_id': reddit_convo_id,
            'reddit_user': reddit_user,
            'reddit_note_id': reddit_note_id,
            'status': 'active',
            'created_at': datetime.utcnow(),
        })
        return True
    except Exception as e:
        logger.error(f'[MongoDB] create_maintenance_subscription_sync failed for {entity}/{target_type}: {e}')
        return False


def maintenance_subscription_exists_sync(entity_norm, mtype, target_type, target_id) -> bool:
    try:
        return sync_maintenance_subscriptions_collection.find_one({
            'entity_norm': entity_norm, 'type': mtype, 'target_type': target_type,
            'target_id': str(target_id), 'status': {'$in': ['active', 'ended_pending']},
        }) is not None
    except Exception as e:
        logger.error(f'[MongoDB] maintenance_subscription_exists_sync failed: {e}')
        return False


def mark_maintenance_subscriptions_ended_sync(entity_norm, mtype) -> int:
    """Flags all active subscriptions for an ended maintenance as pending notification."""
    try:
        res = sync_maintenance_subscriptions_collection.update_many(
            {'entity_norm': entity_norm, 'type': mtype, 'status': 'active'},
            {'$set': {'status': 'ended_pending', 'ended_at': datetime.utcnow()}},
        )
        return res.modified_count
    except Exception as e:
        logger.error(f'[MongoDB] mark_maintenance_subscriptions_ended_sync failed for {entity_norm}: {e}')
        return 0


def get_pending_maintenance_subscriptions_sync(target_type) -> list:
    try:
        return list(sync_maintenance_subscriptions_collection.find(
            {'target_type': target_type, 'status': 'ended_pending'}
        ))
    except Exception as e:
        logger.error(f'[MongoDB] get_pending_maintenance_subscriptions_sync failed: {e}')
        return []


def mark_maintenance_subscription_notified_sync(sub_id) -> None:
    try:
        sync_maintenance_subscriptions_collection.update_one(
            {'_id': sub_id},
            {'$set': {'status': 'notified', 'notified_at': datetime.utcnow()}},
        )
    except Exception as e:
        logger.error(f'[MongoDB] mark_maintenance_subscription_notified_sync failed for {sub_id}: {e}')


# ── Slack pinned auto-updating messages ──────────────────────────────────────

def get_slack_pin_sync(key: str) -> dict | None:
    """Returns the stored {slack_channel, slack_ts} handle for a named pinned message, or None."""
    try:
        return sync_slack_pins_collection.find_one({'key': key})
    except Exception as e:
        logger.error(f'[MongoDB] get_slack_pin_sync failed for {key}: {e}')
        return None


def save_slack_pin_sync(key: str, slack_channel: str, slack_ts: str) -> None:
    """Upserts the Slack message handle (channel + ts) for a named pinned message."""
    try:
        sync_slack_pins_collection.update_one(
            {'key': key},
            {'$set': {
                'slack_channel': slack_channel,
                'slack_ts': slack_ts,
                'updated_at': datetime.utcnow(),
            }},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[MongoDB] save_slack_pin_sync failed for {key}: {e}')


# ── Jira tickets ──────────────────────────────────────────────────────────────

def upsert_jira_ticket_sync(issue_key: str, status: str, summary: str, url: str) -> bool:
    """Upserts a Jira ticket. Returns True if the status changed (or ticket is new)."""
    try:
        existing = sync_jira_tickets_collection.find_one({'issue_key': issue_key}, {'status': 1})
        status_changed = existing is None or existing.get('status') != status
        now = datetime.utcnow()
        update = {
            '$set': {
                'issue_key': issue_key,
                'summary': summary,
                'status': status,
                'url': url,
                'updated_at': now,
            }
        }
        if status_changed:
            update['$set']['status_since'] = now
        sync_jira_tickets_collection.update_one({'issue_key': issue_key}, update, upsert=True)
        return status_changed
    except Exception as e:
        logger.error(f'[MongoDB] Error upserting Jira ticket {issue_key}: {e}')
        return False


def mirror_console_jira_ticket_sync(issue_key: str, status: str, summary: str, url: str, status_since: str) -> None:
    """Mirror a Jira transition into the console fields of jira_tickets (services/jira_store.py) so the console Jira
    tab reflects it live, without waiting for a manual Sync. `status_since` MUST be Jira's `updated`
    timestamp string - the same value the manual Sync writes (_scan_jira_team) and the value the
    console's 'handled' matching keys on. Only the lightweight fields are set; the richer ones
    (reporter, communication_link, ...) fill in on the next Sync. Never raises."""
    try:
        from services import jira_store
        rec = {'issue_key': issue_key, 'status': status, 'summary': summary, 'url': url,
               'synced_at': datetime.utcnow().isoformat()}
        if status_since:
            rec['status_since'] = status_since
        sync_db[jira_store.COLLECTION].update_one(
            {'issue_key': issue_key}, {'$set': jira_store.from_console(rec)}, upsert=True)
    except Exception as e:
        logger.error(f'[MongoDB] Error mirroring console Jira ticket {issue_key}: {e}')


def mark_console_jira_handled_sync(issue_key: str) -> None:
    """Mirror a Slack 'Передано' into the console: record the ticket as handled at its CURRENT
    cycle (jira_tickets.jira_updated, so the exact-string match the console uses holds), so
    the console Jira To-Do drops it immediately (used by services/jira_todo.py). Never raises."""
    try:
        from services import jira_store
        status_since = jira_store.cycle(sync_db, issue_key)
        sync_db['console_jira_handled'].update_one(
            {'key': issue_key},
            {'$set': {'key': issue_key, 'status_since': status_since, 'at': datetime.utcnow().isoformat()}},
            upsert=True)
    except Exception as e:
        logger.error(f'[MongoDB] Error marking console Jira handled {issue_key}: {e}')


def mark_console_jira_dismissed_sync(issue_key: str, status_since: str) -> None:
    """Record a Dismiss for the console To-Do (the same marker the console writes): the card stays hidden
    until the ticket re-enters the status (status_since changes). Never raises."""
    try:
        sync_db['console_jira_dismissed'].update_one(
            {'key': issue_key},
            {'$set': {'key': issue_key, 'status_since': status_since or '', 'at': datetime.utcnow().isoformat()}},
            upsert=True)
    except Exception as e:
        logger.error(f'[MongoDB] Error marking console Jira dismissed {issue_key}: {e}')


def get_stale_cm_tickets_sync(older_than_days: int) -> list:
    """Returns tickets in 'CM In Progress' whose status_since is older than N days."""
    try:
        threshold = datetime.utcnow() - timedelta(days=older_than_days)
        return list(sync_jira_tickets_collection.find({
            'status': 'CM In Progress',
            'status_since': {'$lt': threshold},
        }))
    except Exception as e:
        logger.error(f'[MongoDB] Error querying stale Jira tickets: {e}')
        return []


def update_jira_stale_notified_sync(issue_key: str, slack_channel: str = '', slack_ts: str = '') -> None:
    """Remember when (and where) the "stuck in CM In Progress" card was posted, so a close from the
    console can delete it too."""
    try:
        fields = {'last_stale_notified': datetime.utcnow()}
        if slack_channel and slack_ts:
            fields.update({'slack_stale_channel': slack_channel, 'slack_stale_ts': slack_ts})
        sync_jira_tickets_collection.update_one({'issue_key': issue_key}, {'$set': fields})
    except Exception as e:
        logger.error(f'[MongoDB] Error updating stale_notified for {issue_key}: {e}')


def set_jira_discord_channel_sync(issue_key: str, discord_channel_id: int) -> None:
    try:
        sync_jira_tickets_collection.update_one(
            {'issue_key': issue_key},
            {'$set': {'discord_channel_id': discord_channel_id}},
        )
        logger.info(f'[MongoDB] Discord channel {discord_channel_id} linked to Jira {issue_key}')
    except Exception as e:
        logger.error(f'[MongoDB] Failed to set discord_channel_id for {issue_key}: {e}')


def queue_jira_discord_relink_sync(issue_key: str, discord_channel_id: int) -> None:
    """Point a ticket at a (new) Discord channel AND clear the auto-linked flag so the Discord
    loop links this channel too. Used when adding another user's chat to an existing URG ticket."""
    try:
        sync_jira_tickets_collection.update_one(
            {'issue_key': issue_key},
            {'$set': {'discord_channel_id': discord_channel_id},
             '$unset': {'discord_auto_linked': '', 'discord_link_attempts': ''}},
            upsert=True,
        )
        logger.info(f'[MongoDB] Queued Discord re-link {discord_channel_id} for Jira {issue_key}')
    except Exception as e:
        logger.error(f'[MongoDB] Failed to queue Discord re-link for {issue_key}: {e}')


def mark_jira_ticket_needs_postpone_sync(issue_key: str) -> None:
    """Mark a Jira ticket so the Discord bot re-enters jira-waiting mode on next loop check.
    Called when a Jira ticket transitions back to Backlog from CM In Progress."""
    try:
        sync_jira_tickets_collection.update_one(
            {'issue_key': issue_key},
            {'$set': {'needs_discord_postpone': True}},
        )
        logger.info(f'[MongoDB] Marked needs_discord_postpone for Jira {issue_key}')
    except Exception as e:
        logger.error(f'[MongoDB] Failed to mark needs_discord_postpone for {issue_key}: {e}')


def save_jira_slack_notification_sync(issue_key: str, slack_channel: str, slack_ts: str) -> None:
    """Saves the Slack channel+ts of the CM In Progress notification so Discord bot can delete it later."""
    try:
        sync_jira_tickets_collection.update_one(
            {'issue_key': issue_key},
            {'$set': {'slack_channel': slack_channel, 'slack_ts': slack_ts}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[MongoDB] Error saving Jira Slack notification for {issue_key}: {e}')


def get_jira_slack_notification_sync(issue_key: str):
    """Return (slack_channel, slack_ts) stored for a Jira issue's CM/URG notification, or (None, None).
    Used to delete/update that Slack message when the transfer is done from the console."""
    try:
        doc = sync_jira_tickets_collection.find_one(
            {'issue_key': issue_key}, {'slack_channel': 1, 'slack_ts': 1, '_id': 0}) or {}
        return doc.get('slack_channel'), doc.get('slack_ts')
    except Exception as e:
        logger.error(f'[MongoDB] Error reading Jira Slack notification for {issue_key}: {e}')
        return None, None


def save_jira_ticket_creator_sync(issue_key: str, slack_user_id: str) -> None:
    """Remembers which Slack user (a CM) created a Jira ticket, so we can @mention them
    later (e.g. when a URG ticket reaches Review). Stored as a field that the status-message
    upsert never clears."""
    try:
        sync_jira_tickets_collection.update_one(
            {'issue_key': issue_key},
            {'$set': {'creator_slack_id': slack_user_id}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[MongoDB] Error saving Jira ticket creator for {issue_key}: {e}')


def get_jira_ticket_creator_sync(issue_key: str) -> str | None:
    """Returns the Slack user id of the CM who created the ticket, or None."""
    try:
        doc = sync_jira_tickets_collection.find_one(
            {'issue_key': issue_key}, {'creator_slack_id': 1, '_id': 0}
        )
        return (doc or {}).get('creator_slack_id') or None
    except Exception as e:
        logger.error(f'[MongoDB] Error fetching Jira ticket creator for {issue_key}: {e}')
        return None


# ── URG duplicate detection (local, lag-free) ─────────────────────────────────
# Jira's search index is eventually consistent, so a just-created URG ticket isn't findable via JQL for
# some seconds/minutes - and console-created URG tickets don't even fill the app_ids/playgrounds custom
# fields the JQL check reads. So we ALSO keep a tiny local record of every URG we create and check it
# first: same playground AND (a shared app_id OR the same game name) within the window.

def _urg_tokens(vals) -> list:
    out = set()
    for v in (vals or []):
        s = str(v).strip().lower()
        if s:
            out.add(s)
    return sorted(out)


def save_recent_urg_sync(issue_key: str, app_ids, playgrounds, app_name: str = '') -> None:
    """Record a freshly-created URG ticket so the next request can dedup against it immediately."""
    if not issue_key:
        return
    try:
        sync_urg_dedup_collection.update_one(
            {'issue_key': issue_key},
            {'$set': {'issue_key': issue_key,
                      'apps': _urg_tokens(app_ids),
                      'pgs': _urg_tokens(playgrounds),
                      'name': str(app_name or '').strip().lower(),
                      'created_at': datetime.utcnow()}},
            upsert=True,
        )
    except Exception as e:
        logger.error(f'[MongoDB] Error saving recent URG {issue_key}: {e}')


def find_recent_urg_local(app_ids, playgrounds, app_name: str = '', minutes: int = 60) -> str | None:
    """Key of a URG created in the last `minutes` sharing a playground AND (an app_id OR the game name),
    from the local store only (no Jira search lag). Newest match wins; None if nothing matches."""
    try:
        want_apps = set(_urg_tokens(app_ids))
        want_pgs = set(_urg_tokens(playgrounds))
        want_name = str(app_name or '').strip().lower()
        if not want_pgs or (not want_apps and not want_name):
            return None
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        for doc in sync_urg_dedup_collection.find({'created_at': {'$gte': cutoff}}).sort('created_at', -1):
            if not (want_pgs & set(doc.get('pgs') or [])):
                continue
            if (want_apps & set(doc.get('apps') or [])) or (want_name and want_name == doc.get('name')):
                return doc.get('issue_key')
        return None
    except Exception as e:
        logger.error(f'[MongoDB] Error finding recent URG (local): {e}')
        return None


# ── Discord ticket search ─────────────────────────────────────────────────────

def get_discord_ticket_html_sync(user_id: int, ticket_number: int) -> str | None:
    """Return conversation_html for a specific closed Discord ticket, or None."""
    try:
        doc = sync_discord_tickets_collection.find_one(
            {'user_id': user_id, 'ticket_number': ticket_number},
            {'conversation_html': 1, '_id': 0}
        )
        return (doc or {}).get('conversation_html') or None
    except Exception as e:
        logger.error(f'[MongoDB] Error fetching ticket HTML for user {user_id} ticket #{ticket_number}: {e}')
        return None


def search_discord_tickets_by_user_sync(query: str) -> list:
    """
    Search discord_tickets by Discord username (partial, case-insensitive)
    OR by Discord user_id (if query is a pure integer).
    Returns up to 25 tickets sorted newest-first.
    Projection excludes conversation_html (large field) to keep response light.
    """
    query = (query or '').strip()
    if not query:
        return []

    conditions = []
    try:
        uid = int(query)
        conditions.append({'user_id': uid})
    except ValueError:
        pass
    conditions.append({'user_name': {'$regex': re.escape(query), '$options': 'i'}})

    mongo_query = {'$or': conditions} if len(conditions) > 1 else conditions[0]
    try:
        return list(sync_discord_tickets_collection.find(
            mongo_query,
            {
                'ticket_number': 1, 'user_name': 1, 'user_id': 1,
                'status': 1, 'created_at': 1, 'messages_count': 1,
                'ticket_type': 1, '_id': 0,
            }
        ).sort('created_at', -1).limit(25))
    except Exception as e:
        logger.error(f'[MongoDB] Error searching discord tickets for "{query}": {e}')
        return []

