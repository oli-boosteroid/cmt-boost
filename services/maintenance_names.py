"""Maintenance names: the "[Brazil]" rule shared by the Mattermost maintenance handler and bot start-up."""
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# "[Brazil]" anywhere in a game name (any case, inner spaces, full-width brackets) marks a Brazil-only
# maintenance. Matched loosely because a strict endswith("[brazil]") missed real names in production.
_BRAZIL_TAG = re.compile(r"\s*[\[(（［]\s*brazil\s*[\])）］]\s*", re.IGNORECASE)


def split_brazil(entity):
    """(clean_name, is_brazil): the game name without its [Brazil] tag, and whether it had one."""
    raw = unicodedata.normalize("NFKC", str(entity or "")).replace(" ", " ")
    if not _BRAZIL_TAG.search(raw):
        return str(entity or "").strip(), False
    return re.sub(r"\s{2,}", " ", _BRAZIL_TAG.sub(" ", raw)).strip(), True


def migrate_brazil_maintenances():
    """One-time clean-up (run at bot start): active GAME maintenances whose name still carries a
    [Brazil] tag may still be stored as regular maintenances. Move each one to the Brazil list
    under its clean name, keeping its start time and Slack message. Returns how many were moved."""
    from services.mongo_db_service import sync_active_maintenances_collection as col
    moved = 0
    for doc in list(col.find({"type": "game"})):
        clean, is_brazil = split_brazil(doc.get("entity"))
        if not is_brazil:
            continue
        if col.find_one({"type": "brazil", "entity": clean}):
            col.delete_one({"_id": doc["_id"]})          # already tracked as Brazil: drop the duplicate
        else:
            col.update_one({"_id": doc["_id"]}, {"$set": {"type": "brazil", "entity": clean}})
        moved += 1
        logger.info(f"[Maintenance] moved '{doc.get('entity')}' to Brazil maintenances as '{clean}'")
    return moved
