"""Runs experiments E1-E10 on this laptop and saves the results in experiments/results."""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import ssl
import statistics
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from zephyq import protocol as P                      # noqa: E402
from zephyq.auth import CredentialStore               # noqa: E402
from zephyq.client import ClientConnection, ConnectionLost, ServerError  # noqa: E402
from zephyq.queue_core import QueueManager            # noqa: E402
from zephyq.server import ZephyQServer                # noqa: E402
from zephyq.storage import EventLog                   # noqa: E402
from zephyq.tlsutil import client_context, server_context  # noqa: E402
import make_certs                                     # noqa: E402
from tap_proxy import TapProxy                        # noqa: E402

OUT = os.path.join(ROOT, "experiments", "results")
PROF, PASSWORD = "prof", "experiment-pass-1"
results: dict = {}
lines: list = []


def say(text=""):
    print(text, flush=True)
    lines.append(text)


def stats(values):
    v = sorted(values)
    return {"n": len(v), "mean": statistics.mean(v), "median": statistics.median(v),
            "stdev": statistics.stdev(v) if len(v) > 1 else 0.0, "min": v[0], "max": v[-1],
            "p95": v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]}


def ms(s):
    return {k: (round(x * 1000, 3) if isinstance(x, float) else x) for k, x in s.items()}


# Fixtures
class Lab:
    """A fresh server with its own data folder and credentials."""

    def __init__(self, ssl_ctx=None, data_dir=None):
        self.tmp = tempfile.mkdtemp(prefix="zephyq-exp-")
        self.data_dir = data_dir or os.path.join(self.tmp, "data")
        self.creds = os.path.join(self.tmp, "instructors.json")
        CredentialStore(self.creds).set_password(PROF, PASSWORD)
        self.ssl_ctx = ssl_ctx
        self.clients = []
        self.start()

    def start(self):
        self.mgr = QueueManager(EventLog(os.path.join(self.data_dir, "session.jsonl")))
        self.mgr.load()
        self.server = ZephyQServer(self.mgr, CredentialStore(self.creds), "127.0.0.1", 0, self.ssl_ctx,
                                   login_fail_delay=0.5)
        self.port = self.server.start()

    def stop(self):
        self.server.shutdown()

    def client(self, ctx=None, port=None, reconnect=False, on_reconnect=None, pushes=None):
        pushes = pushes if pushes is not None else []
        c = ClientConnection("127.0.0.1", port or self.port, ctx, on_push=pushes.append,
                             auto_reconnect=reconnect, on_reconnect=on_reconnect, heartbeat=0, max_backoff=1.0)
        c.connect(timeout=10)
        c.pushes = pushes                       # type: ignore[attr-defined]
        self.clients.append(c)
        return c

    def instructor(self, ctx=None):
        c = self.client(ctx)
        c.request("LOGIN", username=PROF, password=PASSWORD)
        return c

    def close(self):
        for c in self.clients:
            try:
                c.close()
            except Exception:
                pass
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


# Experiments
def e1_connectivity():
    say("E1  Network connectivity (TCP connection to the server application)")
    lab = Lab()
    times, ok = [], 0
    for _ in range(50):
        t0 = time.perf_counter()
        try:
            c = ClientConnection("127.0.0.1", lab.port, heartbeat=0, auto_reconnect=False)
            c.connect(timeout=5)
            times.append(time.perf_counter() - t0)
            ok += 1
            c.close()
        except (OSError, ConnectionLost):
            pass
    # TCP port reachability without the protocol (like Test-NetConnection)
    s = socket.create_connection(("127.0.0.1", lab.port), timeout=5)
    port_open = True
    s.close()
    lab.close()
    r = {"attempts": 50, "successful": ok, "port_open": port_open,
         "connect_to_welcome_ms": ms(stats(times))}
    results["E1"] = r
    say("    %d/50 connections reached WELCOME; mean %.2f ms (min %.2f, max %.2f)"
        % (ok, r["connect_to_welcome_ms"]["mean"], r["connect_to_welcome_ms"]["min"], r["connect_to_welcome_ms"]["max"]))


def parse_wire(data: bytes):
    msgs = []
    for line in data.split(b"\n"):
        if line.strip():
            try:
                msgs.append(json.loads(line))
            except ValueError:
                msgs.append({"type": "<unparseable>"})
    return msgs


def e2_tcp_communication():
    say("E2  TCP connection and communication (bytes recorded on the wire)")
    lab = Lab()
    tap = TapProxy("127.0.0.1", lab.port).start()
    c = ClientConnection("127.0.0.1", tap.port, heartbeat=0, auto_reconnect=False)
    c.connect()
    c.request("JOIN", workstation=11, name="Ana", problem="IDE will not start")
    c.request("STATUS")
    c.close()
    time.sleep(0.3)
    up, down = bytes(tap.client_to_server), bytes(tap.server_to_client)
    tap.close()
    lab.close()
    sent, recv = parse_wire(up), parse_wire(down)
    seq = []
    for m in recv[:1]:
        seq.append("S->C " + m["type"])
    for a, b in zip(sent, recv[1:]):
        seq.append("C->S " + a["type"])
        seq.append("S->C " + b["type"])
    with open(os.path.join(OUT, "wire_plain.txt"), "wb") as fh:
        fh.write(b"--- client -> server ---\n" + up + b"\n--- server -> client ---\n" + down)
    r = {"client_to_server_bytes": len(up), "server_to_client_bytes": len(down),
         "messages_sent": len(sent), "messages_received": len(recv), "sequence": seq,
         "every_request_answered": all(m.get("re") == s.get("id") for s, m in zip(sent, recv[1:])),
         "plaintext_visible": b"IDE will not start" in up}
    results["E2"] = r
    say("    sequence: " + ", ".join(seq))
    say("    %d B up / %d B down; every request answered: %s; plaintext visible: %s"
        % (len(up), len(down), r["every_request_answered"], r["plaintext_visible"]))


def e3_fcfs():
    say("E3  First-come first-served ordering")
    # (a) planned sequential order with position checks
    lab = Lab()
    inst = lab.instructor()
    order = [12, 11, 13, 15, 14]
    students = {}
    for ws in order:
        c = lab.client()
        c.request("JOIN", workstation=ws, name="S%d" % ws, problem="p")
        students[ws] = c
    called, pos_errors = [], 0
    remaining = list(order)
    for _ in order:
        t = inst.request("NEXT")
        called.append(t["workstation"])
        remaining.remove(t["workstation"])
        for i, ws in enumerate(remaining, 1):
            if students[ws].request("STATUS")["position"] != i:
                pos_errors += 1
        inst.request("DONE", ticket=t["ticket"])
    # additional request after earlier tickets completed
    late = students[12]
    lt = late.request("JOIN", workstation=12, name="S12", problem="second problem")
    lab.close()
    seq_ok = called == order
    # (b) 40 simultaneous joins from 40 connections
    lab = Lab()
    inst = lab.instructor()
    n = 40
    conns = [lab.client() for _ in range(n)]
    barrier = threading.Barrier(n)
    got = {}

    def go(i):
        barrier.wait()
        got[i] = conns[i].request("JOIN", workstation=100 + i, name="C%d" % i, problem="p")["ticket"]

    th = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    t0 = time.perf_counter()
    for t in th:
        t.start()
    for t in th:
        t.join()
    burst = time.perf_counter() - t0
    tickets = sorted(got.values())
    calls = [inst.request("NEXT")["ticket"] for _ in range(n)]
    lab.close()
    r = {"sequential": {"join_order": order, "call_order": called, "order_matches": seq_ok,
                        "position_errors": pos_errors, "late_request_ticket": lt["ticket"],
                        "late_request_position": lt["position"]},
         "simultaneous": {"clients": n, "unique_tickets": len(set(tickets)), "gapless_1_to_n": tickets == list(range(1, n + 1)),
                          "calls_in_ticket_order": calls == list(range(1, n + 1)), "burst_seconds": round(burst, 4)}}
    results["E3"] = r
    say("    planned order %s -> called %s (match: %s, position errors: %d)" % (order, called, seq_ok, pos_errors))
    say("    40 simultaneous joins: %d unique tickets, gapless: %s, called in ticket order: %s, %.1f ms"
        % (len(set(tickets)), r["simultaneous"]["gapless_1_to_n"], r["simultaneous"]["calls_in_ticket_order"], burst * 1000))


def e4_duplicates():
    say("E4  Duplicate requests and cancellation")
    lab = Lab()
    a = lab.client()
    a.request("JOIN", workstation=11, name="A", problem="p")
    rejected = 0
    for _ in range(10):
        try:
            lab.client().request("JOIN", workstation=11, name="X", problem="p")
        except ServerError as e:
            rejected += e.code == "DUPLICATE"
    same_conn = None
    try:
        a.request("JOIN", workstation=12, name="A", problem="p")
    except ServerError as e:
        same_conn = e.code
    others = []
    for ws in (12, 13, 14, 15):
        c = lab.client()
        c.request("JOIN", workstation=ws, name="S", problem="p")
        others.append(c)
    before = [c.request("STATUS")["position"] for c in others]
    others[1].request("CANCEL")                      # workstation 13 cancels
    after = [others[i].request("STATUS")["position"] for i in (0, 2, 3)]
    rejoin = others[1].request("JOIN", workstation=13, name="S", problem="again")
    lab.close()
    r = {"duplicate_attempts": 10, "duplicates_rejected": rejected, "second_join_same_connection": same_conn,
         "positions_before_cancel_ws12_13_14_15": before, "positions_after_cancel_ws12_14_15": after,
         "rejoin_after_cancel_ticket": rejoin["ticket"], "rejoin_position": rejoin["position"]}
    results["E4"] = r
    say("    %d/10 duplicates rejected; same-connection second JOIN -> %s" % (rejected, same_conn))
    say("    positions %s -> after WS13 cancels %s; WS13 may rejoin (ticket #%d, position %d)"
        % (before, after, rejoin["ticket"], rejoin["position"]))


def e5_lifecycle():
    say("E5  Ticket lifecycle, logging and server-restart recovery")
    lab = Lab()
    inst = lab.instructor()
    toks = {}
    for ws in (21, 22, 23, 24, 25):
        r = lab.client().request("JOIN", workstation=ws, name="S%d" % ws, problem="p")
        toks[r["ticket"]] = r["token"]
    for _ in range(3):
        t = inst.request("NEXT")
        inst.request("DONE", ticket=t["ticket"])
    inst.request("NEXT")                                           # ticket 4 CALLED
    lab.clients[-1].request("CANCEL")                              # ticket 5 CANCELLED
    before = {n: t.status for n, t in lab.mgr.tickets.items()}
    log_path = os.path.join(lab.data_dir, "session.jsonl")
    with open(log_path, encoding="utf-8") as fh:
        events = [json.loads(x) for x in fh]
    shutil.copy(log_path, os.path.join(OUT, "session_E5.jsonl"))
    counts = {}
    for e in events:
        counts[e["event"]] = counts.get(e["event"], 0) + 1
    all_ts = all("ts" in e and "time" in e for e in events)
    lab.stop()
    t0 = time.perf_counter()
    lab.start()                                                    # restart on the same data folder
    recovery = time.perf_counter() - t0
    after = {n: t.status for n, t in lab.mgr.tickets.items()}
    c = lab.client()
    res = c.request("RESUME", ticket=4, token=toks[4])
    new = lab.client().request("JOIN", workstation=26, name="S26", problem="p")
    lab.close()
    r = {"events_by_type": counts, "every_event_timestamped": all_ts, "statuses_before_restart": before,
         "statuses_after_restart": after, "state_identical": before == after,
         "restart_recovery_ms": round(recovery * 1000, 2), "resumed_ticket_status": res["status"],
         "next_ticket_after_restart": new["ticket"]}
    results["E5"] = r
    say("    events %s, all timestamped: %s" % (counts, all_ts))
    say("    restart: state identical %s, recovered in %.1f ms, ticket #4 resumed as %s, next ticket #%d"
        % (before == after, recovery * 1000, res["status"], new["ticket"]))


def e6_faults():
    say("E6  Fault handling and client reconnection")
    lab = Lab()
    c = lab.client()
    cases = [("plain text 'hello'", b"hello\n"), ("unknown type FLY", b'{"type":"FLY","id":1}\n'),
             ("JSON array", b"[1,2,3]\n"), ("invalid UTF-8", b"\xff\xfe\n"),
             ("5000-byte line", b"A" * 5000 + b"\n"), ("missing field", b'{"type":"JOIN","id":2,"workstation":11}\n'),
             ("workstation out of range", b'{"type":"JOIN","id":3,"workstation":0,"name":"a","problem":"b"}\n'),
             ("id is a string", b'{"type":"PING","id":"x"}\n')]
    table = []
    for label, raw in cases:
        n0 = len(c.pushes)
        replies = {}
        if b'"id":' in raw and b'"id":"' not in raw:
            rid = json.loads(raw)["id"]
            slot = [threading.Event(), None]
            c._pending[rid] = slot                       # catch the reply for this raw request
            c._chan.send(raw)
            slot[0].wait(3)
            code = slot[1]["code"] if slot[1] else None
        else:
            c._chan.send(raw)
            wait_for(lambda: len(c.pushes) > n0, 3)
            code = c.pushes[-1].get("code") if len(c.pushes) > n0 else None
        table.append({"input": label, "error_code": code})
    alive = c.request("PING")["type"] == "PONG"
    # abrupt disconnect + automatic reconnection
    other = lab.client()
    other.request("JOIN", workstation=31, name="B", problem="p")
    st = {}

    def on_rec():
        st["resumed"] = holder.request("RESUME", ticket=st["ticket"], token=st["token"])
        st["t"] = time.perf_counter()

    holder = lab.client(reconnect=True, on_reconnect=on_rec)
    q = holder.request("JOIN", workstation=32, name="A", problem="p")
    st.update(ticket=q["ticket"], token=q["token"])
    with lab.server.lock:
        victims = [s for s in lab.server.sessions if s.ticket == q["ticket"]]
    t0 = time.perf_counter()
    for s in victims:
        s.chan.close("simulated failure", flush=False)
    served_meanwhile = other.request("STATUS")["status"] == "WAITING"
    wait_for(lambda: "t" in st, 10)
    total = lab.mgr.summary()["total_requests"]
    lab.close()
    r = {"invalid_inputs": table, "connection_alive_after_invalid_input": alive,
         "server_served_other_client_during_drop": served_meanwhile,
         "reconnect_and_resume_seconds": round(st.get("t", float("nan")) - t0, 3),
         "resumed_status": st.get("resumed", {}).get("status"), "duplicate_tickets_created": total - 2}
    results["E6"] = r
    for row in table:
        say("    %-26s -> %s" % (row["input"], row["error_code"]))
    say("    still usable afterwards: %s; reconnect+resume in %.2f s; duplicate tickets: %d"
        % (alive, r["reconnect_and_resume_seconds"], r["duplicate_tickets_created"]))


def e7_auth():
    say("E7  Instructor authentication")
    lab = Lab()
    rows = []

    def attempt(label, fn):
        try:
            fn()
            rows.append({"case": label, "result": "OK"})
        except ServerError as e:
            rows.append({"case": label, "result": e.code})

    c = lab.client()
    attempt("LIST before login", lambda: c.request("LIST"))
    attempt("NEXT before login", lambda: c.request("NEXT"))
    attempt("LOGIN wrong password", lambda: c.request("LOGIN", username=PROF, password="wrong-pass"))
    attempt("LOGIN unknown user", lambda: c.request("LOGIN", username="nobody", password=PASSWORD))
    t0 = time.perf_counter()
    attempt("LOGIN correct", lambda: c.request("LOGIN", username=PROF, password=PASSWORD))
    login_ms = (time.perf_counter() - t0) * 1000
    attempt("LIST after login", lambda: c.request("LIST"))
    s = lab.client()
    s.request("JOIN", workstation=41, name="S", problem="p")
    attempt("Student connection sends LIST", lambda: s.request("LIST"))
    attempt("Student connection sends LOGIN", lambda: s.request("LOGIN", username=PROF, password=PASSWORD))
    attempt("NEXT after login", lambda: c.request("NEXT"))
    attempt("DONE after login", lambda: c.request("DONE", ticket=1))
    b = lab.client()
    codes = []
    for _ in range(P.MAX_LOGIN_ATTEMPTS):
        try:
            b.request("LOGIN", username=PROF, password="nope-nope")
        except ServerError as e:
            codes.append(e.code)
    closed = b._chan.closed.wait(3)
    lab.close()
    r = {"cases": rows, "five_failures_codes": codes, "closed_after_limit": closed, "valid_login_ms": round(login_ms, 1)}
    results["E7"] = r
    for row in rows:
        say("    %-32s -> %s" % (row["case"], row["result"]))
    say("    5 wrong passwords -> %s, connection closed: %s; valid login took %.0f ms" % (codes, closed, login_ms))


def e8_tls(certdir):
    say("E8  TLS communication")
    good = client_context(os.path.join(certdir, "ok", "ca.crt"))
    sctx = server_context(os.path.join(certdir, "ok", "server.crt"), os.path.join(certdir, "ok", "server.key"))
    lab = Lab(ssl_ctx=sctx)
    c = lab.client(good)
    info = c.tls_info
    sock_version = info.split()[0]
    cipher = info.split()[1]
    c.request("JOIN", workstation=11, name="Ana", problem="IDE will not start")
    # invalid configurations
    invalid = []
    for label, ctx in (("untrusted CA", client_context(os.path.join(certdir, "rogue", "ca.crt"))),
                       ("no TLS on client", None)):
        try:
            lab.client(ctx)
            invalid.append({"setup": label, "rejected": False, "error": ""})
        except (ssl.SSLError, OSError, ConnectionLost) as e:
            invalid.append({"setup": label, "rejected": True, "error": str(e)[:110]})
    lab.close()
    wsc = server_context(os.path.join(certdir, "wrongip", "server.crt"), os.path.join(certdir, "wrongip", "server.key"))
    lab = Lab(ssl_ctx=wsc)
    try:
        lab.client(client_context(os.path.join(certdir, "wrongip", "ca.crt")))
        invalid.append({"setup": "certificate for wrong IP", "rejected": False, "error": ""})
    except (ssl.SSLError, OSError, ConnectionLost) as e:
        invalid.append({"setup": "certificate for wrong IP", "rejected": True, "error": str(e)[:110]})
    lab.close()
    # wire comparison: identical exchange, plain vs TLS
    wire = {}
    for mode in ("plain", "tls"):
        lab = Lab(ssl_ctx=sctx if mode == "tls" else None)
        tap = TapProxy("127.0.0.1", lab.port).start()
        x = ClientConnection("127.0.0.1", tap.port, good if mode == "tls" else None, heartbeat=0, auto_reconnect=False)
        x.connect()
        x.request("JOIN", workstation=11, name="Ana", problem="SECRET-PROBLEM-TEXT")
        x.request("STATUS")
        x.close()
        time.sleep(0.3)
        up, down = bytes(tap.client_to_server), bytes(tap.server_to_client)
        tap.close()
        lab.close()
        wire[mode] = {"bytes_total": len(up) + len(down), "plaintext_found": b"SECRET-PROBLEM-TEXT" in up,
                      "first_byte_hex": "%02x" % up[0]}
        if mode == "tls":
            with open(os.path.join(OUT, "wire_tls.txt"), "w", encoding="ascii") as fh:
                fh.write("first 256 bytes client->server (hex):\n" + up[:256].hex(" ", 16) + "\n")
    # handshake cost: connect -> WELCOME, plain vs TLS
    times = {}
    for mode in ("plain", "tls"):
        lab = Lab(ssl_ctx=sctx if mode == "tls" else None)
        t = []
        for _ in range(30):
            t0 = time.perf_counter()
            x = ClientConnection("127.0.0.1", lab.port, good if mode == "tls" else None, heartbeat=0, auto_reconnect=False)
            x.connect()
            t.append(time.perf_counter() - t0)
            x.close()
        lab.close()
        times[mode] = ms(stats(t))
    r = {"version": sock_version, "cipher": cipher, "invalid_setups": invalid, "wire": wire,
         "connect_to_welcome_ms": times,
         "byte_overhead_percent": round(100.0 * (wire["tls"]["bytes_total"] - wire["plain"]["bytes_total"])
                                        / wire["plain"]["bytes_total"], 1)}
    results["E8"] = r
    say("    connected with %s %s" % (sock_version, cipher))
    for row in invalid:
        say("    %-26s rejected: %s (%s)" % (row["setup"], row["rejected"], row["error"][:70]))
    say("    wire: plain %d B (plaintext visible: %s) vs TLS %d B (plaintext visible: %s, first byte 0x%s) -> +%.1f%%"
        % (wire["plain"]["bytes_total"], wire["plain"]["plaintext_found"], wire["tls"]["bytes_total"],
           wire["tls"]["plaintext_found"], wire["tls"]["first_byte_hex"], r["byte_overhead_percent"]))
    say("    connect->WELCOME: plain %.2f ms, TLS %.2f ms (mean of 30)" % (times["plain"]["mean"], times["tls"]["mean"]))


def e9_summary():
    say("E9  Session summary and waiting time")
    lab = Lab()
    inst = lab.instructor()
    join_wall = {}
    for ws in (51, 52, 53, 54, 55):
        r = lab.client().request("JOIN", workstation=ws, name="S", problem="p")
        join_wall[r["ticket"]] = time.time()
    lab.clients[-1].request("CANCEL")                 # ticket 5 cancelled -> excluded
    stopwatch = {}
    for gap in (1.0, 1.0, 1.0):                       # call tickets 1-3 at ~1 s intervals
        time.sleep(gap)
        t = inst.request("NEXT")
        stopwatch[t["ticket"]] = time.time() - join_wall[t["ticket"]]
        inst.request("DONE", ticket=t["ticket"])
    s = inst.request("SUMMARY")["summary"]
    log_path = os.path.join(lab.data_dir, "session.jsonl")
    with open(log_path, encoding="utf-8") as fh:
        ev = [json.loads(x) for x in fh]
    joined = {e["ticket"]: e["ts"] for e in ev if e["event"] == "JOIN"}
    called = {e["ticket"]: e["ts"] for e in ev if e["event"] == "CALL"}
    per = {n: called[n] - joined[n] for n in called}
    hand_avg = sum(per.values()) / len(per)
    lab.close()
    r = {"total_requests_reported": s["total_requests"], "submitted": 5, "cancelled": s["cancelled"],
         "waiting": s["waiting"], "per_ticket_wait_from_log_s": {k: round(v, 3) for k, v in per.items()},
         "per_ticket_wait_stopwatch_s": {k: round(v, 3) for k, v in stopwatch.items()},
         "avg_wait_server_s": s["avg_wait_seconds"], "avg_wait_hand_from_log_s": round(hand_avg, 3),
         "abs_difference_s": round(abs(s["avg_wait_seconds"] - hand_avg), 3),
         "max_stopwatch_vs_log_diff_ms": round(max(abs(stopwatch[n] - per[n]) for n in per) * 1000, 2)}
    results["E9"] = r
    say("    total requests %d (submitted 5), cancelled %d, waiting %d" % (s["total_requests"], s["cancelled"], s["waiting"]))
    say("    waits from log %s s; server avg %.1f s vs hand-computed %.3f s"
        % (r["per_ticket_wait_from_log_s"], s["avg_wait_seconds"], hand_avg))


def e10_performance(certdir):
    say("E10 Response time and load (supplementary)")
    good = client_context(os.path.join(certdir, "ok", "ca.crt"))
    sctx = server_context(os.path.join(certdir, "ok", "server.crt"), os.path.join(certdir, "ok", "server.key"))
    rtt = {}
    for mode in ("plain", "tls"):
        lab = Lab(ssl_ctx=sctx if mode == "tls" else None)
        c = lab.client(good if mode == "tls" else None)
        for _ in range(50):
            c.request("PING")
        t = []
        for _ in range(500):
            t0 = time.perf_counter()
            c.request("PING")
            t.append(time.perf_counter() - t0)
        lab.close()
        rtt[mode] = ms(stats(t))
    lab = Lab()
    n = 60
    conns = [lab.client() for _ in range(n)]
    errors = []
    barrier = threading.Barrier(n)
    lat = []

    def go(i):
        barrier.wait()
        t0 = time.perf_counter()
        try:
            conns[i].request("JOIN", workstation=200 + i, name="L%d" % i, problem="load")
            lat.append(time.perf_counter() - t0)
        except Exception as e:                       # noqa: BLE001
            errors.append(str(e))

    th = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    total = lab.mgr.summary()["total_requests"]
    lab.close()
    r = {"ping_rtt_ms": rtt, "load": {"clients": n, "errors": len(errors), "tickets_created": total,
                                      "join_latency_ms": ms(stats(lat))}}
    results["E10"] = r
    say("    PING round trip: plain mean %.3f ms (p95 %.3f), TLS mean %.3f ms (p95 %.3f), 500 samples each"
        % (rtt["plain"]["mean"], rtt["plain"]["p95"], rtt["tls"]["mean"], rtt["tls"]["p95"]))
    say("    %d simultaneous JOINs: %d errors, %d tickets, mean latency %.2f ms (max %.2f)"
        % (n, len(errors), total, r["load"]["join_latency_ms"]["mean"], r["load"]["join_latency_ms"]["max"]))


def charts():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        say("(matplotlib not installed: charts skipped)")
        return
    plt.rcParams.update({"font.family": "Segoe UI", "font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False})
    green, blue = "#00703C", "#2B6CB0"
    e8, e10, e9 = results["E8"], results["E10"], results["E9"]
    fig, ax = plt.subplots(figsize=(6.2, 3.0), dpi=200)
    labels = ["Connect → WELCOME", "PING round trip"]
    plain = [e8["connect_to_welcome_ms"]["plain"]["mean"], e10["ping_rtt_ms"]["plain"]["mean"]]
    tls = [e8["connect_to_welcome_ms"]["tls"]["mean"], e10["ping_rtt_ms"]["tls"]["mean"]]
    x = range(len(labels))
    b1 = ax.bar([i - 0.18 for i in x], plain, 0.36, label="Plain TCP", color=blue)
    b2 = ax.bar([i + 0.18 for i in x], tls, 0.36, label="TLS", color=green)
    for bars in (b1, b2):
        for b in bars:
            ax.annotate("%.2f ms" % b.get_height(), (b.get_x() + b.get_width() / 2, b.get_height()),
                        ha="center", va="bottom", fontsize=8, xytext=(0, 2), textcoords="offset points")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel("Mean time (ms)")
    ax.set_title("Plain TCP vs TLS on one host (loopback)", fontsize=11)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_tls_cost.png"))
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.2, 3.0), dpi=200)
    tickets = sorted(int(k) for k in e9["per_ticket_wait_from_log_s"])
    logw = [e9["per_ticket_wait_from_log_s"][k] for k in tickets]
    stop = [e9["per_ticket_wait_stopwatch_s"][k] for k in tickets]
    ax.plot(tickets, logw, "o-", color=green, label="From server log (called − submitted)")
    ax.plot(tickets, stop, "x--", color=blue, label="Independent client stopwatch")
    ax.axhline(e9["avg_wait_server_s"], color="#C05621", lw=1, ls=":", label="Server SUMMARY average")
    ax.set_xticks(tickets)
    ax.set_xlabel("Ticket number")
    ax.set_ylabel("Waiting time (s)")
    ax.set_title("Waiting time: log vs stopwatch vs summary", fontsize=11)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_waiting_time.png"))
    plt.close(fig)
    say("charts written: fig_tls_cost.png, fig_waiting_time.png")


def main():
    os.makedirs(OUT, exist_ok=True)
    h = logging.FileHandler(os.path.join(OUT, "server.log"), mode="w", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logging.getLogger("zephyq").addHandler(h)
    logging.getLogger("zephyq").setLevel(logging.INFO)
    results["environment"] = {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "python": platform.python_version(),
                              "os": platform.platform(), "machine": platform.processor() or platform.machine(),
                              "openssl": ssl.OPENSSL_VERSION, "network": "single host, 127.0.0.1 loopback"}
    say("ZephyQ experiments - %s - Python %s - %s" % (results["environment"]["date"], platform.python_version(),
                                                     platform.platform()))
    say()
    certdir = tempfile.mkdtemp(prefix="zephyq-exp-certs-")
    try:
        make_certs.write_all(os.path.join(certdir, "ok"), ["127.0.0.1"], ["localhost"])
        make_certs.write_all(os.path.join(certdir, "rogue"), ["127.0.0.1"], ["localhost"])
        make_certs.write_all(os.path.join(certdir, "wrongip"), ["10.9.9.9"], [])
        for fn in (e1_connectivity, e2_tcp_communication, e3_fcfs, e4_duplicates, e5_lifecycle, e6_faults,
                   e7_auth, lambda: e8_tls(certdir), e9_summary, lambda: e10_performance(certdir)):
            fn()
            say()
        charts()
    finally:
        shutil.rmtree(certdir, ignore_errors=True)
    with open(os.path.join(OUT, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    with open(os.path.join(OUT, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    say("results written to %s" % OUT)


if __name__ == "__main__":
    main()
