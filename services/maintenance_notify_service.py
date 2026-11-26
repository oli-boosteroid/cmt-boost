"""Notify users about maintenance END via OUR bot (Discord ticket / Reddit ModMail),
as an alternative to the legacy "email to another bot" path.

A subscription links an active game/server maintenance (matched by normalized entity name)
to a target (a Discord ticket channel or a Reddit modmail conversation). When the maintenance ends
(Mattermost admin event, services/mm_maintenance.py: after a 1 h / 30 min delay and only if it really
ended; or an admin ending it from the console), matching subscriptions are flagged `ended_pending`; background workers then send the localized end-message TO THE USER and
move the chat to Waiting on user in the console:
- Discord: a DM relayed through the ticket (handled in the modmail cog loop).
- Reddit: a public modmail reply (handled here).
"""

import logging
from datetime import datetime

from utils.general_utils import clean_title
from services.mongo_db_service import (
    create_maintenance_subscription_sync,
    maintenance_subscription_exists_sync,
    mark_maintenance_subscriptions_ended_sync,
    get_pending_maintenance_subscriptions_sync,
    mark_maintenance_subscription_notified_sync,
)

logger = logging.getLogger(__name__)

# Localized maintenance-END message templates. {name} is replaced with the entity name.
# Keyed by maintenance type (game/server), then language.
TEMPLATES = {
    'game': {
        'en': (
            'Maintenance for the game *{name}* has been completed\n\n'
            'Thank you for your patience and we apologise for any inconvenience caused! Enjoy your game!'
        ),
        'ru': (
            'Обслуживание игры *{name}* завершено\n\n'
            'Благодарим вас за терпение и приносим извинения за доставленные неудобства! '
            'Желаем приятной игры!'
        ),
        'ua': (
            'Технічні роботи в грі *{name}* завершено\n\n'
            'Дякуємо за терпіння та просимо вибачення за спричинені незручності! Приємної гри!'
        ),
    },
    'server': {
        'en': (
            'Maintenance for server *{name}* has been completed\n\n'
            'Thank you for your patience and we apologise for any inconvenience caused! Enjoy your game!'
        ),
        'ru': (
            'Обслуживание сервера *{name}* завершено\n\n'
            'Благодарим вас за терпение и приносим извинения за доставленные неудобства! '
            'Желаем приятной игры!'
        ),
        'ua': (
            'Технічні роботи на сервері *{name}* завершено\n\n'
            'Дякуємо за терпіння та просимо вибачення за спричинені незручності! Приємної гри!'
        ),
    },
}

DEFAULT_LANGUAGE = 'en'
DEFAULT_TYPE = 'game'


def normalize(entity: str) -> str:
    return clean_title(entity or '').lower()


def render_message(entity: str, language: str, mtype: str = DEFAULT_TYPE) -> str:
    by_lang = TEMPLATES.get((mtype or DEFAULT_TYPE).lower(), TEMPLATES[DEFAULT_TYPE])
    template = by_lang.get((language or DEFAULT_LANGUAGE).lower(), by_lang[DEFAULT_LANGUAGE])
    return template.replace('{name}', entity or '')


def create_subscription(entity, mtype, target_type, target_id, language,
                        reddit_convo_id=None, reddit_user=None, reddit_note_id=None) -> bool:
    """Creates a maintenance-end subscription. Deduplicates by (entity, type, target)."""
    entity_norm = normalize(entity)
    if maintenance_subscription_exists_sync(entity_norm, mtype, target_type, target_id):
        logger.info(f'[Maintenance] Subscription already exists for {entity} → {target_type}:{target_id}')
        return True
    ok = create_maintenance_subscription_sync(
        entity, entity_norm, mtype, target_type, target_id, language,
        reddit_convo_id=reddit_convo_id, reddit_user=reddit_user, reddit_note_id=reddit_note_id,
    )
    if ok:
        logger.info(f'[Maintenance] Subscribed {target_type}:{target_id} to {mtype} maintenance end for "{entity}"')
    return ok


def subscribe_reddit_maintenance(entity, mtype, convo_id, language, participant=None) -> bool:
    """Subscribes a Reddit modmail conversation to a maintenance-end notice AND adds a tracking
    'Maintenance: <entity>' mod note on the conversation's participant (the note is removed when
    the maintenance ends, see process_pending_reddit_maintenance).

    Idempotent: if the subscription already exists, no duplicate note is created. Shared by the
    Slack maintenance modal and the mods-only auto-detect path so both create the note the same way.
    """
    from reddit_notifications.mod_notes import get_convo_participant, create_note

    if maintenance_subscription_exists_sync(normalize(entity), mtype, 'reddit', convo_id):
        logger.info(f'[Maintenance] Reddit subscription already exists for "{entity}" (convo {convo_id})')
        return True

    if participant is None:
        participant = get_convo_participant(convo_id)
    note_id = create_note(participant, f'Maintenance: {entity}') if participant else None
    if note_id is False:
        note_id = None  # create_note's "user no longer exists" sentinel -> store null, not a bool
    if participant and not note_id:
        logger.warning(f'[Maintenance] Could not create Reddit mod note for u/{participant} ("{entity}")')

    return create_subscription(entity, mtype, 'reddit', convo_id, language,
                               reddit_convo_id=convo_id, reddit_user=participant, reddit_note_id=note_id)


def on_maintenance_ended(entity, mtype) -> int:
    """Flags matching subscriptions as pending notification. Returns how many were flagged."""
    count = mark_maintenance_subscriptions_ended_sync(normalize(entity), mtype)
    if count:
        logger.info(f'[Maintenance] {count} subscription(s) flagged for ended {mtype} maintenance "{entity}"')
    return count


def set_console_waiting_on_user(convo_id) -> None:
    """Move a chat to Waiting on user in the console (same store as the console status dropdown)."""
    from services.mongo_db_service import sync_db
    now = datetime.utcnow()
    sync_db['console_convo_status'].update_one(
        {'convo_id': str(convo_id)},
        {'$set': {'convo_id': str(convo_id), 'status': 'waiting_user', 'updated_at': now,
                  'author_name': 'System', 'author_email': ''},
         '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


def process_pending_reddit_maintenance() -> None:
    """Sends pending maintenance-end messages to the Reddit users, then cleans up."""
    from reddit_notifications.mod_notes import post_public_reply, delete_note

    for sub in get_pending_maintenance_subscriptions_sync('reddit'):
        try:
            convo_id = sub.get('reddit_convo_id') or sub.get('target_id')
            text = render_message(sub.get('entity', ''), sub.get('language', DEFAULT_LANGUAGE), sub.get('type', DEFAULT_TYPE))
            if not convo_id:
                continue
            from services.console_actions_worker import _via_devvit
            if _via_devvit():
                from reddit_notifications.devvit_inbox import send_reddit_command
                ok, err = send_reddit_command(sub['_id'], 'modmail_reply', conversationId=convo_id,
                                              body=text, isInternal=False)
                if not ok:
                    logger.error(f'[Maintenance] Devvit relay failed for convo {convo_id}: {err}')
                    continue
            elif not post_public_reply(convo_id, text):
                continue
            try:
                from services.reddit_snapshot import mirror_sent_reply
                mirror_sent_reply(convo_id, 'System', text)   # visible in the console right away
                set_console_waiting_on_user(convo_id)
            except Exception as e:
                logger.warning(f'[Maintenance] Console update failed for convo {convo_id}: {e}')
            note_id = sub.get('reddit_note_id')
            reddit_user = sub.get('reddit_user')
            if note_id and reddit_user:
                delete_note(reddit_user, note_id)
            mark_maintenance_subscription_notified_sync(sub['_id'])
            logger.info(f'[Maintenance] Sent Reddit end message to the user for "{sub.get("entity")}" (convo {convo_id})')
        except Exception as e:
            logger.error(f'[Maintenance] Failed to process Reddit subscription {sub.get("_id")}: {e}')


def add_maintenance_notice(entity, mtype, target, ref, language, by=""):
    """Subscribe a chat to the "maintenance ended" notice and leave an internal (mods-only) note in it.
    ONE function for the Slack maintenance modal and the console "Add Maintenance" (queue action
    maintenance_notify). target "reddit": ref = ModMail conversation id; target "discord": ref = the
    ticket's Mongo id or its channel id. Nothing is sent to the user now; the localized message goes out
    when the maintenance ends. Returns (result, error)."""
    entity = (entity or "").strip()
    mtype = mtype or "game"
    language = language or "en"
    if not entity:
        return None, "entity required"
    lang_tag = {"en": "EN", "ru": "RU", "ua": "UA"}.get(language.lower(), language.upper())
    note = ("Maintenance added: subscribed to the %s (%s) %s maintenance. The user will be told "
            "automatically when it ends." % (entity, lang_tag, mtype))
    if target == "reddit":
        from reddit_notifications.mod_notes import post_internal_reply
        convo_id = str(ref or "").strip()
        if not convo_id:
            return None, "convo_id required"
        subscribe_reddit_maintenance(entity, mtype, convo_id, language)
        if not post_internal_reply(convo_id, note):
            return {"ok": True, "note": False}, None     # subscribed; only the internal note failed
        return {"ok": True}, None
    if target != "discord":
        return None, "unknown target %r" % target
    import asyncio
    from bson import ObjectId
    from services.db import main_db
    from discord_notifications.discord_notifications import get_running_discord_bot
    col = main_db()["discord_tickets"]
    ref = str(ref or "").strip()
    ticket = None
    if ref.isdigit():
        ticket = col.find_one({"channel_id": {"$in": [int(ref), ref]}}, sort=[("created_at", -1)])
    else:
        try:
            ticket = col.find_one({"_id": ObjectId(ref)})
        except Exception:
            ticket = None
    if not ticket:
        return None, "Discord ticket not found"
    create_subscription(entity, mtype, "discord", ticket.get("channel_id"), language)
    bot = get_running_discord_bot()
    cog = bot.get_cog("ModMail") if bot and not bot.is_closed() else None
    if not cog:
        return {"ok": True, "note": False}, None
    try:
        ok = asyncio.run_coroutine_threadsafe(cog.send_console_reply(ticket, note, by or "Maintenance", True),
                                              bot.loop).result(timeout=30)
    except Exception as e:
        logger.warning("[Maintenance] internal note failed for ticket %s: %s", ticket.get("_id"), e)
        ok = False
    return {"ok": True, "note": bool(ok)}, None
