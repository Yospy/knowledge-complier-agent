from __future__ import annotations

import unittest

import a


class TerminalCliTests(unittest.TestCase):
    def test_clear_command_resets_state(self) -> None:
        state = a.ChatState(
            messages=[{"role": "user", "content": "hello"}],
            cwd="/wiki",
        )
        result = a.handle_local_command(state, "/clear")
        self.assertIn("Cleared", result or "")
        self.assertEqual([], state.messages)
        self.assertEqual("/", state.cwd)

    def test_iter_ndjson_lines_skips_empty_lines(self) -> None:
        events = list(a.iter_ndjson_lines(['{"type":"status"}', "", '{"type":"done"}']))
        self.assertEqual(["status", "done"], [event["type"] for event in events])

    def test_streaming_buffer_coalesces_small_fragments_until_whitespace(self) -> None:
        buffer = a.StreamingTextBuffer()
        chunks: list[str] = []
        for part in ["Y", "ash", " Wad", "gave ", "built ", "systems"]:
            chunks.extend(buffer.push(part))

        self.assertEqual(["Yash Wadgave "], chunks)
        self.assertEqual("Yash Wadgave ", buffer.displayed_text)
        self.assertEqual("built systems", buffer.pending_text)

    def test_streaming_buffer_flushes_at_sentence_boundary(self) -> None:
        buffer = a.StreamingTextBuffer()
        chunks = buffer.push("Built MCP servers. ")
        self.assertEqual(["Built MCP servers. "], chunks)
        self.assertEqual("", buffer.pending_text)

    def test_streaming_buffer_force_flushes_remainder(self) -> None:
        buffer = a.StreamingTextBuffer()
        buffer.push("Prompt")
        chunks = buffer.flush(force=True)
        self.assertEqual(["Prompt"], chunks)
        self.assertEqual("Prompt", buffer.displayed_text)
        self.assertEqual("", buffer.pending_text)

    def test_suffix_from_final_message_returns_missing_tail(self) -> None:
        suffix = a.suffix_from_final_message("Yash Wadgave ", "Yash Wadgave built systems")
        self.assertEqual("built systems", suffix)

    def test_suffix_from_final_message_returns_none_on_divergence(self) -> None:
        suffix = a.suffix_from_final_message("Yash Wadgave ", "Experience summary")
        self.assertIsNone(suffix)
