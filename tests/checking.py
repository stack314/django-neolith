"""Fixture projects for the check before deployment (ADR 0030).

A project is a directory of its own, written under a test's ``tmp_path``,
and the check runs in a child process with that directory as its working
directory: the check reads the nearest ``pyproject.toml`` upward, and a
process hosts one set of apps (ADR 0022 Decision 5). The files are written
there, not under ``tests/``, so later rules that skip test modules see them
as a project's own code.

``Project.app`` writes a hosted app with a migration, so that the host's
checks of the code pass; ``Project.deployer`` writes the deployer's module
that ``neolith check`` reads, and ``Project.settings`` a settings module that
calls the builder, for ``manage.py checkapps``, run as
``python -m django checkapps``.
"""

import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A bound no run should reach: a run that would is a failure of its own.
_WAIT = 120
SQLITE = "django.db.backends.sqlite3"

_MIGRATION = """\
from django.db import migrations


class Migration(migrations.Migration):
    initial = True
    dependencies: list[tuple[str, str]] = []
    operations: list[migrations.operations.base.Operation] = []
"""

_APP = """\
{imports}from neolith import NeolithAppConfig


class Config(NeolithAppConfig):
    name = {name!r}
    label = {label!r}
    resources = {resources!r}
    default_auto_field = "django.db.models.AutoField"
{more}"""

_DEPLOYER = """\
SECRET_KEY = "a key for one test process only"
ROOT_URLCONF = {urlconf!r}
USE_TZ = True
MIDDLEWARE = [
    "neolith.middleware.RootMiddleware",
    "neolith.middleware.PrincipalMiddleware",
]
NEOLITH = {neolith!r}
"""

_SETTINGS = """\
from neolith.host import build_settings

SECRET_KEY = "a key for one test process only"
ROOT_URLCONF = {urlconf!r}
USE_TZ = True
MIDDLEWARE = [
    "neolith.middleware.RootMiddleware",
    "neolith.middleware.PrincipalMiddleware",
]
NEOLITH = {neolith!r}
{below}
globals().update(build_settings({hosted!r}, globals()))
"""


@dataclass(frozen=True)
class Ran:
    """What one run of the check did."""

    status: int
    stdout: str
    stderr: str

    @property
    def lines(self) -> list[str]:
        """The lines on standard output, the summary included."""
        return self.stdout.splitlines()

    @property
    def findings(self) -> list[str]:
        """The finding lines: standard output without the summary."""
        return [line for line in self.lines if not line.startswith("neolith check:")]

    @property
    def summary(self) -> str:
        """The last line of standard output."""
        return self.lines[-1] if self.lines else ""

    @property
    def errors(self) -> list[str]:
        """The lines on standard error."""
        return self.stderr.splitlines()


class Project:
    """A project directory with apps, a deployer's module and settings."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.write("urls.py", "urlpatterns: list[object] = []\n")

    def write(self, path: str, text: str = "") -> Path:
        """Write ``text``, dedented, to ``path`` under the project."""
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text), encoding="utf-8")
        return target

    def app(
        self,
        name: str,
        *,
        models: str = "",
        label: str | None = None,
        resources: tuple[str, ...] = ("database",),
        config: str = "",
        imports: str = "",
        migrations: bool = True,
    ) -> str:
        """Write a hosted app package ``name`` with ``models``, a migration
        when it has a database, and a config with ``config`` added to its
        class body and ``imports`` above it; return ``name``.
        """
        base = name.replace(".", "/")
        if not (self.root / base / "__init__.py").exists():
            self.write(f"{base}/__init__.py")
        self.write(
            f"{base}/apps.py",
            _APP.format(
                imports=textwrap.dedent(imports),
                name=name,
                label=label or name.rpartition(".")[2],
                resources=resources,
                more=textwrap.indent(textwrap.dedent(config), "    "),
            ),
        )
        self.write(f"{base}/models.py", models)
        if migrations and "database" in resources:
            self.write(f"{base}/migrations/__init__.py")
            self.write(f"{base}/migrations/0001_initial.py", _MIGRATION)
        return name

    def databases(self, *labels: str) -> dict[str, dict[str, str]]:
        """A SQLite alias for each label, at a path that holds no file."""
        return {
            label: {"ENGINE": SQLITE, "NAME": str(self.root / f"{label}.sqlite3")}
            for label in labels
        }

    def deployer(
        self,
        *labels: str,
        neolith: dict[str, Any] | None = None,
        below: str = "",
        urlconf: str = "urls",
    ) -> None:
        """Write ``deployer.py`` with a database for each of ``labels``."""
        if neolith is None:
            neolith = {"DATABASES": self.databases(*labels)}
        text = _DEPLOYER.format(urlconf=urlconf, neolith=neolith)
        self.write("deployer.py", text + textwrap.dedent(below))

    def settings(
        self,
        hosted: list[str],
        *,
        neolith: dict[str, Any],
        below: str = "",
        urlconf: str = "urls",
    ) -> None:
        """Write ``settings.py``, which calls the builder for ``hosted``."""
        self.write(
            "settings.py",
            _SETTINGS.format(
                urlconf=urlconf,
                neolith=neolith,
                hosted=hosted,
                below=textwrap.dedent(below),
            ),
        )

    def pyproject(self, text: str) -> None:
        """Write ``pyproject.toml``."""
        self.write("pyproject.toml", text)

    def env(self) -> dict[str, str]:
        """The child's environment: the project first on the path."""
        return {
            "PYTHONPATH": os.pathsep.join([str(self.root), *sys.path]),
            "PATH": os.environ.get("PATH", ""),
        }

    def run(
        self,
        *arguments: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Ran:
        """Run ``python <arguments>`` in the project, warnings as errors, with
        ``env`` added to the child's environment.
        """
        ran = subprocess.run(  # noqa: S603
            [sys.executable, "-W", "error", *arguments],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd or self.root,
            timeout=_WAIT,
            env=self.env() | (env or {}),
        )
        return Ran(ran.returncode, ran.stdout, ran.stderr)

    def check(
        self,
        *apps: str,
        settings: str = "deployer",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Ran:
        """Run ``neolith check <apps> --settings <settings>``."""
        return self.run(
            "-m", "neolith", "check", *apps, "--settings", settings, cwd=cwd, env=env
        )

    def checkapps(
        self,
        *options: str,
        settings: str = "settings",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Ran:
        """Run ``manage.py checkapps`` on the settings module ``settings``."""
        return self.run(
            "-m",
            "django",
            "checkapps",
            f"--settings={settings}",
            *options,
            cwd=cwd,
            env=env,
        )
