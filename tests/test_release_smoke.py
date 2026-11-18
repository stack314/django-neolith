"""The release workflow's smoke script runs against this tree.

The script in ``.github/workflows/release.yml`` runs only when a tag is
pushed, after the files are built, so a change to what the host admits at
start-up could break the next release without failing any pull request's
checks. This test takes the script from the workflow the way
``docs/releasing.md`` does and runs it in a fresh process, from a directory
with no source tree, so a pull request that changes start-up meets it.

CI installs the package, and there the script runs as written. A run
from the source tree has no installed distribution, so it drops the
script's assertions about the installed files and keeps the host it builds.
"""

import os
import subprocess
import sys
from pathlib import Path

import neolith

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
_OPENS = "venv/bin/python - <<"
_INDENT = " " * 10


def _script() -> str:
    """The smoke script, as ``docs/releasing.md``'s awk command extracts it."""
    lines: list[str] = []
    inside = False
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if not inside:
            inside = _OPENS in line
            continue
        if line.strip() == "EOF":
            break
        lines.append(line.removeprefix(_INDENT))
    assert "import neolith" in lines, "no smoke script found in release.yml"
    return "\n".join(lines) + "\n"


def _installed() -> bool:
    return "site-packages" in Path(neolith.__file__).parts


def test_the_release_smoke_script_passes(tmp_path: Path) -> None:
    script = _script()
    if not _installed():
        script = "\n".join(
            line for line in script.splitlines() if not line.startswith("assert ")
        )
    env = dict(os.environ, TAG=f"v{neolith.__version__}")
    if not _installed():
        source = str(Path(neolith.__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(
            [source, *filter(None, [env.get("PYTHONPATH")])]
        )
    done = subprocess.run(
        [sys.executable, "-"],
        input=script,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(f"neolith {neolith.__version__} on Django "), (
        done.stdout
    )
