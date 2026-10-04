"""Shared setup presentation with a dependency-free installer and plain-text fallback."""

from __future__ import annotations

import getpass
import os
import shutil
import sys
import textwrap
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO


class TerminalScreenError(RuntimeError):
    """The interactive screen stopped before it could return a choice."""


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
        full_screen: bool = True,
    ) -> None:
        self.stream = stream or sys.stdout
        self.output = output or (lambda value: print(value, file=self.stream, flush=True))
        self.rich = (
            output is None
            and self.stream.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
            and os.environ.get("OPSGRAPH_PLAIN") != "1"
        )
        self.width = max(24, min(width or shutil.get_terminal_size((80, 24)).columns, 88))
        size = shutil.get_terminal_size((80, 24))
        self.screen = bool(
            full_screen
            and self.rich
            and sys.stdin.isatty()
            and not os.environ.get("CI")
            and os.environ.get("OPSGRAPH_PLAIN") != "1"
            and size.columns >= 52
            and size.lines >= 20
        )
        self.title = "Your workspace"
        self.context: list[str] = []
        self.choices: tuple[tuple[str, str], ...] = ()
        self.default = ""
        self.last_label = ""
        self.new_menu = False

    def style(self, text: str, code: str = "1;36") -> str:
        text = safe_text(text)
        return f"\033[{code}m{text}\033[0m" if self.rich else text

    def write(self, text: str) -> None:
        if self.screen:
            self.context.append(safe_text(text))
            return
        for line in safe_text(text).split("\n"):
            for wrapped in textwrap.wrap(line, self.width) or [""]:
                self.output(wrapped)

    def heading(self, title: str) -> None:
        self.title = safe_text(title)
        self.context.clear()
        self.choices = ()
        if self.screen:
            return
        self.output("")
        if self.rich:
            self.output(self.style("  OpsGraph  ", "1;30;46") + self.style("  SETUP  ", "1;35"))
            self.output("")
        for line in textwrap.wrap(self.title, self.width - 4):
            self.output(self.style("  " + line))
        self.output(self.style(("─" if self.rich else "-") * min(self.width, 56), "35"))

    def note(self, title: str, lines: tuple[str, ...]) -> None:
        """Show a small help card instead of an uninterrupted documentation dump."""
        if self.screen:
            self.context.append(safe_text(title) + "\n" + "\n\n".join(map(safe_text, lines)))
            return
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
        if self.screen:
            self.context.append(safe_text(text))
            return
        self.output("  " + safe_text(text))

    def question(self, label: str) -> str:
        lines = textwrap.wrap(safe_text(label).strip().removesuffix(":"), self.width - 4)
        return "\n" + self.style("\n".join("  " + line for line in lines), "1") + "\n  > "

    def menu(self, choices: tuple[tuple[str, str], ...], default: str) -> None:
        self.choices = tuple((str(index), label) for index, (_, label) in enumerate(choices, 1))
        self.default = next(
            (str(index) for index, (value, _) in enumerate(choices, 1) if value == default), "1"
        )
        self.new_menu = True
        if self.screen:
            return
        for index, (value, label) in enumerate(choices, 1):
            suffix = " [default]" if value == default else ""
            marker = "›" if self.rich and value == default else " "
            self.write(f"{marker} {index}. {label}{suffix}")
        self.write("Type a number, then press Enter.")

    def ask(self, label: str, *, secret: bool = False) -> str:
        if self.screen:
            try:
                from opsgraph.terminal_screen import ask_screen
            except ImportError:
                self.flush()
                self.screen = False
                if not secret:
                    for value, text in self.choices:
                        self.write(f"  {value}. {text}")
            else:
                if secret or (label != self.last_label and not self.new_menu):
                    self.choices = ()
                self.last_label = label
                self.new_menu = False
                choices, default = self.choices, self.default
                if not secret and ("[y/N]" in label or "[Y/n]" in label):
                    choices = (("yes", "Yes"), ("no", "No"))
                    default = "yes" if "[Y/n]" in label else "no"
                try:
                    return ask_screen(
                        self.title,
                        self.context,
                        label,
                        choices=choices,
                        default=default,
                        secret=secret,
                        allow_help=label.startswith("Connection options:")
                        or any("Type ? for help" in text for text in self.context),
                    )
                finally:
                    self.context.clear()
        return getpass.getpass(self.question(label)) if secret else input(self.question(label))

    def flush(self) -> None:
        """Show final status after leaving setup, without retaining entered credentials."""
        pending, self.context = self.context, []
        for text in pending:
            for line in safe_text(text).split("\n"):
                for wrapped in textwrap.wrap(line, self.width) or [""]:
                    self.output(wrapped)

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
