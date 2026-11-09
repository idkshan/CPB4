"""Test fixtures: a real server on 127.0.0.1 (ephemeral port) and test clients."""
from __future__ import annotations

import os
import queue
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from zephyq.auth import CredentialStore          # noqa: E402
from zephyq.client import ClientConnection       # noqa: E402
from zephyq.queue_core import QueueManager       # noqa: E402
from zephyq.server import ZephyQServer           # noqa: E402
from zephyq.storage import EventLog              # noqa: E402

PROF, PASSWORD = "prof", "correct-horse-42"


class ServerFixture:
    def __init__(self, data_dir: str, creds_path: str, ssl_context=None) -> None:
        self.data_dir = data_dir
        self.creds_path = creds_path
        self.ssl_context = ssl_context
        self.server: Optional[ZephyQServer] = None
        self.port = 0

    @property
    def log_path(self) -> str:
        return os.path.join(self.data_dir, "session.jsonl")

    def start(self) -> "ServerFixture":
        mgr = QueueManager(EventLog(self.log_path))
        mgr.load()
        self.server = ZephyQServer(mgr, CredentialStore(self.creds_path), "127.0.0.1", 0,
                                   self.ssl_context, login_fail_delay=0.01)
        self.port = self.server.start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server = None


class TestClient:
    def __init__(self, port: int, ssl_context=None, reconnect: bool = False, on_reconnect=None) -> None:
        self.pushes: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.states: "queue.Queue[str]" = queue.Queue()
        self.conn = ClientConnection("127.0.0.1", port, ssl_context, on_push=self.pushes.put,
                                     on_state=self.states.put, on_reconnect=on_reconnect,
                                     auto_reconnect=reconnect, heartbeat=0, max_backoff=1.0)
        self.conn.connect(timeout=5)

    def req(self, mtype: str, **fields: Any) -> Dict[str, Any]:
        return self.conn.request(mtype, timeout=5, **fields)

    def raw(self, data: bytes) -> None:
        assert self.conn._chan is not None
        self.conn._chan.send(data)

    def push(self, mtype: str, timeout: float = 3.0) -> Dict[str, Any]:
        import time
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("no %s push within %.1fs" % (mtype, timeout))
            try:
                msg = self.pushes.get(timeout=remaining)
            except queue.Empty:
                continue
            if msg.get("type") == mtype:
                return msg

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.pushes.get_nowait())
            except queue.Empty:
                return out

    def login(self) -> Dict[str, Any]:
        return self.req("LOGIN", username=PROF, password=PASSWORD)

    def close(self) -> None:
        self.conn.close()


class ServerTestCase(unittest.TestCase):
    """Starts a fresh server (fresh session log) for every test."""
    ssl_context_server = None
    ssl_context_client = None

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="zephyq-test-")
        self.creds_path = os.path.join(self.tmp, "instructors.json")
        CredentialStore(self.creds_path).set_password(PROF, PASSWORD)
        self.fx = ServerFixture(os.path.join(self.tmp, "data"), self.creds_path, self.ssl_context_server).start()
        self.clients = []

    def tearDown(self) -> None:
        for c in self.clients:
            try:
                c.close()
            except Exception:
                pass
        self.fx.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, **kw) -> TestClient:
        kw.setdefault("ssl_context", self.ssl_context_client)
        c = TestClient(self.fx.port, **kw)
        self.clients.append(c)
        return c

    def instructor(self) -> TestClient:
        c = self.client()
        c.login()
        return c

    def student(self, ws: int, name: str = "", problem: str = "help please") -> TestClient:
        c = self.client()
        c.queued = c.req("JOIN", workstation=ws, name=name or "Student %d" % ws, problem=problem)
        return c
