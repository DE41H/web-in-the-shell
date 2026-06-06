import json
from pathlib import Path

import aiosqlite

from security.redact import redact

from persistence.crypto import decrypt, encrypt
from persistence.models import Convo, ConvoMessage


def _redact_message(message: ConvoMessage) -> ConvoMessage:
    if isinstance(message.content, str):
        return message.model_copy(update={"content": redact(message.content)})
    return message


def _redact_result(result: dict | None) -> dict | None:
    if result is None:
        return None
    return json.loads(redact(json.dumps(result)))


class ConvoStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> "ConvoStore":
        self._conn = await aiosqlite.connect(self._path)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "ConvoStore must be used as an async context manager: "
                "`async with ConvoStore(path) as store:`."
            )
        return self._conn

    async def get_latest_for_intent(self, intent: str) -> Convo | None:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT id, intent, created_at, updated_at, messages, result
            FROM convos
            WHERE intent = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (intent,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        decrypted_row = (
            row[0],
            row[1],
            row[2],
            row[3],
            decrypt(row[4]),
            decrypt(row[5]) if row[5] else None,
        )
        return Convo.from_row(decrypted_row)

    async def save(self, convo: Convo) -> None:
        conn = self._require_conn()
        cleaned_messages = [_redact_message(m) for m in convo.messages]
        cleaned_result = _redact_result(convo.result)
        messages_json = json.dumps(
            [m.model_dump(mode="json") for m in cleaned_messages]
        )
        result_json = (
            json.dumps(cleaned_result) if cleaned_result is not None else None
        )
        await conn.execute(
            """
            INSERT OR REPLACE INTO convos
                (id, intent, created_at, updated_at, messages, result)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                convo.id,
                convo.intent,
                convo.created_at.isoformat(),
                convo.updated_at.isoformat(),
                encrypt(messages_json),
                encrypt(result_json) if result_json is not None else None,
            ),
        )
        await conn.commit()

    async def clear(self, intent: str) -> int:
        conn = self._require_conn()
        cur = await conn.execute("DELETE FROM convos WHERE intent = ?", (intent,))
        await conn.commit()
        rowcount = cur.rowcount
        await cur.close()
        return rowcount

    async def list_all(self) -> list[Convo]:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT id, intent, created_at, updated_at, messages, result "
            "FROM convos ORDER BY updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        out: list[Convo] = []
        for row in rows:
            decrypted_row = (
                row[0],
                row[1],
                row[2],
                row[3],
                decrypt(row[4]),
                decrypt(row[5]) if row[5] else None,
            )
            out.append(Convo.from_row(decrypted_row))
        return out

    async def clear_all(self) -> int:
        conn = self._require_conn()
        cur = await conn.execute("DELETE FROM convos")
        await conn.commit()
        rowcount = cur.rowcount
        await cur.close()
        return rowcount
