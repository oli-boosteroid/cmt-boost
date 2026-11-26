import os
import ssl
import certifi
import asyncio
import time
from collections import defaultdict, deque
from typing import Dict, List
from datetime import timedelta
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from slack_sdk.web.async_client import AsyncWebClient
import logging

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

_notify_tasks = set()   # strong refs so fire-and-forget console-notify tasks aren't GC'd before they run


def _notify_bg(event_type, title, body="", url=""):
    """Fan a per-user console notification (from the console notify prefs) without blocking the Discord
    loop - the sends are sync HTTP. Best-effort; notify_event itself never raises."""
    try:
        from services.user_notify_service import notify_event
        t = asyncio.create_task(asyncio.to_thread(notify_event, event_type, title, body, url))
        _notify_tasks.add(t)
        t.add_done_callback(_notify_tasks.discard)
    except Exception as e:
        logger.debug("[Discord] console notify (%s) failed to start: %s", event_type, e)


MODERATORS = [m.strip() for m in os.getenv('MODERATORS', '').split(',')]
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ELITE_ROLE_ID = int(os.getenv('DISCORD_ELITE_ROLE_ID', 0))

# Image-spam detection: alert the TG meeting group when a user posts more than 3
# images (i.e. 4+) within the window, server-wide. Alert only, no moderation action.
IMAGE_SPAM_WINDOW_SECONDS = 300     # 5-minute sliding window
IMAGE_SPAM_THRESHOLD = 4           # 4+ images in the window trips the alert
IMAGE_SPAM_ALERT_COOLDOWN = 300    # don't re-alert the same user more than once per window
_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.heic')


def _count_image_attachments(message) -> int:
    """Number of image attachments in a Discord message (by content-type, filename fallback)."""
    count = 0
    for att in message.attachments:
        ctype = (att.content_type or '').lower()
        if ctype.startswith('image/'):
            count += 1
        elif not ctype and att.filename.lower().endswith(_IMAGE_EXTENSIONS):
            count += 1
    return count


def _image_urls(message) -> list:
    """URLs of the image attachments in a message (same test as _count_image_attachments), so the
    image-spam alert can attach a couple of the offending pictures to the Telegram message."""
    urls = []
    for att in message.attachments:
        ctype = (att.content_type or '').lower()
        if ctype.startswith('image/') or (not ctype and att.filename.lower().endswith(_IMAGE_EXTENSIONS)):
            if att.url:
                urls.append(att.url)
    return urls

# MongoDB (sync - used in static methods called from sync context)
from services import db as _shared   # one shared client per cluster for the whole process
_db = _shared.main_db()


class DiscordBot:
    def __init__(self, SLACK_DISCORD_CHANNEL_ID, mongo_db=None):
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.slack_client = AsyncWebClient(token=os.getenv('SLACK_TOKEN'))
        self.slack_channel_id = os.getenv('SLACK_DISCORD_CHANNEL_ID')
        self.discord_forums_id = os.getenv('DISCORD_FORUMS_ID')

        self.mongo_db = mongo_db

        # Rate limiting tracking
        self.message_queue = asyncio.Queue()
        self.sending_messages = False
        self.message_timestamps: Dict[int, List[float]] = defaultdict(list)
        # Image-spam tracking: user_id → [(ts, jump_url, img_count)]; user_id → last-alert ts
        self.image_message_log: Dict[int, List] = defaultdict(list)
        self.image_alert_cooldown: Dict[int, float] = {}
        self.WELCOME_MESSAGE = os.getenv("DISCORD_WELCOME_MESSAGE")

        asyncio.create_task(self.process_message_queue())

        # Intents
        # Only what the bot uses. Presence is deliberately NOT requested: nothing here reads user
        # status/activity, and asking for it (Intents.all()) would make the bot fail to connect once
        # the Presence toggle is switched off in the Developer Portal.
        intents = discord.Intents.default()
        intents.message_content = True     # ModMail DMs, moderator mentions, forum posts, image-spam checks
        intents.members = True             # welcome message on join, Elite role on server boost
        intents.guilds = True

        self.bot = commands.Bot(command_prefix="/", intents=intents)

        async def _setup_hook():
            if self.mongo_db is not None:
                try:
                    from discord_notifications.cogs.modmail import setup as modmail_setup
                    await modmail_setup(self.bot, self.mongo_db)
                    logger.info("[Discord] ModMail cog loaded successfully")
                except Exception as e:
                    logger.error(f"[Discord] Failed to load ModMail cog: {e}")

                try:
                    from discord_notifications.cogs.wishlist import setup as wishlist_setup
                    await wishlist_setup(self.bot, self.mongo_db)
                    logger.info("[Discord] Wishlist cog loaded successfully")
                except Exception as e:
                    logger.error(f"[Discord] Failed to load Wishlist cog: {e}")

                # Sync slash commands to the guild for instant availability
                try:
                    guild_id = int(os.getenv('DISCORD_GUILD_ID', 0))
                    if guild_id:
                        guild_obj = discord.Object(id=guild_id)
                        await self.bot.tree.sync(guild=guild_obj)
                        logger.info(f"[Discord] Slash commands synced to guild {guild_id}")
                    else:
                        await self.bot.tree.sync()
                        logger.info("[Discord] Slash commands synced globally")
                except Exception as e:
                    logger.error(f"[Discord] Failed to sync slash commands: {e}")
            else:
                logger.warning("[Discord] ModMail cog not loaded: MongoDB client not provided")

        self.bot.setup_hook = _setup_hook

        @self.bot.event
        async def on_member_join(member):
            await self.handle_new_member(member)

        @self.bot.event
        async def on_message(message):
            if message.author == self.bot.user:
                return

            # DMs and bot replies are handled exclusively by the ModMail cog.
            # Without this guard, a user tagging the bot or replying to it in DMs
            # would fire both the ModMail handler AND the moderator-mention alert below,
            # causing a duplicate notification in #discord-notifications.
            if not message.guild:
                return
            remember_user_message(message)   # where each user wrote in the last 24h (spam clean-up button)
            if await _delete_if_in_cleanup(message):   # "Delete messages" was pressed while they kept posting
                return

            # Forum thread reply -> keep the console forum-post doc's messages up to date.
            try:
                _parent = getattr(message.channel, "parent_id", None)
                if _parent is not None and self.discord_forums_id:
                    _fids = [int(i.strip()) for i in self.discord_forums_id.split(",") if i.strip()]
                    if _parent in _fids:
                        from services.discord_console_snapshot import append_forum_message
                        await append_forum_message(message)
            except Exception as e:
                logger.error(f"[Discord] forum append failed: {e}")

            # ---- Image-spam detection (server-wide) → alert TG meeting group ----
            img_count = _count_image_attachments(message)
            if img_count:   # moderators are exempt from the image ratelimit
                try:
                    from services.identity_service import get_discord_moderators
                    _img_mods = get_discord_moderators()
                except Exception:
                    _img_mods = MODERATORS
                if message.author.name in _img_mods or getattr(message.author, "display_name", "") in _img_mods:
                    img_count = 0
            if img_count:   # whitelisted users: permanent (console admin) or 2-day (Telegram button)
                try:
                    from services.image_whitelist import is_image_whitelisted
                    _names = (message.author.name, getattr(message.author, "display_name", ""),
                              getattr(message.author, "global_name", "") or "")
                    if is_image_whitelisted(message.author.id, _names):
                        img_count = 0
                except Exception as _e:
                    logger.debug("ignored: %s", _e)
            if img_count:
                now_ts = time.time()
                uid = message.author.id
                log = self.image_message_log[uid]
                log.append((now_ts, message.jump_url, img_count, _image_urls(message)))
                cutoff = now_ts - IMAGE_SPAM_WINDOW_SECONDS
                log[:] = [e for e in log if e[0] >= cutoff]
                total_images = sum(e[2] for e in log)
                last_alert = self.image_alert_cooldown.get(uid, 0)
                if total_images >= IMAGE_SPAM_THRESHOLD and (now_ts - last_alert) >= IMAGE_SPAM_ALERT_COOLDOWN:
                    self.image_alert_cooldown[uid] = now_ts
                    _urls = list(dict.fromkeys(e[1] for e in log))            # dedupe message links, keep order
                    links = "\n".join(_urls[:3]) + (f"\n(+{len(_urls) - 3} more)" if len(_urls) > 3 else "")
                    # One of the offending images to attach to the Telegram alert.
                    image_urls = []
                    for e in log:
                        for u in (e[3] if len(e) > 3 else []):
                            if u not in image_urls:
                                image_urls.append(u)
                    image_urls = image_urls[:1]
                    author_name = message.author.display_name
                    alert_text = f"User {author_name} exceeded the image ratelimit\n{links}"
                    try:
                        import html as _h
                        from telegram_bot.sender import send_image_spam_alert_html
                        from services.reminders_service import on_shift_mentions_html
                        _tags = on_shift_mentions_html()   # @-tag whoever is on shift now (like the SLA alerts)
                        _msg = (_tags + "\n" if _tags else "") + _h.escape(alert_text)
                        await asyncio.to_thread(send_image_spam_alert_html, _msg, image_urls, uid)
                        logger.info(f"[Discord] Image-spam alert sent for {author_name} ({uid}): {total_images} images in window")
                    except Exception as e:
                        logger.error(f"[Discord] Failed to send image-spam alert for {uid}: {e}")

            chat_name = message.channel.name

            # Effective moderator list = .env MODERATORS + console exclusions + linked team
            # Discord accounts; falls back to the .env MODERATORS on any Mongo issue.
            try:
                from services.identity_service import get_discord_moderators
                _mods = get_discord_moderators()
            except Exception:
                _mods = MODERATORS
            mentioned_mods = [
                user for user in message.mentions
                if user.name in _mods or user.display_name in _mods
            ]

            if mentioned_mods:
                mods_names = ", ".join([mod.display_name for mod in mentioned_mods])
                alert_msg = f"{message.author.name} mentioned {mods_names} in {chat_name}: \n{message.content}"
                discord_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}" if message.guild else ""

                # Console bell: same trigger as the Slack mod-mention alert (mirrors the main bot).
                _notify_bg('discord_mod_mention', 'Moderator mentioned (Discord)', alert_msg, discord_url)

                try:
                    author_id = message.author.id
                    author_name = message.author.name
                    email = None
                    if self.mongo_db is not None:
                        try:
                            doc = await self.mongo_db['users_emails'].find_one(
                                {'discord_user_id': {'$in': [author_id, str(author_id)]}},
                                {'cloud_user_id': 1, '_id': 0}, sort=[('cloud_user_id', -1)],
                            )
                            uid = (doc or {}).get('cloud_user_id')
                            email = f"User ID {uid}" if uid else None
                        except Exception as e:
                            logger.error(f"[Discord] Failed to get User ID for user {author_id}: {e}")
                    from services.redact import link_user_ids_slack
                    email_text = link_user_ids_slack(email or "No User ID on file")

                    # Console: snapshot this mention for the Mentions tab.
                    try:
                        from services.discord_console_snapshot import snapshot_mention
                        # Nothing about the user is copied: the console reads the User ID from the directory.
                        await snapshot_mention(message)
                    except Exception as e:
                        logger.error(f"[Discord] mention snapshot failed: {e}")

                    section_text = (
                        f"{alert_msg}\n"
                        f"{author_name} | {email_text}"
                    )
                    if discord_url:
                        section_text += f"\n{discord_url}"

                    action_buttons = [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Add Information"},
                            "action_id": "add_information_button",
                            "style": "primary",
                            "value": discord_url,
                        }
                    ]
                    if not email:
                        action_buttons.append({
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Add Email"},
                            "action_id": "add_discord_user_email_button",
                            "value": f"{author_id}:{author_name}",
                        })

                    msg_blocks = [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": section_text}
                        }
                    ]
                    if discord_url:
                        msg_blocks.append({
                            "type": "actions",
                            "elements": action_buttons,
                        })

                    # text= is what the Telegram bridge reads; include discord_url so it appears in TG
                    slack_text = f"{alert_msg}\n{discord_url}" if discord_url else alert_msg
                    _slack_resp = await self.slack_client.chat_postMessage(
                        channel=self.slack_channel_id,
                        text=slack_text,
                        blocks=msg_blocks,
                    )
                    # Store the Slack channel+ts on the mention doc so an email added later from the
                    # console can be reflected back into this Slack thread.
                    try:
                        from services.mongo_db_service import sync_db
                        await asyncio.to_thread(
                            sync_db["discord_mentions"].update_one,
                            {"_id": str(message.id)},
                            {"$set": {"slack_channel": _slack_resp.get("channel"), "slack_ts": _slack_resp.get("ts")}})
                    except Exception as _e:
                        logger.warning(f"[Discord] could not store mention slack ts: {_e}")
                    logger.info(f"[Discord] Sent Discord notification to Slack {alert_msg}")
                except Exception as e:
                    logger.error(f"[Discord] Failed to send Discord notification to Slack {alert_msg}. Error: {e}")

        @self.bot.event
        async def on_thread_create(thread):
            forum_ids = [int(id.strip()) for id in self.discord_forums_id.split(',')]

            if thread.parent_id in forum_ids:
                try:
                    starter_message = await thread.fetch_message(thread.id)

                    slack_message = {
                        "channel": self.slack_channel_id,
                        "text": f"New post in forum: {thread.name}",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"*New post in Discord forum!*\n\n*Title - {thread.name}*\nContent - {starter_message.content[:300] if starter_message.content else 'No content'}",
                                }
                            },
                            {
                                "type": "context",
                                "elements": [
                                    {
                                        "type": "mrkdwn",
                                        "text": f"Author: {thread.owner.display_name if thread.owner else 'No author'}\n<https://discord.com/channels/{thread.guild.id}/{thread.id}>",
                                    }
                                ]
                            }
                        ]
                    }

                    await self.slack_client.chat_postMessage(**slack_message)
                    logger.info(f"[Discord] Sent Discord forum post notification to Slack {slack_message}")

                    # Console bell: same trigger as the Slack forum-post alert.
                    _notify_bg('discord_forum', 'New Discord forum post',
                               f"{thread.name}: {(starter_message.content or '')[:200]}",
                               f"https://discord.com/channels/{thread.guild.id}/{thread.id}")

                    # Console: snapshot the forum post for the Forum tab.
                    try:
                        from services.discord_console_snapshot import snapshot_forum_post
                        await snapshot_forum_post(thread, starter_message)
                    except Exception as e:
                        logger.error(f"[Discord] forum snapshot failed: {e}")

                except Exception as e:
                    logger.error(f"[Discord] Failed to send forum notification to Slack. Error: {e}")

        @self.bot.event
        async def on_member_update(before: discord.Member, after: discord.Member):
            if before.premium_since is None and after.premium_since is not None:
                if not ELITE_ROLE_ID:
                    return
                elite_role = after.guild.get_role(ELITE_ROLE_ID)
                if elite_role and elite_role not in after.roles:
                    try:
                        await after.add_roles(elite_role, reason="Server boost")
                        logger.info(f"[Discord] Elite role granted to {after.name} for boosting the server")
                        try:
                            await self.slack_client.chat_postMessage(
                                channel=os.getenv('SLACK_FLOOD_CHANNEL_ID'),
                                text=f"Elite role granted to *{after.name}* for boosting the server"
                            )
                        except Exception as e:
                            logger.error(f"[Discord] Failed to send Elite role Slack notification: {e}")
                    except Exception as e:
                        logger.error(f"[Discord] Failed to grant Elite role to {after.name}: {e}")

    async def send_modmail_slack_notification(self, event_type: str, ticket_number, ticket_type: str,
                                              user_name: str, guild_id: int, channel_id: int):
        channel_link = f"https://discord.com/channels/{guild_id}/{channel_id}"

        if event_type == "created":
            text = f"New ticket #{ticket_number} ({ticket_type.upper()}) created by {user_name}\n{channel_link}"
        elif event_type == "message":
            text = f"New message in ticket #{ticket_number} from {user_name}\n{channel_link}"
        elif event_type == "closed":
            text = f"Ticket #{ticket_number} closed by {user_name}\n{channel_link}"
        else:
            return

        try:
            await self.slack_client.chat_postMessage(
                channel=os.getenv('SLACK_DISCORD_MODMAIL_CHANNEL_ID'),
                text=text
            )
            logger.info(f"[Discord] Modmail Slack notification sent: {event_type} ticket #{ticket_number}")
        except Exception as e:
            logger.error(f"[Discord] Failed to send modmail Slack notification: {e}")

    async def handle_new_member(self, member):
        try:
            await self.message_queue.put(member)
        except Exception as e:
            logger.error(f"[Discord] Error handling new member {member.name}: {e}")

    async def process_message_queue(self):
        self.sending_messages = True
        while self.sending_messages:
            try:
                member = await self.message_queue.get()
                current_time = time.time()
                guild_id = member.guild.id

                timestamps = self.message_timestamps[guild_id]
                timestamps = [ts for ts in timestamps if current_time - ts < 60]
                self.message_timestamps[guild_id] = timestamps

                if len(timestamps) < 5:
                    try:
                        await member.send(self.WELCOME_MESSAGE)
                        timestamps.append(current_time)
                        self.message_timestamps[guild_id] = timestamps
                        logger.info(f"[Discord] Sent Discord Welcome Message to user: {member.name}")

                        try:
                            await self.slack_client.chat_postMessage(
                                channel=os.getenv('SLACK_FLOOD_CHANNEL_ID'),
                                text=f"Discord Welcome Message was sent to user: {member}"
                            )
                            logger.info(f"[Discord] Welcome Discord Message notification was sent to Slack. User: {member}")
                        except Exception as e:
                            logger.error(f"[Discord] Welcome Discord Message notification wasn't sent to Slack. User: {member}. Error: {e}")
                    except discord.Forbidden:
                        logger.info(f"Cannot send DM to {member.name}. DMs are disabled.")
                        try:
                            await self.slack_client.chat_postMessage(
                                channel=os.getenv('SLACK_FLOOD_CHANNEL_ID'),
                                text=f"Cannot send DM to {member.name} — DMs are disabled."
                            )
                        except Exception as e:
                            logger.error(f"[Discord] Failed to send DM-disabled notification to Slack. User: {member.name}. Error: {e}")
                    except Exception as e:
                        logger.error(f"Failed to send Discord Welcome Message. Error: {e}")
                else:
                    logger.info(f"[Discord] Rate limit reached for guild {guild_id}, delaying message to {member.name}")
                    await asyncio.sleep(10)
                    await self.message_queue.put(member)

                self.message_queue.task_done()

            except Exception as e:
                logger.error(f"[Discord] Error in Discord Welcome Message queue processing: {e}")
                await asyncio.sleep(1)

    async def run(self):
        await self.bot.start(DISCORD_TOKEN)


# Live discord.py Bot instance, set once the bot starts, so other threads (e.g. the
# console_actions worker) can bridge coroutines onto its loop via run_coroutine_threadsafe.
_running_bot = None


def get_running_discord_bot():
    """The live discord.py Bot (has .loop and .get_cog), or None if not started yet."""
    return _running_bot


def _image_guild(bot):
    gid = int(os.getenv('DISCORD_GUILD_ID', 0) or 0)
    return bot.get_guild(gid) if gid else None


async def resolve_member_name(bot, user_id: int) -> str:
    """Best-effort display name for a Discord user id: member cache -> API member fetch -> API user
    fetch (works even if they left the server) -> the raw id as a last resort."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return str(user_id)
    guild = _image_guild(bot)
    try:
        m = guild.get_member(uid) if guild else None
        if m is None and guild is not None:
            m = await guild.fetch_member(uid)
        if m is not None:
            return m.display_name
    except Exception as _e:
        logger.debug("ignored: %s", _e)
    try:
        u = bot.get_user(uid) or await bot.fetch_user(uid)
        if u is not None:
            return getattr(u, "global_name", None) or u.name
    except Exception as _e:
        logger.debug("ignored: %s", _e)
    return str(uid)


# Where each user posted in the last 24h: user_id -> deque of (posted_at, channel_id, message_id). Filled by
# on_message, so the Telegram "Delete user's messages" button can delete exactly those messages at once
# instead of reading the whole history of every channel (slow, and it missed forum threads / voice chats).
_RECENT_KEEP = timedelta(hours=24)
_RECENT_MAX_PER_USER = 2000
_recent_msgs = defaultdict(deque)


def remember_user_message(message):
    try:
        q = _recent_msgs[message.author.id]
        q.append((message.created_at, message.channel.id, message.id))
        cutoff = discord.utils.utcnow() - _RECENT_KEEP
        while q and (q[0][0] < cutoff or len(q) > _RECENT_MAX_PER_USER):
            q.popleft()
    except Exception as _e:
        logger.debug("ignored: %s", _e)


async def _scan_and_purge(guild, uid, after):
    """Fallback when the bot has no record of the user's messages (e.g. it restarted): scan recent
    history of every text channel, active thread and voice-channel chat, a few at a time."""
    targets = list(guild.text_channels) + list(getattr(guild, "threads", [])) + list(guild.voice_channels)
    sem = asyncio.Semaphore(4)
    total = 0

    async def one(ch):
        nonlocal total
        try:
            perms = ch.permissions_for(guild.me)
            if not (perms.read_message_history and perms.manage_messages):
                return
            async with sem:
                purged = await ch.purge(limit=500, after=after, check=lambda m: m.author.id == uid, bulk=True)
            total += len(purged)
        except (discord.Forbidden, discord.NotFound, AttributeError):
            return
        except discord.HTTPException as e:
            logger.warning(f"[Discord] purge failed in #{getattr(ch, 'name', ch)} for {uid}: {e}")
    await asyncio.gather(*(one(ch) for ch in targets))
    return total


# "Delete messages" pressed while the spammer is still posting (the alert fires at the START of a burst):
# for this long afterwards every new message of that user is deleted as it arrives.
_CLEANUP_WINDOW = timedelta(minutes=10)
_cleanup_until = {}       # user_id -> utc datetime
_cleanup_count = defaultdict(int)


async def _delete_if_in_cleanup(message):
    until = _cleanup_until.get(message.author.id)
    if not until:
        return False
    if discord.utils.utcnow() > until:
        _cleanup_until.pop(message.author.id, None)
        n = _cleanup_count.pop(message.author.id, 0)
        if n:
            logger.info(f"[Discord] Cleanup window ended for {message.author.id}: {n} late message(s) deleted")
        return False
    try:
        await message.delete()
        _cleanup_count[message.author.id] += 1
        logger.info(f"[Discord] Deleted a late message of {message.author.id} in #{getattr(message.channel, 'name', '?')} "
                    f"(cleanup window)")
    except (discord.NotFound, discord.Forbidden):
        pass
    except discord.HTTPException as e:
        logger.warning(f"[Discord] cleanup-window delete failed: {e}")
    return True


async def delete_user_messages_last_24h(bot, user_id: int):
    """Delete every message a user posted in the guild in the last 24h (the Telegram "Delete user's
    messages" button). Uses the record of where they wrote (exact, a few API calls); falls back to a
    bounded parallel scan only when there is no record. Returns (deleted, display_name, already_gone):
    already_gone counts recorded messages that were deleted before (e.g. by the image ratelimit)."""
    guild = _image_guild(bot)
    name = await resolve_member_name(bot, user_id)
    if not guild:
        return 0, name, 0
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return 0, name, 0
    _cleanup_until[uid] = discord.utils.utcnow() + _CLEANUP_WINDOW   # catch what they post from now on
    after = discord.utils.utcnow() - _RECENT_KEEP
    recorded = [r for r in list(_recent_msgs.get(uid, ())) if r[0] >= after]
    if not recorded:
        deleted = await _scan_and_purge(guild, uid, after)
        logger.info(f"[Discord] Deleted {deleted} messages from {name} ({uid}) by scan")
        return deleted, name, 0
    by_channel = defaultdict(list)
    for _ts, ch_id, msg_id in recorded:
        by_channel[ch_id].append(msg_id)
    deleted = gone = 0
    for ch_id, ids in by_channel.items():
        ch = guild.get_channel_or_thread(ch_id) or bot.get_channel(ch_id)
        if ch is None:
            gone += len(ids)
            continue
        for mid in ids:   # one call per message: bulk delete fails as a whole if any one is already gone
            try:
                await ch.get_partial_message(mid).delete()
                deleted += 1
            except discord.NotFound:
                gone += 1
            except discord.Forbidden:
                logger.warning(f"[Discord] no permission to delete in #{getattr(ch, 'name', ch_id)}")
                break
            except discord.HTTPException as e:
                logger.warning(f"[Discord] delete {mid} failed: {e}")
    _recent_msgs.pop(uid, None)
    logger.info(f"[Discord] Deleted {deleted} messages from {name} ({uid}); {gone} were already gone")
    # Safety net for messages the in-memory record does not have (posted before a bot restart): sweep every
    # channel in the BACKGROUND, so the Telegram answer is not held up by it (it can take a minute or two).
    async def _sweep():
        try:
            n = await _scan_and_purge(guild, uid, after)
            logger.info(f"[Discord] Background sweep for {name} ({uid}): {n} more message(s) deleted")
        except Exception as e:
            logger.warning(f"[Discord] background sweep for {uid} failed: {e}")
    asyncio.get_running_loop().create_task(_sweep())
    return deleted, name, gone


async def start_discord_bot(mongo_db=None):
    global _running_bot
    bot_instance = DiscordBot(DISCORD_TOKEN, mongo_db)
    _running_bot = bot_instance.bot
    await bot_instance.run()
