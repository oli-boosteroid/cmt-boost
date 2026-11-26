"""
Compare a moved database with its source: document counts and indexes per collection. Read-only.

    python additional_scripts/compare_counts.py
    python additional_scripts/compare_counts.py --target-only

Source: SRC_MONGODB_URI / SRC_MONGODB_DATABASE, SRC_SNAPSHOT_URI / SRC_SNAPSHOT_DATABASE.
Target: MONGODB_URI / MONGODB_DATABASE, SNAPSHOT_MONGODB_URI / SNAPSHOT_MONGODB_DATABASE.
Exits with 1 when a count differs or an index is missing on the target.
"""
import os
import sys

import certifi
from pymongo import MongoClient


def _client(uri):
    kw = {"tlsCAFile": certifi.where()} if (uri or "").startswith("mongodb+srv://") else {}
    return MongoClient(uri, serverSelectionTimeoutMS=15000, **kw)


def _summary(db):
    out = {}
    for name in sorted(db.list_collection_names()):
        if name.startswith("system."):
            continue
        col = db[name]
        idx = sorted(i["name"] for i in col.list_indexes())
        out[name] = (col.estimated_document_count(), idx)
    return out


def main():
    target_only = "--target-only" in sys.argv
    pairs = [
        ("main", os.getenv("SRC_MONGODB_URI"), os.getenv("SRC_MONGODB_DATABASE", "reddit_posts"),
         os.getenv("MONGODB_URI"), os.getenv("MONGODB_DATABASE", "reddit_posts")),
        ("snapshot", os.getenv("SRC_SNAPSHOT_URI"), os.getenv("SRC_SNAPSHOT_DATABASE", "reddit_snapshots"),
         os.getenv("SNAPSHOT_MONGODB_URI") or os.getenv("MONGODB_URI"),
         os.getenv("SNAPSHOT_MONGODB_DATABASE", "reddit_snapshots")),
    ]
    bad = 0
    for label, src_uri, src_db, dst_uri, dst_db in pairs:
        if not dst_uri:
            print("[%s] target URI not set, skipped" % label)
            continue
        dst = _summary(_client(dst_uri)[dst_db])
        src = {} if target_only or not src_uri else _summary(_client(src_uri)[src_db])
        print("\n== %s: %s -> %s" % (label, src_db if src else "-", dst_db))
        print("%-34s %12s %12s  %s" % ("collection", "source", "target", "note"))
        for name in sorted(set(src) | set(dst)):
            s, d = src.get(name), dst.get(name)
            note = ""
            if src and d is None:
                note, bad = "MISSING on target", bad + 1
            elif src and s is None:
                note = "only on target"
            elif src and s[0] != d[0]:
                note, bad = "count differs (%+d)" % (d[0] - s[0]), bad + 1
            elif src and set(s[1]) - set(d[1]):
                note, bad = "missing index: %s" % ", ".join(sorted(set(s[1]) - set(d[1]))), bad + 1
            print("%-34s %12s %12s  %s" % (name, s[0] if s else "-", d[0] if d else "-", note))
    print("\n%s" % ("OK: everything matches" if not bad else "%d difference(s) - check them above" % bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
