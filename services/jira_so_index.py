"""
Index of Jira ticket titles (SO and anything else the bot sees), for the console's "Link Jira Ticket"
search: an operator starts typing the problem or the ticket number and gets matching tickets that
already exist - mostly the ones still waiting in Backlog - with their titles.

Collection `jira_so_index` (main DB), one document per ticket:
    _id / key   "SO-1234"
    project     "SO"
    num         1234 (int, for "newest first")
    summary     ticket title as Jira has it
    summary_lc  lower-case copy for the search
    status      last known Jira status ("Backlog", "CM In Progress", ...)
    created     Jira `created` (ISO string, may be "")
    updated_at  when we last wrote it
    source      "slack" | "created" | "jira"

Writers:
  * every transition seen in #jira-notifications (jira_notifications.handle_jira_channel_message)
  * every ticket the bot creates (jira_service.create_jira_issue)
  * a cheap Jira sweep: every 5 min the recently updated tickets, every hour all Backlog tickets
    (catches tickets created in Jira directly, which never produce a transition message)
Everything here is best-effort and never raises into the caller.
"""
import logging
import re
import threading
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]+)-(\d+)$")
_lock = threading.Lock()


def _col():
    from services.mongo_db_service import sync_db
    return sync_db["jira_so_index"]


def ensure_indexes():
    try:
        c = _col()
        c.create_index([("status", 1), ("num", -1)], background=True)
        c.create_index([("project", 1), ("num", -1)], background=True)
    except Exception as e:
        logger.debug("[JiraIndex] index create skipped: %s", e)


def upsert(issue_key, summary="", status="", created="", source="slack"):
    """Record (or refresh) one ticket. Empty summary/status never overwrite a known value."""
    key = (issue_key or "").strip().upper()
    m = _KEY_RE.match(key)
    if not m:
        return
    s = {"key": key, "project": m.group(1), "num": int(m.group(2)), "updated_at": datetime.utcnow(),
         "source": source}
    summary = (summary or "").strip()
    if summary:
        s["summary"] = summary[:400]
        s["summary_lc"] = summary[:400].lower()
    if status:
        s["status"] = status.strip()
    if created:
        s["created"] = str(created)
    try:
        _col().update_one({"_id": key}, {"$set": s}, upsert=True)
    except Exception as e:
        logger.debug("[JiraIndex] upsert %s failed: %s", key, e)


def upsert_many(rows, source="jira"):
    """Batch form of upsert for the sweeps: ONE read of the known docs, then writes ONLY tickets whose title,
    status or created date changed (the recent sweep returns up to 200 tickets every 10 minutes, almost all
    unchanged; writing each one bumped updated_at and cost a write per ticket on the main cluster).
    Returns how many were written."""
    from pymongo import UpdateOne
    want = {}
    for key, summary, status, created in rows:
        key = (key or "").strip().upper()
        m = _KEY_RE.match(key)
        if m:
            want[key] = (m, (summary or "").strip()[:400], (status or "").strip(), str(created or ""))
    if not want:
        return 0
    try:
        known = {d["_id"]: d for d in _col().find({"_id": {"$in": list(want)}},
                                                    {"summary": 1, "status": 1, "created": 1})}
    except Exception as e:
        logger.debug("[JiraIndex] batch read failed: %s", e)
        known = {}
    ops, now = [], datetime.utcnow()
    for key, (m, summary, status, created) in want.items():
        d = known.get(key)
        if d and (not summary or d.get("summary") == summary) and (not status or d.get("status") == status) \
                and (not created or d.get("created") == created):
            continue   # nothing new about this ticket
        s = {"key": key, "project": m.group(1), "num": int(m.group(2)), "updated_at": now, "source": source}
        if summary:
            s["summary"] = summary
            s["summary_lc"] = summary.lower()
        if status:
            s["status"] = status
        if created:
            s["created"] = created
        ops.append(UpdateOne({"_id": key}, {"$set": s}, upsert=True))
    if ops:
        try:
            _col().bulk_write(ops, ordered=False)
        except Exception as e:
            logger.debug("[JiraIndex] batch write failed: %s", e)
    return len(ops)


def _search(jql, max_pages=1, extra_fields="", raw=None):
    """([(key, summary, status, created)], complete) for a JQL query (paged, read-only). `complete` is
    True only when Jira said this was the last page (no error, no page cap). `extra_fields` are asked for
    in the same request and the full issues are appended to the `raw` list (auto-link reads comments)."""
    from services.jira_service import JIRA_BASE_URL, _auth_headers, _configured
    if not _configured():
        return [], False
    out, token, pages, complete = [], None, 0, False
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    while True:
        params = {"jql": jql, "fields": "summary,status,created" + ("," + extra_fields if extra_fields else ""),
                  "maxResults": 100}
        if token:
            params["nextPageToken"] = token
        try:
            r = requests.get(url, params=params, headers=_auth_headers(), timeout=20)
        except requests.RequestException as e:
            logger.warning("[JiraIndex] search failed: %s", type(e).__name__)
            break
        if r.status_code != 200:
            logger.warning("[JiraIndex] search HTTP %s", r.status_code)
            break
        data = r.json() or {}
        for it in data.get("issues", []):
            if raw is not None:
                raw.append(it)
            f = it.get("fields") or {}
            out.append((it.get("key", ""), f.get("summary") or "", (f.get("status") or {}).get("name", ""),
                        f.get("created") or ""))
        token = data.get("nextPageToken")
        pages += 1
        if data.get("isLast") or not token:
            complete = True
            break
        if pages >= max_pages:
            break
    return out, complete


def sync_recent():
    """Tickets created or changed in the last 25 minutes (runs every 10 min: one small query; transitions
    already arrive live through Slack, this only catches tickets created / edited in Jira directly)."""
    if not _lock.acquire(blocking=False):
        return
    try:
        from services.jira_service import JIRA_PROJECT_KEY
        issues = []
        # comments come in the same request: a new comment bumps `updated`, so the auto-link
        # (services/jira_autolink.py) sees every fresh comment here without an extra Jira search.
        rows, _ = _search(f"project = {JIRA_PROJECT_KEY} AND updated >= -25m ORDER BY updated DESC", max_pages=2,
                          extra_fields="comment", raw=issues)
        changed = upsert_many(rows)
        if changed:
            logger.info("[JiraIndex] recent sweep: %d ticket(s) read, %d changed", len(rows), changed)
        try:
            from services.jira_autolink import process_issues
            process_issues(issues)
        except Exception as e:
            logger.warning("[AutoLink] run failed: %s", e)
    except Exception as e:
        logger.warning("[JiraIndex] recent sweep failed: %s", e)
    finally:
        _lock.release()


def sync_backlog():
    """Every ticket currently in Backlog (every 6 hours + at start-up), newest first, up to 2000."""
    if not _lock.acquire(blocking=False):
        return
    try:
        from services.jira_service import JIRA_PROJECT_KEY
        rows, complete = _search(f'project = {JIRA_PROJECT_KEY} AND status = "Backlog" ORDER BY created DESC', max_pages=20)
        seen = set(k for k, _s, _st, _c in rows)
        upsert_many(rows)
        # A ticket we still list as Backlog but Jira no longer has there moved on without us seeing the
        # transition: mark it so the console stops offering it as a Backlog ticket.
        if rows and complete:
            _col().update_many({"project": JIRA_PROJECT_KEY, "status": "Backlog", "_id": {"$nin": list(seen)}},
                               {"$set": {"status": "", "updated_at": datetime.utcnow()}})
            logger.info("[JiraIndex] backlog sweep: %d ticket(s)", len(rows))
    except Exception as e:
        logger.warning("[JiraIndex] backlog sweep failed: %s", e)
    finally:
        _lock.release()
