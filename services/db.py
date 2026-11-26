"""
Shared Mongo connections for the whole bot process.

A MongoClient is thread-safe and meant to be shared (each one has its own connection pool and
monitoring threads), so the process keeps ONE sync client per cluster URI and every module takes its
database from here.

Only pymongo + certifi: no bot imports, so this stays safe to import from anywhere (console_db too).

Async (Motor) clients are NOT shared here: a Motor client belongs to the event loop it is first used
on, and the bot runs several loops (Discord, Reddit, Telegram), so each loop keeps its own.
"""
import os
import threading
from pathlib import Path

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(Path(__file__).parent.parent / ".env")

_clients: dict = {}
_lock = threading.Lock()


def client(uri: str | None = None) -> MongoClient:
    """The process-wide MongoClient for `uri` (default MONGODB_URI). Created once, then reused."""
    uri = uri or os.getenv("MONGODB_URI")
    with _lock:
        c = _clients.get(uri)
        if c is None:
            kwargs = {"tlsCAFile": certifi.where()} if (uri or "").startswith("mongodb+srv://") else {}
            c = _clients[uri] = MongoClient(uri, **kwargs)
        return c


def main_db():
    """The bot's main database (MONGODB_URI / MONGODB_DATABASE)."""
    return client()[os.getenv("MONGODB_DATABASE", "reddit_posts")]


def snapshot_uri() -> str | None:
    return os.getenv("SNAPSHOT_MONGODB_URI") or os.getenv("MONGODB_URI")


def snapshot_db():
    """The separate snapshot database (SNAPSHOT_MONGODB_URI, falls back to the main cluster)."""
    return client(snapshot_uri())[os.getenv("SNAPSHOT_MONGODB_DATABASE", "reddit_snapshots")]
