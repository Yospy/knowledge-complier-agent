from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedSource:
    source_id: str
    input_name: str
    input_type: str
    raw_path: str
    text_content: str | None = None
    original_filename: str | None = None
    original_bytes: bytes | None = None
    mime_type: str | None = None


@dataclass
class CompileOutcome:
    success: bool
    raw_written: bool
    concepts_written: list[str]
    index_updated: bool
    final_output: str
    error: str | None = None


@dataclass
class IngestionRunContext:
    store: object
    source: NormalizedSource
    raw_written: bool = False
    concepts_written: list[str] = field(default_factory=list)
    index_updated: bool = False


@dataclass
class QueryRunContext:
    store: object
    cwd: str
    event_queue: asyncio.Queue[dict[str, Any]]

    async def emit(self, event: dict[str, Any]) -> None:
        await self.event_queue.put(event)
