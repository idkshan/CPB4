"""Phase 2 - automatic position updates, reconnection, server-restart recovery and TLS.

These build on the Phase 1 features and are tested only after those pass.
"""
import os
import shutil
import ssl
import tempfile
import time
import unittest

from helpers import ServerFixture, ServerTestCase, TestClient
from zephyq.client import ConnectionLost, ServerError
from zephyq.tlsutil import client_context, server_context

import make_certs   # tools/make_certs.py (pure-stdlib certificate generator)
from tap_proxy import TapProxy


class AutomaticUpdateTests(ServerTestCase):
    def test_cancel_pushes_positions_and_queue(self):
        inst = self.instructor()
        a, b, c = self.student(11), self.student(12), self.student(13)
        while inst.push("QUEUE")["waiting_count"] != 3:     # updates from the three JOINs
            pass
        a.req("CANCEL")
        self.assertEqual(b.push("POSITION")["position"], 1)
        self.assertEqual(c.push("POSITION")["position"], 2)
        q = inst.push("QUEUE")
        self.assertEqual([t["ticket"] for t in q["tickets"]], [2, 3])
        self.assertEqual(q["waiting_count"], 2)

    def test_next_pushes_called_and_positions(self):
        inst = self.instructor()
        a = self.student(11)
        b = self.student(12)
        inst.req("NEXT")
        self.assertEqual(a.push("CALLED")["workstation"], 11)
        self.assertEqual(b.push("POSITION")["position"], 1)

    def test_join_behind_does_not_spam_positions(self):
        a = self.student(11)
        self.student(12)
        self.student(13)
        time.sleep(0.3)
        self.assertEqual([m for m in a.drain() if m["type"] == "POSITION"], [],
                         "position only changes when someone ahead leaves")


class ReconnectTests(ServerTestCase):
    def test_resume_on_new_connection_no_duplicate(self):
        inst = self.instructor()
        a = self.student(11)
        token = a.queued["token"]
        a.close()
        # a brand-new connection resumes the same ticket with its token
        a2 = self.client()
        r = a2.req("RESUME", ticket=1, token=token)
        self.assertEqual((r["ticket"], r["status"], r["position"]), (1, "WAITING", 1))
        self.assertEqual(inst.req("SUMMARY")["summary"]["total_requests"], 1, "no duplicate ticket")
        # pushes now reach the new connection
        inst.req("NEXT")
        self.assertEqual(a2.push("CALLED")["ticket"], 1)

    def test_resume_with_wrong_token_rejected(self):
        self.student(11)
        with self.assertRaises(ServerError) as cm:
            self.client().req("RESUME", ticket=1, token="guess")
        self.assertEqual(cm.exception.code, "NOT_FOUND")

    def test_automatic_reconnect_after_connection_drop(self):
        holder = {}

        def on_reconnect():
            holder["resumed"] = holder["c"].req("RESUME", ticket=1, token=holder["token"])

        c = self.client(reconnect=True, on_reconnect=on_reconnect)
        holder["c"] = c
        holder["token"] = c.req("JOIN", workstation=11, name="Ana", problem="p")["token"]
        # the server side drops every connection (e.g. a network glitch)
        with self.fx.server.lock:
            sessions = list(self.fx.server.sessions)
        for s in sessions:
            s.chan.close("simulated network failure", flush=False)
        deadline = time.monotonic() + 8
        while "resumed" not in holder and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("resumed", holder, "client should reconnect and resume")
        self.assertEqual(holder["resumed"]["ticket"], 1)
        self.assertEqual(c.req("STATUS")["status"], "WAITING")
        inst = self.instructor()
        self.assertEqual(inst.req("SUMMARY")["summary"]["total_requests"], 1)

    def test_server_restart_recovers_queue(self):
        inst = self.instructor()
        a, b, c = self.student(11), self.student(12), self.student(13)
        inst.req("NEXT")                              # #1 CALLED
        c.req("CANCEL")                               # #3 CANCELLED
        token_b = b.queued["token"]
        self.fx.stop()                                # server goes down
        for x in (inst, a, b, c):
            x.conn._chan.closed.wait(3)

        self.fx.start()                               # same data dir, new process state
        b2 = self.client()
        r = b2.req("RESUME", ticket=2, token=token_b)
        self.assertEqual((r["status"], r["position"]), ("WAITING", 1))
        inst2 = self.instructor()
        tickets = inst2.req("LIST")["tickets"]
        self.assertEqual([(t["ticket"], t["status"]) for t in tickets], [(1, "CALLED"), (2, "WAITING")])
        with self.assertRaises(ServerError):          # duplicate prevention after restart
            self.client().req("JOIN", workstation=12, name="B", problem="again")
        d = self.student(14)
        self.assertEqual(d.queued["ticket"], 4, "ticket numbering continues after restart")
        inst2.req("DONE", ticket=1)
        self.assertEqual(inst2.req("SUMMARY")["summary"]["total_requests"], 4)

    def test_server_shutdown_sends_bye_with_reconnect(self):
        c = self.client()
        self.fx.stop()
        self.assertIs(c.push("BYE")["reconnect"], True)


class TLSTests(unittest.TestCase):
    """Uses certificates generated on the fly by tools/make_certs.py."""

    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.mkdtemp(prefix="zephyq-certs-")
        cls.rogue = os.path.join(cls.certs, "rogue")
        cls.wrong_ip = os.path.join(cls.certs, "wrong-ip")
        make_certs.write_all(cls.certs, ["127.0.0.1"], ["localhost"])
        make_certs.write_all(cls.rogue, ["127.0.0.1"], ["localhost"])       # a different, untrusted CA
        make_certs.write_all(cls.wrong_ip, ["10.9.9.9"], [])                # trusted shape, wrong address

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certs, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zephyq-tls-")
        from zephyq.auth import CredentialStore
        creds = os.path.join(self.tmp, "instructors.json")
        CredentialStore(creds).set_password("prof", "correct-horse-42")
        ctx = server_context(os.path.join(self.certs, "server.crt"), os.path.join(self.certs, "server.key"))
        self.fx = ServerFixture(os.path.join(self.tmp, "data"), creds, ctx).start()
        self.good = client_context(os.path.join(self.certs, "ca.crt"))
        self.to_close = []

    def tearDown(self):
        for c in self.to_close:
            c.close()
        self.fx.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tls_connection_works(self):
        c = TestClient(self.fx.port, self.good)
        self.to_close.append(c)
        self.assertTrue(c.conn.tls_info.startswith("TLSv1."), c.conn.tls_info)
        self.assertTrue(c.conn.welcome["tls"])
        r = c.req("JOIN", workstation=11, name="Ana", problem="over TLS")
        self.assertEqual(r["ticket"], 1)

    def test_untrusted_ca_rejected(self):
        bad = client_context(os.path.join(self.rogue, "ca.crt"))
        with self.assertRaises(ssl.SSLCertVerificationError):
            TestClient(self.fx.port, bad)

    def test_certificate_for_wrong_address_rejected(self):
        self.fx.stop()
        ctx = server_context(os.path.join(self.wrong_ip, "server.crt"), os.path.join(self.wrong_ip, "server.key"))
        self.fx.ssl_context = ctx
        self.fx.start()
        trusting = client_context(os.path.join(self.wrong_ip, "ca.crt"))
        with self.assertRaises(ssl.SSLCertVerificationError) as cm:
            TestClient(self.fx.port, trusting)
        self.assertIn("IP address mismatch", str(cm.exception))

    def test_plain_client_cannot_talk_to_tls_server(self):
        with self.assertRaises((ConnectionLost, OSError)):
            TestClient(self.fx.port, None)

    def test_wire_bytes_are_encrypted(self):
        """Controlled substitute for a Wireshark capture: record every byte on the wire."""
        tap = TapProxy("127.0.0.1", self.fx.port).start()
        try:
            c = TestClient(tap.port, self.good)
            c.req("JOIN", workstation=11, name="Ana", problem="SECRET-PROBLEM-TEXT")
            c.close()
            time.sleep(0.2)
            wire = bytes(tap.client_to_server) + bytes(tap.server_to_client)
            self.assertNotIn(b"SECRET-PROBLEM-TEXT", wire)
            self.assertNotIn(b"JOIN", wire)
            self.assertEqual(tap.client_to_server[0], 0x16, "starts with a TLS handshake record")
        finally:
            tap.close()


class PlainWireTest(ServerTestCase):
    def test_plain_tcp_is_readable_on_the_wire(self):
        """Contrast with TLSTests.test_wire_bytes_are_encrypted."""
        tap = TapProxy("127.0.0.1", self.fx.port).start()
        try:
            c = TestClient(tap.port)
            c.req("JOIN", workstation=11, name="Ana", problem="SECRET-PROBLEM-TEXT")
            c.close()
            time.sleep(0.2)
            self.assertIn(b"SECRET-PROBLEM-TEXT", bytes(tap.client_to_server))
            self.assertIn(b'"type":"QUEUED"', bytes(tap.server_to_client))
        finally:
            tap.close()


if __name__ == "__main__":
    unittest.main()
