"""
Checks every console action before the worker runs it.

  - the type must be known;
  - an action with an author needs an existing console user with the section permission
    (admin-only types need an admin);
  - an action without an author is allowed only for a few system types, in the shape the console produces.

check(action, users_col) returns None when the action may run, otherwise the reason.
"""
import os
import threading
import time
from collections import defaultdict, deque

# Cloud account look-ups per requester per hour.
CLOUD_LOOKUP_LIMIT_PER_HOUR = int(os.getenv("CLOUD_LOOKUP_LIMIT_PER_HOUR") or 120)
_CLOUD_TYPES = {"cloud_user_lookup", "cloud_sessions"}
_lookups = defaultdict(deque)
_lookups_lock = threading.Lock()


def _lookup_allowed(who: str, now=None) -> bool:
    now = now or time.time()
    with _lookups_lock:
        q = _lookups[who]
        while q and q[0] < now - 3600:
            q.popleft()
        if len(q) >= CLOUD_LOOKUP_LIMIT_PER_HOUR:
            return False
        q.append(now)
        return True

# Section permission needed to run a type (mirror of the console's _ACTION_PERM).
ACTION_PERM = {
    "reddit_note": "reddit", "reddit_reply": "reddit", "reddit_archive": "reddit", "reddit_highlight": "reddit",
    "reddit_post_reply": "posts",
    "discord_reply": "discord", "discord_status": "discord", "discord_postpone": "discord",
    "discord_jira_link": "discord", "discord_mention_email": "discord",
    "telegram_reply": "telegram",
    "game_cards_generate": "gamecards", "game_cards_set": "gamecards", "game_cards_set_row": "gamecards",
    "game_cards_create_week": "gamecards", "game_cards_sync": "gamecards", "game_cards_delete": "gamecards",
    "game_testing_set": "gamecards", "game_testing_sync": "gamecards",
    "jira_todo": "jira",
    "wishlist_set_status": "wishlist",
}

# Types only an admin may run (mirror of the console's admin-only gate).
ADMIN_ONLY = {"known_issues_add", "known_issues_set_canned", "known_issues_update", "known_issues_sync",
              "game_list_sync", "schedule_set", "schedule_create_month", "schedule_sync", "id_topup"}

# Types any signed-in console user may run.
LOGIN_ONLY = {"ping", "maintenance_notify", "maintenance_end", "maintenance_sheet_add", "report",
              "create_so_ticket", "create_urgent", "add_suggestion", "add_proposal",
              "cloud_user_lookup", "cloud_sessions", "notify_test", "notify_user_dm", "admin_notify_push",
              "jira_slack_delete", "urg_slack_delete"}

KNOWN_TYPES = set(ACTION_PERM) | ADMIN_ONLY | LOGIN_ONLY


def _console_url():
    return (os.getenv("CONSOLE_URL") or "https://cmtconsole.xyz/console/").strip()


def _system_ok(action):
    """Actions the console server queues on its own, without a signed-in author."""
    atype = action.get("type")
    payload = action.get("payload") or {}
    source = action.get("source") or ""
    if atype == "admin_notify_push":
        return True                                    # push to admins only (notify_event decides who)
    if atype == "maintenance_sheet_add":
        return True                                    # one row in the maintenance log sheet
    if atype == "notify_user_dm" and source == "leave":
        # leave-request DM: target is a console user, the link points to the console
        url = str(payload.get("url") or "")
        return not url or url.startswith(_console_url().split("/console")[0])
    if atype == "schedule_set" and source == "leave":
        return True                                    # VACATION / SICK LEAVE cell after an admin approval
    if atype == "cloud_user_lookup" and source == "facebook":
        return True                                    # Facebook extension binding a User ID
    return False


def _find_user(users_col, email):
    if not email or users_col is None:
        return None
    return users_col.find_one({"email": str(email).strip().lower()}, {"role": 1, "permissions": 1, "email": 1})


def check(action, users_col):
    """None if the action may run, else the reason it may not."""
    reason = _check(action, users_col)
    if reason is None and action.get("type") in _CLOUD_TYPES:
        who = (action.get("author_email") or action.get("source") or "system").strip().lower()
        if not _lookup_allowed(who):
            return "too many account look-ups in the last hour, try again later"
    return reason


def _check(action, users_col):
    atype = action.get("type") or ""
    if atype not in KNOWN_TYPES:
        return "action type is not allowed: %s" % atype
    author = (action.get("author_email") or action.get("created_by") or "").strip().lower()
    if not author or author == "system":
        if _system_ok(action):
            if atype == "notify_user_dm":
                target = (action.get("payload") or {}).get("user_email") or ""
                if not _find_user(users_col, target):
                    return "notify_user_dm target is not a console user"
            return None
        return "action has no author"
    user = _find_user(users_col, author)
    if not user:
        return "author is not a console user"
    if user.get("role") == "admin":
        return None
    if atype in ADMIN_ONLY:
        return "admin only"
    if atype == "notify_user_dm":
        return "admin only"                            # only leave decisions (admins) DM a team member
    need = ACTION_PERM.get(atype)
    if need and need not in (user.get("permissions") or []):
        return "author lacks the %s permission" % need
    return None
