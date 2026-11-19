"""The pytest plugin that records which tests ran and passed (ADR 0018)."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ARGS = ("-p", "scripts.spec_trace_plugin", "-p", "no:django", "-p", "no:cacheprovider")
INI = (
    "[pytest]\nmarkers =\n    spec(*ids): requirement IDs\n"
    "    conformance_case(name): a conformance case\n"
)


Tests = dict[str, dict[str, object]]


def run(pytester: pytest.Pytester, source: str, *args: str) -> Tests:
    """Run ``source`` as a test module and return the report's tests."""
    pytester.makeini(INI)
    pytester.makepyfile(test_example=source)
    pytester.runpytest(*ARGS, "--spec-report=report.json", *args)
    report = json.loads((pytester.path / "report.json").read_text())
    assert report.keys() == {"format", "environment", "tests"}
    assert report["format"] == 2
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


@pytest.mark.parametrize("value", ["~/report.json", "$REPORTS/report.json"])
def test_home_and_variables_are_expanded(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # A shell does not expand "~" after "=", so the plugin does, as --junitxml
    # does.
    pytester.mkdir("home")
    monkeypatch.setenv("HOME", str(pytester.path / "home"))
    monkeypatch.setenv("REPORTS", str(pytester.path / "home"))
    pytester.makepyfile(
        "import pytest\n\n@pytest.mark.spec('CON-1')\ndef test_it(): pass\n"
    )
    pytester.runpytest(*ARGS, f"--spec-report={value}").assert_outcomes(passed=1)
    report = json.loads((pytester.path / "home/report.json").read_text())
    assert list(report["tests"]) == ["test_home_and_variables_are_expanded.py::test_it"]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "new/",
        "new/.",
        "new/..",
        ".",
        "sub",
        "afile/report.json",
        "missing/report.json",
        "x" * 300,
    ],
    ids=[
        "empty",
        "slash",
        "dot",
        "dot dot",
        "here",
        "directory",
        "under a file",
        "missing directory",
        "too long",
    ],
)
def test_a_path_that_cannot_be_opened_is_refused_before_the_run(
    pytester: pytest.Pytester, value: str
) -> None:
    pytester.mkdir("sub")
    (pytester.path / "afile").write_text("")
    pytester.makepyfile("def test_it(): pass\n")
    result = pytester.runpytest(*ARGS, f"--spec-report={value}")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["ERROR: --spec-report*"])
    result.stdout.no_fnmatch_line("*passed*")
    assert sorted(path.name for path in pytester.path.iterdir()) == [
        "afile",
        "sub",
        "test_a_path_that_cannot_be_opened_is_refused_before_the_run.py",
    ]


@pytest.mark.parametrize("option", ["--help", "--markers"])
def test_a_command_without_a_session_leaves_the_report_alone(
    pytester: pytest.Pytester, option: str
) -> None:
    (pytester.path / "report.json").write_text("earlier")
    result = pytester.runpytest(*ARGS, "--spec-report=report.json", option)
    assert result.ret == pytest.ExitCode.OK
    assert (pytester.path / "report.json").read_text() == "earlier"


def test_a_run_that_dies_in_the_session_leaves_no_earlier_report(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    (pytester.path / "report.json").write_text('{"format": 1, "tests": {}}')
    pytester.makepyfile("import os\n\ndef test_dies(): os._exit(3)\n")
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    result = pytester.runpytest_subprocess(*ARGS, "--spec-report=report.json")
    assert result.ret == 3
    assert (pytester.path / "report.json").read_text() == ""


@pytest.mark.parametrize(
    ("option", "outside"),
    [
        (["--spec-report", "{}"], False),
        (["--spec-report={}"], False),
        (["--spec-report={}"], True),
    ],
    ids=["space", "equals", "outside the project"],
)
def test_the_option_works_when_the_report_already_exists(
    tmp_path: Path, option: list[str], outside: bool
) -> None:
    # The repository's pytest setup, run twice as the bare pytest command runs
    # it: in a fresh process, without the current directory on sys.path (-P).
    # pytest picks conftests from the paths among its arguments before it
    # loads them, so when the plugin came from a conftest, the second run took
    # the existing report for a test path. It finds its configuration file
    # before it loads any plugin, so only "=" works for a report outside the
    # project.
    project = tmp_path / "project"
    for source in [
        ROOT / "pyproject.toml",
        *ROOT.glob("scripts/*.py"),
        # The suite's settings build the test host from its apps, so every
        # module but the tests themselves comes along.
        *(
            path
            for path in ROOT.glob("tests/**/*.py")
            if not path.name.startswith("test_")
        ),
    ]:
        target = project / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
    (project / "tests" / "test_cited.py").write_text(
        "import pytest\n\n@pytest.mark.spec('CON-1')\ndef test_it(): pass\n"
    )
    report = (tmp_path if outside else project) / "spec-report.json"
    args = [arg.format(report if outside else report.name) for arg in option]
    # Only this process's pytest on PYTHONPATH, so the project is not, and no
    # options from the environment, as pytester does.
    env = {
        name: value for name, value in os.environ.items() if name != "PYTEST_ADDOPTS"
    }
    env["PYTHONPATH"] = str(Path(pytest.__file__).parents[1])
    for _ in range(2):
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-P", "-m", "pytest", *args],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
        assert f"pytest-{pytest.__version__}," in result.stdout
        assert json.loads(report.read_text())["tests"] == {
            "tests/test_cited.py::test_it": {"ids": ["CON-1"], "outcome": "passed"}
        }


# Format 2: the environment, the conformance case and the transport outcomes
# (ADR 0032, the admitted set and Decision 9)


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def test_the_report_says_where_it_ran(pytester: pytest.Pytester) -> None:
    pytester.makeini(INI)
    pytester.makepyfile("def test_it(): pass\n")
    pytester.runpytest(*ARGS, "--spec-report=report.json")
    report = json.loads((pytester.path / "report.json").read_text())
    assert report["environment"] == {
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "packages": {
            "django-neolith": _version("django-neolith"),
            "Django": _version("Django"),
        },
    }
    assert report["environment"]["packages"]["Django"] is not None


def test_the_conformance_case_is_recorded(pytester: pytest.Pytester) -> None:
    tests = run(
        pytester,
        """
import pytest

@pytest.mark.conformance_case("echo_round_trips")
@pytest.mark.spec("CON-1")
def test_cited(): pass

@pytest.mark.conformance_case("echo_round_trips")
def test_cites_nothing(): pass

@pytest.mark.conformance_case("deselected_case")
def test_dropped(): pass

class TestCases:
    pytestmark = pytest.mark.conformance_case("on_the_class")

    @pytest.mark.conformance_case("on_the_class")
    @pytest.mark.parametrize("n", [1, 2])
    def test_param(self, n): pass

def test_plain(): pass
""",
        "-k",
        "not dropped",
    )
    ids = {name.split("::", 1)[1]: entry for name, entry in tests.items()}
    assert ids == {
        "test_cited": {
            "ids": ["CON-1"],
            "outcome": "passed",
            "case": "echo_round_trips",
        },
        "test_cites_nothing": {
            "ids": [],
            "outcome": "passed",
            "case": "echo_round_trips",
        },
        "test_dropped": {"ids": [], "outcome": "not run", "case": "deselected_case"},
        "TestCases::test_param[1]": {
            "ids": [],
            "outcome": "passed",
            "case": "on_the_class",
        },
        "TestCases::test_param[2]": {
            "ids": [],
            "outcome": "passed",
            "case": "on_the_class",
        },
    }


@pytest.mark.parametrize(
    ("markers", "message"),
    [
        ("@pytest.mark.conformance_case()", "takes one case name as a string"),
        ("@pytest.mark.conformance_case(1)", "takes one case name as a string"),
        ("@pytest.mark.conformance_case('a', 'b')", "takes one case name as a string"),
        ("@pytest.mark.conformance_case(name='a')", "takes one case name as a string"),
        (
            "@pytest.mark.conformance_case('a')\n@pytest.mark.conformance_case('b')",
            "a test runs one conformance case, not a, b",
        ),
    ],
)
def test_conformance_case_arguments_must_be_one_name(
    pytester: pytest.Pytester, markers: str, message: str
) -> None:
    pytester.makeini(INI)
    pytester.makepyfile(f"import pytest\n\n{markers}\ndef test_it(): pass\n")
    result = pytester.runpytest(*ARGS, "--spec-report=report.json")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*test_it: *{message}*"])


SAME = {"in-process": "same", "http": "same"}
DIFFERENT = {"in-process": "replied 1", "http": "replied 2"}


@pytest.mark.parametrize(
    ("recorded", "outcome", "transport_outcomes"),
    [
        ([SAME], "passed", SAME),
        ([DIFFERENT], "passed", DIFFERENT),
        ([{}], "error", None),
        ([{"http": 1}], "error", None),
        ([{"in-process": "same", "http": 1}], "error", None),
        ([{1: "same"}], "error", None),
        ([["same"]], "error", None),
        ([SAME, SAME], "error", None),
    ],
)
def test_transport_outcomes_are_carried(
    pytester: pytest.Pytester,
    recorded: list[object],
    outcome: str,
    transport_outcomes: dict[str, str] | None,
) -> None:
    # As over_both does: a fixture's teardown records them on the test. A
    # record that is not one map of names to strings is an error.
    source = (
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def record(request):\n"
        "    yield\n"
        f"    for value in {recorded!r}:\n"
        "        request.node.user_properties.append(('transport_outcomes', value))\n"
        "\n"
        "@pytest.mark.spec('CON-1')\n"
        "def test_it(record): pass\n"
    )
    (entry,) = run(pytester, source).values()
    expected: dict[str, object] = {"ids": ["CON-1"], "outcome": outcome}
    if transport_outcomes is not None:
        expected["transport_outcomes"] = transport_outcomes
    assert entry == expected


def test_a_failing_case_keeps_its_transport_outcomes(
    pytester: pytest.Pytester,
) -> None:
    source = (
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def record(request):\n"
        "    yield\n"
        "    value = {'in-process': 'replied 1', 'http': 'raised X'}\n"
        "    request.node.user_properties.append(('transport_outcomes', value))\n"
        "\n"
        "@pytest.mark.spec('CON-1')\n"
        "def test_it(record): assert False\n"
    )
    (entry,) = run(pytester, source).values()
    assert entry == {
        "ids": ["CON-1"],
        "outcome": "failed",
        "transport_outcomes": {"in-process": "replied 1", "http": "raised X"},
    }


def test_other_properties_and_earlier_phases_are_not_transport_outcomes(
    pytester: pytest.Pytester,
) -> None:
    # Only a property named transport_outcomes counts, and only as the
    # teardown report has it: a value the call phase saw is not kept beside
    # a later error.
    source = (
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def record(request):\n"
        "    yield\n"
        "    props = request.node.user_properties\n"
        "    props.append(('transport_outcomes', {'http': 'x'}))\n"
        "\n"
        "@pytest.mark.spec('CON-1')\n"
        "def test_other(record_property):\n"
        "    record_property('duration', 1)\n"
        "\n"
        "@pytest.mark.spec('CON-1')\n"
        "def test_twice(request, record):\n"
        "    props = request.node.user_properties\n"
        "    props.append(('transport_outcomes', {'http': 'y'}))\n"
    )
    tests = {
        name.split("::", 1)[1]: entry for name, entry in run(pytester, source).items()
    }
    assert tests == {
        "test_other": {"ids": ["CON-1"], "outcome": "passed"},
        "test_twice": {"ids": ["CON-1"], "outcome": "error"},
    }


def test_a_report_path_of_another_kind_is_refused(pytester: pytest.Pytester) -> None:
    pytester.makeini(INI)
    pytester.makepyfile("def test_it(): pass\n")
    os.mkfifo(pytester.path / "report.json")
    result = pytester.runpytest(*ARGS, "--spec-report=report.json")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--spec-report cannot write *: not a regular file*"])
