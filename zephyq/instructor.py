"""ZephyQ instructor client.

    python -m zephyq.instructor --host 192.168.56.1 [--tls] [--username NAME]

Commands: list, next, done [ticket], summary, help, quit
"""
from __future__ import annotations

import argparse
import getpass
import ssl
import sys
from typing import Any, Dict, List, Optional, Set

from .cli_common import (PROMPT, add_connection_args, ask, clock_time, fmt_duration, notify,
                         say, seconds_since, ssl_context_from_args)
from .client import ClientConnection, ConnectionLost, ServerError

HELP = """Commands:
  list (l)            show students being served and the waiting line
  next (n)            call the next student (first come, first served)
  done (d) [ticket]   mark a called ticket as resolved (default: the one you are serving)
  summary (s)         session summary: number of requests and average waiting time
  quit (q)            log out"""


class InstructorApp:
    def __init__(self, args) -> None:
        self.args = args
        self.username: Optional[str] = args.username
        self.password: Optional[str] = None
        self.tickets: List[Dict[str, Any]] = []
        self.known: Set[int] = set()
        self.conn = ClientConnection(args.host, args.port, ssl_context_from_args(args),
                                     on_push=self.on_push, on_state=lambda s: notify("[connection] " + s),
                                     on_reconnect=self.on_reconnect)

    # ------------------------------------------------- server messages --
    def on_push(self, msg: Dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "QUEUE":
            self._update(msg["tickets"], announce=True)
        elif t == "BYE":
            notify("[server] %s" % msg.get("reason", "closing connection"))
        elif t == "ERR":
            notify("[server error] %s: %s" % (msg.get("code"), msg.get("message")))

    def _update(self, tickets: List[Dict[str, Any]], announce: bool) -> None:
        current = {t["ticket"] for t in tickets}
        if announce:
            for t in tickets:
                if t["ticket"] not in self.known and t["status"] == "WAITING":
                    notify("[new request] #%s  WS %s  %s: %s  (%s waiting)"
                           % (t["ticket"], t["workstation"], t["name"], t["problem"],
                              sum(1 for x in tickets if x["status"] == "WAITING")))
            for gone in sorted(self.known - current):
                old = next((x for x in self.tickets if x["ticket"] == gone), None)
                if old is not None and old["status"] == "WAITING":
                    notify("[left queue] #%s (WS %s) cancelled the request." % (gone, old["workstation"]))
        self.tickets, self.known = tickets, current

    def on_reconnect(self) -> None:
        if self.username and self.password:
            self.conn.request("LOGIN", username=self.username, password=self.password)
            notify("[connection] logged in again as %s" % self.username)
            self.list()

    # ---------------------------------------------------------- actions --
    def login(self) -> bool:
        for _ in range(3):
            user = self.username or ask("Username: ").strip()
            if user == "quit":
                return False
            pw = getpass.getpass("Password: ")
            try:
                r = self.conn.request("LOGIN", username=user, password=pw)
            except ServerError as exc:
                say("Login failed: %s" % exc.message)
                self.username = None
                if exc.code == "RATE_LIMITED":
                    return False
                continue
            self.username, self.password = user, pw
            say("Logged in as %s. %s student(s) waiting." % (user, r["waiting_count"]))
            return True
        return False

    def list(self) -> None:
        r = self.conn.request("LIST")
        self._update(r["tickets"], announce=False)
        if not r["tickets"]:
            say("Nobody is waiting.")
            return
        say("%-4s %-6s %-4s %-18s %-8s %-9s %s" % ("Pos", "Ticket", "WS", "Name", "Waiting", "Status", "Problem"))
        for t in r["tickets"]:
            pos = str(t.get("position", "-"))
            waited = t["wait_seconds"] if t["status"] == "CALLED" else seconds_since(t["submitted_at"])
            problem = t["problem"] if len(t["problem"]) <= 45 else t["problem"][:42] + "..."
            say("%-4s #%-5s %-4s %-18s %-8s %-9s %s" % (pos, t["ticket"], t["workstation"], t["name"][:18],
                                                       fmt_duration(waited), t["status"], problem))
        say("%d waiting." % r["waiting_count"])

    def next(self) -> None:
        try:
            t = self.conn.request("NEXT")
        except ServerError as exc:
            say(exc.message)
            return
        say("Now serving ticket #%s - workstation %s, %s" % (t["ticket"], t["workstation"], t["name"]))
        say("  Problem: %s" % t["problem"])
        say("  Waited %s (submitted %s). Type 'done' when finished."
            % (fmt_duration(t["wait_seconds"]), clock_time(t["submitted_at"])))

    def done(self, arg: str) -> None:
        if arg:
            if not arg.lstrip("#").isdigit():
                say("Usage: done [ticket number]")
                return
            number = int(arg.lstrip("#"))
        else:
            called = [t["ticket"] for t in self.conn.request("LIST")["tickets"] if t["status"] == "CALLED"]
            if len(called) != 1:
                say("You are serving %s. Use: done <ticket>" %
                    ("nobody" if not called else "tickets " + ", ".join("#%d" % n for n in called)))
                return
            number = called[0]
        try:
            t = self.conn.request("DONE", ticket=number)
        except ServerError as exc:
            say(exc.message)
            return
        say("Ticket #%s resolved (service time %s)." %
            (t["ticket"], fmt_duration(seconds_since(t["called_at"]) - seconds_since(t["resolved_at"]))))

    def summary(self) -> None:
        s = self.conn.request("SUMMARY")["summary"]
        say("Session %s (started %s)" % (s["session"], clock_time(s["session_started"])))
        say("  Total requests        : %s" % s["total_requests"])
        say("  Waiting / being served: %s / %s" % (s["waiting"], s["being_served"]))
        say("  Resolved / cancelled  : %s / %s" % (s["resolved"], s["cancelled"]))
        say("  Average waiting time  : %s  (over %s called request(s); min %s, max %s)"
            % (fmt_duration(s["avg_wait_seconds"]), s["called_total"],
               fmt_duration(s["min_wait_seconds"]), fmt_duration(s["max_wait_seconds"])))
        say("  Average service time  : %s" % fmt_duration(s["avg_service_seconds"]))
        if s["longest_current_wait_seconds"] is not None:
            say("  Longest current wait  : %s" % fmt_duration(s["longest_current_wait_seconds"]))

    # -------------------------------------------------------------- run --
    def run(self) -> int:
        try:
            self.conn.connect()
        except (OSError, ssl.SSLError, ConnectionLost) as exc:
            say("Cannot connect to %s:%d - %s" % (self.args.host, self.args.port, exc))
            return 1
        say("Connected to ZephyQ at %s:%d [%s]" % (self.args.host, self.args.port, self.conn.tls_info))
        if not self.login():
            self.conn.close()
            return 1
        self.list()
        say("Type 'help' for commands.")
        while True:
            line = ask(PROMPT).strip()
            cmd, _, arg = line.partition(" ")
            cmd = cmd.lower()
            if cmd in ("quit", "q", "exit"):
                break
            try:
                if cmd in ("list", "l"):
                    self.list()
                elif cmd in ("next", "n"):
                    self.next()
                elif cmd in ("done", "d"):
                    self.done(arg.strip())
                elif cmd in ("summary", "s"):
                    self.summary()
                elif cmd in ("help", "h", "?"):
                    say(HELP)
                elif cmd:
                    say("Unknown command. " + HELP)
            except ConnectionLost as exc:
                say("Not connected (%s). The client is reconnecting - try again in a moment." % exc)
            except ServerError as exc:
                say("Server error: %s" % exc.message)
        self.conn.close()
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zephyq.instructor", description="ZephyQ instructor client")
    add_connection_args(ap)
    ap.add_argument("--username")
    args = ap.parse_args(argv)
    try:
        return InstructorApp(args).run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
