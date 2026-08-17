# SPDX-License-Identifier: AGPL-3.0-or-later
"""The TypeScript and index.html have to agree about what exists.

element() throws when an id is missing, so a stale reference is not a cosmetic
mismatch - it takes out the click handler it sits in, and every handler wired
after it. That failure is invisible to both the type checker (the id is a
string) and to anyone testing on an instance where the relevant panel never
appears.

It escaped once and stayed escaped for eleven releases: the setup screen's
download handler read a token field that had been deleted from the markup
months earlier, so Download, Resume and Update basemap all threw before
sending anything. Nobody saw it, because a machine that already has a basemap
never shows those buttons. The first fresh install found it immediately.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

def web_dir() -> Path:
    """Find the web sources, in the repository or in the test image.

    The layouts differ - api/tests/ sits two levels under the repository root,
    and /app/tests/ sits one level under /app - so the directory is found by
    looking upwards for it rather than by counting parents.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "web" / "index.html").is_file():
            return parent / "web"
    raise AssertionError(
        "web/index.html not found above this test. In the test image it is "
        "put there by the Dockerfile's test stage."
    )


WEB = web_dir()

# element('foo') and element<HTMLInputElement>('foo'), single or double quoted.
CALL = re.compile(r"""element(?:<[^>]+>)?\(\s*['"]([^'"]+)['"]\s*\)""")
MARKUP_ID = re.compile(r"""\bid=['"]([^'"]+)['"]""")


def markup_ids() -> set[str]:
    return set(MARKUP_ID.findall((WEB / "index.html").read_text()))


def sources() -> list[Path]:
    return sorted((WEB / "src").glob("*.ts"))


def test_sources_exist() -> None:
    """A silent glob miss would make every test below vacuously pass."""
    assert sources(), "no TypeScript found to check"
    assert markup_ids(), "no ids found in index.html"


@pytest.mark.parametrize("source", sources(), ids=lambda path: path.name)
def test_every_element_id_exists_in_markup(source: Path) -> None:
    known = markup_ids()
    missing = sorted(
        {name for name in CALL.findall(source.read_text()) if name not in known}
    )
    assert not missing, (
        f"{source.name} calls element() for ids that are not in index.html: "
        f"{', '.join(missing)}. element() throws, so this kills the handler "
        "it is in and every handler wired after it."
    )


def test_markup_ids_are_unique() -> None:
    """Two elements sharing an id means element() silently returns the wrong one."""
    found = MARKUP_ID.findall((WEB / "index.html").read_text())
    duplicates = sorted({name for name in found if found.count(name) > 1})
    assert not duplicates, f"index.html reuses ids: {', '.join(duplicates)}"
