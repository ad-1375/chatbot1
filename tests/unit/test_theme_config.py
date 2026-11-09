"""Theme tokens stay inside the closed Groww palette (§7.11)."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".streamlit" / "config.toml"

PALETTE = {
    "#00D09C",  # primary green
    "#00B386",  # deep green
    "#E8FBF6",  # green tint
    "#44475B",  # ink
    "#7C7E8C",  # muted
    "#FFFFFF",  # surface
    "#F7F8FA",  # subtle surface
    "#E6E9EB",  # border
}


def _theme() -> dict[str, object]:
    with CONFIG.open("rb") as handle:
        return tomllib.load(handle)["theme"]


def test_theme_is_light_only() -> None:
    assert _theme()["base"] == "light"


def test_theme_uses_only_groww_colours() -> None:
    for key, value in _theme().items():
        if isinstance(value, str) and value.startswith("#"):
            assert value.upper() in PALETTE, f"{key} uses off-palette {value}"


def test_structural_tokens_match_the_design_system() -> None:
    theme = _theme()
    assert theme["primaryColor"] == "#00D09C"
    assert theme["backgroundColor"] == "#FFFFFF"
    assert theme["secondaryBackgroundColor"] == "#F7F8FA"
    assert theme["textColor"] == "#44475B"
    assert theme["borderColor"] == "#E6E9EB"
    assert theme["linkColor"] == "#00B386"


def test_server_still_binds_to_localhost() -> None:
    with CONFIG.open("rb") as handle:
        server = tomllib.load(handle)["server"]
    assert server["address"] == "localhost"
