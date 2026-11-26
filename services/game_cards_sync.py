"""
Game Cards sync (bot side) - weekly game-card sheet via a Google Apps Script Web App.

The console does NOT import the sheet's existing history: it only shows weeks CREATED from the console
(create_week / generate), tagged `source: "console"` in Mongo. The Google Sheet is the full archive;
Mongo keeps just the newest KEEP_WEEKS console weeks (+ their screenshot ZIPs) to stay lean.

- generate(): reuses the Slack card generator (services.steam_service) - parse Steam links, fetch each
  game, append rows to the week tab, stash per-card screenshots ZIP, and refresh that week in Mongo.
- create_week()/set_row()/set_cell(): write to the sheet, then refresh that one week in Mongo.

Config: webapp_url in console_config key "game_cards" (Admin) or env GAME_CARDS_WEBAPP_URL; secret in
env GAME_CARDS_SECRET (falls back to KNOWN_ISSUES_SECRET).
"""
import base64
import io
import logging
import os
from services.apps_script_url import safe_webapp_url
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

SYNC_INTERVAL = 86400  # once a day; edits/generate/"Sync now" refresh on demand
KEEP_WEEKS = 3         # mirror only the newest console weeks; the sheet keeps the full archive
_lock = threading.Lock()
_started = False


def _db():
    from services.db import main_db
    sync_db = main_db()
    return sync_db


def _webapp_url():
    doc = _db()["console_config"].find_one({"key": "game_cards"}) or {}
    return safe_webapp_url(((doc.get("value") or {}).get("webapp_url") or os.getenv("GAME_CARDS_WEBAPP_URL", "") or "").strip(), "game_cards")


def _secret():
    return (os.getenv("GAME_CARDS_SECRET", "") or os.getenv("KNOWN_ISSUES_SECRET", "") or "").strip()


def configured():
    return bool(_webapp_url() and _secret())


def _read_json(r):
    try:
        return r.json(), None
    except Exception:
        body = (r.text or "").strip().replace("\n", " ")
        hint = " — web app returned HTML, not JSON. Re-deploy with Access = 'Anyone', use the /exec URL." if body[:1] == "<" else ""
        return None, "HTTP %s: %s%s" % (r.status_code, body[:160], hint)


def _post(payload):
    url, secret = _webapp_url(), _secret()
    if not url:
        return None, "webapp_url not configured"
    if not secret:
        return None, "GAME_CARDS_SECRET not set on the bot"
    payload = dict(payload)
    payload["token"] = secret
    try:
        r = requests.post(url, json=payload, timeout=30)
    except Exception as e:
        return None, "request failed: %s" % e
    return _read_json(r)


def _read_all():
    url, secret = _webapp_url(), _secret()
    if not url:
        return None, "webapp_url not configured"
    if not secret:
        return None, "GAME_CARDS_SECRET not set on the bot"
    try:
        r = requests.get(url, params={"token": secret, "action": "read"}, timeout=30)
    except Exception as e:
        return None, "read request failed: %s" % e
    data, err = _read_json(r)
    if err:
        return None, "read failed %s" % err
    if not (isinstance(data, dict) and data.get("ok")):
        return None, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")
    return data, None


def _register_week(name):
    """Mark a week as console-created (so it's the only kind the console shows)."""
    _db()["game_cards"].update_one(
        {"_id": name},
        {"$set": {"week": name, "source": "console"},
         "$setOnInsert": {"created_at": datetime.utcnow().isoformat(), "rows": []}},
        upsert=True)


def _refresh_week(name):
    """Re-read one week's tab from the sheet into its Mongo doc (console weeks only)."""
    data, err = _read_all()
    if err:
        return False, err
    weeks = data.get("weeks") or {}
    order = data.get("order") or []
    col = _db()["game_cards"]
    if name not in weeks:
        col.delete_one({"_id": name})
        return True, "gone"
    col.update_one(
        {"_id": name},
        {"$set": {"week": name, "order_index": order.index(name) if name in order else 0,
                  "rows": weeks[name].get("rows") or [], "source": "console",
                  "synced_at": datetime.utcnow().isoformat()},
         "$setOnInsert": {"created_at": datetime.utcnow().isoformat()}},
        upsert=True)
    return True, "ok"


def sync_once():
    """Refresh ONLY console-created weeks from their sheet tabs (never import other/old tabs); drop
    console weeks whose tab was deleted; keep just the newest KEEP_WEEKS + prune their screenshot ZIPs."""
    data, err = _read_all()
    if err:
        return False, err
    weeks = data.get("weeks") or {}
    order = data.get("order") or []
    col = _db()["game_cards"]
    # One-time / ongoing cleanup: forget any legacy docs that weren't created via the console.
    col.delete_many({"source": {"$ne": "console"}})
    kept = 0
    for doc in list(col.find({"source": "console"})):
        name = doc["_id"]
        if name in weeks:
            col.update_one({"_id": name},
                           {"$set": {"week": name, "order_index": order.index(name) if name in order else 0,
                                     "rows": weeks[name].get("rows") or [],
                                     "synced_at": datetime.utcnow().isoformat()}})
            kept += 1
        else:
            col.delete_one({"_id": name})   # tab removed in the sheet
    # Retention: keep the newest KEEP_WEEKS console weeks; prune the rest + their images.
    from services import blob_store
    extra = list(col.find({"source": "console"}).sort("created_at", -1))[KEEP_WEEKS:]
    for doc in extra:
        col.delete_one({"_id": doc["_id"]})
        blob_store.delete_blobs("game_card_images", {"week": doc["_id"]})   # ZIPs live on the blob store
    return True, "%d console weeks" % (kept - len(extra))


def set_cell(week, row_index, col, value):
    data, err = _post({"action": "set", "week": week, "row_index": row_index, "col": col, "value": value})
    if err:
        return False, err
    if isinstance(data, dict) and data.get("ok"):
        try:
            _refresh_week(week)
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        return True, "set"
    return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")


def set_row(week, row_index, fields):
    data, err = _post({"action": "set_row", "week": week, "row_index": row_index, "card": fields or {}})
    if err:
        return False, err
    if isinstance(data, dict) and data.get("ok"):
        try:
            _refresh_week(week)
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        return True, "set"
    return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")


def delete_row(week, row_index):
    """Delete one card row from the week tab (removes extra games / duplicates), then refresh
    that week in Mongo. Positional row_index (0-based within the tab), matching set_row. Best-effort:
    also drops the deleted card's orphaned screenshot ZIP."""
    # Capture the card title first (from the current mirror) so we can clean up its screenshot ZIP after.
    title = ""
    try:
        doc = _db()["game_cards"].find_one({"_id": week}, {"rows": 1})
        rows = (doc or {}).get("rows") or []
        if isinstance(row_index, int) and 0 <= row_index < len(rows):
            title = (rows[row_index] or {}).get("title") or ""
    except Exception:
        title = ""
    data, err = _post({"action": "delete_row", "week": week, "row_index": row_index})
    if err:
        return False, err
    if isinstance(data, dict) and data.get("ok"):
        try:
            _refresh_week(week)
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        if title:
            try:
                from services import blob_store
                blob_store.delete_blobs("game_card_images", {"_id": _img_key(week, title)})
            except Exception as _e:
                logger.debug("ignored: %s", _e)
        return True, "deleted"
    return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")


def create_week(week):
    data, err = _post({"action": "create_week", "week": week})
    if err:
        return False, err
    if isinstance(data, dict) and data.get("ok"):
        name = data.get("name") or week
        _register_week(name)
        try:
            _refresh_week(name)
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        return True, name
    return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")


def _img_key(week, title):
    return "%s||%s" % (week, str(title or "").strip().lower())


def _store_images(week, title, screenshot_urls):
    """Download up to 6 screenshots, zip them, and stash the ZIP (base64) so the console can serve it."""
    if not screenshot_urls:
        return
    dest = tempfile.mkdtemp(prefix="gc_shots_")
    try:
        from services.steam_service import download_screenshots
        paths = download_screenshots(screenshot_urls, dest)
        if not paths:
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                zf.write(p, os.path.basename(p))
        from services import blob_store
        blob_store.blob_db["game_card_images"].update_one(   # off the main cluster, onto the blob store
            {"_id": _img_key(week, title)},
            {"$set": {"week": week, "title": title, "count": len(paths),
                      "zip_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
                      "at": datetime.utcnow().isoformat()}},
            upsert=True)
    except Exception as e:
        logger.warning("[GameCards] image store failed for %s: %s", title, e)
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _norm_date(s):
    """Normalise a release date to dd.mm.yyyy when parseable (Steam gives '6 Mar, 2026' etc.)."""
    s = (s or "").strip()
    if not s:
        return ""
    from datetime import datetime as _dt
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%d.%m.%Y", "%Y-%m-%d", "%b %Y", "%B %Y"):
        try:
            return _dt.strptime(s, fmt).strftime("%d.%m.%Y")
        except Exception as _e:
            logger.debug("ignored: %s", _e)
    return s   # unparseable (e.g. "Coming soon", "Q3 2026") -> leave as-is


def generate(week, links_text, platform, section, items=None):
    """Generate cards for the given week and append them to the week tab. Reuses the Slack
    generator's steam_service. Returns (ok, message).

    Either pasted Steam links (one platform/section for all), or `items`: [{url, platform, section,
    source}] - one entry per game, as picked in the console's "New this week" panel."""
    if items:
        jobs = [((it.get("url") or "").strip(), it.get("platform") or "Steam", it.get("section") or "Library",
                 it.get("source") or "") for it in items if (it.get("url") or "").strip()]
    else:
        urls = [u.strip() for u in (links_text or "").replace(",", "\n").split("\n") if u.strip()]
        jobs = [(u, platform or "Steam", section or "Library", "") for u in urls]
    if not jobs:
        return False, "no Steam links provided"
    try:
        from services.steam_service import fetch_game_data, scrape_steam_page, extract_game_info
        from utils.general_utils import extract_steam_app_id
    except Exception as e:
        return False, "generator not available: %s" % e
    from services.steam_service import norm_title
    week_doc = _db()["game_cards"].find_one({"week": week}) or {}
    existing = {norm_title(r.get("title")) for r in (week_doc.get("rows") or [])}
    cards, metas, done, skipped, dupes = [], [], 0, 0, []
    for url, platform, section, source in jobs:
        app_id = extract_steam_app_id(url)
        if not app_id:
            skipped += 1
            continue
        try:
            api_data = fetch_game_data(app_id)
            if not api_data:
                skipped += 1
                continue
            name = api_data.get("name", app_id)
            if norm_title(name) in existing:   # already has a card this week -> no duplicate row
                dupes.append(name)
                continue
            existing.add(norm_title(name))
            youtube_url, raw_related = scrape_steam_page(app_id, name, api_data)
            from services.steam_service import pick_related
            related = pick_related(app_id, name, raw_related)
            info = extract_game_info(app_id, platform, section, api_data, youtube_url, ", ".join(related))
            card = {"title": info.get("title", ""), "description": info.get("description", ""),
                    "platform": info.get("platform", ""), "description_url": info.get("description_url", ""),
                    "pegi": info.get("pegi_rating", ""), "release_date": _norm_date(info.get("release_date", "")),
                    "publisher": info.get("publisher", ""), "developer": info.get("developer", ""),
                    "app_link": info.get("app_link", ""), "youtube": info.get("youtube_link", ""),
                    "related_apps": info.get("related_apps", "")}
            cards.append(card)
            metas.append({"key": _img_key(week, card["title"]), "week": week, "title": card["title"],
                          "section": section, "platform": platform, "steam_app_id": str(app_id), "source": source})
            _store_images(week, card["title"], info.get("screenshot_urls") or [])
            done += 1
        except Exception as e:
            logger.warning("[GameCards] failed on %s: %s", url, e)
            skipped += 1
    dup_note = (", already in this week: %s" % ", ".join(dupes)) if dupes else ""
    if not cards:
        return False, "0 games processed (skipped %d%s)" % (skipped, dup_note)
    _post({"action": "create_week", "week": week})   # ensure the tab exists (ignores "already exists")
    data, err = _post({"action": "append", "week": week, "cards": cards})
    if err:
        return False, "append failed: %s" % err
    if not (isinstance(data, dict) and data.get("ok")):
        return False, "webapp error: %s" % (data.get("error") if isinstance(data, dict) else "bad response")
    _register_week(week)
    # Remember per card: its section (Install shows Description URL in the console) and which
    # "NEW! Game ... becoming alive" event it came from (so it leaves the suggestions list).
    for m in metas:
        _db()["game_card_meta"].update_one({"key": m["key"]}, {"$set": m}, upsert=True)
    try:
        _refresh_week(week)
    except Exception as _e:
        logger.debug("ignored: %s", _e)
    return True, "added %d card(s)%s%s" % (done, (", skipped %d" % skipped) if skipped else "", dup_note)


def _loop():
    time.sleep(25)
    while True:
        try:
            if configured():
                with _lock:
                    sync_once()
        except Exception as e:
            logger.error("[GameCards] sync loop error: %s", e)
        time.sleep(SYNC_INTERVAL)


def start_game_cards_sync():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="game-cards-sync").start()
    logger.info("[GameCards] sync scheduler started (every %ds, Apps Script Web App)", SYNC_INTERVAL)
