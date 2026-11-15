"""The README's example runs as written, under the settings it states.

Each Python block in README.md opens with ``# <path>`` lines that name the
file the code below belongs to. The test writes those files as they are,
adds only what Django needs and the README leaves to the project (a secret
key, a database and a URLconf), and runs them in a fresh process, since a
process hosts one set of apps (ADR 0022 Decision 5). So the example and the
host's checks before it serves cannot drift apart.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from markdown_it import MarkdownIt

README = Path(__file__).resolve().parents[1] / "README.md"
_HEADER = re.compile(r"^# (\S+\.py)$")

# What the README leaves to the project; nothing here is a neolith setting.
_PROJECT = """
SECRET_KEY = "readme-test-only"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
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
    files["settings.py"] += _PROJECT
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
