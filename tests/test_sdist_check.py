"""The sdist check: the sdist carries every file git tracks, and nothing else."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts import sdist_check

TOP = "django_neolith-1.0"


def _sdist(tmp_path: Path, names: list[str], *, dirs: tuple[str, ...] = ()) -> Path:
    """An sdist at ``tmp_path/dist`` holding ``names``, each a small file."""
    path = tmp_path / "dist" / f"{TOP}.tar.gz"
    path.parent.mkdir(exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name in dirs:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            tar.addfile(member)
        for name in names:
            data = b"x\n"
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return path


def _repo(tmp_path: Path, paths: list[str]) -> Path:
    """A git repository at ``tmp_path/repo`` that tracks ``paths``."""
    root = tmp_path / "repo"
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x\n", encoding="utf-8")
    git = ["git", "-C", str(root)]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    if paths:
        # -f: an ignore file of the machine running the tests ignores nothing.
        subprocess.run([*git, "add", "-f", "--", *paths], check=True)  # noqa: S603
    return root


# Lower-case paths, so never one of the files the build writes (PKG-INFO).
_PATH = st.from_regex(r"[a-z]{1,3}(/[a-z]{1,3}){0,2}", fullmatch=True)


@given(tracked=st.sets(_PATH, max_size=8), carried=st.sets(_PATH, max_size=8))
def test_the_problems_name_exactly_the_files_on_one_side_only(
    tracked: set[str], carried: set[str]
) -> None:
    problems = sdist_check.compare(tracked, carried | set(sdist_check.GENERATED))
    named = {problem.split(": ")[0] for problem in problems}
    assert named == tracked ^ carried
    assert len(problems) == len(named)


def test_a_tracked_file_left_out_is_a_problem() -> None:
    assert sdist_check.compare({"a.md", "docs/adr/0001-x.md"}, {"a.md"}) == [
        "docs/adr/0001-x.md: tracked, but the sdist leaves it out"
    ]


def test_an_untracked_file_carried_is_a_problem() -> None:
    assert sdist_check.compare({"a.md"}, {"a.md", "notes.txt"}) == [
        "notes.txt: in the sdist, but not tracked"
    ]


def test_the_contents_are_the_files_under_the_one_top_directory(
    tmp_path: Path,
) -> None:
    sdist = _sdist(
        tmp_path,
        [f"{TOP}/PKG-INFO", f"{TOP}/a.md", f"{TOP}/docs/adr/0001-x.md"],
        dirs=(TOP, f"{TOP}/docs"),
    )
    assert sdist_check.contents(sdist) == (
        {"PKG-INFO", "a.md", "docs/adr/0001-x.md"},
        [],
    )


@pytest.mark.parametrize(
    ("names", "problem"),
    [
        (
            [f"{TOP}/a.md", "other-1.0/b.md"],
            "other-1.0/b.md: outside the sdist's one top directory",
        ),
        ([f"{TOP}/a.md", "b.md"], "b.md: outside the sdist's one top directory"),
    ],
)
def test_a_file_outside_the_top_directory_is_a_problem(
    tmp_path: Path, names: list[str], problem: str
) -> None:
    _, problems = sdist_check.contents(_sdist(tmp_path, names))
    assert problems == [problem]


def test_tracked_is_what_git_tracks(tmp_path: Path) -> None:
    root = _repo(tmp_path, [".gitignore", "a b.md", "docs/adr/0001-x.md"])
    (root / "untracked.txt").write_text("x", encoding="utf-8")
    assert sdist_check.tracked(root) == {".gitignore", "a b.md", "docs/adr/0001-x.md"}


def test_tracked_outside_a_repository_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(sdist_check.SdistError, match="git ls-files"):
        sdist_check.tracked(tmp_path)


def test_main_passes_when_the_sdist_is_the_tracked_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["a.md", "docs/adr/0001-x.md"])
    sdist = _sdist(
        tmp_path, [f"{TOP}/PKG-INFO", f"{TOP}/a.md", f"{TOP}/docs/adr/0001-x.md"]
    )
    assert sdist_check.main([str(sdist), "--root", str(root)]) == 0
    assert capsys.readouterr().out == (
        f"sdist: {TOP}.tar.gz carries the 2 tracked files\n"
    )


def test_main_fails_and_names_each_file_left_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["a.md", "docs/adr/0001-x.md", "spec/01-x.md"])
    sdist = _sdist(tmp_path, [f"{TOP}/PKG-INFO", f"{TOP}/a.md"])
    assert sdist_check.main([str(sdist), "--root", str(root)]) == 1
    assert capsys.readouterr().out == (
        "docs/adr/0001-x.md: tracked, but the sdist leaves it out\n"
        "spec/01-x.md: tracked, but the sdist leaves it out\n"
        f"sdist: {TOP}.tar.gz has 2 problems\n"
    )


def test_main_fails_on_a_file_that_is_not_an_sdist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["a.md"])
    bad = tmp_path / "bad.tar.gz"
    bad.write_text("not a tarball", encoding="utf-8")
    assert sdist_check.main([str(bad), "--root", str(root)]) == 1
    assert capsys.readouterr().out.startswith("sdist: bad.tar.gz cannot be read: ")


def test_main_counts_one_problem_in_the_singular(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, ["a.md"])
    sdist = _sdist(tmp_path, [f"{TOP}/PKG-INFO", f"{TOP}/a.md", f"{TOP}/b.md"])
    assert sdist_check.main([str(sdist), "--root", str(root)]) == 1
    assert capsys.readouterr().out == (
        f"b.md: in the sdist, but not tracked\nsdist: {TOP}.tar.gz has 1 problem\n"
    )


def test_a_name_that_is_not_utf8_compares_as_tarfile_reads_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = b"caf\xe9.txt"
    root = tmp_path / "repo"
    root.mkdir()
    (root / os.fsdecode(name)).write_text("x\n", encoding="utf-8")
    git = ["git", "-C", str(root)]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    subprocess.run([*git, "add", "-f", "--", os.fsdecode(name)], check=True)  # noqa: S603
    sdist = _sdist(tmp_path, [f"{TOP}/PKG-INFO", f"{TOP}/{os.fsdecode(name)}"])
    assert sdist_check.main([str(sdist), "--root", str(root)]) == 0
    assert (
        capsys.readouterr().out == f"sdist: {TOP}.tar.gz carries the 1 tracked file\n"
    )
