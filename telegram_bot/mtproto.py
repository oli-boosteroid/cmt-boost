"""
MTProto client (Telethon) - lets the console reply in the discussion group "as the group"
(anonymous admin), which the Bot API cannot do.

Only a real USER account (MTProto) that is an ANONYMOUS admin of the discussion group can post so the
message shows as the group itself (sender_chat = the group) rather than a personal account. This module
runs a Telethon user client on its own asyncio loop in a daemon thread (same pattern as the Discord bot
loop) and exposes a thread-safe `send_group_reply(...)` the console_actions worker calls.

Env (VM):
  TG_API_ID, TG_API_HASH   -- from https://my.telegram.org (application creds)
  TG_MTPROTO_SESSION       -- StringSession of the team account (an ANONYMOUS admin of the group)

The account must be an admin of the discussion group with "Remain Anonymous" ON, and a member of that
group. No channel id is needed - we send to the group and `send_as` the group itself.

Generate the session string ONCE, on a machine where you can type the login code:
  pip install telethon
  python -m telegram_bot.mtproto
Then store the printed string as TG_MTPROTO_SESSION (secret - it is full account access).
"""
import asyncio
import logging
import os
import threading

logger = logging.getLogger(__name__)

_client = None
_loop = None
_ready = threading.Event()


def _env():
    return (int(os.getenv("TG_API_ID", "0") or 0),
            os.getenv("TG_API_HASH", "") or "",
            os.getenv("TG_MTPROTO_SESSION", "") or "")


def mtproto_ready() -> bool:
    """True when the client is connected and env is configured."""
    api_id, api_hash, session = _env()
    return bool(api_id and api_hash and session) and _ready.is_set()


def start_mtproto():
    """Start the Telethon client on a daemon thread. No-op (returns None) if env is missing."""
    global _loop
    api_id, api_hash, session = _env()
    if not (api_id and api_hash and session):
        logger.info("[MTProto] not started (TG_API_ID / TG_API_HASH / TG_MTPROTO_SESSION missing)")
        return None
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except Exception as e:
        logger.error("[MTProto] telethon not installed: %s", e)
        return None

    _loop = asyncio.new_event_loop()

    def _run():
        global _client
        asyncio.set_event_loop(_loop)
        _client = TelegramClient(StringSession(session), api_id, api_hash, loop=_loop)
        try:
            _loop.run_until_complete(_client.connect())
            if not _loop.run_until_complete(_client.is_user_authorized()):
                logger.error("[MTProto] session is not authorized — regenerate TG_MTPROTO_SESSION")
                return
            _ready.set()
            logger.info("[MTProto] client connected (send-as-group ready)")
            _loop.run_forever()
        except Exception as e:
            logger.error("[MTProto] client loop error: %s", e)

    threading.Thread(target=_run, daemon=True, name="mtproto-client").start()
    return _loop


def send_group_reply(chat_id, text, reply_to=None) -> bool:
    """Send `text` into the discussion group `chat_id` AS THE GROUP (anonymous admin), optionally
    threaded under `reply_to` (the user's message id). Blocking; runs on the client loop."""
    if not mtproto_ready() or _client is None or _loop is None:
        return False

    async def _send():
        group = await _client.get_entity(int(chat_id))
        try:
            await _client.send_message(group, text, reply_to=reply_to, send_as=group, link_preview=False)
        except Exception:
            # Fallback: an anonymous admin's plain send also shows as the group.
            await _client.send_message(group, text, reply_to=reply_to, link_preview=False)
        return True

    fut = asyncio.run_coroutine_threadsafe(_send(), _loop)
    return bool(fut.result(timeout=30))


def _generate_session():
    """Interactive one-time session generator. Run locally: python -m telegram_bot.mtproto

    Steps it walks you through:
      1) api_id + api_hash  -> from https://my.telegram.org (log in with the TEAM account's phone)
      2) phone number       -> the team account (e.g. +380...)
      3) login code         -> Telegram sends it to that account (its app / SMS)
      4) 2FA password       -> only if the account has two-step verification set
    Then it prints the session string. Put it in TG_MTPROTO_SESSION.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id = int((os.getenv("TG_API_ID") or input("api_id: ")).strip())
    api_hash = (os.getenv("TG_API_HASH") or input("api_hash: ")).strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    client.start()   # prompts for phone -> code -> 2FA password as needed
    print("\n=== TG_MTPROTO_SESSION (keep secret; full account access) ===\n")
    print(client.session.save())
    print("\n=============================================================")
    me = client.get_me()
    print("Logged in as: %s (@%s)" % (getattr(me, "first_name", ""), getattr(me, "username", "")))
    print("Make sure THIS account is an ANONYMOUS admin of the discussion group.")
    client.disconnect()


if __name__ == "__main__":
    _generate_session()
