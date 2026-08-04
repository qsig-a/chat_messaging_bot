"""SQLite cache - not a source of truth.

Two tables: `seen` de-duplicates retried webhooks, `outbound` maps SignalWire
SIDs to the chat message whose reaction a delivery-status callback updates.
Deleting the file loses only in-flight reaction updates.
"""

from __future__ import annotations

import sqlite3
import time


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS seen (sid TEXT PRIMARY KEY, ts INTEGER)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbound ("
            " sid TEXT PRIMARY KEY, channel_id TEXT, message_id TEXT, ts INTEGER)"
        )
        self._db.commit()

    def already_seen(self, sid: str) -> bool:
        """Insert-or-report. SignalWire retries webhooks; this makes them idempotent."""
        if not sid:
            return False
        try:
            self._db.execute(
                "INSERT INTO seen (sid, ts) VALUES (?, ?)", (sid, int(time.time()))
            )
            self._db.commit()
            return False
        except sqlite3.IntegrityError:
            return True

    def remember_outbound(self, sid: str, channel_id: str, message_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO outbound (sid, channel_id, message_id, ts)"
            " VALUES (?,?,?,?)",
            (sid, str(channel_id), str(message_id), int(time.time())),
        )
        self._db.commit()

    def forget_outbound(self, sid: str) -> None:
        """Drop a recorded SID so a late status callback cannot revive it."""
        self._db.execute("DELETE FROM outbound WHERE sid = ?", (sid,))
        self._db.commit()

    def lookup_outbound(self, sid: str) -> tuple[str, str] | None:
        row = self._db.execute(
            "SELECT channel_id, message_id FROM outbound WHERE sid = ?", (sid,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def prune(self, days: int = 30) -> None:
        cutoff = int(time.time()) - days * 86400
        self._db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
        self._db.execute("DELETE FROM outbound WHERE ts < ?", (cutoff,))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
