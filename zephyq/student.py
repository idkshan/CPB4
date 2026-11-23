"""Student program: join the queue, check status, cancel, and get an alert when called."""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from typing import Any, Dict, Optional

from . import protocol as P
from . import ui
from .cli_common import (add_connection_args, ask, clock_time, command_help, header, notify, prompt,
                         say, set_prompt, ssl_context_from_args, state_line)
from .client import ClientConnection, ConnectionLost, ServerError

COMMANDS = [
    ("join", "j", "Ask for help (workstation, name and a short problem description)"),
    ("status", "s", "Show your ticket and your place in line"),
    ("cancel", "c", "Withdraw your request"),
    ("help", "h", "Show this list"),
    ("quit", "q", "Close the program (an active request stays in the queue)"),
]


class StudentApp:
    def __init__(self, args) -> None:
        self.args = args
        self.workstation: int = args.workstation
        self.name: Optional[str] = args.name
        self.state_path = os.path.join(os.path.expanduser(args.state_dir),
                                       "ticket-%s-%d-ws%d.json" % (args.host, args.port, self.workstation))
        self.saved: Optional[Dict[str, Any]] = self._load_state()
        self.conn = ClientConnection(args.host, args.port, ssl_context_from_args(args),
                                     on_push=self.on_push, on_state=lambda s: notify(state_line(s)),
                                     on_reconnect=self.on_reconnect)

    # State file
    def _load_state(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _save_state(self, ticket: int, token: str) -> None:
        self.saved = {"ticket": ticket, "token": token}
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self.saved, fh)

    def _clear_state(self) -> None:
        self.saved = None
        try:
            os.remove(self.state_path)
        except OSError:
            pass

    # Server messages
    def on_push(self, msg: Dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "POSITION":
            notify("%s Ticket #%s: you are now %s in line %s"
                   % (ui.tag("update", "yellow"), msg["ticket"], ui.style("#%s" % msg["position"], "bold", "yellow"),
                      ui.dim("(%s waiting)" % msg["waiting_count"])))
        elif t == "CALLED":
            notify("\a" + called_banner(msg))
        elif t == "RESOLVED":
            self._clear_state()
            notify("%s %s Type %s if you need help again."
                   % (ui.tag("done", "green"), ui.ok("Ticket #%s was marked resolved." % msg["ticket"]),
                      ui.info("join")))
        elif t == "CANCELLED":
            self._clear_state()
            notify("%s Ticket #%s was cancelled from another window." % (ui.tag("cancelled", "yellow"), msg["ticket"]))
        elif t == "BYE":
            notify("%s %s" % (ui.tag("server", "yellow"), msg.get("reason", "closing connection")))
        elif t == "ERR":
            notify("%s %s: %s" % (ui.tag("server error", "red"), msg.get("code"), msg.get("message")))

    def on_reconnect(self) -> None:
        if self.saved:
            self.resume(out=notify)     # runs on the reconnect thread

    # Actions
    def resume(self, out=say) -> None:
        assert self.saved is not None
        try:
            r = self.conn.request("RESUME", ticket=self.saved["ticket"], token=self.saved["token"])
        except ServerError as exc:
            if exc.code == "NOT_FOUND":     # e.g. the instructor started a new session
                self._clear_state()
                out("%s Your saved ticket is no longer known to the server." % ui.tag("info", "cyan"))
                return
            raise
        if r["status"] in ("WAITING", "CALLED"):
            out(ui.tag("resumed", "cyan") + " " + ticket_line(r) + "\n" + ticket_panel("Your request", r))
        else:
            self._clear_state()
            out("%s Your previous ticket #%s is %s." % (ui.tag("info", "cyan"), r["ticket"], r["status"]))

    def join(self) -> None:
        if self.saved:
            say(ui.warn("You already have ticket #%s." % self.saved["ticket"]) +
                " Use %s or %s." % (ui.info("status"), ui.info("cancel")))
            return
        say()
        say(ui.rule("Request help"))
        name = self.name or ask("  Your name: ").strip()
        if not name:
            say()
            return
        if self.name:
            say("  Your name: " + ui.bold(name))
        problem = ask("  Describe the problem %s: " % ui.dim("(max %d characters)" % P.MAX_PROBLEM_LEN)).strip()
        if not problem:
            say(ui.warn("  Not sent - a short description is required."))
            say()
            return
        try:
            r = self.conn.request("JOIN", workstation=self.workstation, name=name, problem=problem)
        except ServerError as exc:
            say(ui.bad("  Could not join: %s" % exc.message))
            say()
            return
        self.name = name
        self._save_state(r["ticket"], r["token"])
        say()
        say(ticket_panel("Queued!", r, color="green"))
        say(ui.dim("  You can keep working - this window will alert you when the instructor is on the way."))
        say()

    def status(self) -> None:
        try:
            r = self.conn.request("STATUS")
        except ServerError as exc:
            if exc.code == "NO_TICKET":
                say("You have no request. Type %s to ask for help." % ui.info("join"))
                return
            raise
        say()
        say(ticket_panel("Your request", r))
        say()

    def cancel(self) -> None:
        try:
            r = self.conn.request("CANCEL")
        except ServerError as exc:
            say(ui.bad("Could not cancel: %s" % exc.message))
            return
        self._clear_state()
        say(ui.tag("cancelled", "yellow") + " Ticket #%s cancelled." % r["ticket"])

    # Run
    def run(self) -> int:
        try:
            self.conn.connect()
        except (OSError, ssl.SSLError, ConnectionLost) as exc:
            say(ui.bad("Cannot connect to %s:%d - %s" % (self.args.host, self.args.port, exc)))
            return 1
        set_prompt("ws %d" % self.workstation)
        say()
        say(header("Student", self.args.host, self.args.port, self.conn.tls_info,
                   [("Workstation", ui.bold(str(self.workstation)))]))
        say(ui.dim("Type %s to see the commands." % ui.plain(ui.info("help"))))
        if self.saved:
            self.resume()
        if not self.saved:
            self.join()
        actions = {"join": self.join, "j": self.join, "status": self.status, "s": self.status,
                   "cancel": self.cancel, "c": self.cancel}
        while True:
            cmd = ask(prompt()).strip().lower()
            if cmd in ("quit", "q", "exit"):
                break
            if cmd in ("help", "h", "?"):
                say(command_help(COMMANDS))
            elif cmd in actions:
                try:
                    actions[cmd]()
                except ConnectionLost as exc:
                    say(ui.bad("Not connected (%s)." % exc) + " The client is reconnecting - try again in a moment.")
            elif cmd:
                say(ui.warn("Unknown command '%s'." % cmd) + " Type %s to see the commands." % ui.info("help"))
        self.conn.close()
        if self.saved:
            say(ui.dim("Your request #%s stays in the queue. Run the client again to see it." % self.saved["ticket"]))
        return 0


# Displays
def ticket_line(r: Dict[str, Any]) -> str:
    if r["status"] == "WAITING":
        return "Ticket #%s - you are #%s in line." % (r["ticket"], r.get("position"))
    return "Ticket #%s is %s." % (r["ticket"], r["status"])


def ticket_panel(title: str, r: Dict[str, Any], color: str = "cyan") -> str:
    status = r["status"]
    rows = [("Ticket", ui.bold("#%s" % r["ticket"])), ("Workstation", str(r["workstation"]))]
    if status == "WAITING":
        rows.append(("Place in line", "you are %s in line %s"
                     % (ui.style("#%s" % r.get("position"), "bold", "yellow"),
                        ui.dim("(%s waiting in total)" % r.get("waiting_count")))))
        rows.append(("Status", ui.warn("WAITING")))
    elif status == "CALLED":
        rows.append(("Status", ui.style("CALLED - the instructor is on the way", "bold", "green")))
    else:
        rows.append(("Status", status))
    rows.append(("Problem", r["problem"]))
    rows.append(("Submitted", clock_time(r["submitted_at"])))
    if r.get("called_at"):
        rows.append(("Called", clock_time(r["called_at"])))
    return ui.panel(title, ui.fields(rows), color=color)


def called_banner(msg: Dict[str, Any]) -> str:
    lines = ["",
             ui.style(">>>   IT'S YOUR TURN   <<<", "bold", "green"),
             "",
             ui.bold(msg["message"]),
             ui.dim("Ticket #%s" % msg["ticket"]),
             ""]
    return ui.panel("Ticket #%s called" % msg["ticket"], [ui.pad(l, 60, "^") for l in lines], color="green", width=66)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zephyq.student", description="ZephyQ student client")
    add_connection_args(ap)
    ap.add_argument("--workstation", type=int, help="your workstation number (%d-%d)"
                    % (P.MIN_WORKSTATION, P.MAX_WORKSTATION))
    ap.add_argument("--name", help="your name (asked when joining if omitted)")
    ap.add_argument("--state-dir", default="~/.zephyq", help="where the ticket token is saved (default: %(default)s)")
    args = ap.parse_args(argv)
    ui.init(args.color)
    while args.workstation is None:
        raw = ask("Workstation number: ").strip()
        if raw == "quit":
            return 0
        if raw.isdigit() and P.MIN_WORKSTATION <= int(raw) <= P.MAX_WORKSTATION:
            args.workstation = int(raw)
        else:
            say(ui.warn("Please type a number from %d to %d." % (P.MIN_WORKSTATION, P.MAX_WORKSTATION)))
    if not P.MIN_WORKSTATION <= args.workstation <= P.MAX_WORKSTATION:
        ap.error("--workstation must be %d-%d" % (P.MIN_WORKSTATION, P.MAX_WORKSTATION))
    try:
        return StudentApp(args).run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
