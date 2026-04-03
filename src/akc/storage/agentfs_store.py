from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import posixpath
from stat import S_ISDIR
from typing import Any

from agentfs_sdk import AgentFS, AgentFSOptions


ROOT_DIRECTORIES = ("/raw", "/wiki", "/wiki/concepts", "/logs", "/system", "/system/source_hashes")
logger = logging.getLogger(__name__)


def canonical_agentfs_path(path: str) -> str:
    if not path:
        raise ValueError("Path is required.")

    cleaned = posixpath.normpath("/" + path.lstrip("/"))
    if cleaned == "/..":
        raise ValueError("Invalid path.")
    return cleaned


@dataclass
class SearchResult:
    path: str
    snippet: str


@dataclass
class SourceHashRecord:
    sha256: str
    source_id: str
    input_name: str
    input_type: str
    raw_path: str
    ingested_at: str


class AgentFSStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._agentfs: AgentFS | None = None

    async def open(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info("Opening AgentFS at %s", self.db_path)
        self._agentfs = await AgentFS.open(AgentFSOptions(path=self.db_path))
        await self.ensure_layout()

    async def close(self) -> None:
        if self._agentfs is not None:
            logger.info("Closing AgentFS at %s", self.db_path)
            await self._agentfs.close()
            self._agentfs = None

    @property
    def fs(self) -> Any:
        if self._agentfs is None:
            raise RuntimeError("AgentFS is not initialized.")
        return self._agentfs.fs

    async def ensure_layout(self) -> None:
        for path in ROOT_DIRECTORIES:
            await self.ensure_dir(path)

    async def ensure_dir(self, path: str) -> None:
        canonical = canonical_agentfs_path(path)
        if await self.exists(canonical):
            return
        logger.info("Creating AgentFS directory %s", canonical)
        await self.fs.mkdir(canonical)

    async def exists(self, path: str) -> bool:
        canonical = canonical_agentfs_path(path)
        try:
            await self.fs.stat(canonical)
        except Exception:
            return False
        return True

    async def is_dir(self, path: str) -> bool:
        canonical = canonical_agentfs_path(path)
        stats = await self.fs.stat(canonical)
        return bool(S_ISDIR(stats.mode))

    async def list_dir(self, path: str) -> list[str]:
        canonical = canonical_agentfs_path(path)
        if not await self.exists(canonical):
            return []
        return sorted(await self.fs.readdir(canonical))

    async def read_text(self, path: str) -> str:
        canonical = canonical_agentfs_path(path)
        return str(await self.fs.read_file(canonical, encoding="utf-8"))

    async def write_text(self, path: str, content: str) -> None:
        canonical = canonical_agentfs_path(path)
        parent = posixpath.dirname(canonical)
        if parent and parent != "/" and not await self.exists(parent):
            await self.ensure_dir(parent)
        logger.info("Writing AgentFS file %s (%d chars)", canonical, len(content))
        await self.fs.write_file(canonical, content, encoding="utf-8")

    async def append_text(self, path: str, content: str) -> None:
        canonical = canonical_agentfs_path(path)
        existing = ""
        if await self.exists(canonical):
            existing = await self.read_text(canonical)
        logger.info("Appending AgentFS file %s (%d chars)", canonical, len(content))
        await self.write_text(canonical, existing + content)

    async def walk_files(self, path: str) -> list[str]:
        canonical = canonical_agentfs_path(path)
        if not await self.exists(canonical):
            return []

        files: list[str] = []
        for child in await self.list_dir(canonical):
            child_path = canonical_agentfs_path(posixpath.join(canonical, child))
            if await self.is_dir(child_path):
                files.extend(await self.walk_files(child_path))
            else:
                files.append(child_path)
        return sorted(files)

    async def search_files(self, query: str, path: str, limit: int = 10) -> list[SearchResult]:
        lowered = query.strip().lower()
        if not lowered:
            return []

        matches: list[SearchResult] = []
        for file_path in await self.walk_files(path):
            if len(matches) >= limit:
                break

            text = await self.read_text(file_path)
            haystack = text.lower()
            index = haystack.find(lowered)
            if index == -1:
                continue

            snippet_start = max(0, index - 80)
            snippet_end = min(len(text), index + len(query) + 80)
            snippet = text[snippet_start:snippet_end].replace("\n", " ").strip()
            matches.append(SearchResult(path=file_path, snippet=snippet))
        return matches

    async def read_source_hash_record(self, sha256: str) -> SourceHashRecord | None:
        path = f"/system/source_hashes/{sha256}.json"
        if not await self.exists(path):
            return None
        payload = json.loads(await self.read_text(path))
        return SourceHashRecord(**payload)

    async def write_source_hash_record(self, record: SourceHashRecord) -> None:
        path = f"/system/source_hashes/{record.sha256}.json"
        await self.write_text(path, json.dumps(record.__dict__, indent=2, sort_keys=True) + "\n")
