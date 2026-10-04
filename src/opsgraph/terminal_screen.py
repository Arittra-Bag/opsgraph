"""Keyboard-first setup screens. Configuration decisions stay in the existing wizard."""

from __future__ import annotations

from collections.abc import Sequence

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box, Button, Frame, TextArea

from opsgraph.terminal_ui import TerminalScreenError, safe_text

THEME = Style.from_dict(
    {
        "": "bg:#101624 #e5edf8",
        "brand": "bg:#161f32 #6fe4d3 bold",
        "badge": "bg:#24304b #bbadff bold",
        "sidebar": "bg:#161f32 #bcc9de",
        "active-step": "#6fe4d3 bold",
        "muted": "#a6b7d0",
        "frame.border": "#465b79",
        "frame.label": "#c1afff bold",
        "question": "#f1f5fc bold",
        "choice": "#cfdaed",
        "selected": "bg:#293e52 #8bf4dc bold",
        "text-area": "bg:#101624 #e5edf8",
        "input": "bg:#22334a #ffffff",
        "button": "bg:#273651 #e5edf8",
        "button.focused": "bg:#6fe4d3 #101624 bold",
        "scrollbar.background": "bg:#161f32",
        "scrollbar.button": "bg:#516583",
        "footer": "bg:#161f32 #aebfd7",
    }
)


class SetupScreen:
    def __init__(
        self,
        title: str,
        context: Sequence[str],
        label: str,
        *,
        choices: Sequence[tuple[str, str]] = (),
        default: str = "",
        secret: bool = False,
        allow_help: bool = True,
        input=None,
        output=None,
    ) -> None:
        self.choices = tuple((str(value), safe_text(text)) for value, text in choices)
        self.selected = next(
            (index for index, (value, _) in enumerate(self.choices) if value == default), 0
        )
        self.secret = secret
        self.field = TextArea(
            multiline=False, password=secret, style="class:input", height=1, history=DummyHistory()
        )
        self.notes = TextArea(
            text="\n\n".join(safe_text(item) for item in context),
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            focusable=True,
            height=Dimension(min=1, preferred=5, max=14),
        )
        self.keys = KeyBindings()

        @self.keys.add("c-c")
        @self.keys.add("escape")
        def cancel(event):
            event.app.exit(exception=KeyboardInterrupt())

        self.keys.add("tab")(focus_next)
        self.keys.add("s-tab")(focus_previous)

        @self.keys.add("pageup")
        def scroll_up(event):
            self.notes.buffer.cursor_up(count=5)

        @self.keys.add("pagedown")
        def scroll_down(event):
            self.notes.buffer.cursor_down(count=5)

        if self.choices:
            self.choice_control = FormattedTextControl(
                self.choice_text,
                focusable=True,
                get_cursor_position=lambda: Point(0, self.selected),
            )
            control = Window(
                self.choice_control,
                height=Dimension(min=2, preferred=len(self.choices)),
                wrap_lines=True,
                right_margins=[ScrollbarMargin()],
            )
            focused = self.choice_control
            selected_filter = Condition(lambda: self.app.layout.has_focus(self.choice_control))

            @self.keys.add("up", filter=selected_filter)
            def previous(event):
                self.selected = (self.selected - 1) % len(self.choices)

            @self.keys.add("down", filter=selected_filter)
            def following(event):
                self.selected = (self.selected + 1) % len(self.choices)

            @self.keys.add("enter", filter=selected_filter)
            def choose(event):
                self.submit()

            for index in range(min(9, len(self.choices))):
                self.bind_number(index, selected_filter)
        else:
            control = self.field
            focused = self.field
            self.field.accept_handler = lambda _: self.submit()

        if not secret and allow_help:

            @self.keys.add("f1")
            def help_requested(event):
                event.app.exit(result="?")

            if self.choices:

                @self.keys.add("?", filter=selected_filter)
                def menu_help(event):
                    event.app.exit(result="?")

        question = Window(
            FormattedTextControl([("class:question", safe_text(label).strip().rstrip(":"))]),
            wrap_lines=True,
            height=Dimension(min=1, preferred=2, max=4),
        )
        buttons = VSplit(
            [
                Button("Continue", handler=self.submit, width=14),
                Window(width=2),
                Button("Cancel", handler=lambda: self.app.exit(exception=KeyboardInterrupt())),
                Window(),
            ],
            height=1,
        )
        content = HSplit(
            [
                Frame(self.notes, title="Guide and details"),
                Window(height=1),
                question,
                Box(control, padding=0, style="class:input" if secret else ""),
                Window(height=1),
                buttons,
            ],
            width=lambda: (
                self.app.output.get_size().columns
                - (26 if self.app.output.get_size().columns >= 100 else 0)
                - 6
            ),
        )
        active = next((n for n in (1, 2, 3) if f"Step {n} of 3" in title), 0)
        steps = [("01", "Database"), ("02", "Model service"), ("03", "Review and save")]
        sidebar = Window(
            FormattedTextControl(
                [("class:badge", " YOUR WORKSPACE\n\n")]
                + [
                    ("class:active-step" if active == n else "class:sidebar", f" {num}  {name}\n\n")
                    for n, (num, name) in enumerate(steps, 1)
                ]
                + [("class:muted", " Read-only access\n\n Evidence first\n\n Private settings")]
            ),
            style="class:sidebar",
            width=24,
        )
        header = Window(
            FormattedTextControl([("class:brand", "  OpsGraph  "), ("class:badge", "  SETUP  ")]),
            height=1,
            style="class:brand",
        )
        navigation = (
            "↑↓ / 1–9 Choose  Enter Next  Tab Focus" if self.choices else "Enter Next  Tab Focus"
        )
        hint = "  Masked entry  " + navigation if secret else "  " + navigation
        hint += (
            "\n  PgUp/PgDn Scroll  "
            + ("F1 Help  " if not secret and allow_help else "")
            + "Esc/Ctrl+C Cancel"
        )
        footer = Window(FormattedTextControl([("class:footer", hint)]), height=2, wrap_lines=True)
        body = VSplit(
            [
                ConditionalContainer(
                    Box(sidebar, padding=1, style="class:sidebar"),
                    Condition(lambda: self.app.output.get_size().columns >= 100),
                ),
                Box(
                    Frame(
                        content,
                        title=safe_text(title),
                        width=lambda: (
                            self.app.output.get_size().columns
                            - (26 if self.app.output.get_size().columns >= 100 else 0)
                            - 4
                        ),
                    ),
                    padding=0,
                    padding_left=2,
                    padding_right=2,
                    width=Dimension(weight=1),
                ),
            ]
        )
        root = ConditionalContainer(
            HSplit([header, body, footer]),
            Condition(
                lambda: (
                    self.app.output.get_size().columns >= 52
                    and self.app.output.get_size().rows >= 20
                )
            ),
            alternative_content=Window(
                FormattedTextControl(
                    "Resize your terminal to at least 52 columns and 20 rows.\n"
                    "Your settings have not been saved. Press Escape or Ctrl+C to cancel.",
                    focusable=True,
                ),
                wrap_lines=True,
            ),
        )
        self.app = Application(
            layout=Layout(root, focused_element=focused),
            key_bindings=self.keys,
            style=THEME,
            full_screen=True,
            mouse_support=False,
            input=input,
            output=output,
        )

    def choice_text(self):
        return [
            (
                "class:selected" if index == self.selected else "class:choice",
                f" {'›' if index == self.selected else ' '}  {index + 1}  {label}\n",
            )
            for index, (_, label) in enumerate(self.choices)
        ]

    def bind_number(self, index, selected_filter):
        @self.keys.add(str(index + 1), filter=selected_filter)
        def numbered(event):
            self.selected = index
            self.submit()

    def submit(self):
        self.app.exit(result=self.choices[self.selected][0] if self.choices else self.field.text)
        return True

    def run(self) -> str:
        try:
            return self.app.run(set_exception_handler=False)
        finally:
            # Clear credential buffers even when cancellation or rendering fails.
            self.field.buffer.reset()


def ask_screen(
    title, context, label, *, choices=(), default="", secret=False, allow_help=True
) -> str:
    try:
        return SetupScreen(
            title,
            context,
            label,
            choices=choices,
            default=default,
            secret=secret,
            allow_help=allow_help,
        ).run()
    except Exception:
        raise TerminalScreenError(
            "The terminal screen stopped. Rerun with OPSGRAPH_PLAIN=1 for text prompts."
        ) from None
