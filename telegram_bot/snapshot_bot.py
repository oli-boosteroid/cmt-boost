"""
Minimal Telegram bot (console-only).

The interactive CMT Helper bot (menus, reports, proposals, testing sessions, lunch, scheduler) was
removed - that whole workflow now lives in the CMT Console. This keeps only the two things the CONSOLE
needs from a running Telegram bot:

  1. the /start deep link that connects a team member's Telegram to their console account so the
     notification engine can DM them (/start cmt_<token>), and
  2. the discussion-group message snapshot (comments_snapshot) that feeds the console Telegram tab.

Everything that SENDS (SLA reminders + image-spam alert via telegram_bot.sender, per-user notification
DMs via services.user_notify_service, and "reply as the group" via telegram_bot.mtproto) goes out over
the Bot HTTP API / MTProto directly and does NOT depend on this polling bot.
"""
import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import NetworkError

from telegram_bot.config import TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)


def _link_console_user(token: str, chat_id) -> bool:
    """Map a console 'connect Telegram' token to the signed-in console user and store their Telegram
    chat_id so the notification engine can DM them. One-time: the token is consumed on use."""
    if not token or not chat_id:
        return False
    try:
        from datetime import datetime, timedelta
        from services.mongo_db_service import sync_db
        link = sync_db["console_notify_links"].find_one({"token": token})
        if not link:
            return False
        created = link.get("created_at")
        if created and (datetime.utcnow() - created) > timedelta(minutes=15):
            sync_db["console_notify_links"].delete_one({"_id": link["_id"]})   # expired -> reject + purge
            return False
        sync_db["console_users"].update_one(
            {"email": link.get("user_email")},
            {"$set": {"telegram_chat_id": int(chat_id)}})
        sync_db["console_notify_links"].delete_one({"_id": link["_id"]})
        return True
    except Exception as e:
        logger.warning("[TG] console notify link failed: %s", e)
        return False


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Console 'Connect Telegram' deep link: /start cmt_<token>. Links the user for notifications.
    A bare /start (no token) is a no-op - there is no interactive menu any more."""
    msg = getattr(update, "message", None)
    text = (getattr(msg, "text", "") or "") if msg else ""
    if "cmt_" not in text:
        return
    tail = text.split("cmt_", 1)[1].strip()
    token = tail.split()[0] if tail else ""
    linked = bool(token) and _link_console_user(token, update.effective_user.id if update.effective_user else None)
    try:
        await msg.reply_text(
            "Telegram connected to your CMT Console account. Your notifications will arrive here."
            if linked else
            "This connect link is invalid or already used. Open the console and generate a new one.")
    except Exception as _e:
        logger.debug("ignored: %s", _e)


async def _error_handler(update, context):
    error = context.error
    if isinstance(error, NetworkError):
        logger.info("[TG] transient network error (auto-retry): %s", error)
    else:
        logger.error("[TG] snapshot bot error: %s", error, exc_info=error)


def start_tg_bot():
    """Entry point (main.py runs this in a daemon thread): a minimal PTB app that only handles the
    console connect deep link and snapshots the discussion group for the console Telegram tab."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("[TG] TELEGRAM_BOT_TOKEN missing - snapshot bot disabled")
        return

    async def _run():
        application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_error_handler(_error_handler)
        application.add_handler(CommandHandler("start", _start))
        # Snapshot user messages in the linked discussion group for the console Telegram tab.
        try:
            from telegram_bot.comments_snapshot import register as _register_comments
            _register_comments(application)
        except Exception as e:
            logger.warning("[TG] comments snapshot not registered: %s", e)
        # Image-spam alert action buttons (delete user's 24h messages / whitelist for 2 days).
        try:
            from telegram_bot.image_mod import register as _register_image_mod
            _register_image_mod(application)
        except Exception as e:
            logger.warning("[TG] image-mod actions not registered: %s", e)
        try:
            await application.bot.set_my_commands([])   # no Menu button - there are no commands
        except NetworkError as e:
            logger.info("[TG] set_my_commands skipped (network): %s", e)   # cosmetic, never worth a crash
        await application.initialize()
        try:
            await application.start()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            logger.info("[TG] snapshot bot started (console-only: /start connect + group snapshot)")
            await asyncio.Event().wait()
        finally:
            try:
                if application.updater and application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()
                await application.shutdown()
            except Exception as _e:
                logger.debug("ignored: %s", _e)

    # A start-up that cannot reach Telegram (connect timeout on the VM, Telegram down) must not end this
    # thread, or the console Telegram tab would stop receiving messages until the bot restarts.
    # It tries again: 10 s, 20 s, ... up to every 5 minutes; a long healthy run resets the pause.
    delay, failures = 10, 0
    while True:
        started = time.time()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except NetworkError as e:            # TimedOut is a NetworkError
            failures += 1
            log = logger.warning if failures == 1 else logger.info
            log("[TG] snapshot bot cannot reach Telegram (%s); retrying in %ss (attempt %d)", e, delay, failures)
        except Exception as e:
            failures += 1
            logger.error("[TG] snapshot bot stopped: %s; restarting in %ss", e, delay, exc_info=True)
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
        if time.time() - started > 600:
            delay, failures = 10, 0
        time.sleep(delay)
        delay = min(delay * 2, 300)
