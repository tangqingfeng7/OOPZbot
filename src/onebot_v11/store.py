from __future__ import annotations

import json
import random
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.json_utils import compact_json


@dataclass(frozen=True, slots=True)
class OneBotId:
    string: str
    number: int
    source: str


@dataclass(frozen=True, slots=True)
class MessageRecord:
    ob_message_id: str
    oopz_message_id: str
    detail_type: str
    area: str = ""
    channel: str = ""
    target: str = ""
    user_id: str = ""
    created_at: int = 0
    raw: dict[str, Any] | None = None


class OneBotStore:
    """SQLite store for v11 numeric IDs and message mapping."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS onebot_v11_id_map (
                    source TEXT NOT NULL UNIQUE,
                    number INTEGER NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS onebot_v11_messages (
                    ob_message_id TEXT NOT NULL PRIMARY KEY,
                    oopz_message_id TEXT NOT NULL,
                    detail_type TEXT NOT NULL,
                    area TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    target TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    raw TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.commit()

    def create_id(self, source: str | int) -> OneBotId:
        if isinstance(source, int):
            return OneBotId(str(source), source, str(source))
        source_text = str(source or "")
        if not source_text:
            raise ValueError("source is required")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT source, number FROM onebot_v11_id_map WHERE source=?",
                (source_text,),
            ).fetchone()
            if row is not None:
                return OneBotId(str(row["source"]), int(row["number"]), str(row["source"]))
            number = self._new_unique_number(conn)
            conn.execute(
                "INSERT INTO onebot_v11_id_map(source, number, created_at) VALUES (?, ?, ?)",
                (source_text, number, int(time.time())),
            )
            conn.commit()
            return OneBotId(source_text, number, source_text)

    def createId(self, source: str | int) -> OneBotId:
        return self.create_id(source)

    def resolve_id(self, number: str | int) -> OneBotId:
        try:
            numeric = int(number)
        except (TypeError, ValueError):
            raise ValueError(f"invalid onebot id: {number!r}") from None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT source, number FROM onebot_v11_id_map WHERE number=?",
                (numeric,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown onebot id: {numeric}")
        return OneBotId(str(row["source"]), int(row["number"]), str(row["source"]))

    def try_resolve_id(self, number: str | int) -> OneBotId | None:
        try:
            return self.resolve_id(number)
        except (KeyError, ValueError):
            return None

    def save_message(self, record: MessageRecord) -> None:
        raw = compact_json(record.raw or {})
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO onebot_v11_messages (
                    ob_message_id, oopz_message_id, detail_type, area, channel,
                    target, user_id, created_at, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ob_message_id) DO UPDATE SET
                    oopz_message_id=excluded.oopz_message_id,
                    detail_type=excluded.detail_type,
                    area=excluded.area,
                    channel=excluded.channel,
                    target=excluded.target,
                    user_id=excluded.user_id,
                    created_at=excluded.created_at,
                    raw=excluded.raw
                """,
                (
                    str(record.ob_message_id),
                    str(record.oopz_message_id),
                    str(record.detail_type),
                    str(record.area or ""),
                    str(record.channel or ""),
                    str(record.target or ""),
                    str(record.user_id or ""),
                    int(record.created_at or time.time()),
                    raw,
                ),
            )
            conn.commit()

    def get_message(self, ob_message_id: str | int) -> MessageRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM onebot_v11_messages WHERE ob_message_id=?",
                (str(ob_message_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row["raw"] or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
        return MessageRecord(
            ob_message_id=str(row["ob_message_id"]),
            oopz_message_id=str(row["oopz_message_id"]),
            detail_type=str(row["detail_type"]),
            area=str(row["area"] or ""),
            channel=str(row["channel"] or ""),
            target=str(row["target"] or ""),
            user_id=str(row["user_id"] or ""),
            created_at=int(row["created_at"] or 0),
            raw=raw,
        )

    def cleanup_messages(self, older_than_seconds: int) -> int:
        cutoff = int(time.time()) - int(older_than_seconds)
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "DELETE FROM onebot_v11_messages WHERE created_at < ?",
                (cutoff,),
            )
            conn.commit()
            return cursor.rowcount

    @staticmethod
    def _new_unique_number(conn: sqlite3.Connection) -> int:
        for _ in range(100):
            number = random.randint(10_000_000, 2_147_483_647)
            row = conn.execute(
                "SELECT 1 FROM onebot_v11_id_map WHERE number=?",
                (number,),
            ).fetchone()
            if row is None:
                return number
        raise RuntimeError("failed to allocate onebot id")


def make_user_source(uid: str) -> str:
    return f"user:{uid}"


def parse_user_source(source: str) -> str:
    return source.removeprefix("user:")


def make_self_source(uid: str) -> str:
    return f"self:{uid}"


def make_group_source(*, area: str, channel: str) -> str:
    return f"group:{area}:{channel}"


def parse_group_source(source: str) -> tuple[str, str]:
    prefix = "group:"
    if not source.startswith(prefix):
        raise ValueError(f"invalid group source: {source!r}")
    rest = source.removeprefix(prefix)
    area, sep, channel = rest.partition(":")
    if not sep or not area or not channel:
        raise ValueError(f"invalid group source: {source!r}")
    return area, channel


def make_message_source(
    *,
    message_id: str,
    area: str = "",
    channel: str = "",
    target: str = "",
) -> str:
    return f"message:{area}:{channel}:{target}:{message_id}"


def parse_message_source(source: str) -> tuple[str, str, str, str]:
    prefix = "message:"
    if not source.startswith(prefix):
        raise ValueError(f"invalid message source: {source!r}")
    parts = source.removeprefix(prefix).split(":", 3)
    if len(parts) != 4 or not parts[3]:
        raise ValueError(f"invalid message source: {source!r}")
    return parts[0], parts[1], parts[2], parts[3]
