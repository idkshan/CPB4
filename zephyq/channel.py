"""One TCP/TLS connection, owned by a single I/O thread.

Both the server and the clients use MessageChannel.  Exactly one thread
reads from and writes to the socket.  Other threads never touch the socket;
they call send(), which puts the message in an outbox and wakes the I/O
thread through a small socket pair.  Keeping every socket operation on one
thread matters for TLS, because an OpenSSL connection must not be read and
written by two threads at the same time.
"""
from __future__ import annotations

import collections
import logging
import select
import socket
import ssl
import threading
import time
from typing import Any, Callable, Deque, Dict, Union

from .protocol import LineParser, ProtocolError, encode

log = logging.getLogger("zephyq.channel")

SEND_TIMEOUT = 10.0     # seconds a peer may take to accept our bytes
_CLOSE = object()       # outbox sentinel: flush, then close

Item = Union[Dict[str, Any], ProtocolError]


class MessageChannel:
    def __init__(
        self,
        sock: socket.socket,
        in_limit: int,
        out_limit: int,
        on_message: Callable[["MessageChannel", Item], None],
        on_close: Callable[["MessageChannel", str], None],
        name: str = "",
    ) -> None:
        self.sock = sock
        self.name = name
        self._parser = LineParser(in_limit)
        self._out_limit = out_limit
        self._on_message = on_message
        self._on_close = on_close
        self._outbox: Deque[Any] = collections.deque()
        self._lock = threading.Lock()
        self._closing = False
        self.closed = threading.Event()
        self.close_reason = ""
        self.last_rx = time.monotonic()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)

    # ------------------------------------------------ called by any thread --
    def send(self, msg: Union[Dict[str, Any], bytes]) -> bool:
        """Queue a message (dict) or raw bytes for sending.  Never blocks."""
        with self._lock:
            if self._closing:
                return False
            self._outbox.append(msg)
        self._wake()
        return True

    def close(self, reason: str = "closed locally", flush: bool = True) -> None:
        """Close after sending everything already queued (if flush)."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self.close_reason = reason
            if not flush:
                self._outbox.clear()
            self._outbox.append(_CLOSE)
        self._wake()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="io-" + self.name, daemon=True)
        t.start()
        return t

    # ---------------------------------------------------- the I/O thread --
    def run(self) -> None:
        reason = "peer closed the connection"
        graceful = False
        try:
            while True:
                if self._flush_outbox():
                    reason, graceful = self.close_reason, True
                    break
                if not (isinstance(self.sock, ssl.SSLSocket) and self.sock.pending()):
                    readable, _, _ = select.select([self.sock, self._wake_r], [], [])
                    if self._wake_r in readable:
                        self._drain_wake()
                    if self.sock not in readable:
                        continue
                self.sock.setblocking(False)
                try:
                    data = self.sock.recv(65536)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    continue    # e.g. a TLS record that carried no application data
                if not data:
                    break
                self.last_rx = time.monotonic()
                for item in self._parser.feed(data):
                    try:
                        self._on_message(self, item)
                    except Exception:   # a handler bug must not kill the connection
                        log.exception("[%s] message handler failed", self.name)
        except (OSError, ValueError) as exc:
            reason = "connection error: %s" % (exc,)
        finally:
            self._teardown(graceful)
            self.closed.set()
            try:
                self._on_close(self, reason)
            except Exception:
                log.exception("[%s] close handler failed", self.name)

    def _flush_outbox(self) -> bool:
        """Send queued messages.  Returns True when the close sentinel is reached."""
        while True:
            with self._lock:
                if not self._outbox:
                    return False
                msg = self._outbox.popleft()
            if msg is _CLOSE:
                return True
            if isinstance(msg, (bytes, bytearray)):
                data = bytes(msg)
            else:
                try:
                    data = encode(msg, self._out_limit)
                except ValueError as exc:
                    log.error("[%s] dropped outgoing message: %s", self.name, exc)
                    continue
            self.sock.settimeout(SEND_TIMEOUT)
            self.sock.sendall(data)

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\0")
        except OSError:
            pass    # buffer full means a wake-up is already pending

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except OSError:
            pass

    def _teardown(self, graceful: bool) -> None:
        with self._lock:
            self._closing = True
        if graceful:
            try:
                self.sock.settimeout(1.0)
                if isinstance(self.sock, ssl.SSLSocket):
                    self.sock = self.sock.unwrap()      # sends TLS close_notify
                self.sock.shutdown(socket.SHUT_RDWR)    # sends TCP FIN
            except (OSError, ValueError):
                pass
        for s in (self.sock, self._wake_r, self._wake_w):
            try:
                s.close()
            except OSError:
                pass
