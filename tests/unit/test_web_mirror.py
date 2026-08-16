from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "src" / "opsgraph" / "web"
PREVIEW = ROOT / "web"


def test_browser_preview_mirrors_packaged_ui_assets():
    for relative in ("index.html", "static/app.css", "static/app.js", "static/favicon.svg"):
        assert (PREVIEW / relative).read_bytes() == (CANONICAL / relative).read_bytes()
