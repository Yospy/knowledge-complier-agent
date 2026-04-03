from __future__ import annotations

import tempfile
import unittest

from akc.agents.ingestion import (
    IngestionStageSession,
    validate_concept_content,
    validate_index_content,
)
from akc.agents.types import NormalizedSource
from akc.storage import AgentFSStore, SourceHashRecord


class AgentFSStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AgentFSStore(f"{self.tempdir.name}/akc.db")
        await self.store.open()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tempdir.cleanup()

    async def test_layout_created_on_open(self) -> None:
        self.assertTrue(await self.store.exists("/raw"))
        self.assertTrue(await self.store.exists("/wiki"))
        self.assertTrue(await self.store.exists("/wiki/concepts"))
        self.assertTrue(await self.store.exists("/logs"))

    async def test_recursive_search_returns_snippets(self) -> None:
        await self.store.write_text(
            "/wiki/concepts/agentic-coding.md",
            "# Agentic Coding\nAgentic systems use tools.\n",
        )
        await self.store.write_text("/wiki/concepts/storage.md", "# Storage\nNo match here.\n")
        matches = await self.store.search_files("tools", "/wiki")
        self.assertEqual(1, len(matches))
        self.assertEqual("/wiki/concepts/agentic-coding.md", matches[0].path)

    async def test_source_hash_record_round_trip(self) -> None:
        record = SourceHashRecord(
            sha256="abc123",
            source_id="source-1",
            input_name="mock.md",
            input_type="markdown",
            raw_path="/raw/source-1.md",
            ingested_at="2026-04-03T00:00:00Z",
        )
        await self.store.write_source_hash_record(record)
        loaded = await self.store.read_source_hash_record("abc123")
        self.assertEqual(record, loaded)


class IngestionStageSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AgentFSStore(f"{self.tempdir.name}/akc.db")
        await self.store.open()
        self.source = NormalizedSource(
            source_id="source-1",
            input_name="mock.md",
            input_type="markdown",
            raw_path="/raw/source-1.md",
            text_content="hello",
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tempdir.cleanup()

    async def test_requires_raw_before_concepts(self) -> None:
        session = IngestionStageSession(store=self.store, source=self.source)
        with self.assertRaises(ValueError):
            await session.write_file(
                "/wiki/concepts/agentfs.md",
                "# AgentFS\n\n## Summary\nx\n\n## Key Ideas\n- a\n\n## Source\n- File: /raw/source-1.md\n- Extracted At: t\n\n## Related Concepts\n- [[X]]\n",
            )

    async def test_rejects_rewrite_of_same_path(self) -> None:
        session = IngestionStageSession(store=self.store, source=self.source)
        await session.write_file("/raw/source-1.md", "# mock\n")
        await session.write_file(
            "/wiki/concepts/agentfs.md",
            "# AgentFS\n\n## Summary\nx\n\n## Key Ideas\n- a\n\n## Source\n- File: /raw/source-1.md\n- Extracted At: t\n\n## Related Concepts\n- [[X]]\n",
        )
        with self.assertRaises(ValueError):
            await session.write_file(
                "/wiki/concepts/agentfs.md",
                "# AgentFS\n\n## Summary\ny\n\n## Key Ideas\n- b\n\n## Source\n- File: /raw/source-1.md\n- Extracted At: t\n\n## Related Concepts\n- [[X]]\n",
            )

    async def test_normalizes_human_readable_concept_paths(self) -> None:
        session = IngestionStageSession(store=self.store, source=self.source)
        await session.write_file("/raw/source-1.md", "# mock\n")
        canonical = await session.write_file(
            "/wiki/concepts/AgentFS Platform.md",
            "# AgentFS Platform\n\n## Summary\nx\n\n## Key Ideas\n- a\n\n## Source\n- File: /raw/source-1.md\n- Extracted At: t\n\n## Related Concepts\n- [[X]]\n",
        )
        self.assertEqual("/wiki/concepts/agentfs-platform.md", canonical)
        self.assertEqual(["agentfs-platform"], session.concepts_written)

    async def test_finalize_requires_index_last(self) -> None:
        session = IngestionStageSession(store=self.store, source=self.source)
        await session.write_file("/raw/source-1.md", "# mock\n")
        await session.write_file(
            "/wiki/concepts/agentfs.md",
            "# AgentFS\n\n## Summary\nx\n\n## Key Ideas\n- a\n\n## Source\n- File: /raw/source-1.md\n- Extracted At: t\n\n## Related Concepts\n- [[X]]\n",
        )
        await session.write_file(
            "/wiki/index.md",
            "# Knowledge Index\n\n## Concepts\n- [[AgentFS]]\n\n## Sources\n- /raw/source-1.md\n\n## Recently Updated\n- [[AgentFS]]\n",
        )
        result = session.finalize()
        self.assertTrue(result.raw_written)
        self.assertEqual(["agentfs"], result.concepts_written)
        self.assertTrue(result.index_updated)

    async def test_staged_writes_do_not_persist_until_commit(self) -> None:
        session = IngestionStageSession(store=self.store, source=self.source)
        await session.write_file("/raw/source-1.md", "# mock\n")
        self.assertFalse(await self.store.exists("/raw/source-1.md"))
        await session.write_file(
            "/wiki/concepts/agentfs.md",
            "# AgentFS\n\n## Summary\nx\n\n## Key Ideas\n- a\n\n## Source\n- File: /raw/source-1.md\n- Extracted At: t\n\n## Related Concepts\n- [[X]]\n",
        )
        await session.write_file(
            "/wiki/index.md",
            "# Knowledge Index\n\n## Concepts\n- [[AgentFS]]\n\n## Sources\n- /raw/source-1.md\n\n## Recently Updated\n- [[AgentFS]]\n",
        )
        session.finalize()
        await session.commit()
        self.assertTrue(await self.store.exists("/raw/source-1.md"))
        self.assertTrue(await self.store.exists("/wiki/index.md"))


class ValidationTests(unittest.TestCase):
    def test_valid_index_content_passes(self) -> None:
        validate_index_content(
            "# Knowledge Index\n\n"
            "## Concepts\n"
            "- [[Agentic Coding]]\n\n"
            "## Sources\n"
            "- /raw/source.md\n\n"
            "## Recently Updated\n"
            "- [[Agentic Coding]]\n"
        )

    def test_index_rejects_prose(self) -> None:
        with self.assertRaises(ValueError):
            validate_index_content(
                "# Knowledge Index\n\n"
                "## Concepts\n"
                "This is prose.\n\n"
                "## Sources\n"
                "- /raw/source.md\n\n"
                "## Recently Updated\n"
                "- [[Agentic Coding]]\n"
            )

    def test_valid_concept_content_passes(self) -> None:
        validate_concept_content(
            "# Agentic Coding\n\n"
            "## Summary\n"
            "A concise explanation.\n\n"
            "## Key Ideas\n"
            "- Uses tools\n\n"
            "## Source\n"
            "- File: /raw/source.md\n"
            "- Extracted At: 2026-04-03T00:00:00Z\n\n"
            "## Related Concepts\n"
            "- [[Tool Use]]\n"
        )

    def test_concept_requires_sections(self) -> None:
        with self.assertRaises(ValueError):
            validate_concept_content("# Missing Sections\n")
