from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

from core.database import DB_PATH, cn_now


class DeltaForceStore:
    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(DB_PATH, timeout=5)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def setup(self) -> None:
        async with self._connect() as connection:
            await connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS delta_force_active_token (
                    user_id TEXT PRIMARY KEY,
                    account_group TEXT NOT NULL DEFAULT 'qq_wechat',
                    framework_token TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delta_force_place_push (
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    area_id TEXT NOT NULL,
                    last_snapshot TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, channel_id, area_id)
                );
                CREATE TABLE IF NOT EXISTS delta_force_daily_keyword_push (
                    channel_id TEXT NOT NULL,
                    area_id TEXT NOT NULL,
                    last_push_date TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, area_id)
                );
                """
            )
            await connection.commit()

    async def get_active_token(self, user_id: str, group: str = "qq_wechat") -> str | None:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT framework_token FROM delta_force_active_token WHERE user_id=? AND account_group=?",
                (str(user_id), str(group)),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            token = row["framework_token"]
            return str(token) if token else None

    async def set_active_token(self, user_id: str, token: str, group: str = "qq_wechat") -> None:
        async with self._connect() as connection:
            await connection.execute(
                """
                INSERT INTO delta_force_active_token (user_id, account_group, framework_token, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    account_group=excluded.account_group,
                    framework_token=excluded.framework_token,
                    updated_at=excluded.updated_at
                """,
                (str(user_id), str(group), str(token), cn_now()),
            )
            await connection.commit()

    async def clear_active_token(self, user_id: str) -> None:
        async with self._connect() as connection:
            await connection.execute("DELETE FROM delta_force_active_token WHERE user_id=?", (str(user_id),))
            await connection.commit()

    async def choose_active_token(
        self,
        user_id: str,
        accounts: list[dict],
        group: str = "qq_wechat",
    ) -> str | None:
        current = await self.get_active_token(user_id, group)
        valid_tokens: list[str] = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            token = str(account.get("frameworkToken") or "").strip()
            if not token:
                continue
            if current and current == token:
                await self.set_active_token(user_id, current, group)
                return current
            if account.get("isValid"):
                valid_tokens.append(token)

        if not valid_tokens:
            await self.clear_active_token(user_id)
            return None

        await self.set_active_token(user_id, valid_tokens[0], group)
        return valid_tokens[0]

    async def upsert_place_push_subscription(
        self,
        user_id: str,
        channel_id: str,
        area_id: str,
        last_snapshot: list[dict] | None = None,
    ) -> None:
        snapshot_text = json.dumps(last_snapshot, ensure_ascii=False) if isinstance(last_snapshot, list) else "[]"
        async with self._connect() as connection:
            await connection.execute(
                """
                INSERT INTO delta_force_place_push (user_id, channel_id, area_id, last_snapshot, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, channel_id, area_id) DO UPDATE SET
                    last_snapshot=excluded.last_snapshot,
                    updated_at=excluded.updated_at
                """,
                (str(user_id), str(channel_id), str(area_id), snapshot_text, cn_now()),
            )
            await connection.commit()

    async def remove_place_push_subscription(self, user_id: str, channel_id: str, area_id: str) -> None:
        async with self._connect() as connection:
            await connection.execute(
                "DELETE FROM delta_force_place_push WHERE user_id=? AND channel_id=? AND area_id=?",
                (str(user_id), str(channel_id), str(area_id)),
            )
            await connection.commit()

    async def has_place_push_subscription(self, user_id: str, channel_id: str, area_id: str) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT 1 FROM delta_force_place_push WHERE user_id=? AND channel_id=? AND area_id=?",
                (str(user_id), str(channel_id), str(area_id)),
            )
            return await cursor.fetchone() is not None

    async def any_place_push_subscriptions(self) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute("SELECT 1 FROM delta_force_place_push LIMIT 1")
            return await cursor.fetchone() is not None

    async def list_place_push_subscriptions(self) -> list[dict]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT user_id, channel_id, area_id, last_snapshot, updated_at FROM delta_force_place_push"
            )
            rows = await cursor.fetchall()
        results: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["last_snapshot"] = json.loads(item.get("last_snapshot") or "[]")
            except (json.JSONDecodeError, TypeError, ValueError):
                item["last_snapshot"] = []
            results.append(item)
        return results

    async def update_place_push_snapshot(
        self,
        user_id: str,
        channel_id: str,
        area_id: str,
        snapshot: list[dict] | None,
    ) -> None:
        snapshot_text = json.dumps(snapshot, ensure_ascii=False) if isinstance(snapshot, list) else "[]"
        async with self._connect() as connection:
            await connection.execute(
                """
                UPDATE delta_force_place_push
                SET last_snapshot=?, updated_at=?
                WHERE user_id=? AND channel_id=? AND area_id=?
                """,
                (snapshot_text, cn_now(), str(user_id), str(channel_id), str(area_id)),
            )
            await connection.commit()

    async def upsert_daily_keyword_push_subscription(
        self,
        channel_id: str,
        area_id: str,
        last_push_date: str = "",
    ) -> None:
        async with self._connect() as connection:
            await connection.execute(
                """
                INSERT INTO delta_force_daily_keyword_push (channel_id, area_id, last_push_date, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id, area_id) DO UPDATE SET
                    last_push_date=excluded.last_push_date,
                    updated_at=excluded.updated_at
                """,
                (str(channel_id), str(area_id), str(last_push_date or ""), cn_now()),
            )
            await connection.commit()

    async def remove_daily_keyword_push_subscription(self, channel_id: str, area_id: str) -> None:
        async with self._connect() as connection:
            await connection.execute(
                "DELETE FROM delta_force_daily_keyword_push WHERE channel_id=? AND area_id=?",
                (str(channel_id), str(area_id)),
            )
            await connection.commit()

    async def has_daily_keyword_push_subscription(self, channel_id: str, area_id: str) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT 1 FROM delta_force_daily_keyword_push WHERE channel_id=? AND area_id=?",
                (str(channel_id), str(area_id)),
            )
            return await cursor.fetchone() is not None

    async def any_daily_keyword_push_subscriptions(self) -> bool:
        async with self._connect() as connection:
            cursor = await connection.execute("SELECT 1 FROM delta_force_daily_keyword_push LIMIT 1")
            return await cursor.fetchone() is not None

    async def list_daily_keyword_push_subscriptions(self) -> list[dict]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT channel_id, area_id, last_push_date, updated_at FROM delta_force_daily_keyword_push"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def mark_daily_keyword_pushed(self, channel_id: str, area_id: str, push_date: str) -> None:
        async with self._connect() as connection:
            await connection.execute(
                """
                UPDATE delta_force_daily_keyword_push
                SET last_push_date=?, updated_at=?
                WHERE channel_id=? AND area_id=?
                """,
                (str(push_date), cn_now(), str(channel_id), str(area_id)),
            )
            await connection.commit()
