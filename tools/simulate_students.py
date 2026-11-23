"""Makes several fake students join in a fixed order to test first-come, first-served."""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zephyq import protocol as P                       # noqa: E402
from zephyq.client import ClientConnection, ServerError  # noqa: E402
from zephyq.tlsutil import client_context              # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--cafile", default=os.path.join("certs", "ca.crt"))
    ap.add_argument("--workstations", default="21,22,23", help="comma-separated join order")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between joins")
    args = ap.parse_args()

    conns = []
    for ws in [int(x) for x in args.workstations.split(",")]:
        tag = "[WS %d]" % ws
        conn = ClientConnection(args.host, args.port, client_context(args.cafile) if args.tls else None,
                                on_push=lambda m, tag=tag: print(tag, "<<", m.get("type"),
                                                                 {k: v for k, v in m.items() if k != "type"}),
                                auto_reconnect=False)
        conn.connect()
        try:
            r = conn.request("JOIN", workstation=ws, name="Sim %d" % ws, problem="simulated request from WS %d" % ws)
            print("%s joined at %s -> ticket #%d, position %d" % (tag, time.strftime("%H:%M:%S"), r["ticket"], r["position"]))
        except ServerError as exc:
            print("%s JOIN rejected: %s %s" % (tag, exc.code, exc.message))
        conns.append(conn)
        time.sleep(args.delay)
    print("All joined. Waiting for updates (Ctrl+C to stop)...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    for c in conns:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
