import os


def _getenv(key: str) -> str:
    return os.getenv(key, "")

def _getint(key: str) -> int:
    return int(os.getenv(key, "0"))


# Telegram
TELEGRAM_BOT_TOKEN = _getenv("TELEGRAM_BOT_TOKEN")
TG_MEET_REPORT_GROUP_ID = _getint("TG_MEET_REPORT_GROUP_ID")
