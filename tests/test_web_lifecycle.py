"""The real server, in a process of its own, started and stopped the way a person does it.

Everything else in the web suites runs the app under a test client. These tests
start ``build_server`` (uvicorn, the real signal handling, the real chat agent
and two fake MCP servers) as a subprocess, talk to it over a socket and stop it
with ``SIGINT``, which is what Ctrl-C sends.
"""

import json
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx2
import pytest

from advisor_agent.database import AdvisorDatabase
from conftest import pid_alive, wait_until
from fakes.serve_fakes import backends

SERVE = Path(__file__).parent / "fakes" / "serve_fakes.py"
URL_LINE = re.compile(r"http://127\.0\.0\.1:(\d+)/#t=(\S+)")
# The server must be gone this long after Ctrl-C (its own limits add up to less).
STOP_WITHIN = 15.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    """``serve_fakes.py`` running as a subprocess."""

    def __init__(self, directory: Path, port: int, *extra: str):
        self.directory = directory
        self.port = port
        self.stdout: list[str] = []
        self.stderr_file = directory / "server.stderr"
        directory.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(SERVE), str(directory), str(port), *extra],
            stdout=subprocess.PIPE,
            stderr=self.stderr_file.open("wb"),
            text=True,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.client: httpx2.Client | None = None
        self.events: list[tuple[str, object]] = []
        self.stream_ended = threading.Event()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout.append(line.rstrip("\n"))

    # -- start --------------------------------------------------------------------------

    def printed_url(self) -> re.Match[str] | None:
        for line in list(self.stdout):
            match = URL_LINE.search(line)
            if match:
                return match
        return None

    def wait_for_url(self, timeout: float = 40.0) -> re.Match[str]:
        assert wait_until(lambda: self.printed_url() is not None or self.process.poll() is not None, timeout), (
            "the server printed no address"
        )
        match = self.printed_url()
        assert match is not None, f"the server ended ({self.process.returncode}) before printing an address:\n{self.stderr()}"
        return match

    def sign_in(self) -> None:
        match = self.wait_for_url()
        assert int(match.group(1)) == self.port
        origin = f"http://127.0.0.1:{self.port}"
        self.client = httpx2.Client(base_url=origin, headers={"Origin": origin}, timeout=10)
        response = self.client.post("/api/login", json={"token": match.group(2)})
        assert response.status_code == 200, response.text

    def stderr(self) -> str:
        return self.stderr_file.read_text(errors="replace") if self.stderr_file.exists() else ""

    # -- a page ---------------------------------------------------------------------------

    def open_event_stream(self) -> None:
        """Hold ``/api/events`` open on a thread, like a page does, and keep what arrives."""
        assert self.client is not None
        started = threading.Event()

        def read() -> None:
            try:
                with self.client.stream("GET", "/api/events", timeout=httpx2.Timeout(10, read=None)) as response:
                    started.set()
                    name = None
                    for line in response.iter_lines():
                        if line.startswith("event: "):
                            name = line[7:]
                        elif line.startswith("data: ") and name:
                            self.events.append((name, json.loads(line[6:])))
            except Exception:
                pass
            finally:
                started.set()
                self.stream_ended.set()

        threading.Thread(target=read, daemon=True).start()
        assert started.wait(10)
        assert wait_until(lambda: any(name == "snapshot" for name, _ in self.events), 10)

    def seen(self, name: str) -> list[object]:
        return [data for event, data in list(self.events) if event == name]

    def wait_azure_ready(self, timeout: float = 40.0) -> None:
        assert self.client is not None

        def ready() -> bool:
            return self.client.get("/api/recommendations").status_code == 200

        assert wait_until(ready, timeout, 0.2), "the Azure connection did not come up"

    # -- stop -------------------------------------------------------------------------------

    def interrupt(self) -> float:
        """Send Ctrl-C and return how long the process took to end."""
        began = time.monotonic()
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(STOP_WITHIN + 10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(5)
            pytest.fail(f"the server did not stop after SIGINT:\n{self.stderr()}")
        return time.monotonic() - began

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(5)
        self._reader.join(5)


@pytest.fixture
def start_server(tmp_path):
    started: list[Server] = []

    def start(*extra: str, port: int | None = None) -> Server:
        server = Server(tmp_path / f"run{len(started)}", port or free_port(), *extra)
        started.append(server)
        return server

    yield start
    for server in started:
        server.close()


def child_pids(server: Server) -> list[int]:
    parts = backends(server.directory)
    return [pid for pid in (parts.pid("azure"), parts.pid("jira")) if pid is not None]


# -- tests ---------------------------------------------------------------------------------


def test_the_address_is_printed_once_the_port_is_open(start_server):
    server = start_server()
    match = server.wait_for_url()

    # Whoever reads the address can use it at once.
    with socket.create_connection(("127.0.0.1", int(match.group(1))), timeout=5):
        pass
    assert match.group(2) and "Open this address" in "\n".join(server.stdout)


def test_a_port_that_is_taken_is_an_error_and_no_address_is_printed(start_server):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        server = start_server(port=taken.getsockname()[1])

        assert wait_until(lambda: server.process.poll() is not None, 30)

    assert server.process.returncode != 0
    assert server.printed_url() is None


def test_ctrl_c_with_a_chat_run_a_waiting_approval_and_an_open_page_ends_it_all(start_server):
    server = start_server()
    server.sign_in()
    server.wait_azure_ready()
    server.open_event_stream()

    assert server.client.post("/api/chat", json={"text": "make a ticket", "client_msg_id": "m1"}).status_code == 202
    assert wait_until(lambda: bool(server.seen("approval_requested")), 60), "the Jira write was never put to the person"
    pids = child_pids(server)
    assert len(pids) == 2 and all(pid_alive(pid) for pid in pids)

    took = server.interrupt()

    assert took < STOP_WITHIN, f"took {took:.1f}s"
    assert server.stream_ended.wait(5)
    assert server.seen("shutdown"), "the page was not told the server is stopping"
    # Nothing is left behind, and the write the person never approved did not happen.
    assert wait_until(lambda: not any(pid_alive(pid) for pid in pids), 10), "a child process survived"
    jira_calls = [line for line in backends(server.directory).calls("jira") if "jira_create_issue" in line]
    assert jira_calls == []
    rows = [row for row in AdvisorDatabase(str(server.directory / "advisor.db")).get_audit_log(limit=50)
            if row["action"] == "jira_tool_approval"]
    # Either label is true (the server was stopping; the chat run was cancelled): what matters
    # is one record, and that it is a "no".
    assert len(rows) == 1 and rows[0]["decision"] in ("shutdown", "cancelled")
    assert rows[0]["user_approval"] in (None, 0, False)


def test_ctrl_c_while_the_azure_server_is_still_starting(start_server):
    server = start_server("hang-azure-start")
    server.sign_in()
    server.open_event_stream()
    assert wait_until(lambda: (server.directory / "azure.pid").exists(), 20)
    pid = int((server.directory / "azure.pid").read_text())
    assert server.client.get("/api/recommendations").status_code == 503

    took = server.interrupt()

    assert took < STOP_WITHIN, f"took {took:.1f}s"
    assert wait_until(lambda: not pid_alive(pid), 10), "the Azure process survived"


def test_ctrl_c_with_nothing_going_on_is_quick_and_clean(start_server):
    server = start_server()
    server.sign_in()
    server.wait_azure_ready()
    pids = child_pids(server)

    took = server.interrupt()

    assert took < STOP_WITHIN
    assert wait_until(lambda: not any(pid_alive(pid) for pid in pids), 10)
    assert "Traceback" not in server.stderr(), server.stderr()
    assert server.process.returncode == 0
