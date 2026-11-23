"""The server: one thread per client, one shared queue, optional TLS."""
from __future__ import annotations

import argparse
import logging
import os
import socket
import ssl
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set

from . import protocol as P
from . import ui
from .auth import CredentialStore, DEFAULT_PATH as DEFAULT_CREDENTIALS
from .channel import MessageChannel
from .protocol import ProtocolError
from .queue_core import ACTIVE, QueueError, QueueManager
from .storage import EventLog
from .tlsutil import describe, server_context

log = logging.getLogger("zephyq.server")

HANDSHAKE_TIMEOUT = 10.0


class Session:
    """Per-connection state."""

    def __init__(self, addr) -> None:
        self.addr = addr
        self.label = "%s:%d" % (addr[0], addr[1])
        self.chan: Optional[MessageChannel] = None
        self.role: Optional[str] = None         # None | "student" | "instructor"
        self.username: Optional[str] = None
        self.ticket: Optional[int] = None       # ticket this connection owns
        self.last_pos: Optional[int] = None     # last POSITION pushed
        self.failed_logins = 0

    def send(self, msg: Dict[str, Any]) -> None:
        if self.chan is not None:
            self.chan.send(msg)


class ZephyQServer:
    def __init__(self, manager: QueueManager, credentials: CredentialStore,
                 host: str = "0.0.0.0", port: int = P.DEFAULT_PORT,
                 ssl_context: Optional[ssl.SSLContext] = None, max_clients: int = 64,
                 login_fail_delay: float = 0.5) -> None:
        self.mgr = manager
        self.creds = credentials
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.max_clients = max_clients
        self.login_fail_delay = login_fail_delay
        self.lock = manager.lock                # one lock for queue + sessions
        self.sessions: Set[Session] = set()
        self.owners: Dict[int, Set[Session]] = {}
        self.instructor: Optional[Session] = None
        self._lsock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._accept_thread: Optional[threading.Thread] = None

    # Lifecycle
    def bind(self) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(16)
        s.settimeout(1.0)       # lets Ctrl+C work on Windows
        self._lsock = s
        self.port = s.getsockname()[1]
        return self.port

    def start(self) -> int:
        """Bind and accept in a background thread (used by tests)."""
        port = self.bind()
        self._accept_thread = threading.Thread(target=self.serve_forever, name="accept", daemon=True)
        self._accept_thread.start()
        return port

    def serve_forever(self) -> None:
        if self._lsock is None:
            self.bind()
        assert self._lsock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                log.exception("accept failed")
                continue
            threading.Thread(target=self._handle_connection, args=(conn, addr),
                             name="conn-%s:%d" % addr[:2], daemon=True).start()

    def shutdown(self, reason: str = "server shutting down") -> None:
        self._stop.set()
        if self._lsock is not None:
            self._lsock.close()
        with self.lock:
            sessions = list(self.sessions)
        for s in sessions:
            s.send({"type": "BYE", "reason": reason, "reconnect": True})
            if s.chan:
                s.chan.close(reason)
        for s in sessions:
            if s.chan:
                s.chan.closed.wait(2.0)
        if self._accept_thread is not None:
            self._accept_thread.join(2.0)
        self.mgr.log.close()

    # Connections
    def _handle_connection(self, raw: socket.socket, addr) -> None:
        session = Session(addr)
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            raw.settimeout(HANDSHAKE_TIMEOUT)
            sock: socket.socket = raw
            if self.ssl_context is not None:
                sock = self.ssl_context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log.warning("%s TLS handshake failed: %s", session.label, exc)
            raw.close()
            return

        chan = MessageChannel(sock, P.MAX_CLIENT_LINE, P.MAX_SERVER_LINE,
                              on_message=lambda c, item: self._on_message(session, item),
                              on_close=lambda c, reason: self._on_close(session, reason),
                              name=session.label)
        session.chan = chan
        with self.lock:
            busy = len(self.sessions) >= self.max_clients
            if not busy:
                self.sessions.add(session)
        log.info("%s connected (%s)", session.label, describe(sock))
        chan.send({"type": "WELCOME", "server": "ZephyQ", "proto": P.PROTOCOL_VERSION,
                   "tls": isinstance(sock, ssl.SSLSocket), "max_line": P.MAX_CLIENT_LINE})
        if busy:
            log.warning("%s rejected: too many connections", session.label)
            chan.send(P.error("SERVER_BUSY", "too many open connections, try again later"))
            chan.close("server busy")
        chan.run()      # this thread now serves the connection until it closes

    def _on_close(self, s: Session, reason: str) -> None:
        with self.lock:
            self.sessions.discard(s)
            if self.instructor is s:
                self.instructor = None
            if s.ticket is not None:
                owners = self.owners.get(s.ticket)
                if owners is not None:
                    owners.discard(s)
                    if not owners:
                        del self.owners[s.ticket]
        log.info("%s disconnected (%s)", s.label, reason)

    # Dispatching
    def _on_message(self, s: Session, item) -> None:
        if isinstance(item, ProtocolError):
            log.info("%s sent a bad frame: %s", s.label, item.code)
            s.send(P.error(item.code, item.message))
            return
        rid = None
        try:
            rid = P.get_request_id(item)
            mtype = P.get_type(item)
            if mtype != "PING":
                log.info("%s -> %s%s", s.label, mtype, _describe_request(mtype, item))
            getattr(self, "_h_" + mtype.lower())(s, item, rid)
        except (ProtocolError, QueueError) as exc:
            log.info("%s <- ERR %s: %s", s.label, exc.code, exc.message)
            s.send(P.error(exc.code, exc.message, rid))
        except Exception:
            log.exception("%s request failed", s.label)
            s.send(P.error("INTERNAL", "internal server error; request not applied", rid))

    def _reply(self, s: Session, rid: Optional[int], mtype: str, **fields: Any) -> None:
        msg: Dict[str, Any] = {"type": mtype}
        msg.update(fields)
        if rid is not None:
            msg["re"] = rid
        s.send(msg)

    @staticmethod
    def _as_student(s: Session) -> None:
        if s.role == "instructor":
            raise ProtocolError("WRONG_ROLE", "an instructor connection cannot send student messages")

    @staticmethod
    def _as_instructor(s: Session) -> None:
        if s.role == "student":
            raise ProtocolError("WRONG_ROLE", "a student connection cannot send instructor messages")
        if s.role != "instructor":
            raise ProtocolError("AUTH_REQUIRED", "LOGIN first")

    def _bind_ticket(self, s: Session, number: int) -> None:
        if s.ticket is not None and s.ticket in self.owners:
            self.owners[s.ticket].discard(s)
        s.role = "student"
        s.ticket = number
        self.owners.setdefault(number, set()).add(s)

    def _owns_active(self, s: Session) -> bool:
        return s.ticket is not None and self.mgr.tickets[s.ticket].status in ACTIVE

    def _push_to_owners(self, number: int, msg: Dict[str, Any], exclude: Optional[Session] = None) -> None:
        for o in self.owners.get(number, ()):
            if o is not exclude:
                o.send(msg)

    def _after_change(self) -> None:
        """Push automatic updates.  Caller holds self.lock."""
        positions = self.mgr.positions()
        for number, owners in self.owners.items():
            pos = positions.get(number)
            if pos is None:
                continue
            for o in owners:
                if o.last_pos != pos:
                    o.last_pos = pos
                    o.send({"type": "POSITION", "ticket": number, "position": pos,
                            "waiting_count": len(positions)})
        if self.instructor is not None:
            self.instructor.send({"type": "QUEUE", "tickets": self.mgr.active_public(),
                                  "waiting_count": len(positions)})

    # Student handlers
    def _h_join(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._as_student(s)
        ws = P.require_int(msg, "workstation", P.MIN_WORKSTATION, P.MAX_WORKSTATION)
        name = P.require_str(msg, "name", P.MAX_NAME_LEN)
        problem = P.require_str(msg, "problem", P.MAX_PROBLEM_LEN)
        with self.lock:
            if self._owns_active(s):
                raise QueueError("ALREADY_HAS_TICKET", "this connection already has ticket #%d" % s.ticket)
            t, token = self.mgr.join(ws, name, problem)
            self._bind_ticket(s, t.number)
            positions = self.mgr.positions()
            s.last_pos = positions[t.number]
            self._reply(s, rid, "QUEUED", token=token, waiting_count=len(positions),
                        **t.public(s.last_pos))
            self._after_change()
        log.info("%s <- QUEUED ticket #%d (workstation %d, position %d)", s.label, t.number, ws, s.last_pos)

    def _h_resume(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._as_student(s)
        number = P.require_int(msg, "ticket", 1, P.MAX_REQUEST_ID)
        token = P.require_str(msg, "token", P.MAX_TOKEN_LEN, strip=False)
        with self.lock:
            if self._owns_active(s) and s.ticket != number:
                raise QueueError("ALREADY_HAS_TICKET", "this connection already has ticket #%d" % s.ticket)
            t = self.mgr.resume(number, token)
            self._bind_ticket(s, number)
            positions = self.mgr.positions()
            s.last_pos = positions.get(number)
            self._reply(s, rid, "OK", waiting_count=len(positions), **t.public(s.last_pos))
        log.info("%s <- OK resumed ticket #%d (%s)", s.label, number, t.status)

    def _h_status(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._as_student(s)
        with self.lock:
            if s.ticket is None:
                raise QueueError("NO_TICKET", "this connection has no ticket; send JOIN or RESUME")
            positions = self.mgr.positions()
            t = self.mgr.tickets[s.ticket]
            self._reply(s, rid, "OK", waiting_count=len(positions), **t.public(positions.get(t.number)))

    def _h_cancel(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._as_student(s)
        with self.lock:
            if s.ticket is None:
                raise QueueError("NO_TICKET", "this connection has no ticket to cancel")
            t = self.mgr.cancel(s.ticket, by="student")
            s.last_pos = None
            self._reply(s, rid, "OK", **t.public())
            self._push_to_owners(t.number, {"type": "CANCELLED", "ticket": t.number}, exclude=s)
            self._after_change()

    # Instructor handlers
    def _h_login(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        if s.role == "student":
            raise ProtocolError("WRONG_ROLE", "a student connection cannot log in")
        user = P.require_str(msg, "username", P.MAX_USERNAME_LEN)
        password = P.require_str(msg, "password", P.MAX_PASSWORD_LEN, strip=False)
        if not self.creds.verify(user, password):      # slow hash: outside the lock
            s.failed_logins += 1
            log.warning("%s failed login as %r (%d/%d)", s.label, user, s.failed_logins, P.MAX_LOGIN_ATTEMPTS)
            time.sleep(self.login_fail_delay)
            if s.failed_logins >= P.MAX_LOGIN_ATTEMPTS:
                s.send(P.error("RATE_LIMITED", "too many failed logins; disconnecting", rid))
                if s.chan:
                    s.chan.close("too many failed logins")
                return
            raise ProtocolError("AUTH_FAILED", "wrong username or password (%d attempts left)"
                                % (P.MAX_LOGIN_ATTEMPTS - s.failed_logins))
        with self.lock:
            old = self.instructor
            if old is not None and old is not s:
                old.send({"type": "BYE", "reconnect": False,
                          "reason": "replaced by a new instructor login from %s" % s.label})
                if old.chan:
                    old.chan.close("replaced by another instructor login")
                old.role = None
            s.role, s.username, s.failed_logins = "instructor", user, 0
            self.instructor = s
            self._reply(s, rid, "OK", username=user, waiting_count=len(self.mgr.waiting()))
        log.info("%s logged in as instructor %r", s.label, user)

    def _h_list(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._as_instructor(s)
        with self.lock:
            self._reply(s, rid, "OK", tickets=self.mgr.active_public(),
                        waiting_count=len(self.mgr.waiting()))

    def _h_next(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._as_instructor(s)
        with self.lock:
            t = self.mgr.call_next()
            self._reply(s, rid, "OK", **t.public())
            for o in self.owners.get(t.number, ()):
                o.last_pos = None
            self._push_to_owners(t.number, {
                "type": "CALLED", "ticket": t.number, "workstation": t.workstation,
                "message": "The instructor is on the way to workstation %d." % t.workstation})
            self._after_change()
        log.info("%s <- OK called ticket #%d (workstation %d, waited %.1fs)",
                 s.label, t.number, t.workstation, t.wait_seconds or 0.0)

    def _h_done(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._as_instructor(s)
        number = P.require_int(msg, "ticket", 1, P.MAX_REQUEST_ID)
        with self.lock:
            t = self.mgr.resolve(number)
            self._reply(s, rid, "OK", **t.public())
            self._push_to_owners(t.number, {"type": "RESOLVED", "ticket": t.number})
            self._after_change()

    def _h_summary(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._as_instructor(s)
        self._reply(s, rid, "OK", summary=self.mgr.summary())

    # Any role
    def _h_ping(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._reply(s, rid, "PONG")

    def _h_quit(self, s: Session, msg: Dict[str, Any], rid: Optional[int]) -> None:
        self._reply(s, rid, "BYE", reason="goodbye", reconnect=False)
        if s.chan:
            s.chan.close("client sent QUIT")


def _describe_request(mtype: str, msg: Dict[str, Any]) -> str:
    """Log-friendly request details (never logs passwords or tokens)."""
    if mtype == "JOIN":
        return " workstation=%r name=%r" % (msg.get("workstation"), msg.get("name"))
    if mtype in ("RESUME", "DONE"):
        return " ticket=%r" % (msg.get("ticket"),)
    if mtype == "LOGIN":
        return " username=%r" % (msg.get("username"),)
    return ""


# Main
class _ConsoleFormatter(logging.Formatter):
    """Console log lines: dim time, colored level, highlighted requests and replies."""
    LEVEL_COLORS = {"WARNING": ("bold", "yellow"), "ERROR": ("bold", "red"), "CRITICAL": ("bold", "red")}

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        when = ui.dim(time.strftime("%H:%M:%S", time.localtime(record.created)))
        colors = self.LEVEL_COLORS.get(record.levelname)
        if colors:
            return "%s %s %s" % (when, ui.style(record.levelname.ljust(7), *colors), ui.style(msg, colors[1]))
        if " -> " in msg:
            msg = msg.replace(" -> ", " " + ui.info("->") + " ", 1)
        elif " <- " in msg:
            msg = msg.replace(" <- ", " " + ui.ok("<-") + " ", 1)
        elif " connected (" in msg or " disconnected (" in msg or " logged in " in msg:
            msg = ui.style(msg, "magenta")
        return "%s %s" % (when, msg)


def setup_logging(log_file: Optional[str], verbose: bool = False) -> None:
    root = logging.getLogger("zephyq")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zephyq.server", description="ZephyQ help-queue server")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to listen on; use the host-only adapter IP in the lab, e.g. 192.168.56.1 (default: %(default)s)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT, help="TCP port (default: %(default)s)")
    ap.add_argument("--data-dir", default="data", help="session log directory (default: %(default)s)")
    ap.add_argument("--credentials", default=DEFAULT_CREDENTIALS, help="instructor accounts file (default: %(default)s)")
    ap.add_argument("--new-session", action="store_true",
                    help="archive the previous session log and start an empty queue (otherwise the previous session is recovered)")
    ap.add_argument("--tls", action="store_true", help="require TLS on every connection")
    ap.add_argument("--cert", default=os.path.join("certs", "server.crt"), help="server certificate (PEM)")
    ap.add_argument("--key", default=os.path.join("certs", "server.key"), help="server private key (PEM)")
    ap.add_argument("--max-clients", type=int, default=64)
    ap.add_argument("--log-file", default=os.path.join("logs", "server.log"), help="application log (default: %(default)s)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                    help="colored console output (default: auto = only in a real terminal)")
    args = ap.parse_args(argv)

    ui.init(args.color)
    setup_logging(args.log_file, args.verbose)
    event_log = EventLog(os.path.join(args.data_dir, "session.jsonl"))
    if args.new_session:
        archived = event_log.archive(os.path.join(args.data_dir, "archive"))
        if archived:
            log.info("previous session archived to %s", archived)
    mgr = QueueManager(event_log)
    replayed = mgr.load()
    if replayed:
        s = mgr.summary()
        log.info("recovered session %s from %s: %d tickets, %d waiting, %d being served",
                 mgr.session_id, event_log.path, s["total_requests"], s["waiting"], s["being_served"])
    else:
        log.info("started new session %s (log: %s)", mgr.session_id, event_log.path)

    creds = CredentialStore(args.credentials)
    if not creds.users:
        log.warning("no instructor accounts in %s - create one with: python -m zephyq.auth add <username>",
                    args.credentials)

    ctx = None
    if args.tls:
        try:
            ctx = server_context(args.cert, args.key)
        except (OSError, ssl.SSLError) as exc:
            log.error("cannot load TLS certificate/key (%s, %s): %s", args.cert, args.key, exc)
            return 1

    server = ZephyQServer(mgr, creds, args.host, args.port, ctx, args.max_clients)
    try:
        port = server.bind()
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", args.host, args.port, exc)
        return 1
    summary = mgr.summary()
    print(ui.panel("ZephyQ " + ui.symbol("dot") + " Server", ui.fields([
        ("Listening on", ui.bold("%s:%d/tcp" % (args.host, port))),
        ("Security", ui.ok("TLS required") if ctx else ui.warn("plain TCP (NOT encrypted)")),
        ("Session", "%s %s" % (mgr.session_id, ui.dim("(recovered)" if replayed else "(new)"))),
        ("Queue", "%d waiting, %d being served" % (summary["waiting"], summary["being_served"])),
        ("Session log", event_log.path),
        ("Instructors", ", ".join(sorted(creds.users)) or ui.bad("none - run: python -m zephyq.auth add <name>")),
    ])), flush=True)
    print(ui.dim("Press Ctrl+C to stop. Activity appears below.") + "\n", flush=True)
    log.info("ZephyQ server listening on %s:%d/tcp (%s). Press Ctrl+C to stop.",
             args.host, port, "TLS required" if ctx else "plain TCP, NOT encrypted")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping...")
    finally:
        server.shutdown()
        log.info("server stopped; session log kept at %s", event_log.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
