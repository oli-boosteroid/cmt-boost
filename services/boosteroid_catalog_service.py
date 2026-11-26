"""
Boosteroid Catalog Sync Service
================================
Fetches the full Boosteroid public catalog, detects new games and keeps a local JSON file as the only persistent store.
On startup the file is read into two in-memory indexes so all lookups are
O(1) with zero I/O or API calls.

Initial seed (first run, no catalog file yet)
---------------------------------------------
Instead of hammering the API with 10+ requests at once, the catalog is
collected one page per hour via the scheduler:

  Startup (no catalog)  → background thread fetches page 1, saves progress
  Every hour (scheduler)→ seed_next_page() fetches the next page
  After ~11 hours       → catalog file is complete, memory is loaded

Weekly sync (catalog already exists)
--------------------------------------
  Every Friday 19:00 → sync_catalog()
    - Fetches ALL pages in one go (already have full catalog, just checking for updates)
    - Detects new games vs. in-memory _known_ids
    - Overwrites catalog file + reloads memory

Files on disk:
  data/boosteroid_catalog.json       - final catalog (loaded into memory on startup)
  data/boosteroid_catalog_seed.json  - temporary progress file during initial seed
                                       (deleted once seed is complete)
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from utils.general_utils import clean_title

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

BOOSTEROID_API_BASE = "https://cloud.boosteroid.com/api/v1/public"
CATALOG_FILE = Path("data/boosteroid_catalog.json")
SEED_FILE    = Path("data/boosteroid_catalog_seed.json")


# Guard: only one seed page runs at a time (scheduler fires every hour)
_seed_lock = threading.Lock()

# In-memory indexes - populated by load_catalog_from_file() and sync_catalog()
_known_ids:  set[int]        = set()
_name_index: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_cached_game(g: dict, platforms_map: dict[int, str]) -> dict:
    """Converts a raw API game object into the minimal dict stored in the cache."""
    platform_names = [
        platforms_map.get(pid, str(pid)) for pid in (g.get("platform") or [])
    ]
    return {
        "id":             g["id"],
        "name":           g.get("name") or "",
        "name_lower":     clean_title(g.get("name") or "").lower(),
        "collection":     g.get("collection"),
        "platform_names": platform_names,
        "icon":           g.get("icon") or "",
    }


def _fetch_page(page: int) -> tuple[list[dict], int]:
    """
    Fetches a single page from GET /applications.
    Returns (games_on_page, last_page).  Raises on error.
    """
    resp = requests.get(
        f"{BOOSTEROID_API_BASE}/applications",
        params={"page": page, "paginate": 200},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    games = body.get("data", [])
    last_page = body.get("meta", {}).get("last_page", 1)
    return games, last_page


def _fetch_platforms_map() -> dict[int, str]:
    """Returns {platform_id: platform_name}. Empty dict on failure."""
    try:
        resp = requests.get(
            f"{BOOSTEROID_API_BASE}/applications/platforms",
            timeout=10,
        )
        resp.raise_for_status()
        return {p["id"]: p["name"] for p in resp.json()}
    except Exception as e:
        logger.error(f"[BoosteroidCatalog] Failed to fetch platforms map: {e}")
        return {}


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _save_catalog_to_file(games: list[dict]) -> None:
    """
    Writes the final game list to disk atomically (temp → rename).
    Also rebuilds the in-memory indexes.
    """
    global _known_ids, _name_index

    payload = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "total":     len(games),
        "games":     games,
    }

    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=CATALOG_FILE.parent, suffix=".tmp")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, CATALOG_FILE)
    except Exception as e:
        logger.error(f"[BoosteroidCatalog] Failed to save catalog file: {e}")
        return

    try:
        _known_ids  = {g["id"] for g in games}
        _name_index = {g["name_lower"]: g for g in games if g.get("name_lower")}
    except Exception as e:
        logger.error(f"[BoosteroidCatalog] Failed to rebuild in-memory indexes: {e}")
        return
    logger.info(f"[BoosteroidCatalog] Catalog file saved and indexes rebuilt ({len(games)} games, {len(_name_index)} in name index).")


def load_catalog_from_file() -> None:
    """
    Reads ``data/boosteroid_catalog.json`` into the in-memory indexes.
    Call once at application startup - instant, no network I/O.
    Safe to call even if the file doesn't exist yet.
    """
    global _known_ids, _name_index

    if not CATALOG_FILE.exists():
        logger.info("[BoosteroidCatalog] No catalog file found — will seed gradually.")
        return

    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"[BoosteroidCatalog] Failed to read/parse catalog file: {e}")
        return

    games: list[dict] = data.get("games", [])
    # Build indexes defensively - skip any game record missing required keys
    new_known_ids:  set[int]        = set()
    new_name_index: dict[str, dict] = {}
    skipped = 0
    for g in games:
        gid   = g.get("id")
        gname = g.get("name_lower")
        if gid is None or not gname:
            skipped += 1
            continue
        new_known_ids.add(gid)
        new_name_index[gname] = g

    _known_ids  = new_known_ids
    _name_index = new_name_index
    logger.info(
        f"[BoosteroidCatalog] Loaded {len(_known_ids)} games from catalog file, "
        f"{len(_name_index)} in name index "
        f"(synced_at={data.get('synced_at', 'unknown')}"
        + (f", skipped {skipped} malformed records" if skipped else "") + ")."
    )


# ---------------------------------------------------------------------------
# Gradual initial seed - one page per hour
# ---------------------------------------------------------------------------

def seed_next_page() -> None:
    """
    Fetches exactly ONE page of the Boosteroid catalog per call.

    Intended to run every hour via the scheduler during the initial seed.
    Once the catalog file already exists (normal operation), returns immediately.

    Progress is tracked in ``data/boosteroid_catalog_seed.json``:
      {"next_page": 3, "last_page": 11, "games": [...games collected so far...]}

    When the last page is collected:
      - Resolves platform names
      - Saves the final catalog file
      - Reloads in-memory indexes
      - Deletes the seed progress file
    """
    # Skip if we already have a complete catalog
    if CATALOG_FILE.exists():
        return

    if not _seed_lock.acquire(blocking=False):
        logger.debug("[BoosteroidCatalog] Seed already in progress, skipping.")
        return

    try:
        # Load existing progress or start fresh
        if SEED_FILE.exists():
            try:
                with open(SEED_FILE, encoding="utf-8") as f:
                    progress = json.load(f)
                next_page = progress.get("next_page", 1)
                last_page = progress.get("last_page")
                collected = progress.get("games", [])
            except Exception as e:
                logger.error(f"[BoosteroidCatalog] Corrupt seed file, restarting: {e}")
                next_page, last_page, collected = 1, None, []
        else:
            next_page, last_page, collected = 1, None, []

        # Fetch one page
        try:
            page_games, last_page = _fetch_page(next_page)
        except Exception as e:
            logger.error(
                f"[BoosteroidCatalog] Seed page {next_page} fetch failed: {e} — will retry next hour."
            )
            return

        if not page_games:
            # API returned an empty page - log and retry next hour (don't advance)
            logger.warning(
                f"[BoosteroidCatalog] Page {next_page} returned 0 games — will retry next hour."
            )
            return

        collected.extend(page_games)
        logger.info(
            f"[BoosteroidCatalog] Seed progress: page {next_page}/{last_page} "
            f"— {len(collected)} games collected so far."
        )

        if next_page >= last_page:
            # All pages collected - build and save the final catalog
            if not collected:
                logger.error(
                    "[BoosteroidCatalog] Seed finished but collected 0 games — aborting to avoid empty catalog."
                )
                return
            platforms_map = _fetch_platforms_map()
            cached_games = [_build_cached_game(g, platforms_map) for g in collected]
            _save_catalog_to_file(cached_games)
            SEED_FILE.unlink(missing_ok=True)
            logger.info(
                f"[BoosteroidCatalog] Initial seed complete — "
                f"{len(cached_games)} games loaded into memory."
            )
        else:
            # Save progress and wait for next hour
            SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
            progress_data = {
                "next_page": next_page + 1,
                "last_page": last_page,
                "games":     collected,
            }
            try:
                tmp_fd, tmp_path = tempfile.mkstemp(dir=SEED_FILE.parent, suffix=".tmp")
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(progress_data, f, ensure_ascii=False)
                os.chmod(tmp_path, 0o644)
                os.replace(tmp_path, SEED_FILE)
            except Exception as e:
                logger.error(f"[BoosteroidCatalog] Failed to save seed progress: {e}")
    finally:
        _seed_lock.release()


# ---------------------------------------------------------------------------
# Weekly sync - new-games-only using orderBy=new
# ---------------------------------------------------------------------------

def _fetch_new_applications() -> list[dict]:
    """
    Fetches only games not yet in _known_ids, using orderBy=new (newest IDs first).

    Pages through the API one by one. As soon as an entire page contains only
    already-known IDs, stops - everything beyond that point is old.

    In a typical week this means 1–2 requests instead of ~10.
    """
    new_games: list[dict] = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f"{BOOSTEROID_API_BASE}/applications",
                params={"page": page, "paginate": 200, "orderBy": "new"},
                timeout=20,
            )
            resp.raise_for_status()
            body = resp.json()
            page_games = body.get("data", [])
            last_page  = body.get("meta", {}).get("last_page", 1)
        except Exception as e:
            logger.error(f"[BoosteroidCatalog] Sync fetch page {page} failed: {e}")
            break

        if not page_games:
            break

        truly_new = [g for g in page_games if g["id"] not in _known_ids]
        new_games.extend(truly_new)

        logger.debug(
            f"[BoosteroidCatalog] Sync page {page}/{last_page}: "
            f"{len(truly_new)} new / {len(page_games)} total on page"
        )

        # Once a page has at least one known ID, everything after is old - stop.
        if len(truly_new) < len(page_games):
            break
        if page >= last_page:
            break
        page += 1

    return new_games


def sync_catalog() -> dict:
    """
    Weekly sync - called by the Friday 19:00 scheduler.
    Skips silently if the initial seed is still in progress.

    Uses orderBy=new so only the first 1–2 pages need to be fetched in most weeks.
    New games are appended to the catalog file; the full catalog is NOT re-fetched.

    Returns {"new": int, "total": int}.
    """
    if not CATALOG_FILE.exists():
        logger.info(
            "[BoosteroidCatalog] Weekly sync skipped — initial seed still in progress."
        )
        return {"new": 0, "total": 0}

    logger.info("[BoosteroidCatalog] Starting weekly sync (orderBy=new)...")

    new_games = _fetch_new_applications()

    if not new_games:
        logger.info("[BoosteroidCatalog] No new games found this week.")
        return {"new": 0, "total": len(_known_ids)}

    logger.info(
        f"[BoosteroidCatalog] {len(new_games)} new game(s) detected."
    )

    platforms_map = _fetch_platforms_map()

    # Enrich with platform names before posting
    for g in new_games:
        g["platform_names"] = [
            platforms_map.get(pid, str(pid)) for pid in (g.get("platform") or [])
        ]

    # Append new games to the existing catalog file
    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            catalog = json.load(f)
        existing_games: list[dict] = catalog.get("games", [])
    except Exception as e:
        logger.error(f"[BoosteroidCatalog] Could not read catalog for append: {e}")
        existing_games = []

    cached_new = [_build_cached_game(g, platforms_map) for g in new_games]
    _save_catalog_to_file(existing_games + cached_new)

    total = len(_known_ids)
    logger.info(
        f"[BoosteroidCatalog] Sync complete — new: {len(new_games)}, total: {total}."
    )
    return {"new": len(new_games), "total": total}


# ---------------------------------------------------------------------------
# Catalog lookup
# ---------------------------------------------------------------------------

def find_in_catalog(game_name: str) -> dict | None:
    """
    Instant in-memory lookup - no I/O, no API call.
    Returns the cached game dict if found, None otherwise.
    """
    clean = clean_title(game_name).lower()
    if not clean:
        return None
    result = _name_index.get(clean)
    if result is None and not _name_index:
        logger.error(f"[BoosteroidCatalog] Catalog index is EMPTY — game not checked: '{clean}'")
    if result is None and _name_index:
        # Loose match: ignore punctuation and "(Xbox)"-style suffixes, so "ARK Survival Ascended"
        # or "Fallout 2" still find "ARK: Survival Ascended" / "Fallout 2 (Epic)".
        from services.steam_service import norm_title
        global _norm_index, _norm_src
        if _norm_src is not _name_index:
            _norm_index = {}
            for k, g in _name_index.items():
                _norm_index.setdefault(norm_title(k), g)
            _norm_src = _name_index
        result = _norm_index.get(norm_title(clean))
    return result


_norm_index: dict = {}
_norm_src = None


def catalog_games() -> list:
    """All cached catalog games (dicts with id / name / ...)."""
    return list(_name_index.values())


def catalog_loaded() -> bool:
    """Returns True if the Boosteroid catalog has been loaded into memory."""
    return bool(_name_index)


# ---------------------------------------------------------------------------
# Event-driven updates (Mattermost admin-service events, services/mattermost_events.py)
# ---------------------------------------------------------------------------
# A game added on Boosteroid ("change status from disabled to: ENABLED") is fetched from the public
# API by its ID and written into the catalog; a removed game ("... to: DISABLED") is dropped. This
# keeps the catalog current within seconds; the weekly sync is only a safety net.

_catalog_lock = threading.Lock()
_platforms_cache: dict[int, str] = {}


def _platforms() -> dict[int, str]:
    global _platforms_cache
    if not _platforms_cache:
        _platforms_cache = _fetch_platforms_map()
    return _platforms_cache


def _read_catalog_games() -> list[dict] | None:
    """Current catalog list from disk, or None when there is no catalog file yet (initial seed running)."""
    if not CATALOG_FILE.exists():
        return None
    with open(CATALOG_FILE, encoding="utf-8") as f:
        return json.load(f).get("games", [])


def upsert_game(game_id: int) -> bool:
    """Fetch one game from the public API and add it to the catalog (or refresh it). True if written."""
    try:
        resp = requests.get(f"{BOOSTEROID_API_BASE}/applications/{int(game_id)}", timeout=15)
        if resp.status_code == 404:
            # Not visible yet: the public API lists only enabled games and can lag a little behind the
            # "enabled" event. Try again later instead of losing the game.
            _retry_later(game_id)
            return False
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        if not data.get("id"):
            return False
        cached = _build_cached_game(data, _platforms())
    except Exception as e:
        logger.error(f"[BoosteroidCatalog] Fetching game {game_id} failed: {e}")
        return False
    with _catalog_lock:
        games = _read_catalog_games()
        if games is None:
            logger.info(f"[BoosteroidCatalog] No catalog file yet - '{cached['name']}' will come with the seed")
            return False
        games = [g for g in games if g.get("id") != cached["id"]] + [cached]
        _save_catalog_to_file(games)
    logger.info(f"[BoosteroidCatalog] Added '{cached['name']}' ({cached['id']}) from a Mattermost event")
    return True


_RETRY_DELAYS = (120, 600, 1800)   # seconds: 2, 10, 30 minutes after the event
_retrying: set[int] = set()


def _retry_later(game_id: int) -> None:
    gid = int(game_id)
    if gid in _retrying:
        return
    _retrying.add(gid)

    def run():
        import time as _time
        try:
            for delay in _RETRY_DELAYS:
                _time.sleep(delay)
                try:
                    resp = requests.get(f"{BOOSTEROID_API_BASE}/applications/{gid}", timeout=15)
                except Exception:
                    continue
                if resp.status_code == 200:
                    _retrying.discard(gid)
                    upsert_game(gid)
                    return
            logger.warning(f"[BoosteroidCatalog] Game {gid} still not in the public API after retries - not added")
        finally:
            _retrying.discard(gid)

    logger.info(f"[BoosteroidCatalog] Game {gid} not in the public API yet - will retry")
    threading.Thread(target=run, daemon=True, name=f"catalog-retry-{gid}").start()


def remove_game(game_id: int) -> bool:
    """Drop a game from the catalog (it was removed from the service). True if it was there."""
    with _catalog_lock:
        games = _read_catalog_games()
        if games is None:
            return False
        kept = [g for g in games if g.get("id") != int(game_id)]
        if len(kept) == len(games):
            return False
        _save_catalog_to_file(kept)
    logger.info(f"[BoosteroidCatalog] Removed game {game_id} from the catalog (Mattermost event)")
    return True
