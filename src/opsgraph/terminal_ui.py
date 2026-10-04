"""Readable terminal output with a plain-text fallback and no runtime dependencies."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO


def safe_text(value: str) -> str:
    """Keep untrusted labels from issuing terminal commands or hiding text."""
    return "".join(
        char
        for char in str(value)
        if char == "\n" or not unicodedata.category(char).startswith("C")
    )


class TerminalUI:
    def __init__(
        self,
        output: Callable[[str], object] | None = None,
        *,
        stream: TextIO | None = None,
        width: int | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.output = output or (lambda value: print(value, file=self.stream, flush=True))
        self.rich = (
            output is None
            and self.stream.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        )
        self.width = max(24, min(width or shutil.get_terminal_size((80, 24)).columns, 88))

    def style(self, text: str, code: str = "1;36") -> str:
        text = safe_text(text)
        return f"\033[{code}m{text}\033[0m" if self.rich else text

    def write(self, text: str) -> None:
        for line in safe_text(text).split("\n"):
            for wrapped in textwrap.wrap(line, self.width) or [""]:
                self.output(wrapped)

    def heading(self, title: str) -> None:
        self.output("")
        self.output(self.style("┌" if self.rich else "+", "36"))
        for line in textwrap.wrap(safe_text(title), self.width):
            self.output(self.style(line))
        self.output(self.style(("─" if self.rich else "-") * min(self.width, 40), "36"))

    def note(self, title: str, lines: tuple[str, ...]) -> None:
        """Show a small help card instead of an uninterrupted documentation dump."""
        self.output("")
        for line in textwrap.wrap(safe_text(title), self.width):
            self.output(self.style(line, "1;36"))
        rail = "│" if self.rich else "|"
        for text in lines:
            for line in textwrap.wrap(safe_text(text), self.width - 4) or [""]:
                self.output(self.style(rail, "36") + "  " + line)
        self.output(self.style("└" if self.rich else "+", "36"))

    def literal(self, text: str) -> None:
        """Preserve a copyable example without inserting line breaks into its value."""
        self.output("  " + safe_text(text))

    def question(self, label: str) -> str:
        lines = textwrap.wrap(safe_text(label).strip().removesuffix(":"), self.width - 4)
        return "\n" + self.style("\n".join("  " + line for line in lines), "1") + "\n  > "

    def menu(self, choices: tuple[tuple[str, str], ...], default: str) -> None:
        for index, (value, label) in enumerate(choices, 1):
            suffix = " [default]" if value == default else ""
            marker = "›" if self.rich and value == default else " "
            self.write(f"{marker} {index}. {label}{suffix}")
        self.write("Type a number, then press Enter.")

    @contextmanager
    def progress(self, label: str) -> Iterator[None]:
        """Report completed operations only after their subprocess exits successfully."""
        stop = threading.Event()

        def animate() -> None:
            index = 0
            while not stop.wait(0.12):
                frame = "|/-\\"[index % 4]
                message = f"[{frame}] {safe_text(label)}"[: self.width]
                self.stream.write("\r" + self.style(message))
                self.stream.flush()
                index += 1

        worker = None
        if self.rich and not os.environ.get("CI"):
            worker = threading.Thread(target=animate, daemon=True)
            worker.start()
        else:
            self.write(f"[working] {label}")
        outcome = "stopped"
        try:
            yield
            outcome = "done"
        finally:
            stop.set()
            if worker is not None:
                worker.join()
                self.stream.write("\r" + " " * self.width + "\r")
            self.output(self.style(f"[{outcome}] {label}", "1;32" if outcome == "done" else "1;31"))
