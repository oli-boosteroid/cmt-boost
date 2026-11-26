import asyncio
import logging
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from discord import app_commands, RawReactionActionEvent, Message, TextChannel
from discord.ext import commands, tasks
from dotenv import load_dotenv
from slack_sdk.web.async_client import AsyncWebClient

from utils.general_utils import (
    clean_title,
    extract_link,
    extract_steam_app_id,
    extract_epic_game_slug,
    strip_epic_slug_suffix,
    extract_xbox_product_id,
    detect_store,
)
from services.boosteroid_catalog_service import find_in_catalog
from services import wishlist_store

env_path = Path(__file__).parent.parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

SMM_USER_IDS = {int(uid.strip()) for uid in os.getenv('SMM_USER_ID', '0').split(',') if uid.strip()}
WISHLIST_CHANNEL = int(os.getenv('WISHLIST_CHANNEL', 0))
WISHLIST_TRIGGER_REACTION = int(os.getenv('WISHLIST_TRIGGER_REACTION', 0))
DISCORD_GUILD_ID = int(os.getenv('DISCORD_GUILD_ID', 0))

DAILY_LINK_LIMIT = 3

# Auto-react scheduler config
_AUTO_REACT_HOUR_UTC = int(os.getenv('WISHLIST_AUTO_REACT_HOUR_UTC', '5'))   # 5 UTC = 8:00 Kyiv (UTC+3)
_AUTO_REACT_LOOKBACK_DAYS = int(os.getenv('WISHLIST_AUTO_REACT_LOOKBACK_DAYS', '30'))
_AUTO_REACT_BATCH = 5       # messages per batch
_AUTO_REACT_COOLDOWN = 300  # seconds between batches (5 minutes)
_AUTO_REACT_TIME = dtime(hour=_AUTO_REACT_HOUR_UTC, tzinfo=timezone.utc)


class WishlistCog(commands.Cog):
    def __init__(self, bot: commands.Bot, mongo_db):
        self.bot = bot
        self.collection = mongo_db['wishlist_submissions']
        self.url_mappings = mongo_db['wishlist_url_mappings']
        self.slack_client = AsyncWebClient(token=os.getenv('SLACK_TOKEN'))
        self.slack_wishlist_channel = (
            os.getenv('SLACK_WISHLIST_NOTIFICATIONS_CHANNEL_ID') or os.getenv('SLACK_FLOOD_CHANNEL_ID')
        )

    async def cog_load(self) -> None:
        """Release stuck processing claims and start the daily auto-react scheduler."""
        try:
            result = await self.collection.update_many(
                {"processing": True, "added_to_wishlist": False},
                {"$unset": {"processing": ""}},
            )
            if result.modified_count:
                logger.info(f"[Wishlist] Released {result.modified_count} stuck processing claim(s) on startup")
        except Exception as e:
            logger.error(f"[Wishlist] Failed to release stuck processing claims: {e}")
        self.auto_react_wishlist.start()

    def cog_unload(self) -> None:
        self.auto_react_wishlist.cancel()

    # ------------------------------------------------------------------
    # Daily auto-react scheduler
    # ------------------------------------------------------------------

    @tasks.loop(time=_AUTO_REACT_TIME)
    async def auto_react_wishlist(self) -> None:
        """
        Runs daily at _AUTO_REACT_TIME UTC.
        Scans the wishlist channel for messages without the trigger reaction
        (i.e. not yet approved by an SMM) and adds the reaction automatically,
        which triggers _process_smm_wishlist_reaction for each one.
        Rate limit: _AUTO_REACT_BATCH reactions, then _AUTO_REACT_COOLDOWN seconds rest.
        """
        channel: TextChannel = self.bot.get_channel(WISHLIST_CHANNEL)
        if not channel:
            logger.error("[Wishlist] Auto-react: wishlist channel not found")
            return

        emoji = self.bot.get_emoji(WISHLIST_TRIGGER_REACTION)
        if not emoji:
            logger.error(f"[Wishlist] Auto-react: trigger emoji {WISHLIST_TRIGGER_REACTION} not in bot cache")
            return

        since = datetime.now(timezone.utc) - timedelta(days=_AUTO_REACT_LOOKBACK_DAYS)
        logger.info(f"[Wishlist] Auto-react started — scanning messages since {since.date()}")

        # Collect all unprocessed messages into a list BEFORE sleeping.
        # Sleeping inside an async generator (channel.history) causes the HTTP
        # connection to time out, which silently ends iteration early.
        pending: list[Message] = []
        async for message in channel.history(limit=None, after=since, oldest_first=True):
            if message.author.bot:
                continue
            if not extract_link(message.content):
                continue
            already_reacted = any(
                hasattr(r.emoji, 'id') and r.emoji.id == WISHLIST_TRIGGER_REACTION
                for r in message.reactions
            )
            if not already_reacted:
                pending.append(message)

        logger.info(f"[Wishlist] Auto-react: {len(pending)} unprocessed message(s) found")

        batch_count = 0
        total_queued = 0

        for message in pending:
            try:
                await message.add_reaction(emoji)
            except Exception as e:
                logger.warning(f"[Wishlist] Auto-react: could not react to msg {message.id}: {e}")
                continue

            await self._process_smm_wishlist_reaction(message)

            batch_count += 1
            total_queued += 1

            if batch_count >= _AUTO_REACT_BATCH:
                logger.info(
                    f"[Wishlist] Auto-react: batch of {_AUTO_REACT_BATCH} done, "
                    f"cooling down for {_AUTO_REACT_COOLDOWN // 60} min"
                )
                await asyncio.sleep(_AUTO_REACT_COOLDOWN)
                batch_count = 0

        logger.info(f"[Wishlist] Auto-react finished — {total_queued} message(s) processed")

    @auto_react_wishlist.before_loop
    async def _before_auto_react(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Slash command: manual trigger
    # ------------------------------------------------------------------

    @app_commands.command(
        name="wishlist-react",
        description="Manually trigger auto-react on all unprocessed wishlist messages",
    )
    @app_commands.guilds(discord.Object(id=DISCORD_GUILD_ID))
    @app_commands.default_permissions(administrator=True)
    async def cmd_wishlist_react(self, interaction: discord.Interaction) -> None:
        if interaction.user.id not in SMM_USER_IDS:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Starting wishlist auto-react scan...", ephemeral=True
        )
        await self.auto_react_wishlist()
        await interaction.followup.send("Auto-react scan complete.", ephemeral=True)

    # ------------------------------------------------------------------
    # Anti-spam helpers (on_message)
    # ------------------------------------------------------------------

    async def _count_user_submissions_today(self, user_id: int) -> int:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        return await self.collection.count_documents({
            "requestor_discord_id": user_id,
            "submitted_at": {"$gte": since},
        })

    async def _link_submitted_today(self, url: str, user_id: int) -> bool:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        doc = await self.collection.find_one({
            "url": url,
            "requestor_discord_id": user_id,
            "submitted_at": {"$gte": since},
        })
        return doc is not None

    async def _user_already_added_this_game(self, user_id: int, url: str) -> bool:
        doc = await self.collection.find_one({
            "requestor_discord_id": user_id,
            "url": url,
            "added_to_wishlist": True,
        })
        return doc is not None

    async def _record_submission(self, url: str, store: str, user_id: int, username: str) -> None:
        await self.collection.insert_one({
            "url": url,
            "store": store,
            "requestor_discord_id": user_id,
            "requestor_discord_name": username,
            "submitted_at": datetime.now(timezone.utc),
            "added_to_wishlist": False,
            "added_at": None,
        })

    # ------------------------------------------------------------------
    # Per-user processing claim (keyed by url + user_id)
    # ------------------------------------------------------------------

    async def _try_claim_for_processing(self, url: str, user_id: int) -> bool:
        """
        Atomically marks this specific user's submission as 'processing'.
        Each user's submission is independent - different users claiming the
        same URL at the same time don't block each other.
        """
        doc = await self.collection.find_one_and_update(
            {
                "url": url,
                "requestor_discord_id": user_id,
                "added_to_wishlist": False,
                "processing": {"$ne": True},
            },
            {"$set": {"processing": True}},
        )
        return doc is not None

    async def _release_claim(self, url: str, user_id: int) -> None:
        await self.collection.update_one(
            {"url": url, "requestor_discord_id": user_id, "processing": True, "added_to_wishlist": False},
            {"$unset": {"processing": ""}},
        )

    async def _mark_as_added(self, url: str, user_id: int, game_name: str, entity_type: str) -> None:
        await self.collection.update_one(
            {"url": url, "requestor_discord_id": user_id, "processing": True, "added_to_wishlist": False},
            {"$set": {
                "added_to_wishlist": True,
                "added_at": datetime.now(timezone.utc),
                "title": game_name,
                "entity_type": entity_type,
            }, "$unset": {"processing": ""}},
        )

    async def _user_already_has_title_and_platform(self, user_id: int, game_name: str, entity_type: str) -> bool:
        """
        Returns True if this user already had this exact game+platform combo added.
        Allows the same game from different platforms (Steam vs Epic).
        """
        existing = await self.collection.find_one({
            "requestor_discord_id": user_id,
            "title": game_name,
            "entity_type": entity_type,
            "added_to_wishlist": True,
        })
        return existing is not None

    async def _check_learned_mapping(self, url: str):
        doc = await self.url_mappings.find_one({"url": url})
        if doc:
            return doc.get("game_name"), doc.get("entity_type")
        return None, None

    # ------------------------------------------------------------------
    # Slack helpers
    # ------------------------------------------------------------------

    async def _notify_slack_manual_action(self, message: Message, url: str, reason: str) -> None:
        try:
            await self.slack_client.chat_postMessage(
                channel=self.slack_wishlist_channel,
                text=f"Cannot process Discord wishlist request — {reason}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*Cannot process Discord wishlist request*\n"
                                f"*User:* {message.author.name}\n"
                                f"*Message:* {message.content[:300]}\n"
                                f"*Reason:* {reason}"
                            ),
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Add Game Manually"},
                                "action_id": "open_gaming_proposal_button",
                                "value": url or "",
                            }
                        ],
                    },
                ],
            )
        except Exception as e:
            logger.error(f"[Wishlist] Failed to send manual-action Slack notification: {e}")

    async def _notify_slack_info(self, text: str) -> None:
        try:
            await self.slack_client.chat_postMessage(
                channel=self.slack_wishlist_channel,
                text=text,
            )
        except Exception as e:
            logger.error(f"[Wishlist] Failed to send Slack info notification: {e}")

    # ------------------------------------------------------------------
    # Anti-spam: on_message
    # ------------------------------------------------------------------

    async def _resolve_game_name(self, url: str, store: str) -> str:
        if store == "steam":
            game_id = extract_steam_app_id(url)
            return await self._get_steam_game_name(game_id) if game_id else ""
        elif store == "epic":
            slug = extract_epic_game_slug(url)
            return await self._get_epic_game_name(slug) if slug else ""
        elif store == "xbox":
            product_id, slug = extract_xbox_product_id(url)
            if product_id:
                return await self._get_xbox_game_name(product_id, slug)
            m = re.search(r'xbox\.com/[^/]+/games/([^/?#\s]+)', url)
            if m and m.group(1) != 'store':
                return self._slug_to_name(m.group(1))
            return ""
        return ""

    async def _dm_user(self, user, text: str) -> None:
        try:
            await user.send(text)
        except Exception as _e:
            logger.debug("ignored: %s", _e)

    @commands.Cog.listener()
    async def on_message(self, message: Message) -> None:
        if message.author.bot:
            return
        if not message.channel or message.channel.id != WISHLIST_CHANNEL:
            return

        url = extract_link(message.content)
        if not url:
            return

        user = message.author
        store = detect_store(url)

        if store == "steam":
            app_id = extract_steam_app_id(url)
            if app_id:
                url = f"https://store.steampowered.com/app/{app_id}/"

        count = await self._count_user_submissions_today(user.id)
        if count >= DAILY_LINK_LIMIT:
            await message.delete()
            await self._dm_user(
                user,
                f"Your wishlist request was removed.\n"
                f"You have already submitted {DAILY_LINK_LIMIT} links in the last 24 hours. "
                f"Please try again later."
            )
            return

        if await self._link_submitted_today(url, user.id):
            await message.delete()
            await self._dm_user(user, "Your wishlist request was removed.\nThis link has already been submitted today.")
            return

        if await self._user_already_added_this_game(user.id, url):
            await message.delete()
            await self._dm_user(
                user,
                "Your wishlist request was removed.\n"
                "A request for this game from you has already been added to the wishlist."
            )
            return

        await self._record_submission(url, store, user.id, user.name)
        logger.info(f"[Wishlist] Recorded submission from {user.name}: {url}")

    # ------------------------------------------------------------------
    # SMM reaction: on_raw_reaction_add
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, reaction: RawReactionActionEvent) -> None:
        if not reaction.member or reaction.member.id not in SMM_USER_IDS:
            return
        if reaction.channel_id != WISHLIST_CHANNEL:
            return
        if reaction.emoji.id != WISHLIST_TRIGGER_REACTION:
            return

        channel: TextChannel = await self.bot.fetch_channel(reaction.channel_id)
        message: Message = await channel.fetch_message(reaction.message_id)
        await self._process_smm_wishlist_reaction(message)

    # ------------------------------------------------------------------
    # Core wishlist processing - one user per reaction
    # ------------------------------------------------------------------

    async def _process_smm_wishlist_reaction(self, message: Message) -> None:
        url = extract_link(message.content)
        user = message.author

        if not url:
            logger.warning(f"[Wishlist] No link found in message: {message.content!r}")
            await self._notify_slack_manual_action(message, url="", reason="No game store link found in the message")
            return

        store = detect_store(url)
        if store == "steam":
            app_id = extract_steam_app_id(url)
            if app_id:
                url = f"https://store.steampowered.com/app/{app_id}/"

        # Ensure a submission document exists for this specific user.
        user_doc = await self.collection.find_one({"url": url, "requestor_discord_id": user.id})
        if not user_doc:
            await self._record_submission(url, detect_store(url), user.id, user.name)
            logger.info(f"[Wishlist] Created submission for {user.name} (message pre-dates bot): {url}")

        # Claim processing right for this specific user's submission.
        if not await self._try_claim_for_processing(url, user.id):
            logger.info(f"[Wishlist] {user.name}'s request for {url} is already added or being processed — skipping")
            return

        # ── Unknown store: check learned mappings ─────────────────────────────
        entity_type_map = {"steam": "Steam", "epic": "Epic", "xbox": "Xbox"}
        entity_type = entity_type_map.get(store)
        game_name = None

        if not entity_type:
            learned_name, learned_type = await self._check_learned_mapping(url)
            if learned_name and learned_type:
                game_name = learned_name
                entity_type = learned_type
                logger.info(f"[Wishlist] Using learned mapping for {url}: '{game_name}' ({entity_type})")
            else:
                await self._release_claim(url, user.id)
                await self._notify_slack_manual_action(
                    message, url=url, reason=f"Unrecognized store URL: `{url}`"
                )
                return

        # ── Resolve game name ──────────────────────────────────────────────────
        if game_name is None:
            game_name_raw = await self._resolve_game_name(url, store)
            if not game_name_raw:
                await self._release_claim(url, user.id)
                await self._notify_slack_manual_action(
                    message, url=url,
                    reason=f"Could not resolve game name from the store page (`{url}`). The link may be region-locked or temporarily unavailable.",
                )
                return
            game_name = clean_title(game_name_raw)
        else:
            game_name = clean_title(game_name)

        # ── Per-user duplicate guard (same game + same platform) ───────────────
        if await self._user_already_has_title_and_platform(user.id, game_name, entity_type):
            await self._release_claim(url, user.id)
            logger.info(f"[Wishlist] '{game_name}' ({entity_type}) already added for {user.name} — skipping")
            await self._notify_slack_info(
                f"Wishlist skipped: *{game_name}* ({entity_type}) — {user.name} already has this game from this platform"
            )
            return

        # ── Catalog check ──────────────────────────────────────────────────────
        catalog_entry = find_in_catalog(game_name)
        if catalog_entry:
            collection = catalog_entry.get("collection") or "—"
            boosteroid_id = catalog_entry.get("id")
            link = f"https://boosteroid.com/games/#/application/{boosteroid_id}" if boosteroid_id else ""
            await self._release_claim(url, user.id)
            logger.info(f"[Wishlist] '{game_name}' is in Boosteroid catalog — skipping")
            try:
                link_text = f" — <{link}|view on Boosteroid>" if link else ""
                await self.slack_client.chat_postMessage(
                    channel=self.slack_wishlist_channel,
                    text=(
                        f"Wishlist skipped: *{game_name}* is already in the "
                        f"Boosteroid catalog (collection: `{collection}`){link_text}"
                    ),
                )
            except Exception as e:
                logger.error(f"[Wishlist] Failed to send catalog-hit Slack notification: {e}")
            return

        # ── Record in our wishlist ─────────────────────────────────────────────
        try:
            game = await asyncio.to_thread(
                wishlist_store.add_request, game_name, entity_type, "discord", requestor=user.name, url=url)
        except Exception as e:
            await self._release_claim(url, user.id)
            logger.error(f"[Wishlist] Could not record '{game_name}' ({entity_type}) for {user.name}: {e}")
            return
        await self._mark_as_added(url, user.id, game_name, entity_type)
        logger.info(f"[Wishlist] Recorded '{game_name}' ({entity_type}) for {user.name}. "
                    f"Total requests: {game.get('requests')}, status {game.get('status')}")
        await self._notify_slack_info(wishlist_store.request_note(game, user.name))

    # ------------------------------------------------------------------
    # Store lookups (game names)
    # ------------------------------------------------------------------

    async def _get_steam_game_name(self, game_id: str) -> str:
        api_url = f"https://store.steampowered.com/api/appdetails?appids={game_id}&l=english"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return ""
                    data = await resp.json()
                    return data[game_id]['data']['name']
        except (aiohttp.ClientError, KeyError, TypeError):
            return ""

    async def _get_epic_game_name(self, slug: str) -> str:
        clean_slug = strip_epic_slug_suffix(slug)
        slugs_to_try = [clean_slug] if clean_slug != slug else [slug]
        if slug != clean_slug:
            slugs_to_try.append(slug)

        async with aiohttp.ClientSession() as session:
            for attempt_slug in slugs_to_try:
                api_url = f"https://store-content.ak.epicgames.com/api/en-US/content/products/{attempt_slug}"
                try:
                    async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            name = data['pages'][0]['productName']
                            if name:
                                return name
                except (aiohttp.ClientError, KeyError, TypeError, IndexError):
                    pass

        return self._slug_to_name(clean_slug)

    async def _get_xbox_game_name(self, product_id: str, slug: str) -> str:
        api_url = f"https://displaycatalog.mp.microsoft.com/v7.0/products/{product_id}?languages=en-US&market=US"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return self._slug_to_name(slug)
                    data = await resp.json(content_type=None)
                    return data['Product']['LocalizedProperties'][0]['ProductTitle']
        except (aiohttp.ClientError, KeyError, TypeError, IndexError):
            return self._slug_to_name(slug)

    @staticmethod
    def _slug_to_name(slug: str) -> str:
        return slug.replace("-", " ").title()


async def setup(bot: commands.Bot, mongo_db) -> None:
    await bot.add_cog(WishlistCog(bot, mongo_db))
