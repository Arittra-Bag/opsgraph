import io
import re

import pytest

from opsgraph.terminal_ui import TerminalUI, safe_text


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


def test_narrow_plain_output_wraps_and_keeps_prompts_distinct():
    output = []
    ui = TerminalUI(output.append, width=32)
    ui.heading("Step 1 of 3: Your database")
    ui.write("Paste your read-only connection string. You can skip it and connect later.")
    ui.menu((("local", "Local PostgreSQL"), ("neon", "Neon")), "local")
    question = ui.question("Where does your database run?")
    assert all(len(line) <= 32 for line in output)
    assert "[default]" in "\n".join(output)
    assert question.endswith("\n  > ")
    assert "\x1b" not in "\n".join(output) + question


@pytest.mark.parametrize("plain_mode", ["redirected", "no_color", "dumb"])
def test_terminal_decoration_respects_plain_output(monkeypatch, plain_mode):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = io.StringIO() if plain_mode == "redirected" else TerminalStream()
    if plain_mode == "no_color":
        monkeypatch.setenv("NO_COLOR", "")
    if plain_mode == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    ui = TerminalUI(stream=stream)
    ui.heading("Your database")
    with ui.progress("Installing software"):
        pass
    assert "\x1b" not in stream.getvalue()
    assert "[done] Installing software" in stream.getvalue()


def test_interactive_heading_and_question_have_visible_emphasis(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = TerminalStream()
    ui = TerminalUI(stream=stream)
    ui.heading("Step 2 of 3: Your model service")
    assert "\x1b[1;36m" in stream.getvalue()
    assert "\x1b[1m" in ui.question("Model name")


def test_failed_progress_does_not_claim_completion():
    output = []
    ui = TerminalUI(output.append)
    with pytest.raises(RuntimeError), ui.progress("Installing software"):
        raise RuntimeError("fixture-only failure")
    assert output[-1] == "[stopped] Installing software"
    assert not any("done" in value for value in output)


def test_untrusted_labels_cannot_issue_terminal_control_sequences():
    value = "safe\x1b[2J\x00\r\u202ehidden"
    result = safe_text(value)
    assert not re.search(r"[\x00-\x1f\x7f\u202e]", result)
    assert "safe" in result and "hidden" in result


def test_help_card_wraps_but_connection_example_remains_copyable():
    output = []
    ui = TerminalUI(output.append, width=32)
    ui.note(
        "DigitalOcean PostgreSQL: get your connection",
        ("1. Open your project and copy the connection string.",),
    )
    assert all(len(line) <= 32 for line in output)
    example = "postgresql://READ_ONLY_LOGIN:YOUR_PASSWORD@127.0.0.1:5432/YOUR_DATABASE"
    ui.literal(example)
    assert output[-1] == "  " + example
