"""The README's example runs as written, under the settings it states.

Each Python block in README.md opens with ``# <path>`` lines that name the
file the code below belongs to. The test writes those files as they are,
adds only what Django needs and the README leaves to the project (a secret
key and a URLconf, above the builder call), and runs them in a fresh process, since a
process hosts one set of apps (ADR 0022 Decision 5). So the example and the
host's checks before it serves cannot drift apart.

A code block is either a fence of ``python``, which the test writes and
runs, or one it knows to show and not run; any other fails the test
(``unknown_blocks``).

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
from markdown_it.token import Token

README = Path(__file__).resolve().parents[1] / "README.md"
_HEADER = re.compile(r"^# (\S+\.py)$")
_ABSOLUTE = re.compile(r"^https://")
# The info strings of fences the README shows and the test does not run.
_SHOWN = frozenset(["console", ""])
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
    """Whether a block is one the test writes and runs: a fence of ``python``."""
    return token.type == "fence" and token.info == "python"


def unknown_blocks(text: str) -> list[str]:
    """What to fix for each code block the test neither runs nor shows.

    The test does not guess a block's language the way GitHub does; it fails
    closed, as markdownlint's MD040 does with ``allowed_languages`` and
    ``language_only``. A fence's info string is compared as markdown-it-py
    gives it (``token.info``), with no stripping, case folding, unescaping or
    splitting: it is exactly ``python`` (written and run) or in ``_SHOWN``
    (shown, not run), and anything else, ``py`` or ``settings.py`` included,
    is refused. So is an indented code block. Known gap: code inside raw HTML
    (a ``<pre>`` element) is not seen.
    """
    found: list[str] = []
    for token in MarkdownIt("commonmark").parse(text):
        if token.type not in ("fence", "code_block"):
            continue
        assert token.map is not None
        line = token.map[0] + 1
        if token.type == "code_block":
            found.append(
                f"line {line}: an indented code block; fence it, with 'python'"
                " for code the test should run"
            )
        elif not is_python_fence(token) and token.info not in _SHOWN:
            found.append(
                f"line {line}: fence info string {token.info!r} is not one the"
                " test knows; write 'python' for code the test should run, or"
                " add the language to _SHOWN in tests/test_readme.py for a"
                " block shown and not run"
            )
    return found


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


_REFUSED = [
    " python",
    "python ",
    "Python",
    "py",
    "python3",
    'python title="x"',
    "python,ignore",
    "settings.py",
    "pyi,x",
    "pyth&#111;n",
    "text",
    "console ",
    "Console",
    " ",
]


@pytest.mark.parametrize("info", _REFUSED)
@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_a_fence_the_test_does_not_know_is_refused(fence: str, info: str) -> None:
    sample = f"text\n\n{fence}{info}\n# a.py\nx = 1\n{fence}\n"
    (message,) = unknown_blocks(sample)
    assert message.startswith(f"line 3: fence info string {info!r} ")
    assert readme_files(sample) == {}
    assert python_fence_lines(sample) == []


def test_the_refusal_says_what_to_write() -> None:
    assert unknown_blocks("```py\nx = 1\n```\n") == [
        "line 1: fence info string 'py' is not one the test knows; write"
        " 'python' for code the test should run, or add the language to"
        " _SHOWN in tests/test_readme.py for a block shown and not run"
    ]


def test_an_indented_code_block_is_refused() -> None:
    assert unknown_blocks("text\n\n    x = 1\n") == [
        "line 3: an indented code block; fence it, with 'python' for code"
        " the test should run"
    ]


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_python_is_written_and_run(fence: str) -> None:
    sample = f"{fence}python\n# a.py\nx = 1\n{fence}\n"
    assert unknown_blocks(sample) == []
    assert readme_files(sample) == {"a.py": "x = 1\n"}
    assert python_fence_lines(sample) == [0]


@pytest.mark.parametrize("info", ["console", ""])
@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_a_shown_block_is_not_run(fence: str, info: str) -> None:
    sample = f"{fence}{info}\n# a.py\nx = 1\n{fence}\n"
    assert unknown_blocks(sample) == []
    assert readme_files(sample) == {}
    assert python_fence_lines(sample) == []


@pytest.mark.parametrize(
    ("sample", "line"),
    [
        ("text\n\n- item\n\n  ```py\n  x = 1\n  ```\n", 5),
        ("text\n\n> quote\n>\n> ```py\n> x = 1\n> ```\n", 5),
        ("- item\n\n  > ```py\n  > x = 1\n  > ```\n", 3),
    ],
)
def test_a_nested_fence_is_refused(sample: str, line: int) -> None:
    assert [m.split(" fence ")[0] for m in unknown_blocks(sample)] == [f"line {line}:"]


def test_every_refused_block_is_reported() -> None:
    sample = "```py\nx\n```\n\n```python\ny\n```\n\n    z\n\n~~~sh\nw\n~~~\n"
    assert [m.split(":")[0] for m in unknown_blocks(sample)] == [
        "line 1",
        "line 9",
        "line 11",
    ]


def test_the_readme_has_no_block_the_test_does_not_know() -> None:
    text = README.read_text(encoding="utf-8")
    assert unknown_blocks(text) == []
    assert python_fence_lines(text)
