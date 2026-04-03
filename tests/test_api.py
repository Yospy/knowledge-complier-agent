from __future__ import annotations

import json
from io import BytesIO
import tempfile
import unittest

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from akc.agents.types import CompileOutcome, NormalizedSource
from akc.api.app import IngestionService, create_app
from akc.storage import AgentFSStore


class FakeCompiler:
    def __init__(self, fail_for: str | None = None) -> None:
        self.fail_for = fail_for
        self.seen_sources: list[NormalizedSource] = []

    async def compile_source(self, source: NormalizedSource) -> CompileOutcome:
        self.seen_sources.append(source)
        if self.fail_for and self.fail_for == source.input_name:
            return CompileOutcome(
                success=False,
                raw_written=True,
                concepts_written=["agentic-coding"],
                index_updated=False,
                final_output="",
                error="simulated compile failure",
            )
        return CompileOutcome(
            success=True,
            raw_written=True,
            concepts_written=["agentic-coding"],
            index_updated=True,
            final_output="ok",
            error=None,
        )


class FakeQueryService:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, str]], str]] = []

    async def stream_chat(self, messages: list[dict[str, str]], cwd: str):
        self.calls.append((messages, cwd))
        for event in [
            {"type": "status", "text": "Thinking"},
            {"type": "tool_start", "tool": "cat", "display": "Reading /wiki/index.md", "cwd": cwd},
            {"type": "assistant_delta", "delta": "Hello"},
            {"type": "assistant_final", "message": "Hello", "cwd": "/wiki"},
            {"type": "done"},
        ]:
            yield (json.dumps(event) + "\n").encode("utf-8")


class IngestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AgentFSStore(f"{self.tempdir.name}/akc.db")
        self.compiler = FakeCompiler()
        self.query_service = FakeQueryService()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _client(self) -> TestClient:
        service = IngestionService(store=self.store, compiler=self.compiler)
        app = create_app(service_override=service, query_service_override=self.query_service)
        return TestClient(app)

    def test_ingest_requires_at_least_one_input(self) -> None:
        with self._client() as client:
            response = client.post("/ingest", data={})
        self.assertEqual(400, response.status_code)

    def test_text_input_creates_success_result(self) -> None:
        with self._client() as client:
            response = client.post("/ingest", data={"text": "Agentic systems use tools."})
        payload = response.json()
        self.assertEqual("success", payload["batch_status"])
        self.assertEqual(1, len(payload["results"]))
        self.assertEqual("success", payload["results"][0]["status"])
        self.assertEqual("text", payload["results"][0]["input_type"])
        self.assertTrue(payload["results"][0]["raw_written"])
        self.assertTrue(payload["results"][0]["index_updated"])
        self.assertIsNotNone(payload["results"][0]["content_sha256"])

    def test_partial_success_for_mixed_batch(self) -> None:
        self.compiler.fail_for = "bad.txt"
        with self._client() as client:
            response = client.post(
                "/ingest",
                files=[
                    ("files", ("good.md", b"# Good Source", "text/markdown")),
                    ("files", ("bad.txt", b"will fail", "text/plain")),
                ],
            )
        payload = response.json()
        self.assertEqual("partial_success", payload["batch_status"])
        self.assertEqual(["success", "failure"], [item["status"] for item in payload["results"]])
        self.assertEqual(["agentic-coding"], payload["results"][1]["concepts_written"])
        self.assertTrue(payload["results"][1]["raw_written"])
        self.assertFalse(payload["results"][1]["index_updated"])
        self.assertIsNotNone(payload["results"][0]["content_sha256"])

    def test_unsupported_file_does_not_abort_batch(self) -> None:
        with self._client() as client:
            response = client.post(
                "/ingest",
                files=[
                    ("files", ("good.md", b"# Good Source", "text/markdown")),
                    ("files", ("archive.zip", b"zip-bytes", "application/zip")),
                ],
            )
        payload = response.json()
        self.assertEqual("partial_success", payload["batch_status"])
        self.assertEqual(["success", "failure"], [item["status"] for item in payload["results"]])

    def test_pdf_upload_normalizes_to_raw_markdown(self) -> None:
        buffer = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=144)
        writer.write(buffer)
        pdf_bytes = buffer.getvalue()
        with self._client() as client:
            response = client.post(
                "/ingest",
                files=[("files", ("doc.pdf", pdf_bytes, "application/pdf"))],
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual("success", response.json()["batch_status"])
        source = self.compiler.seen_sources[0]
        self.assertEqual("pdf", source.input_type)
        self.assertIsNotNone(source.original_bytes)
        self.assertIsNone(source.text_content)

    def test_duplicate_file_upload_skips_reingest(self) -> None:
        file_bytes = b"# Same Source"
        with self._client() as client:
            first = client.post(
                "/ingest",
                files=[("files", ("same.md", file_bytes, "text/markdown"))],
            )
            second = client.post(
                "/ingest",
                files=[("files", ("same-again.md", file_bytes, "text/markdown"))],
            )
        first_payload = first.json()
        second_payload = second.json()
        self.assertEqual("success", first_payload["batch_status"])
        self.assertEqual("success", second_payload["batch_status"])
        self.assertEqual("success", first_payload["results"][0]["status"])
        self.assertEqual("duplicate", second_payload["results"][0]["status"])
        self.assertEqual(1, len(self.compiler.seen_sources))
        self.assertEqual(
            first_payload["results"][0]["source_id"],
            second_payload["results"][0]["duplicate_of_source_id"],
        )
        self.assertEqual(
            first_payload["results"][0]["content_sha256"],
            second_payload["results"][0]["content_sha256"],
        )

    def test_duplicate_text_input_skips_reingest(self) -> None:
        with self._client() as client:
            first = client.post("/ingest", data={"text": "Repeated text"})
            second = client.post("/ingest", data={"text": "Repeated text"})
        first_payload = first.json()
        second_payload = second.json()
        self.assertEqual("success", second_payload["batch_status"])
        self.assertEqual("duplicate", second_payload["results"][0]["status"])
        self.assertEqual(1, len(self.compiler.seen_sources))
        self.assertEqual(
            first_payload["results"][0]["source_id"],
            second_payload["results"][0]["duplicate_of_source_id"],
        )

    def test_duplicate_and_failure_batch_is_partial_success(self) -> None:
        self.compiler.fail_for = "bad.txt"
        with self._client() as client:
            first = client.post(
                "/ingest",
                files=[("files", ("same.md", b"# Same Source", "text/markdown"))],
            )
            self.assertEqual("success", first.json()["batch_status"])
            second = client.post(
                "/ingest",
                files=[
                    ("files", ("same-duplicate.md", b"# Same Source", "text/markdown")),
                    ("files", ("bad.txt", b"will fail", "text/plain")),
                ],
            )
        payload = second.json()
        self.assertEqual("partial_success", payload["batch_status"])
        self.assertEqual(["duplicate", "failure"], [item["status"] for item in payload["results"]])

    def test_chat_streams_ndjson_events(self) -> None:
        with self._client() as client:
            with client.stream(
                "POST",
                "/chat",
                json={"messages": [{"role": "user", "content": "hello"}], "cwd": "/"},
            ) as response:
                self.assertEqual(200, response.status_code)
                events = [json.loads(line) for line in response.iter_lines() if line]

        self.assertEqual(
            ["status", "tool_start", "assistant_delta", "assistant_final", "done"],
            [event["type"] for event in events],
        )
        self.assertEqual("/wiki", events[-2]["cwd"])
        self.assertEqual("/", self.query_service.calls[0][1])

    def test_chat_requires_message(self) -> None:
        with self._client() as client:
            response = client.post("/chat", json={"messages": [], "cwd": "/"})
        self.assertEqual(400, response.status_code)
