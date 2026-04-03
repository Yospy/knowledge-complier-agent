from __future__ import annotations

import tempfile
import unittest

from akc.agents.query import ReadOnlyShellSession
from akc.storage import AgentFSStore


class ReadOnlyShellSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AgentFSStore(f"{self.tempdir.name}/akc.db")
        await self.store.open()
        await self.store.write_text("/wiki/index.md", "# Knowledge Index\n")
        await self.store.write_text("/wiki/concepts/agentfs.md", "# AgentFS\n")
        await self.store.write_text("/raw/source.md", "AgentFS stores markdown files.\n")
        self.session = ReadOnlyShellSession(store=self.store, cwd="/")

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tempdir.cleanup()

    async def test_ls_lists_directories(self) -> None:
        entries = await self.session.ls("/")
        self.assertIn("wiki", entries)
        self.assertIn("raw", entries)

    async def test_cd_changes_working_directory(self) -> None:
        cwd = await self.session.cd("/wiki")
        self.assertEqual("/wiki", cwd)
        self.assertEqual("/wiki", self.session.cwd)

    async def test_cat_reads_file(self) -> None:
        content = await self.session.cat("/raw/source.md")
        self.assertIn("AgentFS", content)

    async def test_grep_searches_file_tree(self) -> None:
        matches = await self.session.grep("AgentFS", "/raw")
        self.assertEqual("/raw/source.md", matches[0].path)
        self.assertIn("AgentFS", matches[0].snippet)

    async def test_read_only_operations_do_not_create_files(self) -> None:
        before = await self.store.walk_files("/")
        await self.session.ls("/")
        await self.session.cd("/wiki")
        await self.session.cat("/wiki/index.md")
        await self.session.grep("AgentFS", "/raw")
        after = await self.store.walk_files("/")
        self.assertEqual(before, after)
