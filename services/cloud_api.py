"""
Read-only client for the Boosteroid Operations API (cloud admin).

Used to replace user emails with the internal User ID everywhere the team works (Slack, Jira, the
console), and to list a user's recent streaming sessions. Runs only on the bot VM: the API key lives in
this machine's .env and the console reaches the API through the console_actions queue.

Only two endpoints are ever called, both read-only:
    POST /api/v1/operations/users/search
    POST /api/v1/operations/streaming/streaming-sessions/search
The mutating endpoints of the same API (ban, category, free time, deletions, subscriptions) are
deliberately NOT implemented here.

Environment:
    CLOUD_API_KEY          required; without it every call returns ("", "not configured")
    CLOUD_API_BASE_URL     default https://cloud.boosteroid.com (use https://stage.boosteroid.com to test)
    CLOUD_API_USER_AGENT   default "cmt-bot/1.0" (the API rejects requests without a User-Agent)
    CLOUD_API_CLIENT_DOMAIN optional X-Client-Domain value, only if the key uses a domain whitelist

Privacy: results are trimmed to what the callers need. Emails and IP addresses returned by the API are
dropped here and never reach Mongo, logs, Slack or the console.
"""
import logging
import os
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

_USERS_SEARCH = "/api/v1/operations/users/search"
_SESSIONS_SEARCH = "/api/v1/operations/streaming/streaming-sessions/search"
_TIMEOUT = 15
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_lock = threading.Lock()
_blocked_until = 0.0   # set from Retry-After on a 429 so we stop calling until the ban ends

# Hard ceiling on what this process may send, whatever the callers do. A bug that loops over lookups
# hits this wall instead of the cloud admin: once a window is full, calls fail locally ("try again
# later") without any request leaving the bot. Well under the API's own limit of 60 per minute.
# 50 a minute: the email -> User ID migration takes 30 (one every 2 s) and live lookups keep the rest,
# so together they never reach the API's 60.
_MAX_PER_MINUTE = int(os.getenv("CLOUD_API_MAX_PER_MINUTE", "50") or 50)
_MAX_PER_HOUR = int(os.getenv("CLOUD_API_MAX_PER_HOUR", "3000") or 3000)
_FAIL_PAUSE_SEC = 60          # after a network error / 5xx nothing is sent for this long
_sent = []                    # timestamps of requests sent in the last hour
_refused_logged_at = 0.0


def _allow_request() -> bool:
    """Record one outgoing request if both windows have room. False = refused, send nothing."""
    global _refused_logged_at
    now = time.time()
    with _lock:
        while _sent and now - _sent[0] > 3600:
            _sent.pop(0)
        last_minute = sum(1 for t in _sent if now - t <= 60)
        if last_minute >= _MAX_PER_MINUTE or len(_sent) >= _MAX_PER_HOUR:
            if now - _refused_logged_at > 600:      # one warning per 10 minutes, not one per refusal
                _refused_logged_at = now
                logger.warning("[CloudAPI] local request ceiling reached (%d in the last minute, %d in the last hour) - "
                               "not sending more for now", last_minute, len(_sent))
            return False
        _sent.append(now)
        return True


def stats() -> dict:
    now = time.time()
    with _lock:
        return {"last_minute": sum(1 for t in _sent if now - t <= 60),
                "last_hour": sum(1 for t in _sent if now - t <= 3600),
                "paused_for": max(0, int(_blocked_until - now))}


def configured() -> bool:
    return bool(os.getenv("CLOUD_API_KEY"))


def _base() -> str:
    return (os.getenv("CLOUD_API_BASE_URL") or "https://cloud.boosteroid.com").rstrip("/")


def _headers() -> dict:
    h = {
        "X-Api-Key": os.getenv("CLOUD_API_KEY", ""),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": os.getenv("CLOUD_API_USER_AGENT") or "cmt-bot/1.0",
    }
    dom = (os.getenv("CLOUD_API_CLIENT_DOMAIN") or "").strip()
    if dom:
        h["X-Client-Domain"] = dom
    return h


def _error_text(status, body) -> str:
    """Short operator-facing reason. Never includes request data (so no email ends up in a log)."""
    if status == 401:
        return "cloud API: the API key was rejected"
    if status == 403:
        num = (body or {}).get("error_number")
        if num == 28000015:
            return "cloud API: this server's IP is not whitelisted for the key"
        return "cloud API: the key has no permission for this request"
    if status == 404:
        return "not found"
    if status == 422:
        return "cloud API: " + str((body or {}).get("error_message") or "invalid request")
    if status == 429:
        return "cloud API: rate limit reached, try again in a minute"
    if status in (502, 504):
        return "cloud API is temporarily unavailable"
    return "cloud API error %s" % status


def _what(payload) -> str:
    """Short, address-free description of a request for the log."""
    if payload.get("userEmail"):
        name, _, dom = str(payload["userEmail"]).partition("@")
        return "email=%s***@%s" % (name[:1], dom)
    if payload.get("streamingSessionId"):
        return "session=%s" % str(payload["streamingSessionId"])[:8]
    if payload.get("userId") is not None:
        return "userId=%s" % payload["userId"]
    return "?"


def _caller() -> str:
    """Who asked: the first function outside this module on the call stack."""
    try:
        import sys
        f = sys._getframe(2)
        names = []
        while f is not None and len(names) < 3:
            if f.f_code.co_filename != __file__:
                names.append(f.f_code.co_name)
            f = f.f_back
        return "<-".join(names) or "?"
    except Exception:
        return "?"


def _post(path, payload):
    """POST and return (data, error). `data` is the parsed JSON body on 200, else None.
    Every request that actually leaves the bot is written to the log with who asked and the counters."""
    global _blocked_until
    if not configured():
        return None, "cloud API is not configured (CLOUD_API_KEY)"
    now = time.time()
    if now < _blocked_until:
        return None, "cloud API: rate limit reached, try again in %ds" % int(_blocked_until - now + 1)
    if not _allow_request():
        return None, "cloud API: local request limit reached, try again later"
    _t0 = time.time()
    _short = path.rsplit("/", 2)[-2] + "/" + path.rsplit("/", 1)[-1]
    try:
        r = requests.post(_base() + path, json=payload, headers=_headers(), timeout=_TIMEOUT)
        _st = stats()
        logger.info("[CloudAPI] request %s %s -> HTTP %s in %dms | by %s | sent: %d last min, %d last hour",
                    _short, _what(payload), r.status_code, int((time.time() - _t0) * 1000), _caller(),
                    _st["last_minute"], _st["last_hour"])
    except requests.RequestException as e:
        logger.info("[CloudAPI] request %s %s -> no answer (%s) | by %s", _short, _what(payload),
                    type(e).__name__, _caller())
        logger.warning("[CloudAPI] %s request failed: %s", path, type(e).__name__)
        with _lock:
            _blocked_until = time.time() + _FAIL_PAUSE_SEC     # do not hammer an API that is down
        return None, "cloud API is unreachable"
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200:
        return body, None
    if r.status_code == 429:
        try:
            wait = int(r.headers.get("Retry-After") or r.headers.get("X-Retry-After") or 60)
        except ValueError:
            wait = 60
        with _lock:
            _blocked_until = time.time() + max(1, min(wait, 3600))
    if r.status_code in (401, 403) or r.status_code >= 500:
        # Bad key / IP not whitelisted / API down: repeating the call cannot help, so pause.
        with _lock:
            _blocked_until = max(_blocked_until, time.time() + (600 if r.status_code in (401, 403) else _FAIL_PAUSE_SEC))
    if r.status_code != 404:
        logger.warning("[CloudAPI] %s -> HTTP %s (error_number %s)", path, r.status_code,
                       (body or {}).get("error_number"))
    return None, _error_text(r.status_code, body)


def _user_public(u: dict) -> dict:
    """The part of a user record we keep: identifiers and state, no email, no name."""
    return {
        "user_id": u.get("userId"),
        "is_active": bool(u.get("userIsActive")),
        "is_blocked": bool(u.get("userIsBlocked")),
        "category": u.get("userCategoryName") or "",
        "country": u.get("userCountryIsoCode") or "",
    }


# Answers are kept in memory so the same lookup never reaches the cloud admin twice in a row
# (a form re-opened, a refresh, two operators on the same chat). A found user is kept for 30 min,
# a "not found" for 10 min; errors are never cached.
_USER_TTL = 1800
_USER_MISS_TTL = 600
_user_cache = {}   # "e:<email>" / "u:<id>" -> (expires_at, user dict or None)


def _user_cache_get(key):
    hit = _user_cache.get(key)
    if hit and hit[0] > time.time():
        return hit
    return None


def _user_cache_put(key, user):
    if len(_user_cache) > 5000:
        _user_cache.clear()
    _user_cache[key] = (time.time() + (_USER_TTL if user else _USER_MISS_TTL), user)


def find_user(email: str = "", user_id=None):
    """Resolve a user by email or by User ID. Returns (user dict, None) or (None, reason)."""
    key = ("u:%s" % str(user_id).strip()) if user_id not in (None, "") else ("e:" + (email or "").strip().lower())
    hit = _user_cache_get(key)
    if hit:
        return (dict(hit[1]), None) if hit[1] else (None, "user not found")
    payload = {}
    if user_id not in (None, ""):
        try:
            payload["userId"] = int(user_id)
        except (TypeError, ValueError):
            return None, "User ID must be a number"
    elif email:
        email = email.strip()
        if not _EMAIL_RE.match(email):
            return None, "that is not a valid email address"
        payload["userEmail"] = email
    else:
        return None, "an email or a User ID is required"
    body, err = _post(_USERS_SEARCH, payload)
    if err:
        if err == "not found":
            _user_cache_put(key, None)
            return None, "user not found"
        return None, err
    data = (body or {}).get("data") or {}
    if not data.get("userId"):
        _user_cache_put(key, None)
        return None, "user not found"
    user = _user_public(data)
    _user_cache_put(key, user)
    if user.get("user_id") is not None:
        _user_cache_put("u:%s" % user["user_id"], user)
    return dict(user), None


def _session_public(s: dict) -> dict:
    pg = s.get("playground") or {}
    term = s.get("terminationReason") or {}
    return {
        "session_id": s.get("streamingSessionId") or "",
        "user_id": s.get("userId"),
        "application_id": s.get("applicationId"),
        "created_at": s.get("createdAt") or "",
        "started_at": s.get("startedAt") or "",
        "length": s.get("length") or 0,
        "playground": pg.get("title") or "",
        "ended": term.get("code") or "",
    }


_SESSIONS_TTL = 3600
_SESSIONS_FORCE_MIN = 60   # a Refresh within a minute of the last real answer reuses it
_sessions_cache = {}   # user id -> (expires_at, rows)


def recent_sessions(user_id, limit: int = 5, last_days: int = 30, force: bool = False):
    """The user's most recent sessions, newest first. Returns (list, None) or (None, reason).

    One API request returns up to 25 sessions (the API's page limit) and the answer is kept for an hour
    per user, so opening a form again costs nothing. `force=True` (the form's Refresh button) asks again."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None, "User ID must be a number"
    limit = max(1, min(int(limit or 5), 25))
    hit = _sessions_cache.get(uid)
    now = time.time()
    if hit and hit[0] > now and (not force or now - (hit[0] - _SESSIONS_TTL) < _SESSIONS_FORCE_MIN):
        return hit[1][:limit], None
    body, err = _post(_SESSIONS_SEARCH, {"userId": uid, "createdLastDays": max(1, min(int(last_days or 30), 365)),
                                         "page": 1, "perPage": 25})
    if err:
        return (None, "user not found") if err == "not found" else (None, err)
    rows = [_session_public(s) for s in ((body or {}).get("data") or [])]
    rows.sort(key=lambda s: s.get("created_at") or "", reverse=True)   # do not rely on the API's order
    if len(_sessions_cache) > 2000:
        _sessions_cache.clear()
    _sessions_cache[uid] = (time.time() + _SESSIONS_TTL, rows)
    return rows[:limit], None


_owner_cache = {}   # session id -> owner user id (a session never changes owner, kept for the process life)


def session_owner(session_id: str):
    """User ID that owns a streaming session. Returns (user_id, None) or (None, reason)."""
    sid = (session_id or "").strip()
    if not _UUID_RE.match(sid):
        return None, "that is not a session ID"
    if sid.lower() in _owner_cache:
        return _owner_cache[sid.lower()], None
    body, err = _post(_SESSIONS_SEARCH, {"streamingSessionId": sid, "page": 1, "perPage": 1})
    if err:
        return (None, "session not found") if err == "not found" else (None, err)
    rows = (body or {}).get("data") or []
    if not rows or not rows[0].get("userId"):
        return None, "session not found"
    if len(_owner_cache) > 5000:
        _owner_cache.clear()
    _owner_cache[sid.lower()] = rows[0]["userId"]
    return rows[0]["userId"], None
