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

import pytest
from markdown_it import MarkdownIt
from markdown_it.common.utils import unescapeAll
from markdown_it.token import Token

README = Path(__file__).resolve().parents[1] / "README.md"
_HEADER = re.compile(r"^# (\S+\.py)$")
_ABSOLUTE = re.compile(r"^https://")
# The names GitHub highlights as Python source (Linguist's Python aliases and
# extensions, and the languages it renders with the Python grammar), lower-cased.
# Checked with GitHub's renderer (POST /markdown); the console and traceback
# languages (pycon, pytb) are not Python source and are not here.
_PYTHON = frozenset(
    [
        "python",
        "python3",
        "py3",
        "py",
        "rusthon",
        "sage",
        "gyp",
        "gypi",
        "lmi",
        "pyde",
        "pyi",
        "pyp",
        "pyt",
        "pyw",
        "rpy",
        "spec",
        "tac",
        "wsgi",
        "xpy",
        "bzl",
        "bazel",
        "starlark",
        "snakemake",
    ]
)
# GitHub ends the language at the first space, tab or comma.
_LANGUAGE_END = re.compile(r"[ \t,]")
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


def is_python_fence(token: Token) -> bool:
    """Whether a fence is a Python block, as GitHub reads its info string.

    markdown-it-py keeps the whole info string in ``token.info``, as written
    but for the backslash escapes and entities the renderer resolves, so this
    resolves them too. GitHub then cuts the language at the first ASCII space,
    tab or comma (not at other Unicode whitespace) and matches it, ignoring
    case, against the names it highlights as Python.
    """
    info = unescapeAll(token.info).strip(" \t")
    language = _LANGUAGE_END.split(info, maxsplit=1)[0]
    return token.type == "fence" and language.lower() in _PYTHON


def readme_files(text: str | None = None) -> dict[str, str]:
    """The files the Python blocks write, by path (the README's, by default)."""
    files: dict[str, str] = {}
    if text is None:
        text = README.read_text(encoding="utf-8")
    for token in MarkdownIt("commonmark").parse(text):
        if not is_python_fence(token):
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
        if is_python_fence(token) and token.map
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


@pytest.mark.parametrize(
    "info",
    [
        "python",
        "py",
        "python3",
        "Python",
        "  python  ",
        'python title="x"',
        "py {1,3}",
        "python\ttitle=x",
        "pyth&#111;n",
        "python,ignore",
        "py,x",
        "py3",
        "pyi",
        "rusthon",
        "sage",
        "Starlark",
        "bzl",
        "snakemake",
        "py&#32;x",
    ],
)
@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_any_python_fence_is_a_python_block(fence: str, info: str) -> None:
    sample = f"{fence}{info}\n# a.py\nx = 1\n{fence}\n"
    assert readme_files(sample) == {"a.py": "x = 1\n"}
    assert python_fence_lines(sample) == [0]


@pytest.mark.parametrize(
    "info",
    [
        "",
        "console",
        "text python",
        "pythonic",
        "python-3",
        "ipython",
        "pyc",
        "pycon",
        "pytb",
        "py\u00a0x",
        "\u00a0py",
        "py&nbsp;x",
        "py\u2003x",
    ],
)
@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_other_fences_are_not_python_blocks(fence: str, info: str) -> None:
    sample = f"{fence}{info}\n# a.py\nx = 1\n{fence}\n"
    assert readme_files(sample) == {}
    assert python_fence_lines(sample) == []


def test_a_python_block_outside_the_markers_is_found_in_any_spelling() -> None:
    sample = (
        f"{_START}\n\n~~~py title=x\nx = 1\n~~~\n\n{_END}\n\n```python3\ny = 2\n```\n"
    )
    start, end = marker_lines(sample)[_START][0], marker_lines(sample)[_END][0]
    assert [n for n in python_fence_lines(sample) if not start < n < end] == [8]
