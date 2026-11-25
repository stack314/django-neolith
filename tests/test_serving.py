"""The helper that starts hosts for tests (``tests/serving.py``), on a tiny
host written to ``tmp_path``: it starts, answers, stops on SIGTERM, and runs
the one-shot commands, waiting on what the host prints, never on time (T3).
"""

import json
import signal
import socket
import sys
import textwrap
from pathlib import Path

import pytest

from tests import serving

# Not a secret: it only shows that the environment reaches the host.
_WORD = "a-word-from-the-environment"

_FILES = {
    "tiny/__init__.py": "",
    "tiny/apps.py": """\
        from neolith import NeolithAppConfig


        class TinyConfig(NeolithAppConfig):
            name = "tiny"
            label = "tiny"
            resources = ("database",)
            default_auto_field = "django.db.models.AutoField"
        """,
    "tiny/models.py": """\
        from django.db import models


        class Word(models.Model):
            text = models.TextField()
        """,
    "tiny/migrations/__init__.py": "",
    "tiny/migrations/0001_initial.py": """\
        from django.db import migrations, models


        class Migration(migrations.Migration):
            initial = True
            dependencies = []
            operations = [
                migrations.CreateModel(
                    name="Word",
                    fields=[
                        (
                            "id",
                            models.AutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("text", models.TextField()),
                    ],
                ),
            ]
        """,
    "tiny/urls.py": """\
        import json
        import os
        import sys

        from django.http import HttpResponse
        from django.urls import path


        def word(request):
            return HttpResponse(os.environ["TINY_WORD"])


        def seen(request):
            return HttpResponse(
                json.dumps(
                    {"names": sorted(os.environ), "warnoptions": sys.warnoptions}
                )
            )


        urlpatterns = [path("word/", word), path("seen/", seen)]
        """,
    "tiny_host.py": """\
        import os

        SECRET_KEY = "a key for one test process only"
        ALLOWED_HOSTS = ["127.0.0.1", "[::1]"]
        MIDDLEWARE = [
            "neolith.middleware.RootMiddleware",
            "neolith.middleware.PrincipalMiddleware",
        ]
        ROOT_URLCONF = "tiny.urls"
        USE_TZ = True
        NEOLITH = {
            "DATABASES": {
                "tiny": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": os.path.join(os.environ["TINY_DATA"], "tiny.sqlite3"),
                }
            }
        }
        """,
    "broken_host.py": """\
        raise RuntimeError("this host cannot start")
        """,
}


@pytest.fixture
def tiny(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """The tiny host's directory, and the environment it reads."""
    root = tmp_path / "site"
    for name, text in _FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    return root, {"TINY_WORD": _WORD, "TINY_DATA": str(data)}


def _migrated(root: Path, env: dict[str, str]) -> None:
    done = serving.once("migrate", ["tiny"], settings="tiny_host", cwd=root, env=env)
    assert done.returncode == 0, done.stderr


def test_a_host_starts_answers_and_stops_on_sigterm(
    tiny: tuple[Path, dict[str, str]],
) -> None:
    root, env = tiny
    _migrated(root, env)
    with serving.served(["tiny"], settings="tiny_host", cwd=root, env=env) as host:
        assert host.port > 0
        assert host.request("GET", "/word/") == (200, _WORD.encode())
        assert host.wait_for("GET /word/")
        assert host.stop() == 0
    assert host.process.returncode == 0
    assert host.lines[-1][1].startswith(serving.STOPPED)
    assert _WORD not in host.printed()
    for argument in host.arguments:
        assert _WORD not in argument
        assert env["TINY_DATA"] not in argument


def test_migrate_and_check_run_once(tiny: tuple[Path, dict[str, str]]) -> None:
    root, env = tiny
    _migrated(root, env)
    checked = serving.once("check", ["tiny"], settings="tiny_host", cwd=root, env=env)
    assert checked.returncode == 0, checked.stderr
    assert "neolith check: 0 errors" in checked.stdout + checked.stderr


def test_a_host_that_ends_fails_the_wait_with_what_it_printed(
    tiny: tuple[Path, dict[str, str]],
) -> None:
    root, env = tiny
    host = serving.Host(["tiny"], settings="broken_host", cwd=root, env=env)
    try:
        with pytest.raises(pytest.fail.Exception) as failed:
            host.serving()
        assert "before the host ended" in str(failed.value)
        assert "this host cannot start" in str(failed.value)
        assert host.ended() == 1
    finally:
        host.close()


def test_a_host_killed_by_close_is_gone(tiny: tuple[Path, dict[str, str]]) -> None:
    root, env = tiny
    _migrated(root, env)
    with serving.served(["tiny"], settings="tiny_host", cwd=root, env=env) as host:
        pass
    assert host.process.returncode == -signal.SIGKILL


@pytest.mark.parametrize(
    ("apps", "settings"),
    [
        (["tiny"], "tiny_host --verbose"),
        (["tiny", "-W ignore"], "tiny_host"),
        (["tiny"], "SECRET=value"),
        ([], "tiny_host"),
    ],
)
def test_only_module_names_reach_the_command_line(
    tmp_path: Path, apps: list[str], settings: str
) -> None:
    with pytest.raises(ValueError, match=r"module name|at least one app"):
        serving.Host(apps, settings=settings, cwd=tmp_path, env={})
    with pytest.raises(ValueError, match=r"module name|at least one app"):
        serving.once("check", apps, settings=settings, cwd=tmp_path, env={})


def test_apps_given_as_one_string_are_refused(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="not one string"):
        serving.Host("tiny", settings="tiny_host", cwd=tmp_path, env={})


def test_only_migrate_and_check_run_once(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one-shot"):
        serving.once("run", ["tiny"], settings="tiny_host", cwd=tmp_path, env={})


def test_the_host_sees_only_the_given_environment_and_warnings_as_errors(
    tiny: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, env = tiny
    monkeypatch.setenv("NEOLITH_TEST_SENTINEL", "from the parent")
    _migrated(root, env)
    with serving.served(["tiny"], settings="tiny_host", cwd=root, env=env) as host:
        status, body = host.request("GET", "/seen/")
    assert status == 200
    seen = json.loads(body)
    # Python itself may set LC_CTYPE when it coerces the C locale (PEP 538).
    assert set(seen["names"]) - {"LC_CTYPE"} == {"PATH", "PYTHONPATH", *env}
    assert seen["warnoptions"] == ["error"]
    assert host.arguments[:5] == [sys.executable, "-W", "error", "-m", "neolith"]


def test_a_line_printed_before_the_last_one_waited_for_can_still_be_waited_for(
    tiny: tuple[Path, dict[str, str]],
) -> None:
    root, env = tiny
    _migrated(root, env)
    with serving.served(["tiny"], settings="tiny_host", cwd=root, env=env) as host:
        assert host.request("GET", "/word/")[0] == 200
        assert host.request("GET", "/seen/")[0] == 200
        assert "GET /seen/" in host.wait_for("GET /seen/")
        assert "GET /word/" in host.wait_for("GET /word/")
        assert host.stop() == 0


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback here")
def test_a_host_bound_to_ipv6_loopback_answers(
    tiny: tuple[Path, dict[str, str]],
) -> None:
    root, env = tiny
    _migrated(root, env)
    with serving.served(
        ["tiny"], settings="tiny_host", cwd=root, env=env, bind="[::1]:0"
    ) as host:
        assert host.request("GET", "/word/") == (200, _WORD.encode())


@pytest.mark.parametrize(
    "bind", ["127.0.0.1", "[::1]", "[fe80::1%eth0]:0", "0x7f000001:0", "a b:0", ":0"]
)
def test_a_bind_address_neolith_would_refuse_is_refused_first(
    tmp_path: Path, bind: str
) -> None:
    with pytest.raises(ValueError, match="bind"):
        serving.Host(["tiny"], settings="tiny_host", cwd=tmp_path, env={}, bind=bind)
