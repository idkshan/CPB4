"""Client connection: sends requests, matches replies, sends heartbeats and reconnects."""
from __future__ import annotations

import itertools
import socket
import ssl
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import protocol as P
from .channel import MessageChannel
from .protocol import ProtocolError
from .tlsutil import describe


class ConnectionLost(Exception):
    pass


class ServerError(Exception):
    def __init__(self, reply: Dict[str, Any]) -> None:
        super().__init__("%s: %s" % (reply.get("code"), reply.get("message")))
        self.code = reply.get("code")
        self.message = reply.get("message")
        self.reply = reply


class ClientConnection:
    def __init__(self, host: str, port: int = P.DEFAULT_PORT,
                 ssl_context: Optional[ssl.SSLContext] = None,
                 on_push: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_state: Optional[Callable[[str], None]] = None,
                 on_reconnect: Optional[Callable[[], None]] = None,
                 auto_reconnect: bool = True, heartbeat: float = 10.0,
                 max_backoff: float = 10.0) -> None:
        self.host, self.port = host, port
        self.ssl_context = ssl_context
        self.on_push = on_push or (lambda m: None)
        self.on_state = on_state or (lambda s: None)
        self.on_reconnect = on_reconnect
        self.auto_reconnect = auto_reconnect
        self.heartbeat = heartbeat
        self.max_backoff = max_backoff
        self.tls_info = ""
        self.welcome: Dict[str, Any] = {}
        self._chan: Optional[MessageChannel] = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: Dict[int, list] = {}
        self._user_closed = False
        self._no_reconnect = False
        self._welcome_evt = threading.Event()
        self._reconnecting = threading.Lock()
        self._hb_thread: Optional[threading.Thread] = None

    # Connect
    @property
    def connected(self) -> bool:
        return self._chan is not None and not self._chan.closed.is_set()

    def connect(self, timeout: float = 10.0) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock: socket.socket = raw
            if self.ssl_context is not None:
                sock = self.ssl_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        self.tls_info = describe(sock)
        self._welcome_evt.clear()
        chan = MessageChannel(sock, P.MAX_SERVER_LINE, P.MAX_CLIENT_LINE,
                              self._on_message, self._on_close, name="%s:%d" % (self.host, self.port))
        self._chan = chan
        chan.start()
        if not self._welcome_evt.wait(timeout) or chan.closed.is_set():
            chan.close("no WELCOME", flush=False)
            raise ConnectionLost("server did not greet us (is it a ZephyQ server? TLS on both sides?)")
        if self._hb_thread is None and self.heartbeat:
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
            self._hb_thread.start()

    def close(self) -> None:
        """Polite disconnect: QUIT, wait for BYE, close.  No reconnect afterwards."""
        self._user_closed = True
        if self.connected:
            try:
                self.request("QUIT", timeout=3)
            except (ServerError, ConnectionLost):
                pass
        if self._chan is not None:
            self._chan.close("client closed")
            self._chan.closed.wait(3)

    # Messaging
    def request(self, mtype: str, timeout: float = 15.0, **fields: Any) -> Dict[str, Any]:
        chan = self._chan
        if chan is None or chan.closed.is_set():
            raise ConnectionLost("not connected")
        rid = next(self._ids)
        slot = [threading.Event(), None]
        with self._lock:
            self._pending[rid] = slot
        msg = {"type": mtype, "id": rid}
        msg.update(fields)
        if not chan.send(msg):
            with self._lock:
                self._pending.pop(rid, None)
            raise ConnectionLost("connection is closing")
        if not slot[0].wait(timeout):        # type: ignore[union-attr]
            with self._lock:
                self._pending.pop(rid, None)
            raise ConnectionLost("no reply from server within %.0fs" % timeout)
        reply = slot[1]
        if reply is None:
            raise ConnectionLost("connection lost while waiting for the reply")
        if reply.get("type") == "ERR":
            raise ServerError(reply)
        return reply

    def send_nowait(self, mtype: str, **fields: Any) -> None:
        if self._chan is not None:
            msg = {"type": mtype}
            msg.update(fields)
            self._chan.send(msg)

    def _on_message(self, chan: MessageChannel, item) -> None:
        if isinstance(item, ProtocolError):
            self.on_state("received an invalid message from the server: %s" % item.message)
            return
        mtype = item.get("type")
        if mtype == "WELCOME":
            self.welcome = item
            chan.welcomed = True        # type: ignore[attr-defined]
            self._welcome_evt.set()
            return
        re = item.get("re")
        if re is not None:
            with self._lock:
                slot = self._pending.pop(re, None)
            if slot is not None:
                slot[1] = item
                slot[0].set()
                return
        if mtype == "PONG":
            return
        if mtype == "BYE" and item.get("reconnect") is False:
            self._no_reconnect = True
        if mtype == "ERR" and item.get("code") == "SERVER_BUSY":
            self._welcome_evt.set()
        self.on_push(item)

    def _on_close(self, chan: MessageChannel, reason: str) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot[0].set()           # reply stays None -> ConnectionLost
        if chan is not self._chan:
            return
        self._welcome_evt.set()
        if self._user_closed or not getattr(chan, "welcomed", False):
            return      # never fully connected: connect() reports the failure
        self.on_state("disconnected (%s)" % reason)
        if self.auto_reconnect and not self._no_reconnect:
            threading.Thread(target=self._reconnect_loop, name="reconnect", daemon=True).start()

    # Resilience
    def _reconnect_loop(self) -> None:
        if not self._reconnecting.acquire(blocking=False):
            return
        try:
            delay = 1.0
            while not self._user_closed:
                self.on_state("reconnecting to %s:%d in %.0fs..." % (self.host, self.port, delay))
                time.sleep(delay)
                try:
                    self.connect()
                except (OSError, ssl.SSLError, ConnectionLost) as exc:
                    self.on_state("reconnect failed: %s" % (exc,))
                    delay = min(delay * 2, self.max_backoff)
                    continue
                self.on_state("reconnected (%s)" % self.tls_info)
                if self.on_reconnect is not None:
                    try:
                        self.on_reconnect()
                    except (ServerError, ConnectionLost) as exc:
                        self.on_state("could not restore session: %s" % (exc,))
                return
        finally:
            self._reconnecting.release()

    def _heartbeat_loop(self) -> None:
        while not self._user_closed:
            time.sleep(self.heartbeat)
            chan = self._chan
            if chan is None or chan.closed.is_set():
                continue
            if time.monotonic() - chan.last_rx > 3 * self.heartbeat:
                chan.close("server not responding", flush=False)
            else:
                chan.send({"type": "PING"})
