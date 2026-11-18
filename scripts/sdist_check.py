"""Check that the sdist carries every file git tracks, and nothing else.

The tests and the documentation checks read files from across the repository:
the specification, the decision records, the site's sources, README.md and
pyproject.toml. A test run from an unpacked sdist can read only what the sdist
carries, so the sdist carries the whole tracked tree. Holding it to that, not
to a list of the files the tests read today, keeps a new test or a new
document from depending on a file the sdist leaves out.

The check compares the sdist's files, under its one top directory, with
``git ls-files`` at the repository root. Each file on one side only is a
problem, except the files the build backend writes (``PKG-INFO``).

Usage: ``python scripts/sdist_check.py dist/django_neolith-<version>.tar.gz``.
It prints one line per problem and a summary line, and exits 1 on a problem.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files the build backend writes into the sdist, which git does not track.
GENERATED = ("PKG-INFO",)


class SdistError(Exception):
    """The sdist or the repository cannot be read; the message says why."""


def tracked(root: Path) -> set[str]:
    """The files git tracks under ``root``, as POSIX paths relative to it."""
    try:
        done = subprocess.run(
            ["git", "ls-files", "-z"],  # noqa: S607
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SdistError(f"git ls-files failed in {root}: {error}") from error
    return {name for name in done.stdout.decode("utf-8").split("\0") if name}


def contents(sdist: Path) -> tuple[set[str], list[str]]:
    """The files in ``sdist``, relative to its one top directory.

    Returns:
        The files, and a problem for each member outside the top directory
        that the first member names.

    Raises:
        SdistError: ``sdist`` is not a readable tar archive.
    """
    try:
        with tarfile.open(sdist) as tar:
            members = tar.getmembers()
    except (OSError, tarfile.TarError) as error:
        raise SdistError(f"{sdist.name} cannot be read: {error}") from error
    files: set[str] = set()
    problems: list[str] = []
    top = members[0].name.partition("/")[0] if members else ""
    for member in members:
        first, _, rest = member.name.partition("/")
        if member.isdir():
            continue
        if first != top or not rest:
            problems.append(f"{member.name}: outside the sdist's one top directory")
            continue
        files.add(rest)
    return files, problems


def compare(tracked: set[str], carried: set[str]) -> list[str]:
    """A problem for each file that is tracked or carried, but not both."""
    problems = [
        f"{path}: tracked, but the sdist leaves it out"
        for path in sorted(tracked - carried)
    ]
    problems += [
        f"{path}: in the sdist, but not tracked"
        for path in sorted(carried - tracked - set(GENERATED))
    ]
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sdist", type=Path, help="the .tar.gz that build wrote")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        files = tracked(args.root)
        carried, problems = contents(args.sdist)
    except SdistError as error:
        print(f"sdist: {error}")
        return 1
    problems += compare(files, carried)
    for problem in problems:
        print(problem)
    if problems:
        noun = "problem" if len(problems) == 1 else "problems"
        print(f"sdist: {args.sdist.name} has {len(problems)} {noun}")
        return 1
    print(f"sdist: {args.sdist.name} carries the {len(files)} tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
