from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from akc.agents.ingestion import OpenAIIngestionCompiler
from akc.agents.types import NormalizedSource
from akc.storage import AgentFSStore


def concept_markdown(source_path: str) -> str:
    return (
        "# Sample Concept\n\n"
        "## Summary\n"
        "A concise explanation.\n\n"
        "## Key Ideas\n"
        "- Uses tools\n\n"
        "## Source\n"
        f"- File: {source_path}\n"
        "- Extracted At: 2026-04-03T00:00:00Z\n\n"
        "## Related Concepts\n"
        "- [[Related Concept]]\n"
    )


def index_markdown(source_path: str) -> str:
    return (
        "# Knowledge Index\n\n"
        "## Concepts\n"
        "- [[Sample Concept]]\n\n"
        "## Sources\n"
        f"- {source_path}\n\n"
        "## Recently Updated\n"
        "- [[Sample Concept]]\n"
    )


class OpenAIIngestionCompilerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AgentFSStore(f"{self.tempdir.name}/akc.db")
        await self.store.open()
        self.files_client = SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(id="file-123")),
            delete=AsyncMock(return_value=SimpleNamespace(id="file-123", deleted=True)),
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tempdir.cleanup()

    def _pdf_source(self) -> NormalizedSource:
        pdf_path = Path("/tmp/sample.pdf")
        return NormalizedSource(
            source_id="pdf-source",
            input_name="sample.pdf",
            input_type="pdf",
            raw_path="/raw/pdf-source.md",
            original_filename=pdf_path.name,
            original_bytes=b"%PDF-1.4 sample pdf bytes",
            mime_type="application/pdf",
        )

    async def test_build_input_items_uses_uploaded_pdf_file_id(self) -> None:
        compiler = OpenAIIngestionCompiler(
            store=self.store,
            model="gpt-5.1",
            files_client=self.files_client,
        )
        input_items = compiler._build_input_items(
            self._pdf_source(),
            uploaded_pdf_file_id="file-abc",
        )
        content = input_items[0]["content"]
        self.assertEqual("input_text", content[0]["type"])
        self.assertEqual({"type": "input_file", "file_id": "file-abc"}, content[1])

    async def test_compile_source_uploads_and_deletes_pdf_file_on_success(self) -> None:
        compiler = OpenAIIngestionCompiler(
            store=self.store,
            model="gpt-5.1",
            files_client=self.files_client,
        )
        source = self._pdf_source()

        async def fake_run(_agent, *, input, context, max_turns):
            self.assertEqual("input_file", input[0]["content"][1]["type"])
            self.assertEqual("file-123", input[0]["content"][1]["file_id"])
            await context.store.write_file(source.raw_path, "# Sample PDF Raw\n")
            await context.store.write_file(
                "/wiki/concepts/sample-concept.md",
                concept_markdown(source.raw_path),
            )
            await context.store.write_file("/wiki/index.md", index_markdown(source.raw_path))
            context.raw_written = True
            context.concepts_written = ["sample-concept"]
            context.index_updated = True
            context.store.finalize()
            return SimpleNamespace(final_output="done")

        with patch("akc.agents.ingestion.Runner.run", side_effect=fake_run) as runner_run:
            outcome = await compiler.compile_source(source)

        self.assertTrue(outcome.success)
        self.assertTrue(await self.store.exists(source.raw_path))
        self.assertTrue(await self.store.exists("/wiki/index.md"))
        self.files_client.create.assert_awaited_once()
        self.files_client.delete.assert_awaited_once_with("file-123")
        self.assertEqual(1, runner_run.await_count)

    async def test_compile_source_deletes_uploaded_pdf_after_runner_failure(self) -> None:
        compiler = OpenAIIngestionCompiler(
            store=self.store,
            model="gpt-5.1",
            files_client=self.files_client,
        )
        source = self._pdf_source()

        with patch("akc.agents.ingestion.Runner.run", new=AsyncMock(side_effect=RuntimeError("boom"))):
            outcome = await compiler.compile_source(source)

        self.assertFalse(outcome.success)
        self.assertIn("boom", outcome.error or "")
        self.files_client.create.assert_awaited_once()
        self.files_client.delete.assert_awaited_once_with("file-123")

    async def test_compile_source_returns_failure_when_pdf_upload_fails(self) -> None:
        failing_files_client = SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError("upload failed")),
            delete=AsyncMock(),
        )
        compiler = OpenAIIngestionCompiler(
            store=self.store,
            model="gpt-5.1",
            files_client=failing_files_client,
        )

        outcome = await compiler.compile_source(self._pdf_source())

        self.assertFalse(outcome.success)
        self.assertEqual(False, outcome.raw_written)
        self.assertIn("upload failed", outcome.error or "")
        failing_files_client.delete.assert_not_called()
