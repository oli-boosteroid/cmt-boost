import os


def _getenv(key: str) -> str:
    return os.getenv(key, "")

def _getint(key: str) -> int:
    return int(os.getenv(key, "0"))


# Telegram
TELEGRAM_BOT_TOKEN = _getenv("TELEGRAM_BOT_TOKEN")
TG_GROUP_ID              = _getint("TG_GROUP_ID")
TG_REDDIT_POSTS_TOPIC_ID    = _getint("TG_REDDIT_POSTS_TOPIC_ID")
TG_DISCORD_TOPIC_ID         = _getint("TG_DISCORD_TOPIC_ID")
TG_DISCORD_MODMAIL_TOPIC_ID = _getint("TG_DISCORD_MODMAIL_TOPIC_ID")
TG_MODMAIL_TOPIC_ID         = _getint("TG_MODMAIL_TOPIC_ID")
TG_MEET_REPORT_GROUP_ID     = _getint("TG_MEET_REPORT_GROUP_ID")

_CHANNEL_DEFINITIONS = [
    ("SLACK_SERVERS_MAINTENANCE_CHANNEL_ID", "TG_SERVERS_MAINTENANCE_TOPIC_ID", "TG_SERVERS_MAINTENANCE_LABEL"),
    ("SLACK_GAMES_MAINTENANCE_CHANNEL_ID",   "TG_GAMES_MAINTENANCE_TOPIC_ID",   "TG_GAMES_MAINTENANCE_LABEL"),
    ("SLACK_DISCORD_CHANNEL_ID",             "TG_DISCORD_TOPIC_ID",             "TG_DISCORD_LABEL"),
    ("SLACK_DISCORD_MODMAIL_CHANNEL_ID",    "TG_DISCORD_MODMAIL_TOPIC_ID",     "TG_DISCORD_MODMAIL_LABEL"),
    ("SLACK_MODMAIL_CHANNEL",                "TG_MODMAIL_TOPIC_ID",             "TG_MODMAIL_LABEL"),
    ("SLACK_REDDIT_COMMENTS_CHANNEL",        "TG_REDDIT_COMMENTS_TOPIC_ID",     "TG_REDDIT_COMMENTS_LABEL"),
    ("SLACK_REDDIT_POSTS_CHANNEL",           "TG_REDDIT_POSTS_TOPIC_ID",        "TG_REDDIT_POSTS_LABEL"),
    ("SLACK_JIRA_NOTIFICATIONS_CHANNEL_ID",  "TG_JIRA_TOPIC_ID",                "TG_JIRA_LABEL"),
    ("SLACK_SCHEDULE_CHANNEL_ID",            "TG_SCHEDULE_TOPIC_ID",            "TG_SCHEDULE_LABEL"),
]

_DM_DEFINITIONS = [
    ("SLACK_REPORTS_CHANNEL_ID", "TG_REPORTS_USER_ID"),
    ("SLACK_TL_REPORTS_CHANNEL_ID", "TG_REPORTS_USER_ID"),
]

SLACK_TO_TELEGRAM_DM: dict[str, int] = {
    _getenv(slack_key): _getint(user_id_key)
    for slack_key, user_id_key in _DM_DEFINITIONS
    if _getenv(slack_key)
}

SLACK_TO_TELEGRAM: dict[str, tuple[int, str]] = {
    _getenv(slack_key): (_getint(topic_key), _getenv(label_key))
    for slack_key, topic_key, label_key in _CHANNEL_DEFINITIONS
    if _getenv(slack_key)
}
