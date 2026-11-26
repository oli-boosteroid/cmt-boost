"""
URG recipients: who must be told the result of an urgent request.

A URG ticket can carry several users: the main one (email in the description + the Communication Link
field) and every "Additional user" comment (added by the dedup in find_recent_urg_duplicate). Each of
them gets their own Slack notification and console To-Do card, and the ticket is moved to Done only
when ALL of them are transferred.

State lives in sync_db.urg_recipients (shared with the console):
  {_id: "URG-1|main" / "URG-1|c12345", key, rid, email, link, order, transferred, transferred_by,
   transferred_at, slack_channel, slack_ts}
"""
import logging
import re
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UID_RE = re.compile(r"User\s*ID\W{0,3}(\d{3,12})", re.I)


def _who_label(text):
    """"User ID N" for a recipient: the ID written in the ticket text, else the email in it resolved
    through the cloud admin (our cache / directory first). An address is never stored: "" when no ID."""
    text = str(text or "")
    m = _UID_RE.search(text)
    if m:
        return "User ID %s" % m.group(1)
    em = _EMAIL_RE.search(text)
    if not em:
        return ""
    try:
        from services.user_ref import label_for_email
        return label_for_email(em.group(0)) or ""
    except Exception:
        return ""
_LINK_RE = re.compile(r"Communication link:\s*\[?(https?://[^\s\]|]+)", re.I)
_URL_RE = re.compile(r"https?://[^\s\]|]+")
# Written by handlers.ticket_create (URG_OWN_MARKER) into a "My Own Ticket" description / comment.
_OWN_RE = re.compile(r"own ticket:\s*community manager", re.I)


def _col():
    from services.db import main_db
    sync_db = main_db()
    return sync_db["urg_recipients"]


def _field_text(v):
    if isinstance(v, dict):
        return str(v.get("value") or v.get("url") or v.get("name") or "")
    if isinstance(v, list):
        return " ".join(_field_text(x) for x in v)
    return str(v or "")


def parse_recipients(description, comm_field, comments, reporter_id="", ours=None):
    """description: wiki text (API v2); comm_field: Communication Link field value;
    comments: [{id, body, author:{accountId}}] (API v2); ours: our team's Jira accountIds.
    Returns [{rid, email, link, ours}] - main user first. "ours" = we must tell this user: the main user
    when the ticket's reporter is one of us, an additional user when one of us wrote that comment
    (an L1-bot ticket can carry our users too)."""
    ours = ours or set()
    desc = str(description or "")
    comm = _field_text(comm_field)
    lm = _URL_RE.search(comm)
    out = [{"rid": "main", "email": _who_label(desc), "link": lm.group(0) if lm else comm.strip(),
            "ours": reporter_id in ours, "own": bool(_OWN_RE.search(desc))}]
    for c in comments or []:
        body = str(c.get("body") or "")
        if not re.match(r"^\W*additional user", body.strip(), re.I):
            continue
        lk = _LINK_RE.search(body)
        out.append({"rid": "c%s" % c.get("id"), "email": _who_label(body), "link": lk.group(1) if lk else "",
                    "ours": ((c.get("author") or {}).get("accountId") or "") in ours,
                    "own": bool(_OWN_RE.search(body))})
    return out


_my_id = {}


def _our_ids(base, headers):
    ids = set()
    try:
        from services.identity_service import get_jira_our_user_map
        ids |= set(get_jira_our_user_map().keys())
    except Exception as _e:
        logger.warning("ignored: %s", _e)
    if "id" not in _my_id:
        try:
            r = requests.get(f"{base}/rest/api/2/myself", headers=headers, timeout=10)
            _my_id["id"] = (r.json() or {}).get("accountId", "") if r.status_code == 200 else ""
        except Exception:
            return ids
    if _my_id.get("id"):
        ids.add(_my_id["id"])
    return ids


def fetch_recipients(issue_key):
    """Read the ticket from Jira and store/refresh its recipients. Keeps the transferred flags and
    Slack refs already stored. Returns the stored docs in order."""
    from services.jira_service import JIRA_BASE_URL, _auth_headers, _configured
    try:
        from services.jira_service import JIRA_COMMUNICATION_LINK_FIELD as COMM
    except Exception:
        COMM = "communication_link"
    if not _configured():
        return list_recipients(issue_key)
    try:
        r = requests.get(f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}",
                         params={"fields": "description,reporter,%s" % COMM}, headers=_auth_headers(), timeout=15)
        f = (r.json() or {}).get("fields", {}) if r.status_code == 200 else {}
        rc = requests.get(f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment",
                          params={"maxResults": 200}, headers=_auth_headers(), timeout=15)
        comments = (rc.json() or {}).get("comments", []) if rc.status_code == 200 else []
    except Exception as e:
        logger.error("[URG] recipients fetch failed for %s: %s", issue_key, e)
        return list_recipients(issue_key)
    ours = _our_ids(JIRA_BASE_URL, _auth_headers())
    reporter = ((f.get("reporter") or {}).get("accountId") or "")
    for i, rec in enumerate(parse_recipients(f.get("description"), f.get(COMM), comments, reporter, ours)):
        _col().update_one({"_id": "%s|%s" % (issue_key, rec["rid"])},
                          {"$set": {"key": issue_key, "rid": rec["rid"], "email": rec["email"], "link": rec["link"],
                                    "order": i, "ours": rec["ours"], "own": rec.get("own", False)},
                           "$setOnInsert": {"transferred": False}}, upsert=True)
    return list_recipients(issue_key)


def list_recipients(issue_key):
    return list(_col().find({"key": issue_key}).sort("order", 1))


def set_slack(issue_key, rid, channel, ts):
    _col().update_one({"_id": "%s|%s" % (issue_key, rid)}, {"$set": {"slack_channel": channel, "slack_ts": ts}})


def mark_transferred(issue_key, rid, by=""):
    """Mark one recipient as transferred. Returns how many recipients are still waiting."""
    _col().update_one({"_id": "%s|%s" % (issue_key, rid)},
                      {"$set": {"transferred": True, "transferred_by": by, "transferred_at": datetime.utcnow()}})
    return _col().count_documents({"key": issue_key, "ours": True, "transferred": {"$ne": True}})


def delete_slack(issue_key, rid):
    """Delete the Slack card of one recipient (used when it is transferred from the console)."""
    d = _col().find_one({"_id": "%s|%s" % (issue_key, rid)}) or {}
    if not (d.get("slack_channel") and d.get("slack_ts")):
        return False
    try:
        from jira_notifications.jira_notifications import slack_client
        slack_client.chat_delete(channel=d["slack_channel"], ts=d["slack_ts"])
        return True
    except Exception as e:
        if "message_not_found" in str(e):     # already deleted (by hand or an earlier pass): nothing to do
            logger.debug("[URG] slack message already gone for %s|%s", issue_key, rid)
        else:
            logger.warning("[URG] slack delete failed for %s|%s: %s", issue_key, rid, e)
        return False


def close_own(issue_key, summary="", url="", update_result="", last_comment=""):
    """"My Own Ticket" requests (a community manager's own session, see handlers.ticket_create): there
    is no user to reply to, so at Review every own recipient is transferred automatically and the
    creator is told the result in the console bell + their chosen notification channels.
    Returns (own_count, left) - left = our recipients still waiting (0 -> the caller moves to Done)."""
    recs = [r for r in list_recipients(issue_key) if r.get("own") and not r.get("transferred")]
    if not recs:
        return 0, None
    left = None
    for r in recs:
        left = mark_transferred(issue_key, r["rid"], by="auto:own")
    _notify_own_creators(issue_key, summary, url, update_result, last_comment)
    return len(recs), left


def _notify_own_creators(issue_key, summary, url, update_result, last_comment):
    from services.db import main_db
    sync_db = main_db()
    title = f"Your urgent request {issue_key} is resolved"
    parts = []
    if summary:
        parts.append(summary)
    parts.append("Result: " + (update_result or "not set"))
    if last_comment:
        parts.append("Last comment: " + last_comment[:500])
    body = "\n".join(parts)
    for d in list(sync_db["urg_own"].find({"key": issue_key, "notified": {"$ne": True}})):
        email = d.get("author_email") or ""
        if email:
            try:
                # Personal bell entry (to_email): only this member sees it in the console.
                sync_db["console_notify_feed"].insert_one({
                    "type": "urg_own_result", "to_email": email, "title": title, "body": body,
                    "url": url or "", "route": "", "created_at": datetime.utcnow()})
            except Exception as e:
                logger.warning("[URG] own result feed insert failed for %s: %s", issue_key, e)
            try:
                from services.user_notify_service import notify_user
                notify_user(email, title, body, url or "", force=True)   # their own channels, any hour
            except Exception as e:
                logger.warning("[URG] own result notify failed for %s: %s", issue_key, e)
        sync_db["urg_own"].update_one({"_id": d["_id"]},
                                      {"$set": {"notified": True, "update_result": update_result,
                                                "notified_at": datetime.utcnow()}})
