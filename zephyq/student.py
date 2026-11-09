"""ZephyQ student client.

    python -m zephyq.student --host 192.168.56.1 --workstation 11 [--tls]

Commands: join, status, cancel, help, quit
The ticket number and its secret token are saved in a small file so the
client can RESUME the same ticket after a disconnect, a client restart or a
server restart, without creating a duplicate request.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from typing import Any, Dict, Optional

from . import protocol as P
from .cli_common import (PROMPT, add_connection_args, ask, clock_time, notify, say,
                         ssl_context_from_args)
from .client import ClientConnection, ConnectionLost, ServerError

HELP = """Commands:
  join     request help (workstation, name and a short problem description)
  status   show your ticket and place in line
  cancel   withdraw your request
  quit     leave (an active request stays in the queue; run again to resume it)"""


class StudentApp:
    def __init__(self, args) -> None:
        self.args = args
        self.workstation: int = args.workstation
        self.name: Optional[str] = args.name
        self.state_path = os.path.join(os.path.expanduser(args.state_dir),
                                       "ticket-%s-%d-ws%d.json" % (args.host, args.port, self.workstation))
        self.saved: Optional[Dict[str, Any]] = self._load_state()
        self.conn = ClientConnection(args.host, args.port, ssl_context_from_args(args),
                                     on_push=self.on_push, on_state=lambda s: notify("[connection] " + s),
                                     on_reconnect=self.on_reconnect)

    # ------------------------------------------------------- state file --
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

    # ------------------------------------------------- server messages --
    def on_push(self, msg: Dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "POSITION":
            notify("[update] Ticket #%s: you are now #%s in line (%s waiting)."
                   % (msg["ticket"], msg["position"], msg["waiting_count"]))
        elif t == "CALLED":
            notify("\a" + "*" * 60 + "\n  Ticket #%s: %s\n" % (msg["ticket"], msg["message"]) + "*" * 60)
        elif t == "RESOLVED":
            self._clear_state()
            notify("[done] Ticket #%s was marked resolved. Type 'join' if you need help again." % msg["ticket"])
        elif t == "CANCELLED":
            self._clear_state()
            notify("[cancelled] Ticket #%s was cancelled from another window." % msg["ticket"])
        elif t == "BYE":
            notify("[server] %s" % msg.get("reason", "closing connection"))
        elif t == "ERR":
            notify("[server error] %s: %s" % (msg.get("code"), msg.get("message")))

    def on_reconnect(self) -> None:
        if self.saved:
            self.resume(out=notify)     # runs on the reconnect thread

    # ---------------------------------------------------------- actions --
    def resume(self, out=say) -> None:
        assert self.saved is not None
        try:
            r = self.conn.request("RESUME", ticket=self.saved["ticket"], token=self.saved["token"])
        except ServerError as exc:
            if exc.code == "NOT_FOUND":     # e.g. the instructor started a new session
                self._clear_state()
                out("[info] Your saved ticket is no longer known to the server.")
                return
            raise
        if r["status"] in ("WAITING", "CALLED"):
            out("[resumed] " + describe_ticket(r))
        else:
            self._clear_state()
            out("[info] Your previous ticket #%s is %s." % (r["ticket"], r["status"]))

    def join(self) -> None:
        if self.saved:
            say("You already have ticket #%s. Use 'status' or 'cancel'." % self.saved["ticket"])
            return
        name = self.name or ask("Your name: ").strip()
        if not name:
            return
        problem = ask("Describe the problem (max %d chars): " % P.MAX_PROBLEM_LEN).strip()
        if not problem:
            say("Cancelled - a short description is required.")
            return
        try:
            r = self.conn.request("JOIN", workstation=self.workstation, name=name, problem=problem)
        except ServerError as exc:
            say("Could not join: %s" % exc.message)
            return
        self.name = name
        self._save_state(r["ticket"], r["token"])
        say("Queued! " + describe_ticket(r))
        say("You can keep working - this window will alert you when the instructor is on the way.")

    def status(self) -> None:
        try:
            r = self.conn.request("STATUS")
        except ServerError as exc:
            if exc.code == "NO_TICKET":
                say("You have no request. Type 'join' to ask for help.")
                return
            raise
        say(describe_ticket(r))

    def cancel(self) -> None:
        try:
            r = self.conn.request("CANCEL")
        except ServerError as exc:
            say("Could not cancel: %s" % exc.message)
            return
        self._clear_state()
        say("Ticket #%s cancelled." % r["ticket"])

    # -------------------------------------------------------------- run --
    def run(self) -> int:
        try:
            self.conn.connect()
        except (OSError, ssl.SSLError, ConnectionLost) as exc:
            say("Cannot connect to %s:%d - %s" % (self.args.host, self.args.port, exc))
            return 1
        say("Connected to ZephyQ at %s:%d [%s]" % (self.args.host, self.args.port, self.conn.tls_info))
        say("Workstation %d. Type 'help' for commands." % self.workstation)
        if self.saved:
            self.resume()
        if not self.saved:
            self.join()
        actions = {"join": self.join, "j": self.join, "status": self.status, "s": self.status,
                   "cancel": self.cancel, "c": self.cancel}
        while True:
            cmd = ask(PROMPT).strip().lower()
            if cmd in ("quit", "q", "exit"):
                break
            if cmd in ("help", "h", "?"):
                say(HELP)
            elif cmd in actions:
                try:
                    actions[cmd]()
                except ConnectionLost as exc:
                    say("Not connected (%s). The client is reconnecting - try again in a moment." % exc)
            elif cmd:
                say("Unknown command. " + HELP)
        self.conn.close()
        if self.saved:
            say("Your request #%s stays in the queue. Run the client again to see it." % self.saved["ticket"])
        return 0


def describe_ticket(r: Dict[str, Any]) -> str:
    if r["status"] == "WAITING":
        return ("Ticket #%s (workstation %s) - you are #%s in line, %s waiting in total. Submitted %s."
                % (r["ticket"], r["workstation"], r.get("position"), r.get("waiting_count"),
                   clock_time(r["submitted_at"])))
    if r["status"] == "CALLED":
        return "Ticket #%s - the instructor is on the way (called %s)." % (r["ticket"], clock_time(r["called_at"]))
    return "Ticket #%s is %s." % (r["ticket"], r["status"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zephyq.student", description="ZephyQ student client")
    add_connection_args(ap)
    ap.add_argument("--workstation", type=int, help="your workstation number (%d-%d)"
                    % (P.MIN_WORKSTATION, P.MAX_WORKSTATION))
    ap.add_argument("--name", help="your name (asked when joining if omitted)")
    ap.add_argument("--state-dir", default="~/.zephyq", help="where the ticket token is saved (default: %(default)s)")
    args = ap.parse_args(argv)
    while args.workstation is None:
        raw = ask("Workstation number: ").strip()
        if raw == "quit":
            return 0
        if raw.isdigit() and P.MIN_WORKSTATION <= int(raw) <= P.MAX_WORKSTATION:
            args.workstation = int(raw)
    if not P.MIN_WORKSTATION <= args.workstation <= P.MAX_WORKSTATION:
        ap.error("--workstation must be %d-%d" % (P.MIN_WORKSTATION, P.MAX_WORKSTATION))
    try:
        return StudentApp(args).run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
