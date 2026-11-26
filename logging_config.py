import logging
import re
from logging.handlers import RotatingFileHandler
import sys
import os
from slack_handler import SlackHandler, TransientNoiseFilter
from discord_notifications.cogs.slack_handlers.modmail_slack_handler import ModMailSlackHandler


class SecretRedactFilter(logging.Filter):
    """Never let a Telegram bot token reach a log line (requests errors print the full API URL,
    https://api.telegram.org/bot<token>/sendMessage) - not in the file, stdout or the Slack channel."""
    _TG_TOKEN = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "bot" in msg and self._TG_TOKEN.search(msg):
            record.msg = self._TG_TOKEN.sub("bot<token hidden>", msg)
            record.args = None
        return True


def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # Drop handlers someone else attached before us: a module-level logging.basicConfig() at import time
    # adds a stderr handler, and every line was then logged twice
    # ("INFO:module:..." and "INFO - ..."). From here on only the handlers below exist.
    for h in list(logger.handlers):
        logger.removeHandler(h)

    os.makedirs('data', exist_ok=True)
    
    # File handler
    file_handler = RotatingFileHandler(
        'data/app.log',
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    )
    
    # Console handler
    console_handler = logging.StreamHandler(
        stream=sys.stdout
    )
    console_handler.setFormatter(
        logging.Formatter('%(levelname)s - %(message)s')
    )

    # Errors and warnings Slack handler
    slack_token = os.getenv('SLACK_TOKEN')
    slack_channel = os.getenv('SLACK_ERROR_CHANNEL_ID')

    if slack_token and slack_channel:
        slack_handler = SlackHandler(slack_token, slack_channel)
        slack_handler.setLevel(logging.WARNING)
        slack_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        )
        # Keep transient network noise out of the Slack error channel (file log stays full).
        slack_handler.addFilter(TransientNoiseFilter())
        logger.addHandler(slack_handler)
    else:
        print("Warning: SLACK_TOKEN or SLACK_ERROR_CHANNEL_ID not set. Slack notifications disabled.")
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    for h in logger.handlers:
        h.addFilter(SecretRedactFilter())

    # Suppress noisy third-party loggers
    logging.getLogger('asyncprawcore').setLevel(logging.ERROR)
    logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)
    logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)
    logging.getLogger('discord.client').setLevel(logging.ERROR)
    # Telegram's Updater logs a full traceback on every transient polling network error
    # (Bad Gateway, timeouts) that it retries on its own - keep those out of the logs.
    logging.getLogger('telegram.ext.Updater').setLevel(logging.CRITICAL)
    # httpx logs EVERY request at INFO, incl. the snapshot bot's getUpdates long-poll (a line every
    # few seconds forever) - pure noise. Keep only warnings/errors.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    # Telethon per-connection chatter (reconnects/keepalive) - warnings/errors only.
    logging.getLogger('telethon.network.mtprotosender').setLevel(logging.WARNING)
    logging.getLogger('telethon').setLevel(logging.WARNING)   # update catch-up chatter ("Got difference for channel ...")

    # Discord ModMail Slack Handler (logs go to the Discord Logs / Flood channel)
    slack_flood_channel = os.getenv('SLACK_FLOOD_CHANNEL_ID')

    if slack_token and slack_flood_channel:
        modmail_handler = ModMailSlackHandler(slack_token, slack_flood_channel)
        modmail_handler.setLevel(logging.INFO)
        modmail_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        )

        # Apply handler only to the ModMail logger. The Slack call runs on a background thread
        # (QueueHandler -> QueueListener), so a log line never blocks the Discord event loop.
        import queue
        from logging.handlers import QueueHandler, QueueListener
        _q = queue.SimpleQueue()
        _listener = QueueListener(_q, modmail_handler, respect_handler_level=True)
        _listener.start()
        modmail_logger = logging.getLogger('discord_notifications.cogs.modmail')
        modmail_logger.addHandler(QueueHandler(_q))

        print("ModMail Slack notifications enabled")
    else:
        print("Warning: SLACK_FLOOD_CHANNEL_ID not set. ModMail Slack notifications disabled.")
