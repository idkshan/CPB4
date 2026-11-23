"""Instructor program: log in, list the queue, call next, mark done, show the summary."""
from __future__ import annotations

import argparse
import getpass
import ssl
import sys
import time
from typing import Any, Dict, List, Optional, Set

from . import ui
from .cli_common import (add_connection_args, ask, clock_time, command_help, fmt_duration, header, notify,
                         prompt, say, seconds_since, set_prompt, ssl_context_from_args, state_line)
from .client import ClientConnection, ConnectionLost, ServerError

COMMANDS = [
    ("list", "l", "Show students being served and the waiting line"),
    ("next", "n", "Call the next student (first come, first served)"),
    ("done [ticket]", "d", "Mark a called ticket as resolved (default: the one you are serving)"),
    ("summary", "s", "Session summary: number of requests and average waiting time"),
    ("help", "h", "Show this list"),
    ("quit", "q", "Log out"),
]


class InstructorApp:
    def __init__(self, args) -> None:
        self.args = args
        self.username: Optional[str] = args.username
        self.password: Optional[str] = None
        self.tickets: List[Dict[str, Any]] = []
        self.known: Set[int] = set()
        self.conn = ClientConnection(args.host, args.port, ssl_context_from_args(args),
                                     on_push=self.on_push, on_state=lambda s: notify(state_line(s)),
                                     on_reconnect=self.on_reconnect)

    # Server messages
    def on_push(self, msg: Dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "QUEUE":
            self._update(msg["tickets"], announce=True)
        elif t == "BYE":
            notify("%s %s" % (ui.tag("server", "yellow"), msg.get("reason", "closing connection")))
        elif t == "ERR":
            notify("%s %s: %s" % (ui.tag("server error", "red"), msg.get("code"), msg.get("message")))

    def _update(self, tickets: List[Dict[str, Any]], announce: bool) -> None:
        current = {t["ticket"] for t in tickets}
        if announce:
            waiting = sum(1 for x in tickets if x["status"] == "WAITING")
            for t in tickets:
                if t["ticket"] not in self.known and t["status"] == "WAITING":
                    notify("%s %s  WS %s  %s: %s  %s"
                           % (ui.tag("new request", "cyan"), ui.bold("#%s" % t["ticket"]), ui.bold(str(t["workstation"])),
                              t["name"], ui.truncate(t["problem"], 50), ui.dim("(%d waiting)" % waiting)))
            for gone in sorted(self.known - current):
                old = next((x for x in self.tickets if x["ticket"] == gone), None)
                if old is not None and old["status"] == "WAITING":
                    notify("%s #%s (WS %s) cancelled the request.  %s"
                           % (ui.tag("left queue", "yellow"), gone, old["workstation"], ui.dim("(%d waiting)" % waiting)))
        self.tickets, self.known = tickets, current

    def on_reconnect(self) -> None:
        if self.username and self.password:
            self.conn.request("LOGIN", username=self.username, password=self.password)
            notify("%s logged in again as %s" % (ui.tag("connection", "green"), self.username))
            self.list()

    # Actions
    def login(self) -> bool:
        say(ui.rule("Log in"))
        for _ in range(3):
            user = self.username or ask("  Username: ").strip()
            if user == "quit":
                return False
            if self.username:
                say("  Username: " + ui.bold(user))
            pw = getpass.getpass("  Password: ")
            try:
                r = self.conn.request("LOGIN", username=user, password=pw)
            except ServerError as exc:
                say(ui.bad("  Login failed: %s" % exc.message))
                self.username = None
                if exc.code == "RATE_LIMITED":
                    return False
                continue
            self.username, self.password = user, pw
            say(ui.tag("ok", "green") + " Logged in as %s. %s student(s) waiting." % (ui.bold(user), r["waiting_count"]))
            return True
        return False

    def list(self) -> None:
        r = self.conn.request("LIST")
        self._update(r["tickets"], announce=False)
        serving = sum(1 for t in r["tickets"] if t["status"] == "CALLED")
        say()
        say("%s   %s   %s   %s" % (ui.bold("Queue"), ui.style("%d waiting" % r["waiting_count"], "bold", "yellow"),
                                  ui.style("%d being served" % serving, "bold", "green"),
                                  ui.dim("as of " + time.strftime("%H:%M:%S"))))
        if not r["tickets"]:
            say(ui.panel("Queue", [ui.dim("Nobody is waiting.")], color="grey"))
            say()
            return
        rows = []
        for t in r["tickets"]:
            called = t["status"] == "CALLED"
            waited = t["wait_seconds"] if called else seconds_since(t["submitted_at"])
            rows.append([
                str(t.get("position", "-")),
                ui.bold("#%s" % t["ticket"]),
                ui.bold(str(t["workstation"])),
                t["name"],
                fmt_duration(waited),
                ui.style("CALLED", "bold", "green") if called else ui.warn("WAITING"),
                t["problem"],
            ])
        say(ui.table(["Pos", "Ticket", "WS", "Name", "Waiting", "Status", "Problem"], rows,
                     aligns="><><><<", flex=6))
        say()

    def next(self) -> None:
        try:
            t = self.conn.request("NEXT")
        except ServerError as exc:
            say(ui.warn(exc.message))
            return
        rows = ui.fields([
            ("Workstation", ui.style(str(t["workstation"]), "bold", "green")),
            ("Student", ui.bold(t["name"])),
            ("Problem", t["problem"]),
            ("Waited", "%s %s" % (ui.bold(fmt_duration(t["wait_seconds"])),
                                  ui.dim("(submitted %s)" % clock_time(t["submitted_at"])))),
        ])
        say()
        say(ui.panel("Now serving ticket #%s" % t["ticket"], rows, color="green"))
        say(ui.dim("  Walk to workstation %s. Type %s when finished." % (t["workstation"], ui.plain(ui.info("done")))))
        say()

    def done(self, arg: str) -> None:
        if arg:
            if not arg.lstrip("#").isdigit():
                say(ui.warn("Usage: done [ticket number]"))
                return
            number = int(arg.lstrip("#"))
        else:
            called = [t["ticket"] for t in self.conn.request("LIST")["tickets"] if t["status"] == "CALLED"]
            if len(called) != 1:
                say(ui.warn("You are serving %s." % ("nobody" if not called else
                                                       "tickets " + ", ".join("#%d" % n for n in called))) +
                    (" Use: done <ticket>" if called else " Type %s to call a student." % ui.info("next")))
                return
            number = called[0]
        try:
            t = self.conn.request("DONE", ticket=number)
        except ServerError as exc:
            say(ui.warn(exc.message))
            return
        service = seconds_since(t["called_at"]) - seconds_since(t["resolved_at"])
        say(ui.tag("done", "green") + " " + ui.ok("Ticket #%s resolved" % t["ticket"]) +
            ui.dim(" (service time %s)." % fmt_duration(service)))

    def summary(self) -> None:
        s = self.conn.request("SUMMARY")["summary"]
        rows = [
            ("Session", "%s %s" % (s["session"], ui.dim("(started %s)" % clock_time(s["session_started"])))),
            ("Total requests", ui.bold(str(s["total_requests"]))),
            ("Waiting", ui.warn(str(s["waiting"]))),
            ("Being served", ui.ok(str(s["being_served"]))),
            ("Resolved", str(s["resolved"])),
            ("Cancelled", str(s["cancelled"])),
            ("Average waiting time", "%s %s" % (ui.style(fmt_duration(s["avg_wait_seconds"]), "bold", "cyan"),
                                                ui.dim("over %s called request(s)" % s["called_total"]))),
            ("Shortest / longest wait", "%s / %s" % (fmt_duration(s["min_wait_seconds"]),
                                                     fmt_duration(s["max_wait_seconds"]))),
            ("Average service time", fmt_duration(s["avg_service_seconds"])),
        ]
        if s["longest_current_wait_seconds"] is not None:
            rows.append(("Longest current wait", ui.warn(fmt_duration(s["longest_current_wait_seconds"]))))
        say()
        say(ui.panel("Session summary", ui.fields(rows)))
        say()

    # Run
    def run(self) -> int:
        try:
            self.conn.connect()
        except (OSError, ssl.SSLError, ConnectionLost) as exc:
            say(ui.bad("Cannot connect to %s:%d - %s" % (self.args.host, self.args.port, exc)))
            return 1
        say()
        say(header("Instructor", self.args.host, self.args.port, self.conn.tls_info))
        if not self.login():
            self.conn.close()
            return 1
        set_prompt("instructor")
        self.list()
        say(ui.dim("Type help to see the commands."))
        while True:
            line = ask(prompt()).strip()
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
                    say(command_help(COMMANDS))
                elif cmd:
                    say(ui.warn("Unknown command '%s'." % cmd) + " Type %s to see the commands." % ui.info("help"))
            except ConnectionLost as exc:
                say(ui.bad("Not connected (%s)." % exc) + " The client is reconnecting - try again in a moment.")
            except ServerError as exc:
                say(ui.bad("Server error: %s" % exc.message))
        self.conn.close()
        say(ui.dim("Logged out."))
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zephyq.instructor", description="ZephyQ instructor client")
    add_connection_args(ap)
    ap.add_argument("--username")
    args = ap.parse_args(argv)
    ui.init(args.color)
    try:
        return InstructorApp(args).run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
