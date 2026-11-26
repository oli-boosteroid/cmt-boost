"""
Report collection sizes and (optionally) prune clearly-disposable OLD rows, to relieve the main Atlas
cluster's RAM working set.

SAFE BY DEFAULT: a plain run only REPORTS - it counts documents and storage size per collection and
prints what a prune WOULD remove. It deletes nothing. Deletion happens only when you pass the explicit
flag for a specific target, and even then only rows past the age threshold. Emails (users_emails) are
NEVER a prune target - they are durable user contact data.

    python additional_scripts/prune_old_rows.py                       # report only (sizes + would-remove)
    python additional_scripts/prune_old_rows.py --closed-discord 90   # delete Done/closed Discord tickets older than 90d
    python additional_scripts/prune_old_rows.py --resolved-posts 120  # delete replied/ignored reddit posts older than 120d
    python additional_scripts/prune_old_rows.py --done-jira 120       # delete Done/closed Jira cache rows older than 120d

Add --apply to actually delete; without it each --flag still only reports its would-remove count.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(Path(__file__).parent.parent / ".env")

APPLY = "--apply" in sys.argv

main_db = MongoClient(os.getenv("MONGODB_URI"))[os.getenv("MONGODB_DATABASE", "reddit_posts")]
snap_db = MongoClient(os.getenv("SNAPSHOT_MONGODB_URI") or os.getenv("MONGODB_URI"))[
    os.getenv("SNAPSHOT_MONGODB_DATABASE", "reddit_snapshots")]

# Collections worth watching for size, per cluster.
_REPORT = {
    "main": ["discord_tickets", "jira_tickets", "telegram_messages", "discord_mentions",
             "users_emails", "game_card_images", "telegram_images", "console_reminders_state"],
    "snapshot": ["reddit_posts_snapshot", "reddit_modmail_snapshot"],
}


def _arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                return default
    return default


def _size(db, coll):
    try:
        s = db.command("collstats", coll)
        return s.get("count", 0), s.get("size", 0), s.get("totalIndexSize", 0)
    except Exception:
        return 0, 0, 0


def report():
    print("Collection sizes (storage that competes for the cluster's RAM cache):")
    for cluster, colls in _REPORT.items():
        db = main_db if cluster == "main" else snap_db
        print("\n  [%s]" % cluster)
        for c in colls:
            n, size, idx = _size(db, c)
            print("    %-26s %8d docs  %8.1f MB data  %6.1f MB idx" % (c, n, size / 1048576.0, idx / 1048576.0))


def _iso_cutoff(days):
    return (datetime.utcnow() - timedelta(days=days)).isoformat()


def prune_closed_discord(days):
    # Discord tickets store created_at as a datetime; use it as a coarse age proxy for closed tickets.
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = {"status": "closed", "created_at": {"$lt": cutoff}}
    n = main_db["discord_tickets"].count_documents(q)
    print("\n[discord_tickets] closed & older than %dd: %d" % (days, n))
    if APPLY and n:
        print("  deleting %d ..." % main_db["discord_tickets"].delete_many(q).deleted_count)


def prune_resolved_posts(days):
    cutoff = _iso_cutoff(days)
    q = {"$and": [{"$or": [{"has_reply": True}, {"ignored": True}]},
                  {"$or": [{"last_comment_at": {"$lt": cutoff}}, {"created": {"$lt": cutoff}}]}]}
    n = snap_db["reddit_posts_snapshot"].count_documents(q)
    print("\n[reddit_posts_snapshot] replied/ignored & older than %dd: %d" % (days, n))
    if APPLY and n:
        print("  deleting %d ..." % snap_db["reddit_posts_snapshot"].delete_many(q).deleted_count)


def prune_done_jira(days):
    cutoff = _iso_cutoff(days)
    q = {"status": {"$in": ["Done", "Closed", "Resolved"]}, "status_since": {"$lt": cutoff}}
    n = main_db["jira_tickets"].count_documents(q)
    print("\n[jira_tickets] Done/Closed & older than %dd: %d" % (days, n))
    if APPLY and n:
        print("  deleting %d ..." % main_db["jira_tickets"].delete_many(q).deleted_count)


def main():
    print("Prune - %s\n" % ("APPLY (will delete where flags given)" if APPLY else "DRY RUN (report only)"))
    report()
    cd = _arg("--closed-discord")
    rp = _arg("--resolved-posts")
    dj = _arg("--done-jira")
    if cd:
        prune_closed_discord(cd)
    if rp:
        prune_resolved_posts(rp)
    if dj:
        prune_done_jira(dj)
    if not any([cd, rp, dj]):
        print("\nNo prune target flags given - reported sizes only. See the file header for prune flags.")
    elif not APPLY:
        print("\nDRY RUN: add --apply to actually delete the counts shown above.")


if __name__ == "__main__":
    main()
