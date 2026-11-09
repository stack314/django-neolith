"""Record which requirements the tests that ran verified (ADR 0018).

A pytest plugin. With ``--spec-report PATH``, it writes a JSON report of every
collected test that carries the ``spec`` marker: the requirement IDs it cites
and how it ended. ``scripts/spec_trace.py --report PATH`` reads the report and
counts a requirement as covered only by tests whose outcome is ``passed``.

Outcomes:

- ``passed``: setup, call and teardown all passed, and the test was not
  expected to fail;
- ``skipped``, ``xfailed``, ``xpassed``: the test did not verify anything;
- ``failed``: the call failed; ``error``: setup or teardown failed;
- ``not run``: collected but deselected, or the run stopped before it.

The marker is read from pytest itself, so it applies however it is attached:
on the test, its class, the module's ``pytestmark`` or a parameter set.
"""

from __future__ import annotations

import json
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


class _Recorder:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.tests: dict[str, dict[str, object]] = {}

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
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
                self.tests[item.nodeid] = {
                    "ids": sorted(set(ids)),
                    "outcome": "not run",
                }

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
        path = self.config.getoption("--spec-report")
        if path is None:
            return
        report = {"format": FORMAT, "tests": self.tests}
        Path(path).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
