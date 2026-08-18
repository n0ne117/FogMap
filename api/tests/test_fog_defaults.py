# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fog colour default is written down twice, so check the two agree.

The server bakes the colour into tiles; the settings endpoint returns what is
*stored*, not what is defaulted, so the colour picker keeps its own copy to
show on an instance that has never chosen one. Two copies of a constant drift -
these did, the first time the default changed - and the symptom is a picker
showing one colour while the tiles are rendered in another.
"""

from __future__ import annotations

import re
from pathlib import Path

from irfaran import composite


def defaults_in_typescript() -> dict[str, str]:
    for parent in Path(__file__).resolve().parents:
        if (parent / "web" / "src" / "main.ts").is_file():
            source = (parent / "web" / "src" / "main.ts").read_text()
            break
    else:  # pragma: no cover - the test image copies web/src in
        raise AssertionError("web/src/main.ts not found above this test")

    block = re.search(
        r"const FOG_COLOUR_DEFAULTS[^{]*\{(.*?)\}", source, re.S
    )
    assert block, "FOG_COLOUR_DEFAULTS not found in main.ts"
    return {
        theme: colour.lower()
        for theme, colour in re.findall(
            r"(\w+):\s*'(#[0-9a-fA-F]{6})'", block.group(1)
        )
    }


def test_both_themes_are_listed() -> None:
    assert set(defaults_in_typescript()) == set(composite.FOG_COLOUR)


def test_the_two_copies_agree() -> None:
    typescript = defaults_in_typescript()
    server = {
        theme: composite.to_hex(colour).lower()
        for theme, colour in composite.FOG_COLOUR.items()
    }
    assert typescript == server, (
        "the fog colour default in web/src/main.ts disagrees with "
        "composite.FOG_COLOUR. The picker would show one colour and the tiles "
        f"would be rendered in another. TypeScript: {typescript}, server: {server}"
    )
