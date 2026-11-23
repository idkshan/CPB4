"""Prints a session report, and optionally a CSV file, from a session log."""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zephyq.queue_core import QueueManager, iso    # noqa: E402
from zephyq.storage import EventLog                # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", default=os.path.join("data", "session.jsonl"))
    ap.add_argument("--csv", help="write per-ticket rows to this CSV file")
    args = ap.parse_args()
    if not os.path.exists(args.log):
        print("no such log file: %s" % args.log, file=sys.stderr)
        return 1

    mgr = QueueManager(EventLog(args.log, readonly=True))
    mgr.load()
    s = mgr.summary()
    print("Session          : %s (started %s)" % (s["session"], s["session_started"]))
    print("Total requests   : %d" % s["total_requests"])
    print("Resolved         : %d" % s["resolved"])
    print("Cancelled        : %d" % s["cancelled"])
    print("Still waiting    : %d   being served: %d" % (s["waiting"], s["being_served"]))
    print("Avg waiting time : %s s over %d called requests (min %s s, max %s s)"
          % (s["avg_wait_seconds"], s["called_total"], s["min_wait_seconds"], s["max_wait_seconds"]))
    print("Avg service time : %s s" % s["avg_service_seconds"])
    print()
    print("%-6s %-4s %-18s %-9s %-9s %-9s %-9s %8s  %s" %
          ("Ticket", "WS", "Name", "Submitted", "Called", "Resolved", "Status", "Wait(s)", "Problem"))
    rows = sorted(mgr.tickets.values(), key=lambda t: t.number)
    for t in rows:
        hhmmss = lambda ts: iso(ts)[11:19] if ts else "-"      # noqa: E731
        print("#%-5d %-4d %-18s %-9s %-9s %-9s %-9s %8s  %s" %
              (t.number, t.workstation, t.name[:18], hhmmss(t.submitted_at), hhmmss(t.called_at),
               hhmmss(t.resolved_at or t.cancelled_at), t.status,
               "-" if t.wait_seconds is None else "%.1f" % t.wait_seconds, t.problem[:50]))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ticket", "workstation", "name", "problem", "status", "submitted_at", "called_at",
                        "resolved_at", "cancelled_at", "wait_seconds", "service_seconds"])
            for t in rows:
                w.writerow([t.number, t.workstation, t.name, t.problem, t.status, iso(t.submitted_at),
                            iso(t.called_at), iso(t.resolved_at), iso(t.cancelled_at),
                            "" if t.wait_seconds is None else round(t.wait_seconds, 1),
                            "" if t.service_seconds is None else round(t.service_seconds, 1)])
        print("\nwrote %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
