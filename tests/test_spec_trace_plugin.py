"""The pytest plugin that records which tests ran and passed (ADR 0018)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ARGS = ("-p", "scripts.spec_trace_plugin", "-p", "no:django", "-p", "no:cacheprovider")


Tests = dict[str, dict[str, object]]


def run(pytester: pytest.Pytester, source: str, *args: str) -> Tests:
    """Run ``source`` as a test module and return the report's tests."""
    pytester.makeini("[pytest]\nmarkers =\n    spec(*ids): requirement IDs\n")
    pytester.makepyfile(test_example=source)
    pytester.runpytest(*ARGS, "--spec-report=report.json", *args)
    report = json.loads((pytester.path / "report.json").read_text())
    assert report["format"] == 1
    tests: Tests = report["tests"]
    return tests


def outcomes(tests: Tests) -> dict[str, object]:
    return {name.split("::", 1)[1]: entry["outcome"] for name, entry in tests.items()}


def test_each_outcome_is_recorded(pytester: pytest.Pytester) -> None:
    tests = run(
        pytester,
        """
import pytest

@pytest.fixture
def broken():
    raise RuntimeError

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError

@pytest.mark.spec("CON-1")
def test_passes(): pass

@pytest.mark.spec("CON-1")
def test_fails(): assert False

@pytest.mark.spec("CON-1")
def test_setup_errors(broken): pass

@pytest.mark.spec("CON-1")
def test_teardown_errors(broken_teardown): pass

@pytest.mark.spec("CON-1")
@pytest.mark.skip(reason="later")
def test_skipped(): pass

@pytest.mark.spec("CON-1")
def test_skips_itself(): pytest.skip("later")

@pytest.mark.spec("CON-1")
@pytest.mark.xfail(reason="known")
def test_xfailed(): assert False

@pytest.mark.spec("CON-1")
@pytest.mark.xfail(reason="known")
def test_xpassed(): pass

@pytest.mark.spec("CON-1")
@pytest.mark.xfail(reason="known", strict=True)
def test_strict_xpass(): pass

def test_cites_nothing(): pass
""",
    )
    assert outcomes(tests) == {
        "test_passes": "passed",
        "test_fails": "failed",
        "test_setup_errors": "error",
        "test_teardown_errors": "error",
        "test_skipped": "skipped",
        "test_skips_itself": "skipped",
        "test_xfailed": "xfailed",
        "test_xpassed": "xpassed",
        "test_strict_xpass": "failed",
    }


def test_markers_on_classes_modules_and_parameters_apply(
    pytester: pytest.Pytester,
) -> None:
    tests = run(
        pytester,
        """
import pytest

pytestmark = pytest.mark.spec("CON-1")
cites = pytest.mark.spec

class TestThing:
    pytestmark = [pytest.mark.spec("CON-2"), pytest.mark.skip(reason="later")]

    def test_in_class(self): pass

@cites("CON-3", "CON-3")
@pytest.mark.parametrize("n", [1, pytest.param(2, marks=pytest.mark.spec("CON-4"))])
def test_param(n): pass
""",
    )
    ids = {name.split("::", 1)[1]: entry for name, entry in tests.items()}
    assert ids == {
        "TestThing::test_in_class": {"ids": ["CON-1", "CON-2"], "outcome": "skipped"},
        "test_param[1]": {"ids": ["CON-1", "CON-3"], "outcome": "passed"},
        "test_param[2]": {"ids": ["CON-1", "CON-3", "CON-4"], "outcome": "passed"},
    }


def test_helpers_and_module_level_skips_cover_nothing(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        helpers="import pytest\n\n@pytest.mark.spec('CON-1')\ndef check(): pass\n"
    )
    tests = run(
        pytester,
        """
import pytest

pytest.importorskip("a_module_that_does_not_exist")

@pytest.mark.spec("CON-1")
def test_never_collected(): pass
""",
    )
    assert tests == {}


def test_deselected_test_is_recorded_as_not_run(pytester: pytest.Pytester) -> None:
    tests = run(
        pytester,
        """
import pytest

@pytest.mark.spec("CON-1")
def test_kept(): pass

@pytest.mark.spec("NOPE-1")
def test_dropped(): pass
""",
        "-k",
        "kept",
    )
    assert outcomes(tests) == {"test_kept": "passed", "test_dropped": "not run"}


@pytest.mark.parametrize(
    "marker", ["pytest.mark.spec()", "pytest.mark.spec(1)", "pytest.mark.spec(id='X')"]
)
def test_marker_arguments_must_be_ids(pytester: pytest.Pytester, marker: str) -> None:
    pytester.makeini("[pytest]\nmarkers =\n    spec(*ids): requirement IDs\n")
    pytester.makepyfile(f"import pytest\n\n@{marker}\ndef test_it(): pass\n")
    result = pytester.runpytest(*ARGS, "--spec-report=report.json")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*the spec marker takes one or more requirement IDs*"])


def test_no_report_without_the_option(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        "import pytest\n\n@pytest.mark.spec('CON-1')\ndef test_it(): pass\n"
    )
    pytester.runpytest(*ARGS).assert_outcomes(passed=1)
    assert not (pytester.path / "report.json").exists()


@pytest.mark.parametrize(
    ("option", "outside"),
    [
        (["--spec-report", "{}"], False),
        (["--spec-report={}"], False),
        (["--spec-report={}"], True),
    ],
    ids=["space", "equals", "outside the root"],
)
def test_the_option_works_when_the_report_already_exists(
    pytester: pytest.Pytester,
    tmp_path_factory: pytest.TempPathFactory,
    option: list[str],
    outside: bool,
) -> None:
    # The repository's own pytest setup, in a fresh process. pytest reads the
    # paths in its arguments before it loads conftests, so when the plugin was
    # loaded from one, the second run took the existing report for a test path.
    # It finds its configuration before it loads any plugin, so only the "="
    # form works for a report outside the project.
    for source in [
        ROOT / "pyproject.toml",
        *ROOT.glob("scripts/*.py"),
        *(
            path
            for path in ROOT.glob("tests/*.py")
            if not path.name.startswith("test_")
        ),
    ]:
        target = pytester.path / source.relative_to(ROOT)
        target.parent.mkdir(exist_ok=True)
        target.write_text(source.read_text())
    (pytester.path / "tests" / "test_cited.py").write_text(
        "import pytest\n\n@pytest.mark.spec('CON-1')\ndef test_it(): pass\n"
    )
    report = (
        tmp_path_factory.mktemp("elsewhere") if outside else pytester.path
    ) / "spec-report.json"
    args = [arg.format(report if outside else report.name) for arg in option]
    for _ in range(2):
        result = pytester.runpytest_subprocess(*args)
        assert result.ret == pytest.ExitCode.OK, result.stderr.str()
        assert json.loads(report.read_text())["tests"] == {
            "tests/test_cited.py::test_it": {"ids": ["CON-1"], "outcome": "passed"}
        }
