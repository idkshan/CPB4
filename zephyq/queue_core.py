"""The first-come, first-served help queue (no networking in this module).

Ordering rule
-------------
TCP keeps the bytes of ONE connection in order, but it says nothing about the
order of messages that travel on DIFFERENT connections from different
workstations.  ZephyQ therefore defines queue order on the server: the ticket
number is assigned by the server while it holds `QueueManager.lock`, so the
first JOIN to acquire the lock gets the next number.  Queue position is the
rank of a WAITING ticket's number.  Clients never choose ticket numbers.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .protocol import MAX_ACTIVE_TICKETS
from .storage import EventLog

WAITING = "WAITING"
CALLED = "CALLED"
RESOLVED = "RESOLVED"
CANCELLED = "CANCELLED"
ACTIVE = (WAITING, CALLED)


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _round(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 1)


class QueueError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Ticket:
    __slots__ = ("number", "workstation", "name", "problem", "token_hash", "status",
                 "submitted_at", "called_at", "resolved_at", "cancelled_at", "cancelled_by")

    def __init__(self, number: int, workstation: int, name: str, problem: str,
                 token_hash: str, submitted_at: float) -> None:
        self.number = number
        self.workstation = workstation
        self.name = name
        self.problem = problem
        self.token_hash = token_hash
        self.status = WAITING
        self.submitted_at = submitted_at
        self.called_at: Optional[float] = None
        self.resolved_at: Optional[float] = None
        self.cancelled_at: Optional[float] = None
        self.cancelled_by: Optional[str] = None

    @property
    def wait_seconds(self) -> Optional[float]:
        """Waiting time = time called - time submitted (None until called)."""
        if self.called_at is None:
            return None
        return self.called_at - self.submitted_at

    @property
    def service_seconds(self) -> Optional[float]:
        if self.called_at is None or self.resolved_at is None:
            return None
        return self.resolved_at - self.called_at

    def public(self, position: Optional[int] = None) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ticket": self.number,
            "workstation": self.workstation,
            "name": self.name,
            "problem": self.problem,
            "status": self.status,
            "submitted_at": iso(self.submitted_at),
            "called_at": iso(self.called_at),
            "resolved_at": iso(self.resolved_at),
            "cancelled_at": iso(self.cancelled_at),
            "wait_seconds": _round(self.wait_seconds),
        }
        if position is not None:
            d["position"] = position
        return d


class QueueManager:
    def __init__(self, event_log: EventLog, clock: Callable[[], float] = time.time,
                 max_active: int = MAX_ACTIVE_TICKETS) -> None:
        self.lock = threading.RLock()
        self.log = event_log
        self.clock = clock
        self.max_active = max_active
        self.tickets: Dict[int, Ticket] = {}
        self.next_number = 1
        self.session_id: Optional[str] = None
        self.session_started: Optional[float] = None
        self._active_by_ws: Dict[int, int] = {}

    # ------------------------------------------------------- persistence --
    def load(self) -> int:
        """Replay the event log (restart recovery).  Returns events replayed."""
        with self.lock:
            events = self.log.read_all()
            for ev in events:
                self._apply(ev)
            if self.session_id is None:
                now = self.clock()
                self._commit({"event": "SESSION_START", "ts": now,
                              "session": time.strftime("%Y%m%d-%H%M%S", time.localtime(now))})
            return len(events)

    def _commit(self, ev: Dict[str, Any]) -> None:
        ev.setdefault("ts", self.clock())
        ev["time"] = iso(ev["ts"])
        self.log.append(ev)     # write-ahead: if this raises, memory is unchanged
        self._apply(ev)

    def _apply(self, ev: Dict[str, Any]) -> None:
        kind, ts = ev.get("event"), ev.get("ts")
        if kind == "SESSION_START":
            self.session_id = ev.get("session")
            self.session_started = ts
            return
        if kind == "JOIN":
            t = Ticket(ev["ticket"], ev["workstation"], ev["name"], ev["problem"],
                       ev["token_hash"], ts)
            self.tickets[t.number] = t
            self._active_by_ws[t.workstation] = t.number
            self.next_number = max(self.next_number, t.number + 1)
            return
        t = self.tickets.get(ev.get("ticket"))     # type: ignore[arg-type]
        if t is None:
            return
        if kind == "CALL":
            t.status, t.called_at = CALLED, ts
        elif kind == "RESOLVE":
            t.status, t.resolved_at = RESOLVED, ts
            self._active_by_ws.pop(t.workstation, None)
        elif kind == "CANCEL":
            t.status, t.cancelled_at, t.cancelled_by = CANCELLED, ts, ev.get("by")
            self._active_by_ws.pop(t.workstation, None)

    # -------------------------------------------------------- operations --
    def join(self, workstation: int, name: str, problem: str) -> Tuple[Ticket, str]:
        with self.lock:
            existing = self._active_by_ws.get(workstation)
            if existing is not None:
                raise QueueError("DUPLICATE", "workstation %d already has active ticket #%d"
                                 % (workstation, existing))
            if len(self._active_by_ws) >= self.max_active:
                raise QueueError("QUEUE_FULL", "the queue is full (%d active requests)" % self.max_active)
            number = self.next_number           # assigned under the lock
            token = secrets.token_urlsafe(18)
            self._commit({"event": "JOIN", "ticket": number, "workstation": workstation,
                          "name": name, "problem": problem, "token_hash": _hash_token(token)})
            return self.tickets[number], token

    def resume(self, number: int, token: str) -> Ticket:
        with self.lock:
            t = self.tickets.get(number)
            if t is None or not hmac.compare_digest(t.token_hash, _hash_token(token)):
                raise QueueError("NOT_FOUND", "no ticket #%d with that token" % number)
            return t

    def cancel(self, number: int, by: str = "student") -> Ticket:
        with self.lock:
            t = self._get(number)
            if t.status not in ACTIVE:
                raise QueueError("INVALID_STATE", "ticket #%d is already %s" % (number, t.status))
            self._commit({"event": "CANCEL", "ticket": number, "by": by})
            return t

    def call_next(self) -> Ticket:
        with self.lock:
            waiting = self.waiting()
            if not waiting:
                raise QueueError("QUEUE_EMPTY", "no student is waiting")
            t = waiting[0]
            self._commit({"event": "CALL", "ticket": t.number})
            return t

    def resolve(self, number: int) -> Ticket:
        with self.lock:
            t = self._get(number)
            if t.status != CALLED:
                hint = " (call it with NEXT first)" if t.status == WAITING else ""
                raise QueueError("INVALID_STATE", "ticket #%d is %s%s" % (number, t.status, hint))
            self._commit({"event": "RESOLVE", "ticket": number})
            return t

    # ------------------------------------------------------------ queries --
    def _get(self, number: int) -> Ticket:
        t = self.tickets.get(number)
        if t is None:
            raise QueueError("NOT_FOUND", "no ticket #%d in this session" % number)
        return t

    def waiting(self) -> List[Ticket]:
        with self.lock:
            return sorted((t for t in self.tickets.values() if t.status == WAITING),
                          key=lambda t: t.number)

    def positions(self) -> Dict[int, int]:
        with self.lock:
            return {t.number: i for i, t in enumerate(self.waiting(), 1)}

    def position(self, number: int) -> Optional[int]:
        return self.positions().get(number)

    def active_public(self) -> List[Dict[str, Any]]:
        """Tickets being served (CALLED) followed by the waiting line, in order."""
        with self.lock:
            called = sorted((t for t in self.tickets.values() if t.status == CALLED),
                            key=lambda t: t.called_at or 0)
            out = [t.public() for t in called]
            out += [t.public(position=i) for i, t in enumerate(self.waiting(), 1)]
            return out

    def summary(self) -> Dict[str, Any]:
        with self.lock:
            now = self.clock()
            all_t = list(self.tickets.values())
            waits = [t.wait_seconds for t in all_t if t.wait_seconds is not None]
            services = [t.service_seconds for t in all_t if t.service_seconds is not None]
            current = [now - t.submitted_at for t in all_t if t.status == WAITING]

            def count(status: str) -> int:
                return sum(1 for t in all_t if t.status == status)

            return {
                "session": self.session_id,
                "session_started": iso(self.session_started),
                "total_requests": len(all_t),
                "waiting": count(WAITING),
                "being_served": count(CALLED),
                "resolved": count(RESOLVED),
                "cancelled": count(CANCELLED),
                "called_total": len(waits),
                "avg_wait_seconds": _round(sum(waits) / len(waits)) if waits else None,
                "min_wait_seconds": _round(min(waits)) if waits else None,
                "max_wait_seconds": _round(max(waits)) if waits else None,
                "avg_service_seconds": _round(sum(services) / len(services)) if services else None,
                "longest_current_wait_seconds": _round(max(current)) if current else None,
            }
