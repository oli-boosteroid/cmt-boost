"""
Known Issues sync (bot side) - via a Google Apps Script Web App (NO Google Cloud, no service account).

A small Apps Script bound to the team sheet exposes one Web App URL:
  GET  <url>?token=<secret>&action=read   -> { ok, categories: { games:{has_entity,rows}, apps:{...},
                                               session:{...}, account:{...} } }
  POST <url>  {token, action:"add", category, entity, problem, solution, link, canned}
       -> the script inserts the row in ALPHABETICAL order by column A and returns { ok:true }.

The bot mirrors the read result into sync_db.known_issues every ~5 min and forwards "add" requests. All
sheet parsing + alphabetical insertion live in the Apps Script; this module is just the HTTP bridge, so
the console never needs Google credentials.

Config:
  webapp_url  - the deployed Web App URL. From console_config key "known_issues" (Admin > Known issues),
                or env KNOWN_ISSUES_WEBAPP_URL as a fallback.
  secret      - the shared token; env KNOWN_ISSUES_SECRET (must match SECRET in the Apps Script).
"""
import logging
import os
from services.apps_script_url import safe_webapp_url
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

SYNC_INTERVAL = 86400  # once a day (edits + "Sync now" refresh on demand, so this only picks up
                       # direct-in-sheet changes; keep it light on the server)
_lock = threading.Lock()
_started = False


def _db():
    from services.db import main_db
    sync_db = main_db()
    return sync_db


def _webapp_url():
    doc = _db()["console_config"].find_one({"key": "known_issues"}) or {}
    return safe_webapp_url(((doc.get("value") or {}).get("webapp_url") or os.getenv("KNOWN_ISSUES_WEBAPP_URL", "") or "").strip(), "known_issues")


def _secret():
    return (os.getenv("KNOWN_ISSUES_SECRET", "") or "").strip()


def configured():
    return bool(_webapp_url() and _secret())


def _read_json(r):
    """Parse a Web App response as JSON, or return a clear error string explaining what came back
    (Apps Script serves an HTML login page instead of JSON when the deployment isn't public)."""
    try:
        return r.json(), None
    except Exception:
        body = (r.text or "").strip().replace("\n", " ")
        hint = ""
        if body[:1] == "<" or "google.com/accounts" in body.lower() or "sign in" in body.lower():
            hint = (" — the web app returned an HTML page, not JSON. Re-deploy it with Access = "
                    "'Anyone' (NOT 'Anyone with Google account') and use the /exec URL "
                    "(Deploy -> Manage deployments -> edit the existing one).")
        return None, "HTTP %s: %s%s" % (r.status_code, body[:160], hint)


def sync_once():
    """Pull every tab from the Apps Script Web App and mirror it into sync_db.known_issues."""
    url, secret = _webapp_url(), _secret()
    if not url:
        return False, "webapp_url not configured"
    if not secret:
        return False, "KNOWN_ISSUES_SECRET not set on the bot"
    try:
        r = requests.get(url, params={"token": secret, "action": "read"}, timeout=25)
    except Exception as e:
        return False, "read request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return False, "read failed %s" % err
    if not (isinstance(data, dict) and data.get("ok")):
        return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")
    cats = data.get("categories") or {}
    col = _db()["known_issues"]
    # Rows as they were BEFORE this sync, to tell which issues are new (-> Telegram notification).
    before = {}
    for _d in col.find({}, {"rows": 1}):
        before[_d["_id"]] = {_row_key(r) for r in (_d.get("rows") or [])}
    ok = 0
    for cat, payload in cats.items():
        payload = payload or {}
        rows = payload.get("rows") or []
        col.update_one({"_id": cat},
                       {"$set": {"category": cat, "has_entity": bool(payload.get("has_entity")),
                                 "rows": rows, "synced_at": datetime.utcnow().isoformat()}},
                       upsert=True)
        ok += 1
        logger.info("[KnownIssues] synced '%s': %d rows", cat, len(rows))
    try:
        _notify_new_issues(before, cats)
    except Exception as e:
        logger.warning("[KnownIssues] new-issue notification failed: %s", e)
    # Track when the CONTENT actually changed (a row added / removed / edited), NOT just when we synced -
    # this drives the "Last updated" shown on the public page. Fingerprint the sheet content (no synced_at)
    # and only bump the timestamp when it differs from last time.
    try:
        import hashlib
        import json as _json
        fp = hashlib.sha256(_json.dumps(cats, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        cfg = _db()["console_config"]
        prev = (cfg.find_one({"key": "known_issues_meta"}) or {}).get("value") or {}
        if prev.get("hash") != fp:
            cfg.update_one({"key": "known_issues_meta"},
                           {"$set": {"key": "known_issues_meta",
                                     "value": {"hash": fp, "updated_at": datetime.utcnow().isoformat()}}},
                           upsert=True)
            logger.info("[KnownIssues] content changed -> Last updated bumped")
    except Exception as e:
        logger.warning("[KnownIssues] content-change tracking failed: %s", e)
    # Known Issues changed -> refresh the Game List sheet's "Known Issues" links (change-gated, so this
    # is a no-op HTTP-wise when counts did not actually move). Best-effort; never fail the KI sync.
    try:
        from services.game_list_sync import push_known_issues_links
        push_known_issues_links()
    except Exception as e:
        logger.warning("[KnownIssues] game-list link refresh failed: %s", e)
    return (ok > 0), "%d categories" % ok


# ---------- Telegram: "New known issue added" ----------
_CAT_LABEL = {"games": "Games", "apps": "Apps", "session": "Session", "account": "Account"}
_NOTIFY_MAX = 10   # per sync; more than that (e.g. a sheet re-import) -> the first 10 + one "and N more" line


def _norm(s):
    return " ".join(str(s or "").split()).lower()


def _row_key(r):
    return (_norm(r.get("entity")), _norm(r.get("problem")))


def _tg_targets():
    """Chats from console Admin > Known issues: one per line, "chat_id" or "chat_id:topic_id"."""
    v = (_db()["console_config"].find_one({"key": "known_issues"}) or {}).get("value") or {}
    out = []
    for line in str(v.get("tg_chats") or "").replace(",", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        chat, _, topic = line.partition(":")
        chat = chat.strip()
        if chat.lstrip("-").isdigit() or chat.startswith("@"):
            out.append((chat, topic.strip() if topic.strip().isdigit() else ""))
    return out


def _tg_send(chat, topic, text):
    import requests as _rq
    from telegram_bot.config import TELEGRAM_BOT_TOKEN
    body = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if topic:
        body["message_thread_id"] = int(topic)
    r = _rq.post("https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN, json=body, timeout=10)
    if not r.ok:
        logger.warning("[KnownIssues] Telegram send to %s failed: %s", chat, (r.text or "")[:200])
    return r.ok


def known_issue_text(cat, row):
    """New known issue added: <Game/App or category> - <problem>\n\n<solution>"""
    who = (row.get("entity") or "").strip() or _CAT_LABEL.get(cat, cat.capitalize())
    text = "New known issue added: %s - %s" % (who, (row.get("problem") or "").strip())
    sol = (row.get("solution") or "").strip()
    text += ("\n\n" + sol if sol else "")
    link = (row.get("link") or "").strip()
    if link.lower().startswith(("http://", "https://")):   # only a real URL from the Link column
        text += "\n\nJira ticket: " + link
    return text


def _ann_id(cat, key):
    import hashlib
    return hashlib.sha1(("%s|%s|%s" % (cat, key[0], key[1])).encode("utf-8")).hexdigest()[:24]


def _notify_new_issues(before, cats):
    """Announce issues that appeared since the deploy, and only once, ever.
    - `known_issues_announced` remembers every issue already seen: a row that vanishes (a tab that briefly
      reads empty, a cut + paste) and comes back is NOT announced again; the insert is atomic, so two
      overlapping syncs can not both send the same issue.
    - The first run after deploy (empty memory) records the whole sheet SILENTLY.
    - A text fix typed into the sheet (same number of rows for that game/app or Session/Account tab, only the
      wording changed) is an edit, not a new issue: nothing is sent for it."""
    from pymongo.errors import DuplicateKeyError
    ann = _db()["known_issues_announced"]
    first_run = ann.estimated_document_count() == 0
    new = []
    for cat, payload in (cats or {}).items():
        rows = [r for r in ((payload or {}).get("rows") or []) if _norm(r.get("problem"))]
        old_keys = before.get(cat, set())
        old_count, new_count = {}, {}
        for k in old_keys:
            old_count[k[0]] = old_count.get(k[0], 0) + 1
        for r in rows:
            e = _row_key(r)[0]
            new_count[e] = new_count.get(e, 0) + 1
        for r in rows:
            k = _row_key(r)
            try:
                ann.insert_one({"_id": _ann_id(cat, k), "cat": cat, "entity": k[0], "problem": k[1],
                                "at": datetime.utcnow()})
            except DuplicateKeyError:
                continue                     # seen before (at any time): never announce twice
            if first_run or cat not in before or k in old_keys:
                continue                     # silent bootstrap / first sync of a tab
            if new_count.get(k[0], 0) <= old_count.get(k[0], 0):
                continue                     # same row count for this game/app: a renamed row, not a new issue
            new.append((cat, r))
    if first_run:
        logger.info("[KnownIssues] new-issue notifications armed (%d existing issue(s) recorded silently)",
                    ann.estimated_document_count())
        return
    if not new:
        return
    targets = _tg_targets()
    if not targets:
        return
    msgs = [known_issue_text(c, r) for c, r in new[:_NOTIFY_MAX]]
    if len(new) > _NOTIFY_MAX:
        msgs.append("And %d more new known issues." % (len(new) - _NOTIFY_MAX))
    for chat, topic in targets:
        for m in msgs:
            _tg_send(chat, topic, m)
    logger.info("[KnownIssues] notified %d chat(s) about %d new issue(s)", len(targets), len(new))


def update_row(category, entity, problem, fields):
    """Overwrite Problem / Solution / Canned of an existing row (an approved "Suggest edit").
    The mirror is updated FIRST, so the re-sync right after does not report a renamed issue as new."""
    url, secret = _webapp_url(), _secret()
    if not url:
        return False, "webapp_url not configured"
    if not secret:
        return False, "KNOWN_ISSUES_SECRET not set on the bot"
    fields = {k: str(v) for k, v in (fields or {}).items() if k in ("problem", "solution", "canned")}
    if not fields:
        return False, "nothing to update"
    payload = {"token": secret, "action": "update", "category": (category or "").strip().lower(),
               "entity": entity or "", "problem": problem or "", "set": fields}
    try:
        r = requests.post(url, json=payload, timeout=25)
    except Exception as e:
        return False, "update request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return False, "update failed %s" % err
    if not (isinstance(data, dict) and data.get("ok")):
        if isinstance(data, dict) and data.get("error") == "unknown action":
            return False, "the sheet's Apps Script is outdated: redeploy services/known_issues_apps_script.gs"
        return False, (data.get("error") if isinstance(data, dict) else "bad response") or "webapp error"
    cat = (category or "").strip().lower()
    try:
        col = _db()["known_issues"]
        doc = col.find_one({"_id": cat}) or {}
        rows = doc.get("rows") or []
        for row in rows:
            if _norm(row.get("problem")) == _norm(problem) and _norm(row.get("entity")) == _norm(entity):
                row.update(fields)
        col.update_one({"_id": cat}, {"$set": {"rows": rows}})
        if "problem" in fields and _norm(fields["problem"]) != _norm(problem):
            _rekey_shots(cat, entity, problem, fields["problem"])
    except Exception as e:
        logger.warning("[KnownIssues] mirror update after edit failed: %s", e)
    try:
        sync_once()
    except Exception as _e:
        logger.debug("[KnownIssues] post-update sync ignored: %s", _e)
    return True, "updated"


def _shot_key(category, entity, problem):
    # Same key as the console's services/ki_shots.issue_key (screenshots are bound to the problem text).
    import hashlib
    raw = "%s|%s|%s" % (_norm(category), _norm(entity), _norm(problem))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _rekey_shots(cat, entity, old_problem, new_problem):
    """A renamed problem keeps its screenshots: move them to the new key."""
    col = _db()["known_issue_shots"]
    old = col.find_one({"_id": _shot_key(cat, entity, old_problem)})
    if not old or not old.get("shots"):
        return
    col.update_one({"_id": _shot_key(cat, entity, new_problem)},
                   {"$setOnInsert": {"category": cat, "entity": entity, "problem": new_problem},
                    "$push": {"shots": {"$each": old["shots"]}}}, upsert=True)
    col.delete_one({"_id": old["_id"]})


def set_canned(category, entity, problem, canned):
    """Fill the Canned Message cell of an existing row via the Apps Script (never overwrites a filled one)."""
    url, secret = _webapp_url(), _secret()
    if not url:
        return False, "webapp_url not configured"
    if not secret:
        return False, "KNOWN_ISSUES_SECRET not set on the bot"
    payload = {"token": secret, "action": "set_canned", "category": (category or "").strip().lower(),
               "entity": entity or "", "problem": problem or "", "canned": canned or ""}
    try:
        r = requests.post(url, json=payload, timeout=25)
    except Exception as e:
        return False, "set_canned request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return False, "set_canned failed %s" % err
    if isinstance(data, dict) and data.get("ok"):
        try:
            sync_once()   # reflect it in the mirror right away
        except Exception as _e:
            logger.debug("[known_issues_sync] post-set_canned sync ignored: %s", _e)
        return True, "updated"
    if isinstance(data, dict) and data.get("error") == "unknown action":
        return False, "the sheet's Apps Script is outdated: redeploy services/known_issues_apps_script.gs"
    return False, (data.get("error") if isinstance(data, dict) else "bad response") or "webapp error"


def append_issue(category, fields):
    """Ask the Apps Script Web App to insert a new row (it places it alphabetically by column A)."""
    url, secret = _webapp_url(), _secret()
    if not url:
        return False, "webapp_url not configured"
    if not secret:
        return False, "KNOWN_ISSUES_SECRET not set on the bot"
    payload = {"token": secret, "action": "add", "category": (category or "").strip().lower(),
               "entity": fields.get("entity", ""), "problem": fields.get("problem", ""),
               "solution": fields.get("solution", ""), "link": fields.get("link", ""),
               "canned": fields.get("canned", "")}
    try:
        r = requests.post(url, json=payload, timeout=25)
    except Exception as e:
        return False, "add request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return False, "add failed %s" % err
    if isinstance(data, dict) and data.get("ok"):
        try:
            sync_once()   # reflect the new row in the mirror right away
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        return True, "added"
    return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")


def _loop():
    time.sleep(20)   # let config/env settle
    while True:
        try:
            if configured():
                with _lock:
                    sync_once()
        except Exception as e:
            logger.error("[KnownIssues] sync loop error: %s", e)
        time.sleep(SYNC_INTERVAL)


def start_known_issues_sync():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="known-issues-sync").start()
    logger.info("[KnownIssues] sync scheduler started (every %ds, Apps Script Web App)", SYNC_INTERVAL)
