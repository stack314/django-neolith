"""The README's example runs as written, under the settings it states.

Each Python block in README.md opens with ``# <path>`` lines that name the
file the code below belongs to. The test writes those files as they are,
adds only what Django needs and the README leaves to the project (a secret
key and a URLconf, above the builder call), and runs them in a fresh process, since a
process hosts one set of apps (ADR 0022 Decision 5). So the example and the
host's checks before it serves cannot drift apart.

The example section sits between two HTML comment markers, so the website can
copy it verbatim (``_START`` and ``_END``).

README.md is also the package's description on PyPI, where a relative link
resolves under the project's page and leads nowhere, so every link and image
in it is absolute.
"""

import os
import re
import subprocess
import sys
import textwrap
from html.parser import HTMLParser
from pathlib import Path

from markdown_it import MarkdownIt

README = Path(__file__).resolve().parents[1] / "README.md"
_HEADER = re.compile(r"^# (\S+\.py)$")
_ABSOLUTE = re.compile(r"^https://")
_START = "<!-- neolith-example:start -->"
_END = "<!-- neolith-example:end -->"

# What the README leaves to the project; nothing here is a neolith setting.
_PROJECT = """
SECRET_KEY = "readme-test-only"
ROOT_URLCONF = "urls"
ALLOWED_HOSTS = ["testserver"]
"""

# A view in the shop app, as the README's last sentence describes.
_VIEW = """
from django.http import HttpRequest, HttpResponse

from shop.orders import place_order


def order(request: HttpRequest) -> HttpResponse:
    return HttpResponse(str(place_order("A1", 4)))
"""

_URLS = """
from django.urls import path

from shop.views import order

urlpatterns = [path("order", order)]
"""

_RUN = """
import django

django.setup()

import neolith
from django.test import Client
from shop.orders import place_order

with neolith.command_scope("shop"):
    print("command", place_order("A1", 2))
response = Client().get("/order")
print("view", response.status_code, response.content.decode())
"""


def readme_files() -> dict[str, str]:
    """The files the README's Python blocks write, by path."""
    files: dict[str, str] = {}
    tokens = MarkdownIt("commonmark").parse(README.read_text(encoding="utf-8"))
    for token in tokens:
        if token.type != "fence" or token.info.strip() != "python":
            continue
        path: str | None = None
        for line in token.content.splitlines(keepends=True):
            header = _HEADER.match(line.rstrip("\n"))
            if header:
                path = header.group(1)
                assert path not in files, f"README.md writes {path} twice"
                files[path] = ""
            else:
                assert path is not None, "a README Python block names no file"
                files[path] += line
    return files


def test_the_readme_names_the_example_files() -> None:
    assert sorted(readme_files()) == [
        "inventory/apps.py",
        "inventory/contracts.py",
        "inventory/services.py",
        "settings.py",
        "shop/apps.py",
        "shop/orders.py",
    ]


def test_the_readme_states_no_project_setting_the_test_adds() -> None:
    stated = readme_files()["settings.py"]
    for name in ("SECRET_KEY", "DATABASES", "ROOT_URLCONF", "ALLOWED_HOSTS"):
        assert name not in stated


def test_the_readme_example_runs_under_its_settings(tmp_path: Path) -> None:
    files = readme_files()
    # Above the builder call, which reads the module's names when it runs.
    files["settings.py"] = _PROJECT + files["settings.py"]
    files["shop/views.py"] = _VIEW
    files["urls.py"] = _URLS
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")
    for package in ("inventory", "shop"):
        (tmp_path / package / "__init__.py").touch()
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", "-c", _RUN],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        timeout=120,
        env={
            "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path]),
            "DJANGO_SETTINGS_MODULE": "settings",
        },
    )
    assert ran.returncode == 0, ran.stderr[-3000:]
    assert ran.stdout.splitlines() == ["command 2", "view 200 4"]


def marker_lines(text: str) -> dict[str, list[int]]:
    """The first line (0-based) of each marker, as a Markdown block, by marker."""
    found: dict[str, list[int]] = {_START: [], _END: []}
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "html_block" and token.map:
            marker = token.content.strip()
            if marker in found:
                found[marker].append(token.map[0])
    return found


def python_fence_lines(text: str) -> list[int]:
    """The first line (0-based) of each Python block."""
    return [
        token.map[0]
        for token in MarkdownIt("commonmark").parse(text)
        if token.type == "fence" and token.info.strip() == "python" and token.map
    ]


def test_each_example_marker_appears_once() -> None:
    text = README.read_text(encoding="utf-8")
    for marker in (_START, _END):
        assert text.count(marker) == 1, marker
        assert len(marker_lines(text)[marker]) == 1, marker


def test_every_python_block_lies_between_the_markers() -> None:
    text = README.read_text(encoding="utf-8")
    (start,), (end,) = marker_lines(text)[_START], marker_lines(text)[_END]
    assert start < end
    blocks = python_fence_lines(text)
    assert blocks
    assert [line for line in blocks if not start < line < end] == []


def test_markers_are_checked_in_markdown_blocks_only() -> None:
    sample = (
        f"{_END}\n\ntext\n\n```python\nx = 1\n```\n\n{_START}\n\n"
        f"```\n{_START}\n```\n\n```python\ny = 2\n```\n"
    )
    found = marker_lines(sample)
    assert found == {_START: [8], _END: [0]}
    assert python_fence_lines(sample) == [4, 14]


class _Attributes(HTMLParser):
    """Every ``href`` and ``src`` in a piece of HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # srcset is left out: only <source> carries it, and PyPI strips <source>,
        # so it is read on GitHub alone, where a relative path resolves.
        self.found.extend(v or "" for k, v in attrs if k in ("href", "src"))


def links(text: str) -> list[str]:
    """Every link and image destination in ``text``, Markdown and HTML."""
    found: list[str] = []
    html = _Attributes()
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "html_block":
            html.feed(token.content)
        for child in token.children or ():
            if child.type == "link_open":
                found.append(str(child.attrGet("href")))
            elif child.type == "image":
                found.append(str(child.attrGet("src")))
            elif child.type == "html_inline":
                html.feed(child.content)
    return found + html.found


def test_every_readme_link_is_absolute() -> None:
    found = links(README.read_text(encoding="utf-8"))
    assert any(link.endswith("/LICENSE") for link in found)
    assert [link for link in found if not _ABSOLUTE.match(link)] == []


def test_relative_links_are_found_in_markdown_and_html() -> None:
    sample = (
        '[a](VISION.md) ![b](c.png) <a href="LICENSE">d</a>\n\n'
        '<p><img src="x.svg"></p>\n'
    )
    assert sorted(links(sample)) == ["LICENSE", "VISION.md", "c.png", "x.svg"]
