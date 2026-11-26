"""
Identity service (console <-> bot).

Single source of truth for "who is who" across platforms. The CMT Console admin links each team
member's accounts (alias + Slack/Telegram/Jira/Discord ids + Reddit username) on the shared
`console_users` collection, and keeps two "clean" exclusion lists in `console_config`
(`ignored_reddit`, `moderators_discord`) for accounts NOT tied to a team member (Reddit bots etc.).

The bot reads the EFFECTIVE values here: the .env value stays as a FALLBACK base and the Mongo
data is overlaid on top. So with no accounts linked yet (today), every getter returns exactly the
old .env behaviour, which keeps startup safe; as the admin fills profiles in, the bot picks them up
(within the short TTL cache) without a redeploy.

Every getter is wrapped so a Mongo hiccup can never break the caller - it just falls back to .env.
Mongo access is imported lazily inside the functions to avoid any import-time coupling/cycles.
"""
import os
import time
import logging

logger = logging.getLogger(__name__)

_CACHE_TTL = 60          # seconds; new links show up within this window, no restart needed
_cache: dict = {}
_cache_at: dict = {}


def _cached(key, producer):
    now = time.time()
    if key in _cache and (now - _cache_at.get(key, 0)) < _CACHE_TTL:
        return _cache[key]
    val = producer()
    _cache[key] = val
    _cache_at[key] = now
    return val


def _users():
    """The console_users collection, or None if Mongo is unavailable."""
    try:
        from services.mongo_db_service import sync_db
        return sync_db["console_users"]
    except Exception as e:
        logger.warning("[Identity] users collection unavailable: %s", e)
        return None


def _config_list(key):
    try:
        from services.mongo_db_service import sync_db
        d = sync_db["console_config"].find_one({"key": key})
        return [str(x).strip() for x in ((d or {}).get("value") or []) if str(x).strip()]
    except Exception as e:
        logger.warning("[Identity] config '%s' unavailable: %s", key, e)
        return []


def _env_csv(name):
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def _console_only():
    """When the admin marks the console Identity authoritative (console_config.identity_console_only),
    the bot uses ONLY console_users data and drops the .env fallback lists - so a stale .env entry cannot
    leak a non-Identity account into mentions/notifications. Default off = the .env + console union."""
    try:
        from services.mongo_db_service import sync_db
        d = sync_db["console_config"].find_one({"key": "identity_console_only"})
        return bool((d or {}).get("value"))
    except Exception:
        return False


def _alias_of(u):
    return (u.get("alias") or (u.get("email", "") or "").split("@")[0] or "").strip()


# ── Reddit ignore list (suppresses the bot's Slack alerts; the console still shows everything) ──
def get_ignored_reddit_users():
    def _build():
        result = set() if _console_only() else set(_env_csv("IGNORED_USERS"))   # .env fallback base (skipped when console-only)
        result |= set(_config_list("ignored_reddit"))       # clean exclusions (bots, non-team mods)
        col = _users()
        if col is not None:
            try:
                for u in col.find({"reddit_username": {"$nin": [None, ""]}}, {"reddit_username": 1}):
                    v = (u.get("reddit_username") or "").strip()
                    if v:
                        result.add(v)
            except Exception as e:
                logger.warning("[Identity] reddit ignore users query failed: %s", e)
        return sorted(result)
    return _cached("ignored_reddit", _build)


# ── Discord moderators (same idea, Discord side) ──
def get_discord_moderators():
    def _build():
        result = set() if _console_only() else set(_env_csv("MODERATORS"))   # .env fallback (skipped when console-only)
        result |= set(_config_list("moderators_discord"))
        col = _users()
        if col is not None:
            try:
                for u in col.find({}, {"discord_name": 1, "discord_id": 1}):
                    for k in ("discord_name", "discord_id"):
                        v = str(u.get(k) or "").strip()
                        if v:
                            result.add(v)
            except Exception as e:
                logger.warning("[Identity] discord moderators query failed: %s", e)
        return sorted(result)
    return _cached("moderators_discord", _build)


# ── Discord image-ratelimit permanent whitelist (console admin list, names or ids, lowercased) ──
def get_image_whitelist_discord():
    return _cached("image_whitelist_discord",
                   lambda: {x.lower().lstrip("@") for x in _config_list("image_whitelist_discord")})


# ── Slack: alias -> Slack user id (for @mentions and per-user stats) ──
def get_slack_user_ids():
    def _build():
        mapping = {}
        if not _console_only():                                       # .env fallback base 
            for entry in os.getenv("SLACK_USER_IDS", "").split(","):
                if ":" in entry:
                    a, s = entry.strip().split(":", 1)
                    if a.strip() and s.strip():
                        mapping[a.strip()] = s.strip()
        col = _users()
        if col is not None:
            try:
                for u in col.find({"slack_id": {"$nin": [None, ""]}}, {"alias": 1, "slack_id": 1, "email": 1}):
                    a = _alias_of(u)
                    if a and (u.get("slack_id") or "").strip():
                        mapping[a] = u["slack_id"].strip()
            except Exception as e:
                logger.warning("[Identity] slack ids query failed: %s", e)
        return mapping
    return _cached("slack_user_ids", _build)


# ── Jira: accountId -> alias (our team). accountId is the full Atlassian id (e.g. "712020:uuid"). ──
def get_jira_our_user_map():
    def _build():
        mapping = {}
        if not _console_only():                                      # .env fallback base 
            for pair in os.getenv("JIRA_OUR_USER_IDS", "").split(","):
                parts = pair.strip().split(":", 2)
                if len(parts) == 3 and parts[0]:
                    mapping[f"{parts[0]}:{parts[1]}"] = parts[2]
                elif len(parts) == 2 and parts[0]:
                    mapping[parts[0]] = parts[1]
        col = _users()
        if col is not None:
            try:
                for u in col.find({"jira_account_id": {"$nin": [None, ""]}},
                                  {"alias": 1, "jira_account_id": 1, "email": 1}):
                    acc = (u.get("jira_account_id") or "").strip()
                    if acc:
                        mapping[acc] = _alias_of(u)
            except Exception as e:
                logger.warning("[Identity] jira user map query failed: %s", e)
        return mapping
    return _cached("jira_our_user_map", _build)

