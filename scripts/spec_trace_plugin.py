"""Record which requirements the tests that ran verified (ADR 0018).

A pytest plugin. With ``--spec-report=PATH``, it writes a JSON report of every
collected test that carries the ``spec`` or the ``conformance_case`` marker:
the requirement IDs it cites, the conformance case it runs, and how it ended.
``scripts/spec_trace.py --report PATH`` reads the report and counts a
requirement as covered only by tests whose outcome is ``passed`` (ADR 0032,
Decision 9).

The report is format 2::

    {"format": 2,
     "environment": {"python": "3.12.7", "architecture": "x86_64",
                     "packages": {"django-neolith": "0.1.0a3", "Django": "5.2.19"}},
     "tests": {"<node id>": {"ids": ["CON-1"], "outcome": "passed",
                             "case": "<name>",
                             "transport_outcomes": {"in-process": "same",
                                                    "http": "same"}}}}

``environment`` is read with ``platform`` and ``importlib.metadata``, so the
checker never imports Django or neolith; a package that is not installed has
the version ``null``. ``case`` is there only for a test with the
``conformance_case`` marker, and ``transport_outcomes`` only for a test that
recorded them in ``user_properties`` (the contract part's ``over_both``).

``pyproject.toml`` loads the plugin with ``-p``, not from a conftest. pytest
picks the conftests to load from the paths among its arguments, and an option
it does not know yet makes its value look like one: an existing ``report.json``
in ``--spec-report report.json`` was taken for a test path. pytest finds its
configuration file the same way, before it loads any plugin, so write
``--spec-report=PATH``. With a space, an existing report outside the project is
still taken for a path. As for ``--junitxml``, the plugin expands ``~`` and
environment variables in PATH itself, since a shell does not expand ``~``
after ``=``; ``scripts/spec_trace.py --report`` does the same. When the session
starts, it empties the report, so a PATH it cannot open for writing is refused
before any test runs, and a run that dies after that leaves no earlier report
behind. ``--help`` and ``--markers`` start no session and leave it alone.

Outcomes:

- ``passed``: setup, call and teardown all passed, and the test was not
  expected to fail;
- ``skipped``, ``xfailed``, ``xpassed``: the test did not verify anything;
- ``failed``: the call failed; ``error``: setup or teardown failed;
- ``not run``: deselected (by ``-k``, ``-m`` or a plugin), or the run
  stopped before it.

A test whose ``transport_outcomes`` is not one map of transport names to
strings is recorded as ``error``: its record of what it verified is broken.

The markers are read from pytest itself, so they apply however they are
attached: on the test, its class, the module's ``pytestmark`` or a parameter
set. A test runs at most one conformance case.
"""

from __future__ import annotations

import json
import os
import platform
import stat
from importlib import metadata
from pathlib import Path

import pytest

FORMAT = 2
PACKAGES = ("django-neolith", "Django")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--spec-report",
        metavar="PATH",
        default=None,
        help="write the requirement IDs cited by each test and its outcome (ADR 0018)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(_Recorder(config), "spec-trace-recorder")


def _emptied(value: str) -> Path:
    """The report's absolute path, emptied; a usage error if it can't be opened.

    ``..`` is left for the system to resolve, as ``spec_trace.py`` leaves it,
    and no directory is created.
    """
    expanded = os.path.expanduser(os.path.expandvars(value))
    if os.path.basename(expanded) in ("", ".", ".."):
        raise pytest.UsageError(f"--spec-report needs a file path, not {value!r}")
    path = Path(expanded).absolute()
    try:
        if os.path.lexists(path) and not stat.S_ISREG(path.stat().st_mode):
            raise pytest.UsageError(
                f"--spec-report cannot write {path}: not a regular file"
            )
        path.write_text("", encoding="utf-8")
    except OSError as error:
        raise pytest.UsageError(
            f"--spec-report cannot write {path}: {error.strerror or error}"
        ) from error
    return path


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def environment() -> dict[str, object]:
    """Where the run happened: the interpreter, the machine and the packages."""
    return {
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "packages": {name: _version(name) for name in PACKAGES},
    }


def _case(item: pytest.Item) -> str | None:
    """The name of the conformance case ``item`` runs, if it runs one."""
    names: list[str] = []
    for mark in item.iter_markers("conformance_case"):
        if len(mark.args) != 1 or mark.kwargs or not isinstance(mark.args[0], str):
            raise pytest.UsageError(
                f"{item.nodeid}: the conformance_case marker takes one case "
                "name as a string, and nothing else"
            )
        names.append(mark.args[0])
    if len(set(names)) > 1:
        raise pytest.UsageError(
            f"{item.nodeid}: a test runs one conformance case, not "
            + ", ".join(sorted(set(names)))
        )
    return names[0] if names else None


def _transport_outcomes(properties: list[tuple[str, object]]) -> object:
    """The test's ``transport_outcomes``; None if none, or the bad values."""
    values = [value for name, value in properties if name == "transport_outcomes"]
    if not values:
        return None
    if len(values) == 1:
        (value,) = values
        if (
            isinstance(value, dict)
            and value
            and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        ):
            return dict(value)
    return values


class _Recorder:
    def __init__(self, config: pytest.Config) -> None:
        self.value: str | None = config.getoption("--spec-report")
        self.path: Path | None = None
        self.tests: dict[str, dict[str, object]] = {}

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        if self.value is not None:
            self.path = _emptied(self.value)

    def record(self, item: pytest.Item) -> None:
        """Note the IDs and case of an item, with the outcome "not run" for now."""
        ids: list[str] = []
        for mark in item.iter_markers("spec"):
            if (
                not mark.args
                or mark.kwargs
                or not all(isinstance(rid, str) for rid in mark.args)
            ):
                raise pytest.UsageError(
                    f"{item.nodeid}: the spec marker takes one or more "
                    "requirement IDs as strings, and nothing else"
                )
            ids.extend(mark.args)
        case = _case(item)
        if ids or case is not None:
            entry: dict[str, object] = {"ids": sorted(set(ids)), "outcome": "not run"}
            if case is not None:
                entry["case"] = case
            self.tests[item.nodeid] = entry

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        # Deselected by -k, -m or another plugin: recorded as "not run", so
        # an unknown or withdrawn ID on them is still checked.
        for item in items:
            self.record(item)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            self.record(item)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        entry = self.tests.get(report.nodeid)
        if entry is None:
            return
        expected_to_fail = hasattr(report, "wasxfail")
        if report.when == "call":
            if report.passed:
                entry["outcome"] = "xpassed" if expected_to_fail else "passed"
            elif report.skipped:
                entry["outcome"] = "xfailed" if expected_to_fail else "skipped"
            else:
                entry["outcome"] = "failed"
        elif report.failed:
            entry["outcome"] = "error"
        elif report.skipped:
            entry["outcome"] = "xfailed" if expected_to_fail else "skipped"
        if report.when == "teardown":
            # A fixture's teardown adds its properties before this report.
            outcomes = _transport_outcomes(report.user_properties)
            if isinstance(outcomes, dict):
                entry["transport_outcomes"] = outcomes
            elif outcomes is not None:
                entry["outcome"] = "error"

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.path is None:
            return
        report = {"format": FORMAT, "environment": environment(), "tests": self.tests}
        self.path.write_text(
            json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
