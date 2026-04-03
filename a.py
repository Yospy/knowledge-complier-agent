from __future__ import annotations

import json
from dataclasses import dataclass, field
import re
import sys
from typing import Iterable

import httpx


BACKEND_URL = "http://127.0.0.1:8000/chat"


@dataclass
class ChatState:
    messages: list[dict[str, str]] = field(default_factory=list)
    cwd: str = "/"


@dataclass
class StreamingTextBuffer:
    pending_text: str = ""
    displayed_text: str = ""

    def push(self, delta: str) -> list[str]:
        if not delta:
            return []
        self.pending_text += delta
        return self._drain_ready()

    def flush(self, force: bool = False) -> list[str]:
        return self._drain_ready(force=force)

    def _drain_ready(self, force: bool = False) -> list[str]:
        chunks: list[str] = []
        while self.pending_text:
            flush_at = self._flush_boundary(force=force)
            if flush_at == 0:
                break
            chunk = self.pending_text[:flush_at]
            self.pending_text = self.pending_text[flush_at:]
            self.displayed_text += chunk
            chunks.append(chunk)
            force = False
        return chunks

    def _flush_boundary(self, force: bool = False) -> int:
        text = self.pending_text
        if not text:
            return 0
        if force:
            return len(text)
        if text.endswith("\n"):
            return len(text)
        if re.search(r"[.!?:][\"')\]]?\s*$", text):
            return len(text)
        if len(text) >= 12 and text[-1].isspace():
            return len(text)
        if len(text) >= 40:
            last_space = text.rfind(" ")
            if last_space >= 12:
                return last_space + 1
            return len(text)
        return 0


def handle_local_command(state: ChatState, command: str) -> str | None:
    normalized = command.strip().lower()
    if normalized in {"/exit", "/quit"}:
        raise SystemExit(0)
    if normalized == "/clear":
        state.messages.clear()
        state.cwd = "/"
        return "Cleared local chat history and reset cwd to /."
    if normalized == "/help":
        return "Local commands: /help, /clear, /exit, /quit"
    return None


def iter_ndjson_lines(lines: Iterable[str]) -> Iterable[dict]:
    for line in lines:
        if not line:
            continue
        yield json.loads(line)


def suffix_from_final_message(displayed_text: str, final_message: str) -> str | None:
    if not final_message:
        return ""
    if not displayed_text:
        return final_message
    if final_message.startswith(displayed_text):
        return final_message[len(displayed_text) :]
    return None


def main() -> int:
    try:
        from rich.console import Console
    except ModuleNotFoundError:
        print("rich is required for Terminal CLI Chat. Install it with: pip install rich", file=sys.stderr)
        return 1

    console = Console()
    state = ChatState()

    console.print("[bold]Terminal CLI Chat[/bold]")
    console.print("[dim]Type /help for local commands.[/dim]")

    with httpx.Client(timeout=None) as client:
        while True:
            try:
                prompt = f"{state.cwd} > "
                user_input = console.input(f"[bold cyan]{prompt}[/bold cyan]")
            except (EOFError, KeyboardInterrupt):
                console.print()
                return 0

            if not user_input.strip():
                continue

            local_result = handle_local_command(state, user_input)
            if local_result is not None:
                console.print(f"[dim]{local_result}[/dim]")
                continue

            pending_messages = [*state.messages, {"role": "user", "content": user_input}]
            buffer = StreamingTextBuffer()
            started_answer = False

            try:
                with client.stream(
                    "POST",
                    BACKEND_URL,
                    json={"messages": pending_messages, "cwd": state.cwd},
                ) as response:
                    response.raise_for_status()
                    status = console.status("Thinking", spinner="dots")
                    status.start()
                    try:
                        for event in iter_ndjson_lines(response.iter_lines()):
                            event_type = event.get("type")
                            if event_type == "status":
                                if not started_answer:
                                    status.update(str(event.get("text", "Thinking")))
                            elif event_type == "tool_start":
                                display = str(event.get("display", "")).strip()
                                if display:
                                    console.print(f"[dim]{display}[/dim]")
                            elif event_type == "assistant_delta":
                                delta = str(event.get("delta", ""))
                                for chunk in buffer.push(delta):
                                    if not started_answer:
                                        status.stop()
                                        console.print("[bold green]Assistant[/bold green]: ", end="")
                                        started_answer = True
                                    console.print(
                                        chunk,
                                        end="",
                                        markup=False,
                                        highlight=False,
                                        soft_wrap=True,
                                    )
                            elif event_type == "assistant_final":
                                final_message = str(event.get("message", ""))
                                for chunk in buffer.flush(force=True):
                                    if not started_answer:
                                        status.stop()
                                        console.print("[bold green]Assistant[/bold green]: ", end="")
                                        started_answer = True
                                    console.print(
                                        chunk,
                                        end="",
                                        markup=False,
                                        highlight=False,
                                        soft_wrap=True,
                                    )

                                suffix = suffix_from_final_message(buffer.displayed_text, final_message)
                                if suffix is None:
                                    if started_answer:
                                        console.print()
                                    status.stop()
                                    if final_message:
                                        console.print(f"[bold green]Assistant[/bold green]: {final_message}")
                                    started_answer = bool(final_message)
                                elif suffix:
                                    if not started_answer:
                                        status.stop()
                                        console.print("[bold green]Assistant[/bold green]: ", end="")
                                        started_answer = True
                                    console.print(
                                        suffix,
                                        end="",
                                        markup=False,
                                        highlight=False,
                                        soft_wrap=True,
                                    )
                                    buffer.displayed_text = final_message

                                if started_answer:
                                    console.print()
                                state.messages = [*pending_messages, {"role": "assistant", "content": final_message}]
                                state.cwd = str(event.get("cwd", state.cwd))
                            elif event_type == "error":
                                status.stop()
                                console.print(f"[bold red]Error[/bold red]: {event.get('message', 'Unknown error')}")
                            elif event_type == "done":
                                break
                    finally:
                        status.stop()
            except SystemExit:
                raise
            except Exception as exc:
                console.print(f"[bold red]Request failed[/bold red]: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
