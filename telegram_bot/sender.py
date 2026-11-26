import re
import logging
import requests

from telegram_bot.config import TELEGRAM_BOT_TOKEN, TG_MEET_REPORT_GROUP_ID

logger = logging.getLogger(__name__)

def _tg_api() -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_to_meet_group(text: str) -> bool:
    """Posts a plain-text message to the weekly-meeting Telegram group (TG_MEET_REPORT_GROUP_ID).

    Plain text (no parse_mode) so arbitrary Discord nicknames need no escaping; bare URLs
    are auto-linked by Telegram. Synchronous - call via asyncio.to_thread from async code.
    """
    if not TG_MEET_REPORT_GROUP_ID:
        logger.warning("[TG] TG_MEET_REPORT_GROUP_ID is not set — meet-group alert skipped")
        return False
    try:
        resp = requests.post(f"{_tg_api()}/sendMessage", json={
            "chat_id": TG_MEET_REPORT_GROUP_ID,
            "text": text,
            "disable_web_page_preview": True,
        }, timeout=10)

        if not resp.ok:
            logger.error(f"[TG] Meet-group message error: {resp.text}")
            return False

        return True

    except requests.RequestException as e:
        logger.error(f"[TG] Meet-group request error: {e}")
        return False


def send_to_meet_group_html(text: str) -> bool:
    """Like send_to_meet_group but with parse_mode=HTML, so SLA reminders can @-mention the on-shift
    CM(s) via <a href="tg://user?id=...">Name</a> inline mentions (works without a public @username).
    Callers must HTML-escape any user text they include."""
    if not TG_MEET_REPORT_GROUP_ID:
        logger.warning("[TG] TG_MEET_REPORT_GROUP_ID is not set - meet-group alert skipped")
        return False
    try:
        resp = requests.post(f"{_tg_api()}/sendMessage", json={
            "chat_id": TG_MEET_REPORT_GROUP_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        if not resp.ok:
            logger.error(f"[TG] Meet-group HTML message error: {resp.text}")
            return False
        return True
    except requests.RequestException as e:
        logger.error(f"[TG] Meet-group HTML request error: {e}")
        return False


def _imgmod_keyboard(uid) -> dict:
    """Inline keyboard for an image-spam alert: delete the user's 24h messages / whitelist them.
    callback_data is handled by telegram_bot.image_mod (registered on the polling bot)."""
    return {"inline_keyboard": [
        [{"text": "Delete the user's messages (24h)", "callback_data": f"imgmod:del:{uid}"}],
        [{"text": "Add to whitelist (2 days)", "callback_data": f"imgmod:wl:{uid}"}],
    ]}


def _send_alert_text(text_html: str, reply_markup: dict) -> bool:
    resp = requests.post(f"{_tg_api()}/sendMessage", json={
        "chat_id": TG_MEET_REPORT_GROUP_ID,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup,
    }, timeout=10)
    if not resp.ok:
        logger.error(f"[TG] image-spam sendMessage error: {resp.text}")
        return False
    return True


def send_image_spam_alert_html(text_html: str, image_urls, uid) -> bool:
    """Post the image-spam alert to the meeting group with the two moderation buttons and ONE of the
    offending images: the photo carries the caption + buttons (the message the callback edits/appends
    to). Falls back to a text message with the buttons if there is no image or Telegram can't fetch it.
    Sync - call via asyncio.to_thread."""
    if not TG_MEET_REPORT_GROUP_ID:
        logger.warning("[TG] TG_MEET_REPORT_GROUP_ID is not set - image-spam alert skipped")
        return False
    kb = _imgmod_keyboard(uid)
    img = next((u for u in (image_urls or []) if u), None)
    try:
        if img:
            resp = requests.post(f"{_tg_api()}/sendPhoto", json={
                "chat_id": TG_MEET_REPORT_GROUP_ID,
                "photo": img,
                "caption": text_html[:1024],
                "parse_mode": "HTML",
                "reply_markup": kb,
            }, timeout=15)
            if not resp.ok:   # e.g. Telegram couldn't fetch the CDN image -> text alert keeps the buttons
                logger.warning(f"[TG] image-spam sendPhoto error, falling back to text: {resp.text}")
                return _send_alert_text(text_html, kb)
            return True
        return _send_alert_text(text_html, kb)
    except requests.RequestException as e:
        logger.error(f"[TG] image-spam alert request error: {e}")
        return False


def _escape_md(text: str) -> str:
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\\-])', r'\\\1', text)


def _clean_slack_text(text: str) -> str:
    text = re.sub(r':[a-zA-Z][a-zA-Z0-9_\-+]*:', '', text)
    text = re.sub(r'<@([A-Z0-9]+)>', r'@\1', text)
    text = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2 (\1)', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    text = re.sub(r'<#[A-Z0-9]+\|([^>]+)>', r'#\1', text)
    return text.strip()


def _format(event: dict, label: str) -> str:
    text = _clean_slack_text(event.get("text", ""))
    return _escape_md(text)

def send_to_user(user_id: int, event: dict) -> bool:
    text = _format(event, "")
    try:
        resp = requests.post(f"{_tg_api()}/sendMessage", json={
            "chat_id": user_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        }, timeout=10)

        if not resp.ok:
            logger.error(f"[TG Bridge] Telegram DM error: {resp.text}")
            return False

        logger.info(f"[TG Bridge] Sent DM to user {user_id}")
        return True

    except requests.RequestException as e:
        logger.error(f"[TG Bridge] DM request error: {e}")
        return False

