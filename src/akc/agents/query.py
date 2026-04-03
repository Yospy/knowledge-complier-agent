from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
import posixpath
from typing import Any, AsyncIterator, Protocol

from agents import Agent, RunContextWrapper, Runner, function_tool

from akc.storage import AgentFSStore, SearchResult, canonical_agentfs_path

from .prompts import build_query_instructions
from .types import QueryRunContext

logger = logging.getLogger(__name__)


class QueryService(Protocol):
    async def stream_chat(self, messages: list[dict[str, str]], cwd: str) -> AsyncIterator[bytes]:
        ...


@dataclass
class ReadOnlyShellSession:
    store: AgentFSStore
    cwd: str = "/"

    def resolve_path(self, path: str, cwd: str | None = None) -> str:
        base = self.cwd if cwd is None else cwd
        if not path or path == ".":
            return canonical_agentfs_path(base)
        if path.startswith("/"):
            return canonical_agentfs_path(path)
        return canonical_agentfs_path(posixpath.join(base, path))

    async def ls(self, path: str = ".") -> list[str]:
        resolved = self.resolve_path(path)
        if not await self.store.exists(resolved):
            raise ValueError(f"Path does not exist: {resolved}")
        if await self.store.is_dir(resolved):
            return await self.store.list_dir(resolved)
        return [posixpath.basename(resolved)]

    async def cd(self, path: str) -> str:
        resolved = self.resolve_path(path)
        if not await self.store.exists(resolved):
            raise ValueError(f"Directory does not exist: {resolved}")
        if not await self.store.is_dir(resolved):
            raise ValueError(f"Path is not a directory: {resolved}")
        self.cwd = resolved
        return self.cwd

    async def cat(self, path: str) -> str:
        resolved = self.resolve_path(path)
        if not await self.store.exists(resolved):
            raise ValueError(f"File does not exist: {resolved}")
        if await self.store.is_dir(resolved):
            raise ValueError(f"Path is a directory, not a file: {resolved}")
        return await self.store.read_text(resolved)

    async def grep(self, query: str, path: str = ".", limit: int = 10) -> list[SearchResult]:
        resolved = self.resolve_path(path)
        if not await self.store.exists(resolved):
            raise ValueError(f"Path does not exist: {resolved}")
        if await self.store.is_dir(resolved):
            return await self.store.search_files(query, resolved, limit=limit)

        content = await self.store.read_text(resolved)
        lowered = query.strip().lower()
        if not lowered:
            return []
        index = content.lower().find(lowered)
        if index == -1:
            return []
        snippet_start = max(0, index - 80)
        snippet_end = min(len(content), index + len(query) + 80)
        snippet = content[snippet_start:snippet_end].replace("\n", " ").strip()
        return [SearchResult(path=resolved, snippet=snippet)]


def _event_type_name(event: Any) -> str | None:
    if isinstance(event, dict):
        return str(event.get("type")) if event.get("type") else None
    return getattr(event, "type", None)


def _event_attr(event: Any, name: str) -> Any:
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


@dataclass
class OpenAIQueryService:
    store: AgentFSStore
    model: str
    max_turns: int = 12

    def __post_init__(self) -> None:
        logger.info("Creating query agent with model %s", self.model)
        self.agent = Agent[QueryRunContext](
            name="AKC Query Agent",
            instructions=build_query_instructions,
            model=self.model,
            tools=[
                self.ls_tool(),
                self.cd_tool(),
                self.cat_tool(),
                self.grep_tool(),
            ],
        )

    async def stream_chat(self, messages: list[dict[str, str]], cwd: str) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        session = ReadOnlyShellSession(store=self.store, cwd=self._resolve_path(cwd or "/"))
        context = QueryRunContext(store=session, cwd=session.cwd, event_queue=queue)
        payload = self._build_input(messages=messages, cwd=context.cwd)
        result = Runner.run_streamed(
            self.agent,
            input=payload,
            context=context,
            max_turns=self.max_turns,
        )

        async def forward_events() -> None:
            saw_text_delta = False
            try:
                await context.emit({"type": "status", "text": "Thinking"})
                async for event in result.stream_events():
                    if _event_type_name(event) != "raw_response_event":
                        continue
                    raw = _event_attr(event, "data")
                    raw_type = _event_type_name(raw)
                    if raw_type == "response.output_text.delta":
                        delta = _event_attr(raw, "delta")
                        if delta:
                            saw_text_delta = True
                            await context.emit({"type": "assistant_delta", "delta": str(delta)})
                    elif raw_type == "response.output_text.done" and saw_text_delta:
                        await context.emit({"type": "status", "text": "Synthesizing"})

                final_message = str(result.final_output or "").strip()
                await context.emit(
                    {
                        "type": "assistant_final",
                        "message": final_message,
                        "cwd": session.cwd,
                    }
                )
            except Exception as exc:
                logger.exception("Query run failed at cwd=%s", context.cwd)
                await context.emit({"type": "error", "message": str(exc)})
            finally:
                await context.emit({"type": "done"})

        task = asyncio.create_task(forward_events())
        try:
            while True:
                event = await queue.get()
                yield (json.dumps(event) + "\n").encode("utf-8")
                if event["type"] == "done":
                    break
        finally:
            await task

    def ls_tool(self):
        @function_tool(name_override="ls")
        async def ls(ctx: RunContextWrapper[QueryRunContext], path: str = ".") -> list[str]:
            session: ReadOnlyShellSession = ctx.context.store
            resolved = session.resolve_path(path)
            await ctx.context.emit(
                {"type": "tool_start", "tool": "ls", "display": f"Listing {resolved}", "cwd": ctx.context.cwd}
            )
            entries = await session.ls(path)
            await ctx.context.emit(
                {
                    "type": "tool_end",
                    "tool": "ls",
                    "display": f"Listed {resolved} ({len(entries)} entries)",
                    "cwd": ctx.context.cwd,
                }
            )
            await ctx.context.emit({"type": "status", "text": "Thinking"})
            return entries

        return ls

    def cd_tool(self):
        @function_tool(name_override="cd")
        async def cd(ctx: RunContextWrapper[QueryRunContext], path: str) -> str:
            session: ReadOnlyShellSession = ctx.context.store
            resolved = session.resolve_path(path)
            await ctx.context.emit(
                {
                    "type": "tool_start",
                    "tool": "cd",
                    "display": f"Changing directory to {resolved}",
                    "cwd": ctx.context.cwd,
                }
            )
            ctx.context.cwd = await session.cd(path)
            await ctx.context.emit(
                {
                    "type": "tool_end",
                    "tool": "cd",
                    "display": f"Current directory is {ctx.context.cwd}",
                    "cwd": ctx.context.cwd,
                }
            )
            await ctx.context.emit({"type": "status", "text": "Thinking"})
            return resolved

        return cd

    def cat_tool(self):
        @function_tool(name_override="cat")
        async def cat(ctx: RunContextWrapper[QueryRunContext], path: str) -> str:
            session: ReadOnlyShellSession = ctx.context.store
            resolved = session.resolve_path(path)
            await ctx.context.emit(
                {"type": "tool_start", "tool": "cat", "display": f"Reading {resolved}", "cwd": ctx.context.cwd}
            )
            content = await session.cat(path)
            await ctx.context.emit(
                {
                    "type": "tool_end",
                    "tool": "cat",
                    "display": f"Read {resolved} ({len(content)} chars)",
                    "cwd": ctx.context.cwd,
                }
            )
            await ctx.context.emit({"type": "status", "text": "Thinking"})
            return content

        return cat

    def grep_tool(self):
        @function_tool(name_override="grep")
        async def grep(
            ctx: RunContextWrapper[QueryRunContext],
            query: str,
            path: str = ".",
            limit: int = 10,
        ) -> list[dict[str, str]]:
            session: ReadOnlyShellSession = ctx.context.store
            resolved = session.resolve_path(path)
            await ctx.context.emit(
                {
                    "type": "tool_start",
                    "tool": "grep",
                    "display": f'Searching {resolved} for "{query}"',
                    "cwd": ctx.context.cwd,
                }
            )
            matches = await session.grep(query, path, limit=limit)
            payload = [{"path": match.path, "snippet": match.snippet} for match in matches]
            await ctx.context.emit(
                {
                    "type": "tool_end",
                    "tool": "grep",
                    "display": f"Found {len(payload)} matches in {resolved}",
                    "cwd": ctx.context.cwd,
                }
            )
            await ctx.context.emit({"type": "status", "text": "Thinking"})
            return payload

        return grep

    def _resolve_path(self, path: str, cwd: str = "/") -> str:
        if not path or path == ".":
            return canonical_agentfs_path(cwd)
        if path.startswith("/"):
            return canonical_agentfs_path(path)
        return canonical_agentfs_path(posixpath.join(cwd, path))

    def _build_input(self, messages: list[dict[str, str]], cwd: str) -> list[dict[str, Any]]:
        transcript = []
        for message in messages:
            role = message.get("role", "user").upper()
            content = message.get("content", "")
            transcript.append(f"{role}: {content}")
        body = "\n\n".join(transcript).strip() or "USER: (no message provided)"
        prompt = (
            f"Current working directory: {cwd}\n\n"
            "Conversation transcript:\n"
            f"{body}\n\n"
            "Answer the latest user request using the filesystem tools when needed."
        )
        return [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}]
