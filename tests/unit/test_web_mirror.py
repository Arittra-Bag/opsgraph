from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "src" / "opsgraph" / "web"
PREVIEW = ROOT / "web"


def test_browser_preview_mirrors_packaged_ui_assets():
    for relative in (
        "index.html",
        "static/app.css",
        "static/app.js",
        "static/events.js",
        "static/view-state.js",
        "static/work-outcomes.js",
        "static/source-setup.js",
        "static/favicon.svg",
        "static/chevron-down.svg",
        "static/icons.svg",
    ):
        assert (PREVIEW / relative).read_bytes() == (CANONICAL / relative).read_bytes()
    for path in (CANONICAL / "static/provider-icons").iterdir():
        assert (PREVIEW / "static/provider-icons" / path.name).read_bytes() == path.read_bytes()
