from __future__ import annotations

import base64
from dataclasses import dataclass, field
import logging
import posixpath
import re
from typing import Any, Protocol

from agents import Agent, RunContextWrapper, Runner, function_tool
from agents.agent import StopAtTools
from openai import AsyncOpenAI

from akc.storage import AgentFSStore, SearchResult, canonical_agentfs_path

from .prompts import build_ingestion_instructions
from .types import CompileOutcome, IngestionRunContext, NormalizedSource

logger = logging.getLogger(__name__)

MAX_CONCEPTS_PER_SOURCE = 5


class Compiler(Protocol):
    async def compile_source(self, source: NormalizedSource) -> CompileOutcome:
        ...


class OpenAIFileClient(Protocol):
    async def create(self, *, file: Any, purpose: str, **kwargs: Any) -> Any:
        ...

    async def delete(self, file_id: str, **kwargs: Any) -> Any:
        ...


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return cleaned.strip("-") or "untitled-concept"


def validate_index_content(content: str) -> None:
    required_sections = ["# Knowledge Index", "## Concepts", "## Sources", "## Recently Updated"]
    lines = [line.rstrip() for line in content.strip().splitlines()]
    positions: list[int] = []
    for section in required_sections:
        try:
            positions.append(lines.index(section))
        except ValueError as exc:
            raise ValueError(f"index.md missing required section: {section}") from exc

    if positions != sorted(positions):
        raise ValueError("index.md sections must remain in the required order.")

    current_section: str | None = None
    for line in lines:
        if line.startswith("#"):
            current_section = line
            continue
        if not line.strip():
            continue
        if current_section in {"## Concepts", "## Recently Updated"}:
            if not re.fullmatch(r"- \[\[[^\]]+\]\]", line):
                raise ValueError("Concept sections may only contain bullet wiki references.")
        elif current_section == "## Sources":
            if not re.fullmatch(r"- /raw/[A-Za-z0-9_.-]+\.md", line):
                raise ValueError("Sources section may only contain raw markdown paths.")
        elif current_section == "# Knowledge Index":
            continue
        else:
            raise ValueError("index.md may not contain extra sections or prose.")


def validate_concept_content(content: str) -> None:
    required_headers = ["## Summary", "## Key Ideas", "## Source", "## Related Concepts"]
    lines = content.strip().splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("Concept content must start with an H1 title.")
    for header in required_headers:
        if header not in content:
            raise ValueError(f"Concept content missing required header: {header}")


@dataclass
class FinalizeResult:
    raw_written: bool
    concepts_written: list[str]
    index_updated: bool


@dataclass
class IngestionStageSession:
    store: AgentFSStore
    source: NormalizedSource
    staged_files: dict[str, str] = field(default_factory=dict)
    write_order: list[str] = field(default_factory=list)
    concepts_written: list[str] = field(default_factory=list)
    finalized: bool = False

    def _canonical(self, path: str) -> str:
        return canonical_agentfs_path(path)

    def _normalize_writable_path(self, path: str) -> str:
        canonical = self._canonical(path)
        if canonical.startswith("/wiki/concepts/") and canonical.endswith(".md"):
            parent = posixpath.dirname(canonical)
            if parent != "/wiki/concepts":
                raise ValueError("Concept files must be written directly under /wiki/concepts.")
            stem = posixpath.splitext(posixpath.basename(canonical))[0]
            return f"/wiki/concepts/{slugify(stem)}.md"
        return canonical

    def _validate_write(self, path: str, content: str) -> str:
        canonical = self._normalize_writable_path(path)
        if canonical in self.staged_files:
            raise ValueError(f"Path already written in this ingest run: {canonical}")
        if self.finalized:
            raise ValueError("No further writes are allowed after finalize_ingest().")

        if not self.write_order:
            if canonical != self.source.raw_path:
                raise ValueError("The first write in an ingest run must be the raw markdown file.")
        elif canonical == self.source.raw_path:
            raise ValueError("The raw markdown file can only be written once.")

        if canonical == self.source.raw_path:
            if not content.strip():
                raise ValueError("Raw markdown content cannot be empty.")
            return canonical

        if canonical.startswith("/wiki/concepts/") and canonical.endswith(".md"):
            if self.source.raw_path not in self.staged_files:
                raise ValueError("Concept files may be written only after the raw markdown file.")
            validate_concept_content(content)
            if len(self.concepts_written) >= MAX_CONCEPTS_PER_SOURCE:
                raise ValueError(
                    f"At most {MAX_CONCEPTS_PER_SOURCE} concept files may be written per source."
                )
            return canonical

        if canonical == "/wiki/index.md":
            if not self.concepts_written:
                raise ValueError("index.md may be written only after at least one concept file.")
            validate_index_content(content)
            return canonical

        raise ValueError(f"Path is not writable during ingest: {canonical}")

    async def write_file(self, path: str, content: str) -> str:
        canonical = self._validate_write(path, content)
        self.staged_files[canonical] = content.strip() + "\n"
        self.write_order.append(canonical)
        if canonical.startswith("/wiki/concepts/"):
            slug = posixpath.splitext(posixpath.basename(canonical))[0]
            if slug not in self.concepts_written:
                self.concepts_written.append(slug)
        return canonical

    async def read_file(self, path: str) -> str:
        canonical = self._canonical(path)
        if canonical in self.staged_files:
            return self.staged_files[canonical]
        return await self.store.read_text(canonical)

    async def list_dir(self, path: str) -> list[str]:
        canonical = self._canonical(path)
        names = set(await self.store.list_dir(canonical))
        prefix = canonical if canonical.endswith("/") else canonical + "/"
        for staged_path in self.staged_files:
            if not staged_path.startswith(prefix):
                continue
            remainder = staged_path[len(prefix) :]
            if not remainder:
                continue
            names.add(remainder.split("/", 1)[0])
        return sorted(names)

    async def search_files(self, query: str, path: str, limit: int = 10) -> list[SearchResult]:
        canonical = self._canonical(path)
        matches: list[SearchResult] = []
        seen_paths: set[str] = set()
        lowered = query.strip().lower()
        if not lowered:
            return matches

        for staged_path, content in sorted(self.staged_files.items()):
            if not staged_path.startswith(canonical.rstrip("/") + "/") and staged_path != canonical:
                continue
            seen_paths.add(staged_path)
            index = content.lower().find(lowered)
            if index == -1:
                continue
            snippet_start = max(0, index - 80)
            snippet_end = min(len(content), index + len(query) + 80)
            snippet = content[snippet_start:snippet_end].replace("\n", " ").strip()
            matches.append(SearchResult(path=staged_path, snippet=snippet))

        for result in await self.store.search_files(query, canonical, limit=limit):
            if result.path in seen_paths:
                continue
            matches.append(result)
            if len(matches) >= limit:
                break
        return matches[:limit]

    def _build_finalize_result(self) -> FinalizeResult:
        if self.source.raw_path not in self.staged_files:
            raise ValueError("Ingest did not create the raw markdown file.")
        if not self.concepts_written:
            raise ValueError("Ingest did not create any concept files.")
        if "/wiki/index.md" not in self.staged_files:
            raise ValueError("Ingest did not create index.md.")
        if self.write_order[-1] != "/wiki/index.md":
            raise ValueError("index.md must be the final non-log write in the ingest run.")
        return FinalizeResult(
            raw_written=True,
            concepts_written=list(self.concepts_written),
            index_updated=True,
        )

    def finalize(self) -> FinalizeResult:
        result = self._build_finalize_result()
        self.finalized = True
        return result

    def can_finalize(self) -> bool:
        try:
            self._build_finalize_result()
        except ValueError:
            return False
        return True

    async def commit(self) -> None:
        for path in self.write_order:
            logger.info("Committing staged file %s", path)
            await self.store.write_text(path, self.staged_files[path])


@dataclass
class OpenAIIngestionCompiler:
    store: AgentFSStore
    model: str
    openai_api_key: str | None = None
    files_client: OpenAIFileClient | None = None
    max_turns: int = 20

    def __post_init__(self) -> None:
        logger.info("Creating ingestion agent with model %s", self.model)
        if self.files_client is None:
            self.files_client = AsyncOpenAI(api_key=self.openai_api_key).files
        self.agent = Agent[IngestionRunContext](
            name="AKC Ingestion Agent",
            instructions=build_ingestion_instructions,
            model=self.model,
            tool_use_behavior=StopAtTools(stop_at_tool_names=["finalize_ingest"]),
            tools=[
                self.list_dir_tool(),
                self.read_file_tool(),
                self.search_files_tool(),
                self.write_file_tool(),
                self.append_file_tool(),
                self.finalize_ingest_tool(),
            ],
        )

    def list_dir_tool(self):
        @function_tool(name_override="list_dir")
        async def list_dir(ctx: RunContextWrapper[IngestionRunContext], path: str) -> list[str]:
            session: IngestionStageSession = ctx.context.store
            return await session.list_dir(path)

        return list_dir

    def read_file_tool(self):
        @function_tool(name_override="read_file")
        async def read_file(ctx: RunContextWrapper[IngestionRunContext], path: str) -> str:
            session: IngestionStageSession = ctx.context.store
            return await session.read_file(path)

        return read_file

    def search_files_tool(self):
        @function_tool(name_override="search_files")
        async def search_files(
            ctx: RunContextWrapper[IngestionRunContext], query: str, path: str
        ) -> list[dict[str, str]]:
            session: IngestionStageSession = ctx.context.store
            results = await session.search_files(query, path)
            return [{"path": result.path, "snippet": result.snippet} for result in results]

        return search_files

    def write_file_tool(self):
        @function_tool(name_override="write_file")
        async def write_file(
            ctx: RunContextWrapper[IngestionRunContext], path: str, content: str
        ) -> str:
            session: IngestionStageSession = ctx.context.store
            try:
                canonical = await session.write_file(path, content)
            except Exception:
                logger.exception(
                    "Tool write_file rejected for source %s path=%s",
                    ctx.context.source.source_id,
                    path,
                )
                raise
            logger.info(
                "Tool write_file invoked for source %s -> %s",
                ctx.context.source.source_id,
                canonical,
            )
            if canonical == ctx.context.source.raw_path:
                ctx.context.raw_written = True
            elif canonical == "/wiki/index.md":
                ctx.context.index_updated = True
            elif canonical.startswith("/wiki/concepts/"):
                slug = posixpath.splitext(posixpath.basename(canonical))[0]
                if slug not in ctx.context.concepts_written:
                    ctx.context.concepts_written.append(slug)
            return canonical

        return write_file

    def append_file_tool(self):
        @function_tool(name_override="append_file")
        async def append_file(
            ctx: RunContextWrapper[IngestionRunContext], path: str, content: str
        ) -> str:
            canonical = canonical_agentfs_path(path)
            if canonical != "/logs/ingestion.log":
                raise ValueError("append_file may only write to /logs/ingestion.log")
            logger.error(
                "Tool append_file invoked for source %s -> %s",
                ctx.context.source.source_id,
                canonical,
            )
            await self.store.append_text(canonical, content)
            return canonical

        return append_file

    def finalize_ingest_tool(self):
        @function_tool(name_override="finalize_ingest")
        async def finalize_ingest(ctx: RunContextWrapper[IngestionRunContext]) -> dict[str, Any]:
            session: IngestionStageSession = ctx.context.store
            result = session.finalize()
            logger.info(
                "Tool finalize_ingest invoked for source %s raw=%s concepts=%s index=%s",
                ctx.context.source.source_id,
                result.raw_written,
                result.concepts_written,
                result.index_updated,
            )
            return {
                "raw_written": result.raw_written,
                "concepts_written": result.concepts_written,
                "index_updated": result.index_updated,
            }

        return finalize_ingest

    async def compile_source(self, source: NormalizedSource) -> CompileOutcome:
        session = IngestionStageSession(store=self.store, source=source)
        context = IngestionRunContext(store=session, source=source)
        logger.info("Starting ingestion compile for %s (%s)", source.source_id, source.input_type)
        uploaded_pdf_file_id: str | None = None
        result: Any = None
        try:
            if source.input_type == "pdf":
                try:
                    uploaded_pdf_file_id = await self._upload_pdf_file(source)
                except Exception as exc:
                    logger.exception("PDF upload failed for %s", source.source_id)
                    return CompileOutcome(
                        success=False,
                        raw_written=False,
                        concepts_written=[],
                        index_updated=False,
                        final_output="",
                        error=str(exc),
                    )

            prompts = [
                self._build_input_items(source, uploaded_pdf_file_id=uploaded_pdf_file_id),
                self._build_input_items(source, uploaded_pdf_file_id=uploaded_pdf_file_id, retry=True),
            ]
            for attempt, input_items in enumerate(prompts, start=1):
                try:
                    result = await Runner.run(
                        self.agent,
                        input=input_items,
                        context=context,
                        max_turns=self.max_turns if attempt == 1 else self.max_turns,
                    )
                except Exception as exc:
                    if self._try_rescue_completed_run(session, context, source):
                        await session.commit()
                        logger.warning(
                            "Recovered completed ingest for %s after runner error on attempt %d",
                            source.source_id,
                            attempt,
                        )
                        return CompileOutcome(
                            success=True,
                            raw_written=context.raw_written,
                            concepts_written=list(context.concepts_written),
                            index_updated=context.index_updated,
                            final_output="" if result is None else str(getattr(result, "final_output", "")),
                            error=None,
                        )
                    logger.exception("Agent run failed for %s on attempt %d", source.source_id, attempt)
                    return CompileOutcome(
                        success=False,
                        raw_written=context.raw_written,
                        concepts_written=list(context.concepts_written),
                        index_updated=context.index_updated,
                        final_output="" if result is None else str(getattr(result, "final_output", "")),
                        error=str(exc),
                    )

                if session.finalized:
                    break
                if self._try_rescue_completed_run(session, context, source):
                    logger.warning(
                        "Recovered completed ingest for %s without explicit finalize_ingest() on attempt %d",
                        source.source_id,
                        attempt,
                    )
                    break

                logger.warning(
                    "Ingest run for %s stopped without finalize_ingest() on attempt %d",
                    source.source_id,
                    attempt,
                )

            if not session.finalized:
                return CompileOutcome(
                    success=False,
                    raw_written=context.raw_written,
                    concepts_written=list(context.concepts_written),
                    index_updated=context.index_updated,
                    final_output="" if result is None else str(result.final_output),
                    error="Ingest did not call finalize_ingest().",
                )

            await session.commit()
            logger.info(
                "Finished ingestion compile for %s concepts=%s index_updated=%s",
                source.source_id,
                context.concepts_written,
                context.index_updated,
            )
            return CompileOutcome(
                success=True,
                raw_written=context.raw_written,
                concepts_written=list(context.concepts_written),
                index_updated=context.index_updated,
                final_output=str(result.final_output),
                error=None,
            )
        finally:
            if uploaded_pdf_file_id:
                await self._cleanup_pdf_file(uploaded_pdf_file_id)

    def _build_input_items(
        self,
        source: NormalizedSource,
        *,
        uploaded_pdf_file_id: str | None = None,
        retry: bool = False,
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []

        if retry:
            instructions = (
                "Continue the same ingest using the current staged filesystem state. "
                f"Create {source.raw_path} first if it does not exist yet, then create any remaining concept files, "
                "then write /wiki/index.md, then call finalize_ingest() immediately. "
                "Do not rewrite any file that already exists in the staged filesystem."
            )
        else:
            instructions = (
                "Ingest this source into AgentFS. "
                f"Create {source.raw_path} first, then the final concept files, then /wiki/index.md, "
                "then call finalize_ingest()."
            )
        content.append({"type": "input_text", "text": instructions})

        if source.text_content is not None:
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        f"Source text for {source.input_name}:\n\n{source.text_content}"
                    ),
                }
            )
        elif source.original_bytes:
            if source.input_type == "pdf":
                if uploaded_pdf_file_id is None:
                    raise ValueError("PDF sources require an uploaded OpenAI file reference.")
                content.append(
                    {
                        "type": "input_file",
                        "file_id": uploaded_pdf_file_id,
                    }
                )
            elif source.input_type == "image" and source.mime_type:
                encoded = base64.b64encode(source.original_bytes).decode("utf-8")
                content.append(
                    {
                        "type": "input_image",
                        "detail": "auto",
                        "image_url": f"data:{source.mime_type};base64,{encoded}",
                    }
                )

        return [{"role": "user", "content": content}]

    async def _upload_pdf_file(self, source: NormalizedSource) -> str:
        if source.original_bytes is None:
            raise ValueError("PDF source bytes are required for upload.")
        filename = source.original_filename or source.input_name
        logger.info("Uploading PDF for source %s to OpenAI Files", source.source_id)
        uploaded = await self.files_client.create(
            file=(filename, source.original_bytes, "application/pdf"),
            purpose="user_data",
        )
        file_id = getattr(uploaded, "id", "")
        if not file_id:
            raise ValueError("OpenAI Files upload did not return a file id.")
        logger.info("Uploaded PDF for source %s as file_id=%s", source.source_id, file_id)
        return file_id

    async def _cleanup_pdf_file(self, file_id: str) -> None:
        try:
            await self.files_client.delete(file_id)
            logger.info("Deleted uploaded OpenAI PDF file %s", file_id)
        except Exception:
            logger.warning("Failed to delete uploaded OpenAI PDF file %s", file_id, exc_info=True)

    def _try_rescue_completed_run(
        self,
        session: IngestionStageSession,
        context: IngestionRunContext,
        source: NormalizedSource,
    ) -> bool:
        if session.finalized or not session.can_finalize():
            return False
        result = session.finalize()
        context.raw_written = result.raw_written
        context.index_updated = result.index_updated
        context.concepts_written = list(result.concepts_written)
        logger.info(
            "Auto-finalized ingest for %s raw=%s concepts=%s index=%s",
            source.source_id,
            result.raw_written,
            result.concepts_written,
            result.index_updated,
        )
        return True
