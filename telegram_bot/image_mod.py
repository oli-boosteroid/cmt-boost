"""
Image-spam alert action buttons (Telegram side).

Handles the two inline buttons on the image-ratelimit alert posted to the meeting group:
  * "Удалить сообщения пользователя"  -> delete every message the user posted in Discord in the last 24h
  * "Добавить в whitelist"            -> exempt the user from the image ratelimit for 2 days

The alert message itself is posted by telegram_bot.sender.send_image_spam_alert_html with callback_data
"imgmod:<del|wl>:<discord_user_id>". This module is registered on the polling bot (snapshot_bot). When a
button is pressed, the action runs and a confirmation line is appended to the alert message, and the
pressed button is removed so it can't be double-fired.

The Discord work runs on the Discord bot's own event loop (a different loop in the same process), reached
via get_running_discord_bot() + run_coroutine_threadsafe.
"""
import asyncio
import html as _html
import logging

from telegram import Update, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ContextTypes

from telegram_bot.config import TG_MEET_REPORT_GROUP_ID

logger = logging.getLogger(__name__)


_in_progress = set()   # (action, uid) currently running: a second press must not start a second run


def register(application):
    application.add_handler(CallbackQueryHandler(_imgmod_callback, pattern=r"^imgmod:"))


async def _run_on_discord(coro_factory):
    """Run a coroutine on the Discord bot's loop from the Telegram loop and await its result."""
    from discord_notifications.discord_notifications import get_running_discord_bot
    bot = get_running_discord_bot()
    if not bot or getattr(bot, "loop", None) is None or bot.is_closed():
        raise RuntimeError("discord bot not ready")
    fut = asyncio.run_coroutine_threadsafe(coro_factory(bot), bot.loop)
    return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=180)


async def _do_delete(uid):
    from discord_notifications.discord_notifications import delete_user_messages_last_24h
    return await _run_on_discord(lambda bot: delete_user_messages_last_24h(bot, uid))


async def _do_whitelist(uid, added_by):
    from discord_notifications.discord_notifications import resolve_member_name
    name = str(uid)
    try:
        name = await _run_on_discord(lambda bot: resolve_member_name(bot, uid)) or name
    except Exception as e:
        logger.warning("[TG] imgmod name resolve failed for %s: %s", uid, e)
    from services.image_whitelist import add_image_whitelist
    await asyncio.to_thread(add_image_whitelist, uid, name, added_by)
    return name


async def _append_and_disable(q, note_html, action):
    """Append a confirmation line to the alert message and drop the button that was just pressed."""
    msg = q.message
    new_rows = []
    for row in (msg.reply_markup.inline_keyboard if msg.reply_markup else []):
        kept = [b for b in row if not (b.callback_data or "").startswith(f"imgmod:{action}:")]
        if kept:
            new_rows.append(kept)
    markup = InlineKeyboardMarkup(new_rows) if new_rows else None
    try:
        if msg.caption is not None:
            base = msg.caption_html or ""
            await q.edit_message_caption(caption=(base + "\n\n" + note_html)[:1024],
                                         parse_mode="HTML", reply_markup=markup)
        else:
            base = msg.text_html or ""
            await q.edit_message_text(text=base + "\n\n" + note_html,
                                      parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        logger.warning("[TG] imgmod append failed: %s", e)
        try:
            await msg.reply_text(note_html, parse_mode="HTML")
        except Exception as _e:
            logger.debug("ignored: %s", _e)


async def _imgmod_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    # Only honour presses inside the meeting group where the alert lives.
    chat = q.message.chat if q.message else None
    if not chat or str(chat.id) != str(TG_MEET_REPORT_GROUP_ID):
        await q.answer("Not available here", show_alert=False)
        return
    parts = (q.data or "").split(":")
    if len(parts) < 3:
        await q.answer()
        return
    action, uid_s = parts[1], parts[2]
    try:
        uid = int(uid_s)
    except (TypeError, ValueError):
        await q.answer()
        return

    key = (action, uid)
    if key in _in_progress:
        await q.answer("Already running, please wait", show_alert=False)
        return
    _in_progress.add(key)
    await q.answer("Working on it...")   # dismiss the spinner immediately (the delete pass can take a moment)
    presser = q.from_user.full_name or (("@" + q.from_user.username) if q.from_user.username else str(q.from_user.id))
    presser_h = _html.escape(presser)

    try:
        if action == "del":
            deleted, name, gone = await _do_delete(uid)
            extra = f" ({gone} more were already deleted)" if gone else ""
            note = (f"Deleted messages of <b>{_html.escape(name)}</b> from the last 24h: {deleted}{extra}. "
                    f"New messages from them are deleted for the next 10 minutes - {presser_h}")
        elif action == "wl":
            name = await _do_whitelist(uid, presser)
            note = f"<b>{_html.escape(name)}</b> added to the whitelist for 2 days - {presser_h}"
        else:
            return
    except asyncio.TimeoutError:
        note = f"Deleting takes longer than 3 minutes, the result will be in the bot logs - {presser_h}"
    except Exception as e:
        logger.error("[TG] imgmod action %s for %s failed: %s", action, uid, e)
        note = f"The action failed ({_html.escape(str(e))}) - {presser_h}"
    finally:
        _in_progress.discard(key)

    await _append_and_disable(q, note, action)
