from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
import re
from typing import AsyncIterator, Protocol
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from akc.agents import OpenAIIngestionCompiler, OpenAIQueryService
from akc.agents.types import CompileOutcome, NormalizedSource
from akc.config import Settings
from akc.storage import AgentFSStore, SourceHashRecord

logger = logging.getLogger(__name__)


class IngestionCompilerProtocol(Protocol):
    async def compile_source(self, source: NormalizedSource) -> CompileOutcome:
        ...


class QueryServiceProtocol(Protocol):
    async def stream_chat(self, messages: list[dict[str, str]], cwd: str) -> AsyncIterator[bytes]:
        ...


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return cleaned.strip("-") or "source"


def timestamp_source_id(name: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{uuid4().hex[:8]}_{slugify(Path(name).stem)}"


class SourceResult(BaseModel):
    source_id: str
    input_name: str
    input_type: str
    status: str
    raw_written: bool = False
    index_updated: bool = False
    concepts_written: list[str] = Field(default_factory=list)
    content_sha256: str | None = None
    duplicate_of_source_id: str | None = None
    duplicate_of_raw_path: str | None = None
    error: str | None = None


class BatchResponse(BaseModel):
    batch_status: str
    results: list[SourceResult]


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    cwd: str = "/"


class IngestionService:
    def __init__(self, store: AgentFSStore, compiler: IngestionCompilerProtocol) -> None:
        self.store = store
        self.compiler = compiler
        self._hash_locks: dict[str, asyncio.Lock] = {}

    async def ingest_batch(
        self, files: list[UploadFile] | None, text: str | None
    ) -> BatchResponse:
        results: list[SourceResult] = []
        logger.info(
            "Starting ingest batch files=%d has_text=%s",
            len(files or []),
            bool(text and text.strip()),
        )

        if not files and not (text and text.strip()):
            raise HTTPException(status_code=400, detail="At least one file or text input is required.")

        for upload in files or []:
            content: bytes | None = None
            try:
                logger.info("Preparing uploaded file %s", upload.filename or "uploaded-source")
                source, content = await self._prepare_upload(upload)
            except Exception as exc:
                source_id = timestamp_source_id(upload.filename or "uploaded-source")
                await self._log_failure(source_id, str(exc))
                logger.exception("Failed to prepare uploaded file %s", upload.filename or "uploaded-source")
                results.append(
                    SourceResult(
                        source_id=source_id,
                        input_name=upload.filename or "uploaded-source",
                        input_type="unknown",
                        status="failure",
                        error=str(exc),
                    )
                )
                continue

            assert content is not None
            results.append(await self._handle_source(source, content))

        if text and text.strip():
            source, content = self._prepare_text_input(text)
            results.append(await self._handle_source(source, content))

        statuses = {item.status for item in results}
        batch_status = "success"
        if statuses == {"failure"}:
            batch_status = "failure"
        elif "failure" in statuses:
            batch_status = "partial_success"
        logger.info("Finished ingest batch status=%s", batch_status)
        return BatchResponse(batch_status=batch_status, results=results)

    async def _handle_source(self, source: NormalizedSource, content_bytes: bytes) -> SourceResult:
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        lock = self._hash_locks.setdefault(content_sha256, asyncio.Lock())
        async with lock:
            existing = await self.store.read_source_hash_record(content_sha256)
            if existing is not None:
                logger.info(
                    "Skipping duplicate source %s sha256=%s original_source=%s",
                    source.input_name,
                    content_sha256,
                    existing.source_id,
                )
                return SourceResult(
                    source_id=existing.source_id,
                    input_name=source.input_name,
                    input_type=source.input_type,
                    status="duplicate",
                    raw_written=False,
                    index_updated=False,
                    concepts_written=[],
                    content_sha256=content_sha256,
                    duplicate_of_source_id=existing.source_id,
                    duplicate_of_raw_path=existing.raw_path,
                    error=None,
                )

            result = await self._compile_source(source)
            result.content_sha256 = content_sha256
            if result.status == "success":
                await self.store.write_source_hash_record(
                    SourceHashRecord(
                        sha256=content_sha256,
                        source_id=source.source_id,
                        input_name=source.input_name,
                        input_type=source.input_type,
                        raw_path=source.raw_path,
                        ingested_at=datetime.now(UTC).isoformat(),
                    )
                )
            return result

    async def _compile_source(self, source: NormalizedSource) -> SourceResult:
        logger.info("Starting source compile %s", source.source_id)
        outcome = await self.compiler.compile_source(source)
        if not outcome.success and outcome.error:
            await self._log_failure(source.source_id, outcome.error)
            logger.error("Compilation failed for source %s: %s", source.source_id, outcome.error)

        return SourceResult(
            source_id=source.source_id,
            input_name=source.input_name,
            input_type=source.input_type,
            status="success" if outcome.success else "failure",
            raw_written=outcome.raw_written,
            index_updated=outcome.index_updated,
            concepts_written=outcome.concepts_written,
            error=outcome.error,
        )

    async def _prepare_upload(self, upload: UploadFile) -> tuple[NormalizedSource, bytes]:
        filename = upload.filename or "uploaded-source"
        content = await upload.read()
        content_type = (upload.content_type or "").lower()
        inferred_type = self._infer_input_type(filename, content_type)
        logger.info(
            "Detected input type for %s as %s (content_type=%s)",
            filename,
            inferred_type,
            content_type or "unknown",
        )

        text_content: str | None = None
        original_bytes: bytes | None = None
        mime_type: str | None = None

        if inferred_type in {"markdown", "text"}:
            text_content = content.decode("utf-8")
            mime_type = "text/markdown" if inferred_type == "markdown" else "text/plain"
        else:
            original_bytes = content
            mime_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        source_id = timestamp_source_id(filename)
        return NormalizedSource(
            source_id=source_id,
            input_name=filename,
            input_type=inferred_type,
            raw_path=f"/raw/{source_id}.md",
            text_content=text_content,
            original_filename=filename,
            original_bytes=original_bytes,
            mime_type=mime_type,
        ), content

    def _prepare_text_input(self, text: str) -> tuple[NormalizedSource, bytes]:
        normalized_text = text.strip() + "\n"
        source_id = timestamp_source_id("inline-text")
        return NormalizedSource(
            source_id=source_id,
            input_name="inline-text",
            input_type="text",
            raw_path=f"/raw/{source_id}.md",
            text_content=normalized_text,
        ), normalized_text.encode("utf-8")

    def _infer_input_type(self, filename: str, content_type: str) -> str:
        suffix = Path(filename).suffix.lower()
        if content_type == "application/pdf" or suffix == ".pdf":
            return "pdf"
        if content_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            return "image"
        if content_type in {"text/markdown", "text/x-markdown"} or suffix in {".md", ".markdown"}:
            return "markdown"
        if content_type.startswith("text/") or suffix in {".txt"}:
            return "text"
        raise ValueError(f"Unsupported file type for {filename}")

    async def _log_failure(self, source_id: str, error: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        logger.error("Logging ingestion failure for %s: %s", source_id, error)
        await self.store.append_text(
            "/logs/ingestion.log",
            f"[{timestamp}] ERROR: {error}\nFile: {source_id}\n",
        )


def create_app(
    settings: Settings | None = None,
    service_override: IngestionService | None = None,
    query_service_override: QueryServiceProtocol | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if service_override is not None:
            opened_store = False
            if getattr(service_override.store, "_agentfs", None) is None:
                await service_override.store.open()
                opened_store = True
            app.state.ingestion_service = service_override
            app.state.query_service = query_service_override
            logger.info("App started with injected ingestion service")
            try:
                yield
            finally:
                if opened_store:
                    await service_override.store.close()
            return

        loaded_settings = settings or Settings.from_env()
        logger.info(
            "App startup with model=%s agentfs=%s",
            loaded_settings.model,
            loaded_settings.agentfs_db_path,
        )
        store = AgentFSStore(loaded_settings.agentfs_db_path)
        await store.open()
        compiler = OpenAIIngestionCompiler(
            store=store,
            model=loaded_settings.model,
            openai_api_key=loaded_settings.openai_api_key,
        )
        app.state.ingestion_service = IngestionService(store=store, compiler=compiler)
        app.state.query_service = OpenAIQueryService(
            store=store,
            model=loaded_settings.model,
        )
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(title="Agentic Knowledge Compiler", lifespan=lifespan)

    @app.post("/ingest", response_model=BatchResponse)
    async def ingest(
        files: list[UploadFile] | None = File(default=None),
        text: str | None = Form(default=None),
    ) -> BatchResponse:
        service: IngestionService = app.state.ingestion_service
        return await service.ingest_batch(files=files, text=text)

    @app.post("/chat")
    async def chat(request: ChatRequest) -> StreamingResponse:
        service: QueryServiceProtocol | None = app.state.query_service
        if service is None:
            raise HTTPException(status_code=503, detail="Query service is not configured.")
        if not request.messages:
            raise HTTPException(status_code=400, detail="At least one message is required.")

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in service.stream_chat(
                    messages=[message.model_dump() for message in request.messages],
                    cwd=request.cwd,
                ):
                    yield chunk
            except Exception as exc:
                logger.exception("Chat request failed")
                yield (json.dumps({"type": "error", "message": str(exc)}) + "\n").encode("utf-8")
                yield (json.dumps({"type": "done"}) + "\n").encode("utf-8")

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    return app
