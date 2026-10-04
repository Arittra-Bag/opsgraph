import io

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from opsgraph.terminal_screen import SetupScreen


def screen_session(keys, *, size=(24, 80), secret=False, choices=(), default=""):
    stream = io.StringIO()
    output = Vt100_Output(stream, lambda: Size(*size), enable_cpr=False)
    with create_pipe_input() as input:
        screen = SetupScreen(
            "Step 1 of 3: Your database",
            ["Use a read-only login.", "An example and instructions remain scrollable."],
            "Paste connection string" if secret else "Choose your database host",
            secret=secret,
            choices=choices,
            default=default,
            input=input,
            output=output,
        )
        input.send_text(keys)
        result = screen.run()
        assert screen.field.text == ""
    return result, stream.getvalue()


CHOICES = (("1", "Local PostgreSQL"), ("2", "Supabase"), ("3", "Neon"))


@pytest.mark.parametrize(
    "keys,expected",
    [("\x1b[B\r", "2"), ("\x1b[A\r", "3"), ("3", "3"), ("?", "?"), ("\r", "1")],
)
def test_keyboard_selection_and_visible_help(keys, expected):
    result, output = screen_session(keys, choices=CHOICES, default="1")
    assert result == expected
    assert "OpsGraph" in output
    assert "\x1b[?1049l" in output


def test_secret_paste_is_masked_and_buffer_cleared():
    value = "fixture-only-password-never-render"
    result, output = screen_session("\x1b[200~" + value + "\x1b[201~\r", secret=True)
    assert result == value
    assert value not in output
    assert "fixture-only" not in output


def test_render_failure_has_safe_recovery_message_and_no_driver_details(monkeypatch):
    from opsgraph.terminal_screen import ask_screen
    from opsgraph.terminal_ui import TerminalScreenError

    def fail(self):
        raise RuntimeError("fixture-private-render-detail")

    monkeypatch.setattr(SetupScreen, "run", fail)
    with pytest.raises(TerminalScreenError, match="OPSGRAPH_PLAIN=1") as result:
        ask_screen("Setup", [], "Key", secret=True)
    assert "fixture-private-render-detail" not in str(result.value)


def test_cancellation_clears_secret_and_restores_terminal():
    with create_pipe_input() as input:
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(24, 80), enable_cpr=False)
        screen = SetupScreen("Setup", [], "Key", secret=True, input=input, output=output)
        input.send_text("fixture-secret\x03")
        with pytest.raises(KeyboardInterrupt):
            screen.run()
        assert screen.field.text == ""
        assert "fixture-secret" not in stream.getvalue()
        assert "\x1b[?1049l" in stream.getvalue()


@pytest.mark.parametrize("size", [(20, 52), (24, 80), (32, 120)])
def test_layout_fits_supported_sizes_and_scrolls_long_menus(size):
    choices = tuple((str(n), f"Database provider {n}") for n in range(1, 12))
    result, output = screen_session("\x1b[A\r", size=size, choices=choices)
    assert result == "11"
    assert "Window too small" not in output


def test_scroll_help_and_tab_focus_can_return_to_menu():
    result, output = screen_session("\x1b[6~\t\x1b[Z\x1b[B\r", choices=CHOICES)
    assert result == "2"
    assert "Window too small" not in output


def test_small_terminal_shows_resize_guidance_and_keeps_cancel_available():
    with create_pipe_input() as input:
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(16, 45), enable_cpr=False)
        screen = SetupScreen("Setup", [], "Select", choices=CHOICES, input=input, output=output)
        input.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            screen.run()
        assert "Resize your terminal" in stream.getvalue()
        assert "Window too small" not in stream.getvalue()


def test_form_borders_stay_aligned_with_long_guidance():
    with create_pipe_input() as input:
        output = Vt100_Output(io.StringIO(), lambda: Size(24, 80), enable_cpr=False)
        screen = SetupScreen(
            "Your database",
            ["A detailed explanation. " * 20],
            "Choose",
            choices=CHOICES,
            input=input,
            output=output,
        )

        @screen.keys.add("c-p")
        def inspect_frame(event):
            frame = screen.app.renderer.last_rendered_screen
            assert all(frame.data_buffer[y][77].char == "│" for y in range(2, 21))
            event.app.exit(result="checked")

        input.send_text("\x10")
        assert screen.run() == "checked"


def test_screen_wizard_cancellation_uses_existing_validation_and_save_boundary(
    tmp_path, monkeypatch
):
    from opsgraph import setup
    from opsgraph.terminal_ui import TerminalUI

    original = TerminalUI.__init__
    screens = []

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.screen = True

    def ask_screen(title, context, label, **kwargs):
        screens.append((title, label, kwargs.get("choices")))
        keys = "3" if label.startswith("Connection options") else "\r"
        if label.startswith("Save configuration?"):
            keys = "2"
        assert not kwargs.get("secret"), "Skipped database and Ollama need no credential entry"
        with create_pipe_input() as input:
            output = Vt100_Output(io.StringIO(), lambda: Size(24, 80), enable_cpr=False)
            screen = SetupScreen(title, context, label, input=input, output=output, **kwargs)
            input.send_text(keys)
            return screen.run()

    monkeypatch.setattr(TerminalUI, "__init__", init)
    monkeypatch.setattr("opsgraph.terminal_screen.ask_screen", ask_screen)
    monkeypatch.setattr("psycopg.connect", lambda *_a, **_k: pytest.fail("Unexpected network"))
    output = []
    assert setup.run_setup(tmp_path / "workspace", flow="quick", output_fn=output.append) == 1
    assert not (tmp_path / "workspace" / ".env").exists()
    assert any("Step 3" in title for title, _, _ in screens)
    assert "cancelled" in " ".join(output).lower()
