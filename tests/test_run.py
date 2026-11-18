"""``neolith run`` and ``neolith migrate``: the lifecycle in order, Serve and
Drain, and the schema step (spec 05; ADR 0022 Decisions 9 to 12).

A process hosts one set of apps, and Drain answers a signal, so every test
of the command line starts ``python -m neolith`` as a process of its own,
in a directory that holds the deployer's settings module. A test holds a
request in flight through named pipes (``tests/run_apps/desk/views.py``),
and waits on lines the host prints, never on time (T3).
"""

import contextlib
import http.client
import importlib.metadata
import ipaddress
import os
import queue
import signal
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neolith import _checks, _cli, _server

DESK = "tests.run_apps.desk"
SQLITE = "django.db.backends.sqlite3"
# A bound no test waits out: a test that would wait for it fails on its own
# timeout instead.
LONG = 600
_WAIT = 60

_DEPLOYER = """\
SECRET_KEY = "a key for one test process only"
ALLOWED_HOSTS = ["127.0.0.1", "[::1]", "localhost"]
MIDDLEWARE = ["neolith.middleware.RootMiddleware"]
ROOT_URLCONF = "tests.run_apps.desk.urls"
USE_TZ = True
NEOLITH = {neolith!r}
"""


class Site:
    """A directory with the deployer's settings module ``deployer``, a
    SQLite file per app, and the pipes the desk app's views wait on."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pipes = root / "pipes"
        self.pipes.mkdir()

    def database(self, label: str) -> Path:
        return self.root / f"{label}.sqlite3"

    def databases(self, *labels: str) -> dict[str, dict[str, str]]:
        return {
            label: {"ENGINE": SQLITE, "NAME": str(self.database(label))}
            for label in labels
        }

    def deployer(
        self, neolith: dict[str, Any] | None = None, *, below: str = ""
    ) -> None:
        if neolith is None:
            neolith = {"DATABASES": self.databases("desk")}
        text = _DEPLOYER.format(neolith=neolith) + textwrap.dedent(below)
        (self.root / "deployer.py").write_text(text, encoding="utf-8")

    def env(self) -> dict[str, str]:
        return {
            "PYTHONPATH": os.pathsep.join([str(self.root), *sys.path]),
            "NEOLITH_RUN_PIPES": str(self.pipes),
            "PATH": os.environ.get("PATH", ""),
        }

    def neolith(
        self, *arguments: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-W", "error", "-m", "neolith", *arguments],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.root,
            timeout=_WAIT,
            env=env or self.env(),
        )

    def migrate(self, *apps: str) -> subprocess.CompletedProcess[str]:
        return self.neolith("migrate", *(apps or (DESK,)), "--settings", "deployer")

    def hold(self, name: str) -> "Held":
        return Held(self.pipes, name)

    def serve(self, *apps: str, bind: str = "127.0.0.1:0") -> "Served":
        return Served(self, list(apps or (DESK,)), bind)


class Held:
    """A request held in flight by the desk app's views."""

    def __init__(self, pipes: Path, name: str) -> None:
        self.started = pipes / f"{name}.started"
        self.release = pipes / f"{name}.release"
        os.mkfifo(self.started)
        os.mkfifo(self.release)

    def wait_started(self) -> None:
        _bounded(lambda: self.started.read_text(encoding="utf-8"))

    def let_go(self) -> None:
        _bounded(lambda: self.release.write_text("go\n", encoding="utf-8"))


def _bounded(work: Callable[[], object]) -> None:
    """Run ``work``, which opens a pipe and so blocks until the other end
    opens it, on a thread of its own, failing the test after ``_WAIT``."""
    done = threading.Event()
    failure: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as error:
            failure.append(error)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(_WAIT), "a pipe was never opened at its other end"
    if failure:
        raise failure[0]


class Served:
    """``neolith run`` in a process of its own, with what it prints."""

    def __init__(self, site: Site, apps: list[str], bind: str) -> None:
        self.process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-W",
                "error",
                "-m",
                "neolith",
                "run",
                *apps,
                "--settings",
                "deployer",
                "--bind",
                bind,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=site.root,
            env=site.env(),
        )
        self.lines: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.seen: list[tuple[str, str]] = []
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.readers = [
            threading.Thread(target=self._read, args=(name, stream), daemon=True)
            for name, stream in (
                ("out", self.process.stdout),
                ("err", self.process.stderr),
            )
        ]
        for reader in self.readers:
            reader.start()
        self.port = 0
        self.ends = 0

    def _read(self, name: str, stream: IO[str]) -> None:
        for line in stream:
            self.lines.put((name, line.rstrip("\n")))
        self.lines.put((name, None))

    def wait_for(self, text: str) -> str:
        """The first line, on either stream, that contains ``text``."""
        while self.ends < 2:
            try:
                name, line = self.lines.get(timeout=_WAIT)
            except queue.Empty:
                break
            if line is None:
                self.ends += 1
                continue
            self.seen.append((name, line))
            if text in line:
                return line
        pytest.fail(f"no line with {text!r}; printed: {self.seen}")

    def serving(self) -> int:
        line = self.wait_for("neolith: serving ")
        address = line.split(" at http://", 1)[1].split("/", 1)[0]
        self.port = int(address.rsplit(":", 1)[1])
        return self.port

    def stop(self, number: int = signal.SIGTERM) -> None:
        self.process.send_signal(number)

    def ended(self) -> tuple[int, list[tuple[str, str]]]:
        """The exit status, and every line printed."""
        status = self.process.wait(timeout=_WAIT)
        while self.ends < 2:
            name, line = self.lines.get(timeout=_WAIT)
            if line is None:
                self.ends += 1
            else:
                self.seen.append((name, line))
        return status, self.seen

    def close(self) -> None:
        """Kill the process if it still runs, and close its pipes."""
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=_WAIT)
        for reader in self.readers:
            reader.join(timeout=_WAIT)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


@pytest.fixture
def served(site: Site) -> Iterator[Callable[..., Served]]:
    started: list[Served] = []

    def start(*apps: str, bind: str = "127.0.0.1:0") -> Served:
        process = site.serve(*apps, bind=bind)
        started.append(process)
        return process

    yield start
    for process in started:
        process.close()


# Every socket a test opens, closed after the test.
_OPENED: list[socket.socket] = []


@pytest.fixture(autouse=True)
def _sockets_closed() -> Iterator[None]:
    yield
    while _OPENED:
        _OPENED.pop().close()


def connect(port: int, host: str = "127.0.0.1") -> socket.socket:
    connection = socket.create_connection((host, port), timeout=_WAIT)
    _OPENED.append(connection)
    return connection


def send(connection: socket.socket, path: str) -> None:
    connection.sendall(
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode("ascii")
    )


def response(connection: socket.socket) -> tuple[int, bytes, str | None]:
    """The status, body and ``Connection`` header of the next response."""
    reply = http.client.HTTPResponse(connection)
    reply.begin()
    return reply.status, reply.read(), reply.getheader("Connection")


def closed_by_server(connection: socket.socket) -> bool:
    """Whether the server has closed the connection: a read finds its end."""
    try:
        return connection.recv(1) == b""
    except ConnectionResetError:
        return True


def lines(seen: list[tuple[str, str]], stream: str) -> list[str]:
    return [line for name, line in seen if name == stream]


# The command line's own input: usage errors leave with status 2.


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["serve", DESK, "--settings", "deployer"],
        ["run", "--settings", "deployer"],
        ["run", DESK],
        ["run", DESK, "--settings", "deployer", "--settings", "deployer"],
        ["run", DESK, "--set", "deployer"],
        ["run", DESK, "--settings", "deployer", "--bind", "8000"],
        ["run", DESK, "--settings", "deployer", "--bind", "1:1", "--bind", "1:1"],
        ["run", DESK, "--settings", "deployer", "--migrate"],
        ["migrate", DESK, "--settings", "deployer", "--bind", "127.0.0.1:0"],
        ["migrate", DESK, "--settings", "deployer", "--check"],
    ],
    ids=[
        "no command",
        "unknown command",
        "no app",
        "no settings",
        "settings twice",
        "abbreviated option",
        "malformed bind",
        "bind twice",
        "unknown option",
        "bind to migrate",
        "check to migrate",
    ],
)
def test_a_mistake_in_the_arguments_exits_with_usage_and_status_2(
    site: Site, arguments: list[str]
) -> None:
    site.deployer()
    ran = site.neolith(*arguments)
    assert ran.returncode == 2, ran.stderr
    assert "usage: neolith" in ran.stderr
    assert not site.database("desk").exists()


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    ("text", "admitted"),
    [
        ("127.0.0.1:8000", ("127.0.0.1", 8000, False)),
        ("0.0.0.0:0", ("0.0.0.0", 0, False)),  # noqa: S104
        ("10.1.2.3:65535", ("10.1.2.3", 65535, False)),
        ("localhost:8000", ("localhost", 8000, False)),
        ("api.example.com:80", ("api.example.com", 80, False)),
        ("a-b.c1:1", ("a-b.c1", 1, False)),
        ("[::1]:0", ("::1", 0, True)),
        ("[2001:db8::1]:8080", ("2001:db8::1", 8080, True)),
        ("[::ffff:10.0.0.1]:80", ("::ffff:10.0.0.1", 80, True)),
    ],
)
def test_bind_admits_an_address_and_a_port(
    text: str, admitted: tuple[str, int, bool]
) -> None:
    assert _cli.parse_bind(text) == admitted


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize(
    "text",
    [
        "",
        "8000",
        ":8000",
        "127.0.0.1",
        "127.0.0.1:",
        "127.0.0.1:65536",
        "127.0.0.1:080",
        "127.0.0.1:-1",
        "127.0.0.1:+80",
        "127.0.0.1: 80",
        "127.0.0.1:\uff18\uff10",
        "127.0.0.1:80:80",
        "01.2.3.4:80",
        "1.2.3:80",
        "256.1.1.1:80",
        "-host:80",
        "host-:80",
        "a..b:80",
        "host_name:80",
        "host.:80",
        f"{'a' * 64}:80",
        f"{'a.' * 127}a:80",
        "hôst:80",
        " localhost:80",
        "::1:80",
        "[::1]",
        "[::1]80",
        "[::1:80",
        "[fe80::1%eth0]:80",
        "[127.0.0.1]:80",
        "[]:80",
        "[::1]:65536",
    ],
)
def test_bind_refuses_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match=r"^--bind: expected <host>:<port>"):
        _cli.parse_bind(text)


def test_the_package_installs_one_console_script() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    found = [s for s in scripts if s.value.startswith("neolith")]
    assert [(s.name, s.value) for s in found] == [("neolith", "neolith._cli:main")]


# The deployer's settings module.


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize("command", ["run", "migrate"])
@pytest.mark.parametrize(
    ("below", "message"),
    [
        (
            "from neolith.host import build_settings\n"
            f"globals().update(build_settings([{DESK!r}], globals()))\n",
            "neolith: settings: deployer already calls build_settings; pass the "
            "module that holds the deployer's names, or serve this one through "
            "DJANGO_SETTINGS_MODULE",
        ),
        (
            "from django.conf import settings\nsettings.DEBUG\n",
            "neolith: settings: deployer cannot be imported: ImproperlyConfigured: "
            "Requested setting DEBUG, but settings are not configured.",
        ),
        (
            "from django.conf import settings\nsettings.configure()\n",
            "neolith: settings: deployer configured Django's settings while it was "
            "imported; it holds the deployer's names, which are read before "
            "settings exist",
        ),
        (
            "raise RuntimeError('the module fails')\n",
            "neolith: settings: deployer cannot be imported: RuntimeError: the "
            "module fails",
        ),
    ],
    ids=["calls the builder", "reads a setting", "configures", "raises"],
)
def test_a_settings_module_of_the_wrong_kind_is_refused(
    site: Site, command: str, below: str, message: str
) -> None:
    site.deployer(below=below)
    ran = site.neolith(command, DESK, "--settings", "deployer")
    assert ran.returncode == 1, ran.stderr
    assert message in ran.stderr.splitlines()[0] + ran.stderr, ran.stderr
    assert not site.database("desk").exists()


@pytest.mark.spec("APP-16")
def test_a_settings_module_that_cannot_be_found_is_refused(site: Site) -> None:
    ran = site.neolith("run", DESK, "--settings", "elsewhere")
    assert ran.returncode == 1, ran.stderr
    assert (
        "neolith: settings: elsewhere cannot be imported: ModuleNotFoundError: No "
        "module named 'elsewhere'" in ran.stderr
    )


@pytest.mark.spec("APP-16")
@pytest.mark.parametrize("command", ["run", "migrate"])
def test_a_default_database_is_refused_with_the_other_settings_refusals(
    site: Site, command: str
) -> None:
    """A host named on the command line is closed (ADR 0022 Decision 4); the
    refusal comes with the other refusals of Configure."""
    site.deployer(
        {
            "DATABASES": {**site.databases("desk", "default")},
            "OTHER": 1,
        }
    )
    ran = site.neolith(command, DESK, "--settings", "deployer")
    assert ran.returncode == 1, ran.stderr
    refusals = [line for line in ran.stderr.splitlines() if line.startswith("neolith:")]
    assert refusals == [
        "neolith: NEOLITH: unknown key 'OTHER'; the keys are DATABASES, "
        "REQUEST_DEADLINE, UNDECLARED_APPS",
        "neolith: database default: neolith run hosts declared apps only in 0.1",
    ]


def test_the_environment_s_settings_module_is_ignored(site: Site) -> None:
    """``--settings`` names the deployer's module; ``DJANGO_SETTINGS_MODULE``
    names a whole settings module, which the command line never reads."""
    (site.root / "whole.py").write_text(
        "raise RuntimeError('DJANGO_SETTINGS_MODULE was read')\n", encoding="utf-8"
    )
    site.deployer(below="from django.conf import settings\nsettings.DEBUG\n")
    env = {**site.env(), "DJANGO_SETTINGS_MODULE": "whole"}
    ran = site.neolith("migrate", DESK, "--settings", "deployer", env=env)
    assert ran.returncode == 1
    assert "Requested setting DEBUG, but settings are not configured" in ran.stderr
    assert "was read" not in ran.stderr


# The phases, in order.


@pytest.mark.spec("APP-1", "APP-38")
@pytest.mark.parametrize(
    ("apps", "refusal", "later"),
    [
        (
            ["tests.run_apps.refused", DESK],
            "app tests.run_apps.refused.apps.RefusedConfig: resources: 'database' "
            "is listed twice; a kind is listed once",
            "neolith.E",
        ),
        (
            [DESK, "tests.run_apps.needs"],
            "(neolith.E004) app contenttypes: has models and no declaration, and "
            "this host has no default database; it is a battery of needs",
            "neolith.E018",
        ),
        (
            [DESK],
            "(neolith.E018) database desk: no database at {desk}; the database "
            "is in memory, or nothing has created it yet (the migrate step creates "
            "the aliases it migrates)",
            "neolith: serving",
        ),
    ],
    ids=["declare", "set-up", "boot"],
)
def test_a_phase_that_refuses_stops_the_host_before_the_next(
    site: Site, apps: list[str], refusal: str, later: str
) -> None:
    site.deployer()
    ran = site.neolith("run", *apps, "--settings", "deployer")
    assert ran.returncode == 1, ran.stderr
    printed = ran.stdout + ran.stderr
    assert f"neolith: {refusal.format(desk=site.database('desk'))}" in printed
    assert later not in printed
    assert "neolith: serving" not in printed


@pytest.mark.spec("APP-33", "APP-46")
def test_a_host_never_changes_a_schema_itself(site: Site) -> None:
    """Neither a missing file nor a file whose app is not migrated is
    created or changed by starting the host."""
    site.deployer()
    ran = site.neolith("run", DESK, "--settings", "deployer")
    assert ran.returncode == 1
    assert not site.database("desk").exists()
    connection = sqlite3.connect(site.database("desk"))
    connection.execute("CREATE TABLE other (id INTEGER)")
    connection.commit()
    connection.close()
    before = site.database("desk").read_bytes()
    ran = site.neolith("run", DESK, "--settings", "deployer")
    assert ran.returncode == 1
    assert (
        "neolith: (neolith.E009) app desk: database desk has 1 unapplied "
        "migrations, the first is 0001_initial; run the migrate step" in ran.stderr
    )
    assert site.database("desk").read_bytes() == before


@pytest.mark.spec("APP-1")
def test_an_address_in_use_stops_the_host(site: Site) -> None:
    site.deployer()
    assert site.migrate().returncode == 0
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        ran = site.neolith(
            "run", DESK, "--settings", "deployer", "--bind", f"127.0.0.1:{port}"
        )
    assert ran.returncode == 1
    assert f"neolith: --bind 127.0.0.1:{port}: cannot listen: " in ran.stderr
    assert "neolith: serving" not in ran.stdout


# One app, alone, on SQLite (spec 06, item 5).


@pytest.mark.spec("APP-23", "APP-38", "APP-43")
@pytest.mark.parametrize("bind", ["127.0.0.1:0", "[::1]:0"], ids=["ipv4", "ipv6"])
def test_one_app_boots_alone_on_sqlite_and_stops(
    site: Site, served: Callable[..., Served], bind: str
) -> None:
    site.deployer()
    migrated = site.migrate()
    assert migrated.returncode == 0, migrated.stderr
    connection = sqlite3.connect(site.database("desk"))
    tables = sorted(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    )
    connection.execute("INSERT INTO desk_note (text) VALUES ('one'), ('two')")
    connection.commit()
    connection.close()
    assert tables == ["desk_note", "django_migrations", "sqlite_sequence"]
    host = served(bind=bind)
    port = host.serving()
    line = host.seen[-1][1]
    assert line.endswith(
        "with Django's development server, which is not for production"
    )
    assert f"neolith: serving {DESK} at http://{bind.rsplit(':', 1)[0]}:{port}/" in line
    with connect(port, "::1" if bind.startswith("[") else "127.0.0.1") as client:
        send(client, "/count/")
        assert response(client)[:2] == (200, b"2")
    host.stop()
    status, seen = host.ended()
    assert status == 0, seen
    assert lines(seen, "out")[-1].startswith("neolith: stopped; ")


# Drain.


@pytest.mark.spec("APP-39", "APP-40", "APP-41", "APP-43")
@pytest.mark.parametrize(
    "number", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"]
)
def test_a_request_in_flight_finishes_and_nothing_new_is_served(
    site: Site, served: Callable[..., Served], number: int
) -> None:
    site.deployer({"DATABASES": site.databases("desk"), "REQUEST_DEADLINE": LONG})
    assert site.migrate().returncode == 0
    held = site.hold("a")
    host = served()
    port = host.serving()
    idle = connect(port)
    send(idle, "/ping/")
    assert response(idle) == (200, b"pong", None)
    busy = connect(port)
    send(busy, "/hold/a/")
    held.wait_started()
    host.stop(number)
    host.wait_for("neolith: draining: ")
    # No new connection, and the idle one is closed, so a request sent on
    # it is not served.
    with pytest.raises(ConnectionRefusedError):
        connect(port)
    assert closed_by_server(idle)
    with contextlib.suppress(OSError):
        send(idle, "/ping/")
    assert closed_by_server(idle)
    # The request in flight makes its contract call and finishes, and its
    # connection closes after the response.
    held.let_go()
    assert response(busy)[:2] == (200, b"a 0")
    assert closed_by_server(busy)
    status, seen = host.ended()
    assert status == 0, seen
    assert "neolith: stopped; every request in flight finished" in lines(seen, "out")


@pytest.mark.spec("APP-41", "APP-43")
def test_a_streamed_response_in_flight_finishes(
    site: Site, served: Callable[..., Served]
) -> None:
    site.deployer({"DATABASES": site.databases("desk"), "REQUEST_DEADLINE": LONG})
    assert site.migrate().returncode == 0
    held = site.hold("s")
    host = served()
    port = host.serving()
    client = connect(port)
    send(client, "/stream/s/")
    held.wait_started()
    host.stop()
    host.wait_for("neolith: draining: ")
    held.let_go()
    assert response(client) == (200, b"s 0", "close")
    status, seen = host.ended()
    assert status == 0, seen


@pytest.mark.spec("APP-41", "APP-43", "APP-44")
def test_a_request_that_outlasts_the_bound_is_cut_and_named(
    site: Site, served: Callable[..., Served]
) -> None:
    site.deployer({"DATABASES": site.databases("desk"), "REQUEST_DEADLINE": 0.5})
    assert site.migrate().returncode == 0
    held = site.hold("slow")
    host = served()
    port = host.serving()
    client = connect(port)
    send(client, "/hold/slow/")
    held.wait_started()
    host.stop()
    status, seen = host.ended()
    assert status == 1, seen
    assert (
        "neolith: drain: a request was still in flight after 0.5 seconds: GET "
        "/hold/slow/ HTTP/1.1" in lines(seen, "err")
    )
    assert closed_by_server(client)


@pytest.mark.spec("APP-44", "APP-45")
def test_a_second_signal_stops_at_once(
    site: Site, served: Callable[..., Served]
) -> None:
    site.deployer({"DATABASES": site.databases("desk"), "REQUEST_DEADLINE": LONG})
    assert site.migrate().returncode == 0
    held = site.hold("b")
    host = served()
    port = host.serving()
    client = connect(port)
    send(client, "/hold/b/")
    held.wait_started()
    host.stop()
    host.wait_for("neolith: draining: ")
    host.stop(signal.SIGINT)
    status, seen = host.ended()
    assert status == 1, seen
    assert (
        "neolith: drain: stopped at once with a request in flight: GET /hold/b/ "
        "HTTP/1.1" in lines(seen, "err")
    )


@pytest.mark.spec("APP-39", "APP-43")
def test_a_signal_delivered_to_a_request_s_thread_starts_drain(
    site: Site, served: Callable[..., Served]
) -> None:
    """The kernel may deliver a signal meant for the process to any thread;
    here it reaches the thread serving the request, which finishes."""
    site.deployer({"DATABASES": site.databases("desk"), "REQUEST_DEADLINE": LONG})
    assert site.migrate().returncode == 0
    host = served()
    port = host.serving()
    client = connect(port)
    send(client, "/signal/")
    assert response(client)[:2] == (200, b"sent")
    host.wait_for("neolith: draining: ")
    status, seen = host.ended()
    assert status == 0, seen


@pytest.mark.spec("APP-44", "APP-45")
def test_a_second_signal_delivered_to_a_request_s_thread_stops_at_once(
    site: Site, served: Callable[..., Served]
) -> None:
    site.deployer({"DATABASES": site.databases("desk"), "REQUEST_DEADLINE": LONG})
    assert site.migrate().returncode == 0
    stuck, signalling = site.hold("stuck"), site.hold("sig")
    host = served()
    port = host.serving()
    first, second = connect(port), connect(port)
    send(first, "/hold/stuck/")
    stuck.wait_started()
    send(second, "/signal/sig/")
    signalling.wait_started()
    host.stop()
    host.wait_for("neolith: draining: ")
    signalling.let_go()
    status, seen = host.ended()
    assert status == 1, seen
    assert (
        "neolith: drain: stopped at once with a request in flight: GET /hold/stuck/ "
        "HTTP/1.1" in lines(seen, "err")
    )


@pytest.mark.spec("APP-43")
def test_a_request_that_a_signal_receiver_ended_is_not_counted_as_in_flight(
    site: Site, served: Callable[..., Served]
) -> None:
    """Django sends ``request_started`` with ``send``, so a receiver that
    raises ends the request before ``request_finished`` (ADR 0022 fact 13);
    the count does not rest on that pair."""
    site.deployer({"DATABASES": site.databases("desk"), "REQUEST_DEADLINE": LONG})
    assert site.migrate().returncode == 0
    host = served()
    port = host.serving()
    client = connect(port)
    send(client, "/boom/")
    assert response(client)[0] == 500
    host.stop()
    status, seen = host.ended()
    assert status == 0, seen


@pytest.mark.spec("APP-42")
@pytest.mark.parametrize(
    ("deadline", "bound"),
    [(None, _checks.DEFAULT_REQUEST_DEADLINE), (0.001, 0.001), (45, 45.0)],
)
def test_drain_is_bounded_by_the_request_deadline_or_30_seconds(
    deadline: float | None, bound: float
) -> None:
    assert _server.drain_bound(deadline) == bound
    assert _checks.DEFAULT_REQUEST_DEADLINE == 30


class _Handler:
    """What the connections table reads of a request handler."""

    def __init__(self) -> None:
        self.connection, self.peer = socket.socketpair()
        _OPENED.extend((self.connection, self.peer))
        self.peer.settimeout(_WAIT)

    def shut(self) -> bool:
        """Whether the host shut the connection down."""
        return self.peer.recv(1) == b""

    def open(self) -> bool:
        """Whether the connection is still open for the host to answer."""
        self.peer.setblocking(False)
        try:
            self.peer.recv(1)
        except BlockingIOError:
            return True
        finally:
            self.peer.settimeout(_WAIT)
        return False


LINE = b"GET /a/ HTTP/1.1\r\n"


@pytest.mark.spec("APP-40", "APP-41")
def test_one_lock_decides_whether_a_request_began_before_drain() -> None:
    table = _server.Connections()
    early, late, idle = _Handler(), _Handler(), _Handler()
    for handler in (early, late, idle):
        assert table.opened(handler)
    # A request whose line was read before Drain is served, and its
    # connection is left open until its response ends.
    assert table.begin(early, LINE)
    assert table.drain() == 1
    assert early.open()
    assert late.shut()
    assert idle.shut()
    # A line read after Drain began is not served, nor is a connection
    # accepted after it.
    assert not table.begin(late, LINE)
    newer = _Handler()
    assert not table.opened(newer)
    # The request in flight ends, and its connection closes.
    assert table.in_flight() == ["GET /a/ HTTP/1.1"]
    assert table.ended(early) is True
    assert table.in_flight() == []
    for handler in (early, late, idle):
        table.closed(handler)


@pytest.mark.spec("APP-41")
def test_a_request_that_ends_before_drain_leaves_its_connection_open() -> None:
    table = _server.Connections()
    handler = _Handler()
    assert table.opened(handler)
    assert table.begin(handler, LINE)
    assert table.ended(handler) is False
    assert handler.open()
    assert table.drain() == 0
    assert handler.shut()


@pytest.mark.spec("APP-44")
def test_cutting_names_each_request_and_shuts_its_connection() -> None:
    table = _server.Connections()
    handler = _Handler()
    assert table.opened(handler)
    assert table.begin(handler, b"GET /\xff\x00" + b"x" * 300 + b" HTTP/1.1\r\n")
    assert table.drain() == 1
    (cut,) = table.cut()
    assert cut.startswith("GET /\xff?xxx")
    assert len(cut) <= 203
    assert cut.endswith("...")
    assert handler.shut()


def test_python_m_neolith_is_the_command_line(site: Site) -> None:
    ran = site.neolith("--help")
    assert ran.returncode == 0
    assert ran.stdout.startswith("usage: neolith ")


# The migrate step.


@pytest.mark.spec("APP-23")
def test_the_migrate_step_migrates_each_app_on_its_own_database(site: Site) -> None:
    site.deployer({"DATABASES": site.databases("desk", "fresh")})
    ran = site.migrate(DESK, "tests.boot_apps.fresh")
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.index("Migrating app desk on database desk") < ran.stdout.index(
        "Migrating app fresh on database fresh"
    )
    rows = {}
    for label in ("desk", "fresh"):
        connection = sqlite3.connect(site.database(label))
        rows[label] = sorted(
            connection.execute("SELECT app, name FROM django_migrations")
        )
        connection.close()
    assert rows == {
        "desk": [("desk", "0001_initial")],
        "fresh": [("fresh", "0001_initial"), ("fresh", "0002_more")],
    }
    again = site.migrate(DESK, "tests.boot_apps.fresh")
    assert again.returncode == 0, again.stderr
    assert "No migrations to apply." in again.stdout


@pytest.mark.spec("APP-47", "APP-48")
@pytest.mark.parametrize(
    ("apps", "refusals"),
    [
        (
            ["tests.boot_apps.bare", "tests.boot_apps.fresh"],
            ["(neolith.E008) app bare: has a database and no migrations"],
        ),
        (
            ["tests.boot_apps.empty", "tests.boot_apps.fresh"],
            ["(neolith.E008) app empty: has a database and no migrations"],
        ),
        (
            ["tests.boot_apps.fresh", "tests.boot_apps.leaning"],
            ["(neolith.E013) app leaning: migration 0001_initial depends on fresh"],
        ),
        (
            ["tests.boot_apps.fresh", "tests.boot_apps.forked"],
            ["(neolith.E011) app forked: conflicting migrations 0002_a, 0002_b"],
        ),
        (
            [
                "tests.boot_apps.bare",
                "tests.boot_apps.fresh",
                "tests.boot_apps.leaning",
            ],
            [
                "(neolith.E008) app bare: has a database and no migrations",
                "(neolith.E013) app leaning: migration 0001_initial depends on fresh",
            ],
        ),
    ],
    ids=["no migrations", "an empty package", "another label", "conflict", "both"],
)
def test_the_migrate_step_refuses_before_it_touches_a_database(
    site: Site, apps: list[str], refusals: list[str]
) -> None:
    labels = [app.rpartition(".")[2] for app in apps]
    site.deployer({"DATABASES": site.databases(*labels)})
    ran = site.migrate(*apps)
    assert ran.returncode == 1, ran.stderr
    printed = [line for line in ran.stderr.splitlines() if line.startswith("neolith:")]
    assert printed == [
        "neolith: the migrate step refused before it touched a database:",
        *(f"neolith: {refusal}" for refusal in refusals),
    ]
    assert [label for label in labels if site.database(label).exists()] == []


@pytest.mark.spec("APP-49")
def test_the_migrate_step_stops_at_the_first_failure_and_says_what_it_did(
    site: Site,
) -> None:
    site.deployer({"DATABASES": site.databases("desk", "failing", "fresh")})
    ran = site.migrate(DESK, "tests.run_apps.failing", "tests.boot_apps.fresh")
    assert ran.returncode == 1, ran.stderr
    printed = [line for line in ran.stderr.splitlines() if line.startswith("neolith:")]
    assert printed == [
        "neolith: app failing: database failing: the migrate step failed: "
        "RuntimeError: this migration fails on purpose",
        "neolith: migrated: desk",
        "neolith: not migrated: failing, fresh",
    ]
    assert "Migrating app fresh" not in ran.stdout
    assert not site.database("fresh").exists()


# The migrate step in a host built by a settings module.

_MANAGE = """\
import sys

from django.core.management import execute_from_command_line

execute_from_command_line(sys.argv)
"""


def _project(site: Site, neolith: dict[str, Any]) -> Callable[..., Any]:
    (site.root / "manage.py").write_text(_MANAGE, encoding="utf-8")
    site.deployer(
        neolith,
        below=(
            "from neolith.host import build_settings\n"
            f"globals().update(build_settings([{DESK!r}], globals()))\n"
        ),
    )

    def manage(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-W", "error", "manage.py", *arguments],
            capture_output=True,
            text=True,
            check=False,
            cwd=site.root,
            timeout=_WAIT,
            env={**site.env(), "DJANGO_SETTINGS_MODULE": "deployer"},
        )

    return manage


@pytest.mark.spec("APP-50")
def test_the_check_reports_what_boot_would_refuse_and_writes_nothing(
    site: Site,
) -> None:
    manage = _project(site, {"DATABASES": site.databases("desk")})
    ran = manage("migrateapps", "--check")
    assert ran.returncode == 1
    assert "(neolith.E018) database desk: no database at" in ran.stderr
    assert not site.database("desk").exists()
    connection = sqlite3.connect(site.database("desk"))
    connection.execute("CREATE TABLE other (id INTEGER)")
    connection.commit()
    connection.close()
    before = site.database("desk").read_bytes()
    ran = manage("migrateapps", "--check")
    assert ran.returncode == 1
    assert (
        "(neolith.E009) app desk: database desk has 1 unapplied migrations, the "
        "first is 0001_initial; run the migrate step" in ran.stderr
    )
    assert site.database("desk").read_bytes() == before
    assert manage("migrateapps").returncode == 0
    after = site.database("desk").read_bytes()
    ran = manage("migrateapps", "--check")
    assert (ran.returncode, ran.stdout, ran.stderr) == (0, "", "")
    assert site.database("desk").read_bytes() == after


@pytest.mark.spec("APP-48")
def test_an_undeclared_app_that_depends_on_a_hosted_app_is_refused(
    site: Site,
) -> None:
    """On a real ``default``, an app without a declaration whose migration
    depends on a hosted app's would record that app's migrations on
    ``default``."""
    manage = _project(
        site,
        {
            "DATABASES": site.databases("desk", "default"),
            "UNDECLARED_APPS": [
                "django.contrib.contenttypes",
                "tests.run_apps.borrower",
            ],
        },
    )
    ran = manage("migrateapps")
    assert ran.returncode == 1
    assert (
        "(neolith.E013) app borrower: migration 0001_initial depends on desk"
        in ran.stderr
    )
    assert not site.database("desk").exists()
    assert not site.database("default").exists()


def test_on_a_real_default_the_step_migrates_undeclared_apps_there(
    site: Site,
) -> None:
    manage = _project(
        site,
        {
            "DATABASES": site.databases("desk", "default"),
            "UNDECLARED_APPS": [
                "django.contrib.contenttypes",
                "django.contrib.humanize",
            ],
        },
    )
    ran = manage("migrateapps")
    assert ran.returncode == 0, ran.stderr
    assert "Migrating app desk on database desk" in ran.stdout
    assert "Migrating app contenttypes on database default" in ran.stdout
    assert "Skipping app humanize: it has no migrations" in ran.stdout
    connection = sqlite3.connect(site.database("default"))
    apps = {row[0] for row in connection.execute("SELECT app FROM django_migrations")}
    connection.close()
    assert apps == {"contenttypes"}
    assert manage("migrateapps", "--check").returncode == 0


@pytest.mark.spec("APP-16")
@given(
    address=st.ip_addresses(),
    port=st.integers(min_value=0, max_value=65535),
)
def test_every_ip_address_and_port_is_admitted_as_written(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int
) -> None:
    ipv6 = address.version == 6
    text = f"[{address}]:{port}" if ipv6 else f"{address}:{port}"
    assert _cli.parse_bind(text) == (str(address), port, ipv6)


@pytest.mark.spec("APP-16")
@given(text=st.text(alphabet="0123456789abfx:.[]%-_ \n", max_size=24))
def test_what_bind_admits_reads_back_as_written(text: str) -> None:
    """An admitted address is an IP address or a host name, and the port a
    number in range, written back exactly as given: nothing is trimmed,
    normalised or guessed."""
    try:
        host, port, ipv6 = _cli.parse_bind(text)
    except ValueError:
        return
    assert (f"[{host}]:{port}" if ipv6 else f"{host}:{port}") == text
    assert 0 <= port <= 65535
    if ipv6:
        assert ipaddress.ip_address(host).version == 6
    else:
        with contextlib.suppress(ValueError):
            assert ipaddress.ip_address(host).version == 4
            return
        labels = host.split(".")
        assert all(label and label[0] != "-" and label[-1] != "-" for label in labels)
        assert not labels[-1].isdigit()
