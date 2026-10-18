"""Internal Watcher DB (SQLite): watches, state rows, and incidents."""

from project.internal.db import connect, db_path
from project.internal.store import InternalStore, correlation_key

__all__ = [
    "InternalStore",
    "connect",
    "correlation_key",
    "db_path",
]
