"""
Reddit mod notes with the user's Boosteroid User ID, written through the Devvit app.

Every 10 minutes: users_emails records that tie a Reddit username to a User ID (cloud_user_id) and whose
note is not written yet (reddit_note_uid differs) get a `user_note` command: the app adds the note
"User ID <n>" on the user, removes an older "User ID ..." note if the ID changed and skips it if the same
note is already there. Only the User ID is written, never an email.

This replaces the old email notes (written through the Reddit API before the block): the user's old email
note (reddit_mod_notes.email_note_id) is deleted by the same command.

Off when the Devvit relay is not configured or REDDIT_USER_ID_NOTES=0.
"""
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

BATCH = 5   # the Devvit app runs at most 5 notes a minute; keep the relay channel quiet
PREFIX = "User ID "


def sync_reddit_user_id_notes() -> int:
    if os.getenv("REDDIT_USER_ID_NOTES") == "0":
        return 0
    from reddit_notifications.devvit_inbox import relay_configured, send_reddit_command
    if not relay_configured():
        return 0
    from services.mongo_db_service import sync_db
    col = sync_db["users_emails"]
    query = {
        "reddit_username": {"$nin": [None, "", "[deleted]"]},
        "cloud_user_id": {"$nin": [None, ""]},
        "$expr": {"$ne": [{"$ifNull": ["$reddit_note_uid", ""]}, {"$toString": "$cloud_user_id"}]},
    }
    sent, done_names = 0, set()
    for d in col.find(query, {"reddit_username": 1, "cloud_user_id": 1}).limit(BATCH * 4):
        name, uid = str(d["reddit_username"]), str(d["cloud_user_id"])
        key = name.lower()
        if key not in done_names:
            if sent >= BATCH:
                break
            old = sync_db["reddit_mod_notes"].find_one(
                {"reddit_user": {"$regex": "^%s$" % re.escape(name), "$options": "i"},
                 "email_note_id": {"$nin": [None, ""]}}, {"email_note_id": 1})
            extra = {"deleteNoteIds": [old["email_note_id"]]} if old else {}
            ok, err = send_reddit_command("note:%s" % d["_id"], "user_note", user=name,
                                          note=PREFIX + uid, replacePrefix=PREFIX, **extra)
            if not ok:
                logger.warning("[RedditNotes] could not queue the User ID note for u/%s: %s", name, err)
                break
            if old:
                sync_db["reddit_mod_notes"].update_one({"_id": old["_id"]}, {"$unset": {"email_note_id": ""}})
            done_names.add(key)
            sent += 1
        col.update_one({"_id": d["_id"]}, {"$set": {"reddit_note_uid": uid, "reddit_note_at": datetime.utcnow()}})
    if sent:
        logger.info("[RedditNotes] queued %d User ID note(s) for the Devvit app", sent)
    return sent
