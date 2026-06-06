import json
from datetime import datetime
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict


class ConvoMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list | None = None
    tool_calls: list | None = None
    tool_call_id: str | None = None
    name: str | None = None


class Convo(BaseModel):
    id: str
    intent: str
    created_at: datetime
    updated_at: datetime
    messages: list[ConvoMessage]
    result: dict | None = None

    def to_llm_messages(self) -> list[dict[str, Any]]:
        return [m.model_dump(exclude_none=True) for m in self.messages]

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Convo":
        convo_id, intent, created_at, updated_at, messages_json, result_json = (
            row[0], row[1], row[2], row[3], row[4], row[5]
        )
        messages = [ConvoMessage(**m) for m in json.loads(messages_json)]
        result = json.loads(result_json) if result_json else None
        return cls(
            id=convo_id,
            intent=intent,
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
            messages=messages,
            result=result,
        )
