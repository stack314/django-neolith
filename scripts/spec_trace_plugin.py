"""Record which requirements the tests that ran verified (ADR 0018).

A pytest plugin. With ``--spec-report=PATH``, it writes a JSON report of every
collected test that carries the ``spec`` marker: the requirement IDs it cites
and how it ended. ``scripts/spec_trace.py --report PATH`` reads the report and
counts a requirement as covered only by tests whose outcome is ``passed``.

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

The marker is read from pytest itself, so it applies however it is attached:
on the test, its class, the module's ``pytestmark`` or a parameter set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FORMAT = 1


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
        path.write_text("", encoding="utf-8")
    except OSError as error:
        raise pytest.UsageError(
            f"--spec-report cannot write {path}: {error.strerror or error}"
        ) from error
    return path


class _Recorder:
    def __init__(self, config: pytest.Config) -> None:
        self.value: str | None = config.getoption("--spec-report")
        self.path: Path | None = None
        self.tests: dict[str, dict[str, object]] = {}

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        if self.value is not None:
            self.path = _emptied(self.value)

    def record(self, item: pytest.Item) -> None:
        """Note the IDs an item cites, with the outcome "not run" for now."""
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
        if ids:
            self.tests[item.nodeid] = {"ids": sorted(set(ids)), "outcome": "not run"}

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

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.path is None:
            return
        report = {"format": FORMAT, "tests": self.tests}
        self.path.write_text(
            json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
