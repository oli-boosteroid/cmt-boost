"""
Game List sync (bot side) - via a Google Apps Script Web App (NO Google Cloud, no service account).

The team keeps a "Game List" sheet with a stable numeric ID per game (columns ID, Title, ...). A small
Apps Script exposes one Web App URL:
  GET <url>?token=<secret>&action=read  -> { ok, games: [ { id, title }, ... ] }

The bot mirrors that into sync_db.game_list (one doc per game, _id = the sheet ID as a string), so both the
console and the public Known Issues page can resolve a game's ID <-> title and deep-link by ID. Only the
ID and title are stored; nothing else from that sheet is read.

Config:
  webapp_url - deployed Web App URL. From console_config key "game_list" (Admin > Known issues), or env
               GAME_LIST_WEBAPP_URL as a fallback.
  secret     - shared token; env GAME_LIST_SECRET (must match SECRET in the Apps Script).
"""
import logging
import os
from services.apps_script_url import safe_webapp_url
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

SYNC_INTERVAL = 86400  # once a day; "Sync now" in Admin refreshes on demand
_lock = threading.Lock()
_started = False


def _db():
    from services.db import main_db
    sync_db = main_db()
    return sync_db


def _webapp_url():
    doc = _db()["console_config"].find_one({"key": "game_list"}) or {}
    return safe_webapp_url(((doc.get("value") or {}).get("webapp_url") or os.getenv("GAME_LIST_WEBAPP_URL", "") or "").strip(), "game_list")


def _secret():
    return (os.getenv("GAME_LIST_SECRET", "") or "").strip()


def configured():
    return bool(_webapp_url() and _secret())


def _read_json(r):
    try:
        return r.json(), None
    except Exception:
        body = (r.text or "").strip().replace("\n", " ")
        hint = ""
        if body[:1] == "<" or "google.com/accounts" in body.lower() or "sign in" in body.lower():
            hint = (" - the web app returned an HTML page, not JSON. Re-deploy it with Access = "
                    "'Anyone' (NOT 'Anyone with Google account') and use the /exec URL.")
        return None, "HTTP %s: %s%s" % (r.status_code, body[:160], hint)


def sync_once():
    """Pull the Game List and mirror it into sync_db.game_list (replacing the whole set)."""
    url, secret = _webapp_url(), _secret()
    if not url:
        return False, "webapp_url not configured"
    if not secret:
        return False, "GAME_LIST_SECRET not set on the bot"
    try:
        r = requests.get(url, params={"token": secret, "action": "read"}, timeout=25)
    except Exception as e:
        return False, "read request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return False, "read failed %s" % err
    if not (isinstance(data, dict) and data.get("ok")):
        return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")
    games = data.get("games") or []
    col = _db()["game_list"]
    now = datetime.utcnow().isoformat()
    from pymongo import UpdateOne
    from services.game_title import norm_game_title
    # Index the normalized title so _game_ids_by_title can do an indexed {norm: {$in: ...}} lookup of just
    # the games that have an issue, instead of scanning the whole ~3000-row catalog on every request.
    try:
        col.create_index("norm")
    except Exception as _e:
        logger.debug("ignored: %s", _e)
    seen = set()
    ops = []
    for g in games:
        gid = str((g or {}).get("id") or "").strip()
        title = str((g or {}).get("title") or "").strip()
        if not gid or not title:
            continue
        platform = str((g or {}).get("platform") or "").strip().lower()
        ops.append(UpdateOne({"_id": gid}, {"$set": {"id": gid, "title": title, "norm": norm_game_title(title),
                                                     "platform": platform, "synced_at": now}}, upsert=True))
        seen.add(gid)
    # One round trip for the whole catalog (was 3000 individual upserts) - light on Mongo.
    if ops:
        col.bulk_write(ops, ordered=False)
    # Drop rows that vanished from the sheet so stale IDs cannot linger.
    try:
        if seen:
            col.delete_many({"_id": {"$nin": list(seen)}})
    except Exception as e:
        logger.warning("[GameList] prune failed: %s", e)
    logger.info("[GameList] synced %d games", len(seen))
    # Refresh the sheet's Known-Issues links to match the new catalog (change-gated, best-effort).
    try:
        push_known_issues_links()
    except Exception as e:
        logger.warning("[GameList] link push after sync failed: %s", e)
    return (len(seen) > 0), "%d games" % len(seen)


def _public_base():
    return (os.getenv("KNOWN_ISSUES_PUBLIC_BASE", "https://cmtconsole.xyz") or "").rstrip("/")


def _note_ids():
    """Which game IDs get a cell note (the issue Description/Solution). 'all' = every game with issues;
    otherwise a comma list. Default just the Arc Raiders test (2528) so notes start small."""
    raw = (os.getenv("GAME_LIST_NOTE_IDS", "2528") or "").strip()
    if raw.lower() == "all":
        return "all"
    return set(x.strip() for x in raw.split(",") if x.strip())


def _issue_note(issues):
    """One cell note for a game: 'Description: <problem>' + 'Solution: <canned or solution>' per issue."""
    blocks = []
    for it in issues:
        desc = (it.get("problem") or "").strip()
        sol = (it.get("canned") or "").strip() or (it.get("solution") or "").strip()
        if not desc and not sol:
            continue
        b = "Description: " + desc
        if sol:
            b += "\n\nSolution: " + sol
        blocks.append(b)
    return ("\n\n----------\n\n".join(blocks))[:4000]


def push_known_issues_links(force=False):
    """Write a 'Known Issues' hyperlink per game into the Game List sheet ('N issues found' -> that game's
    public page), plus optional cell notes. Fully computed from Mongo (no extra reads), CHANGE-GATED (a
    sha256 of the payload is stored in console_config; identical -> skip the write entirely) and sent as
    ONE batch the Apps Script applies with a single setValues. So it runs only when Known Issues / the
    catalog actually change, and costs ~one HTTP POST then. Triggered after a known-issues / game-list sync.
    force=True bypasses the change gate (used by the console's manual "Sync now", so a hand-edited sheet
    always gets rewritten even when the computed payload did not change)."""
    url, secret = _webapp_url(), _secret()
    if not url or not secret:
        return False, "not configured"
    db = _db()
    from services.game_title import norm_game_title, split_launcher
    ki = db["known_issues"].find_one({"_id": "games"}) or {}
    # index issues by base title, keeping each one's launcher so a "(Xbox)" issue only counts for the Xbox
    # row while a launcher-less issue counts for every platform of that game.
    by_base = {}
    for r in (ki.get("rows") or []):
        b, launcher = split_launcher(r.get("entity"))
        if b:
            by_base.setdefault(b, []).append((launcher, r))
    note_ids = _note_ids()
    base = _public_base()
    links, notes = [], {}
    for g in db["game_list"].find({}, {"id": 1, "title": 1, "norm": 1, "platform": 1}):
        gid = str(g.get("id") or "").strip()
        if not gid:
            continue
        b = g.get("norm") or norm_game_title(g.get("title"))
        plat = str(g.get("platform") or "").strip().lower()
        issues = [row for (launcher, row) in by_base.get(b, []) if (not launcher) or launcher == plat]
        n = len(issues)
        text = ("%d issue%s found" % (n, "" if n == 1 else "s")) if n else "No issues found"
        links.append({"id": gid, "text": text, "url": "%s/knownissues/%s" % (base, gid)})
        if issues and (note_ids == "all" or gid in note_ids):
            note = _issue_note(issues)
            if note:
                notes[gid] = note
    if not links:
        return True, "no games"
    import hashlib
    import json as _json
    # "fmt" is a write-format version: bump it whenever the Apps Script write changes (formula -> rich
    # text = 2; rich-text link WITHOUT underline = 3) so the change-gate forces a one-time rewrite of every
    # cell after that change, even though the link text/url payload itself did not move.
    digest = hashlib.sha256(_json.dumps({"fmt": 3, "l": links, "n": notes}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    cfg = db["console_config"]
    if not force and (cfg.find_one({"key": "game_list_links_hash"}) or {}).get("value") == digest:
        return True, "unchanged"   # nothing changed -> no sheet write at all
    try:
        r = requests.post(url, json={"token": secret, "action": "write_links", "links": links, "notes": notes}, timeout=90)
    except Exception as e:
        return False, "write request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return False, "write failed %s" % err
    if not (isinstance(data, dict) and data.get("ok")):
        return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")
    cfg.update_one({"key": "game_list_links_hash"}, {"$set": {"key": "game_list_links_hash", "value": digest}}, upsert=True)
    logger.info("[GameList] wrote %d Known-Issues links, %d notes", len(links), len(notes))
    return True, "wrote %d links, %d notes" % (len(links), len(notes))


def _loop():
    time.sleep(25)   # let config/env settle
    while True:
        try:
            if configured():
                with _lock:
                    sync_once()
        except Exception as e:
            logger.error("[GameList] sync loop error: %s", e)
        time.sleep(SYNC_INTERVAL)


def start_game_list_sync():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="game-list-sync").start()
    logger.info("[GameList] sync scheduler started (every %ds, Apps Script Web App)", SYNC_INTERVAL)
