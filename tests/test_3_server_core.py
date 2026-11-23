"""Phase 1 - basic operations over real TCP connections (plain TCP, localhost)."""
import json
import time
import unittest

from helpers import PASSWORD, PROF, ServerTestCase
from zephyq import protocol as P
from zephyq.client import ConnectionLost, ServerError


class ServerCoreTests(ServerTestCase):
    def assertErr(self, code, fn, *args, **kw):
        with self.assertRaises(ServerError) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        return cm.exception

    # Basics
    def test_welcome_and_ping(self):
        c = self.client()
        self.assertEqual(c.conn.welcome["type"], "WELCOME")
        self.assertEqual(c.conn.welcome["proto"], P.PROTOCOL_VERSION)
        self.assertFalse(c.conn.welcome["tls"])
        self.assertEqual(c.conn.welcome["max_line"], P.MAX_CLIENT_LINE)
        r = c.req("PING")
        self.assertEqual(r["type"], "PONG")
        self.assertIn("re", r, "every reply echoes the request id")

    def test_join_reply(self):
        s = self.student(11, "Ana", "pip install fails")
        q = s.queued
        self.assertEqual(q["type"], "QUEUED")
        self.assertEqual((q["ticket"], q["position"], q["waiting_count"]), (1, 1, 1))
        self.assertEqual(q["status"], "WAITING")
        self.assertEqual((q["workstation"], q["name"], q["problem"]), (11, "Ana", "pip install fails"))
        self.assertTrue(q["submitted_at"])
        self.assertTrue(q["token"])

    def test_fcfs_across_connections(self):
        inst = self.instructor()
        a, b, c = self.student(11), self.student(12), self.student(13)
        self.assertEqual([x.queued["ticket"] for x in (a, b, c)], [1, 2, 3])
        order = []
        for student in (a, b, c):
            r = inst.req("NEXT")
            order.append(r["workstation"])
            called = student.push("CALLED")
            self.assertEqual(called["ticket"], r["ticket"])
            inst.req("DONE", ticket=r["ticket"])
            student.push("RESOLVED")
        self.assertEqual(order, [11, 12, 13])
        self.assertErr("QUEUE_EMPTY", inst.req, "NEXT")

    def test_status(self):
        self.student(11)
        b = self.student(12)
        r = b.req("STATUS")
        self.assertEqual((r["ticket"], r["position"], r["waiting_count"]), (2, 2, 2))
        self.assertErr("NO_TICKET", self.client().req, "STATUS")

    # Duplicates
    def test_duplicate_workstation_rejected(self):
        self.student(11)
        other = self.client()
        e = self.assertErr("DUPLICATE", other.req, "JOIN", workstation=11, name="X", problem="p")
        self.assertIn("#1", e.message)

    def test_one_ticket_per_connection(self):
        a = self.student(11)
        self.assertErr("ALREADY_HAS_TICKET", a.req, "JOIN", workstation=12, name="X", problem="p")

    # Cancel
    def test_cancel_updates_positions(self):
        inst = self.instructor()
        a, b, c = self.student(11), self.student(12), self.student(13)
        r = a.req("CANCEL")
        self.assertEqual(r["status"], "CANCELLED")
        self.assertEqual(b.req("STATUS")["position"], 1)
        self.assertEqual(c.req("STATUS")["position"], 2)
        self.assertEqual([t["ticket"] for t in inst.req("LIST")["tickets"]], [2, 3])
        self.assertErr("INVALID_STATE", a.req, "CANCEL")
        # the freed workstation may join again
        a.req("JOIN", workstation=11, name="Ana", problem="again")

    def test_done_requires_called(self):
        inst = self.instructor()
        self.student(11)
        self.assertErr("INVALID_STATE", inst.req, "DONE", ticket=1)
        self.assertErr("NOT_FOUND", inst.req, "DONE", ticket=42)
        self.assertErr("MISSING_FIELD", inst.req, "DONE")

    # Authentication
    def test_instructor_commands_need_login(self):
        c = self.client()
        for cmd in ("LIST", "NEXT", "SUMMARY"):
            self.assertErr("AUTH_REQUIRED", c.req, cmd)
        self.assertErr("AUTH_FAILED", c.req, "LOGIN", username=PROF, password="wrong-password")
        self.assertErr("AUTH_FAILED", c.req, "LOGIN", username="nobody", password=PASSWORD)
        self.assertEqual(c.req("LOGIN", username=PROF, password=PASSWORD)["username"], PROF)
        self.assertEqual(c.req("LIST")["tickets"], [])

    def test_login_rate_limit_disconnects(self):
        c = self.client()
        for _ in range(P.MAX_LOGIN_ATTEMPTS - 1):
            self.assertErr("AUTH_FAILED", c.req, "LOGIN", username=PROF, password="nope-nope")
        self.assertErr("RATE_LIMITED", c.req, "LOGIN", username=PROF, password="nope-nope")
        self.assertTrue(c.conn._chan.closed.wait(3), "server must close the connection")

    def test_roles_are_separate(self):
        s = self.student(11)
        self.assertErr("WRONG_ROLE", s.req, "LOGIN", username=PROF, password=PASSWORD)
        self.assertErr("WRONG_ROLE", s.req, "LIST")
        inst = self.instructor()
        self.assertErr("WRONG_ROLE", inst.req, "JOIN", workstation=12, name="x", problem="y")
        self.assertErr("WRONG_ROLE", inst.req, "CANCEL")

    def test_second_instructor_login_replaces_first(self):
        first = self.instructor()
        self.instructor()
        bye = first.push("BYE")
        self.assertIs(bye["reconnect"], False)
        self.assertTrue(first.conn._chan.closed.wait(3))

    # Invalid input
    def test_invalid_messages_get_errors_and_connection_survives(self):
        c = self.client()
        self.assertErr("UNKNOWN_TYPE", c.req, "FLY")
        self.assertErr("MISSING_FIELD", c.req, "JOIN", workstation=11, name="x")
        self.assertErr("INVALID_FIELD", c.req, "JOIN", workstation=0, name="x", problem="y")
        self.assertErr("INVALID_FIELD", c.req, "JOIN", workstation="11", name="x", problem="y")
        self.assertErr("INVALID_FIELD", c.req, "JOIN", workstation=11, name="x" * 51, problem="y")
        self.assertErr("INVALID_FIELD", c.req, "JOIN", workstation=11, name="x", problem="y" * 201)
        c.raw(b"this is not json\n")
        self.assertEqual(c.push("ERR")["code"], "BAD_FRAME")
        c.raw(b'{"type":"JOIN","id":"abc"}\n')
        self.assertEqual(c.push("ERR")["code"], "INVALID_FIELD")
        c.raw(b"X" * (P.MAX_CLIENT_LINE + 100) + b"\n")
        self.assertEqual(c.push("ERR")["code"], "TOO_LONG")
        c.raw(b"\xff\xfe\n")
        self.assertEqual(c.push("ERR")["code"], "BAD_FRAME")
        self.assertEqual(c.req("PING")["type"], "PONG", "still usable after bad input")

    def test_abrupt_disconnect_keeps_ticket_and_server(self):
        a = self.student(11)
        a.conn._chan.close("simulated crash", flush=False)
        a.conn._chan.closed.wait(3)
        b = self.student(12)
        self.assertEqual(b.queued["position"], 2, "ticket #1 is still in the queue")

    # Quit + summary
    def test_quit_gets_bye_and_close(self):
        c = self.client()
        r = c.req("QUIT")
        self.assertEqual(r["type"], "BYE")
        self.assertTrue(c.conn._chan.closed.wait(3))
        with self.assertRaises(ConnectionLost):
            c.req("PING")

    def test_summary_matches_log_timestamps(self):
        inst = self.instructor()
        for ws in (11, 12, 13):
            self.student(ws)
            time.sleep(0.05)
        for _ in range(2):
            t = inst.req("NEXT")
            inst.req("DONE", ticket=t["ticket"])
        s = inst.req("SUMMARY")["summary"]
        self.assertEqual(s["total_requests"], 3)
        self.assertEqual((s["resolved"], s["waiting"], s["called_total"]), (2, 1, 2))
        with open(self.fx.log_path, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh]
        joined = {e["ticket"]: e["ts"] for e in events if e["event"] == "JOIN"}
        called = {e["ticket"]: e["ts"] for e in events if e["event"] == "CALL"}
        expected = sum(called[n] - joined[n] for n in called) / len(called)
        self.assertAlmostEqual(s["avg_wait_seconds"], round(expected, 1), places=1)
        self.assertEqual([e["event"] for e in events].count("RESOLVE"), 2)


if __name__ == "__main__":
    unittest.main()
