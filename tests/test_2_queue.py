"""Phase 1 - queue logic: FCFS order, duplicates, timestamps, waiting time, recovery."""
import json
import os
import shutil
import tempfile
import threading
import unittest

import helpers  # noqa: F401
from zephyq.queue_core import CALLED, CANCELLED, RESOLVED, WAITING, QueueError, QueueManager
from zephyq.storage import EventLog


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zephyq-q-")
        self.path = os.path.join(self.tmp, "session.jsonl")
        self.clock = FakeClock()
        self.q = self.make()

    def tearDown(self):
        self.q.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self) -> QueueManager:
        q = QueueManager(EventLog(self.path), clock=self.clock)
        q.load()
        return q

    def test_fcfs_order_and_positions(self):
        a, _ = self.q.join(11, "Ana", "p")
        b, _ = self.q.join(12, "Ben", "p")
        c, _ = self.q.join(13, "Cy", "p")
        self.assertEqual([a.number, b.number, c.number], [1, 2, 3])
        self.assertEqual(self.q.positions(), {1: 1, 2: 2, 3: 3})
        self.assertEqual(self.q.call_next().number, 1)
        self.assertEqual(self.q.positions(), {2: 1, 3: 2})
        self.assertEqual(self.q.call_next().number, 2)
        self.assertEqual(self.q.call_next().number, 3)
        with self.assertRaises(QueueError) as cm:
            self.q.call_next()
        self.assertEqual(cm.exception.code, "QUEUE_EMPTY")

    def test_duplicate_workstation_rejected_until_closed(self):
        t, _ = self.q.join(11, "Ana", "p")
        with self.assertRaises(QueueError) as cm:
            self.q.join(11, "Someone else", "p")
        self.assertEqual(cm.exception.code, "DUPLICATE")
        self.q.call_next()
        with self.assertRaises(QueueError):            # CALLED is still active
            self.q.join(11, "Ana", "again")
        self.q.resolve(t.number)
        t2, _ = self.q.join(11, "Ana", "new problem")  # allowed after resolve
        self.assertEqual(t2.number, 2)
        self.q.cancel(t2.number)
        self.q.join(11, "Ana", "third")                # allowed after cancel

    def test_cancel_updates_positions(self):
        self.q.join(11, "A", "p")
        b, _ = self.q.join(12, "B", "p")
        self.q.join(13, "C", "p")
        self.q.cancel(b.number)
        self.assertEqual(self.q.positions(), {1: 1, 3: 2})
        self.assertEqual(b.status, CANCELLED)
        with self.assertRaises(QueueError) as cm:
            self.q.cancel(b.number)
        self.assertEqual(cm.exception.code, "INVALID_STATE")

    def test_resolve_requires_called(self):
        t, _ = self.q.join(11, "A", "p")
        with self.assertRaises(QueueError) as cm:
            self.q.resolve(t.number)
        self.assertEqual(cm.exception.code, "INVALID_STATE")
        with self.assertRaises(QueueError) as cm:
            self.q.resolve(99)
        self.assertEqual(cm.exception.code, "NOT_FOUND")

    def test_timestamps_and_waiting_time(self):
        a, _ = self.q.join(11, "A", "p")            # t=0
        self.clock.advance(30)
        b, _ = self.q.join(12, "B", "p")            # t=30
        self.clock.advance(60)
        self.q.call_next()                          # A called t=90 -> waited 90
        self.clock.advance(120)
        self.q.resolve(a.number)                    # A resolved t=210 -> service 120
        self.q.call_next()                          # B called t=210 -> waited 180
        self.assertEqual(a.wait_seconds, 90)
        self.assertEqual(a.service_seconds, 120)
        self.assertEqual(b.wait_seconds, 180)
        s = self.q.summary()
        self.assertEqual(s["total_requests"], 2)
        self.assertEqual(s["resolved"], 1)
        self.assertEqual(s["being_served"], 1)
        self.assertEqual(s["avg_wait_seconds"], 135.0)
        self.assertEqual(s["min_wait_seconds"], 90.0)
        self.assertEqual(s["max_wait_seconds"], 180.0)
        self.assertEqual(s["avg_service_seconds"], 120.0)

    def test_cancelled_and_waiting_excluded_from_average(self):
        a, _ = self.q.join(11, "A", "p")
        self.q.join(12, "B", "p")
        self.clock.advance(10)
        self.q.cancel(a.number)
        s = self.q.summary()
        self.assertIsNone(s["avg_wait_seconds"])
        self.assertEqual(s["cancelled"], 1)
        self.assertEqual(s["longest_current_wait_seconds"], 10.0)

    def test_log_records_every_event_with_timestamp(self):
        t, _ = self.q.join(11, "A", "p")
        self.q.call_next()
        self.q.resolve(t.number)
        with open(self.path, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh]
        self.assertEqual([e["event"] for e in events], ["SESSION_START", "JOIN", "CALL", "RESOLVE"])
        for e in events:
            self.assertIn("ts", e)
            self.assertIn("time", e)
        self.assertNotIn("token", events[1], "only a hash of the resume token is stored")

    def test_restart_recovery_replays_log(self):
        a, token_a = self.q.join(11, "A", "p")
        b, token_b = self.q.join(12, "B", "p")
        self.q.join(13, "C", "p")
        self.q.call_next()
        self.q.cancel(3)
        self.q.log.close()

        q2 = self.make()                         # "server restart"
        self.assertEqual(q2.session_id, self.q.session_id)
        self.assertEqual(q2.tickets[1].status, CALLED)
        self.assertEqual(q2.tickets[2].status, WAITING)
        self.assertEqual(q2.tickets[3].status, CANCELLED)
        self.assertEqual(q2.positions(), {2: 1})
        self.assertEqual(q2.resume(2, token_b).number, 2)
        with self.assertRaises(QueueError):
            q2.resume(2, token_a)
        with self.assertRaises(QueueError):      # duplicate prevention survives restart
            q2.join(12, "B", "again")
        self.assertEqual(q2.join(14, "D", "p")[0].number, 4, "numbering continues")
        q2.resolve(1)
        self.assertEqual(q2.tickets[1].status, RESOLVED)
        q2.log.close()

    def test_damaged_last_line_is_skipped(self):
        self.q.join(11, "A", "p")
        self.q.log.close()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"event":"JOIN","tick')          # crash in the middle of a write
        q2 = self.make()
        self.assertEqual(len(q2.tickets), 1)
        q2.join(12, "B", "p")                          # must start on a fresh line
        q2.log.close()
        q3 = self.make()
        self.assertEqual(sorted(q3.tickets), [1, 2])
        q3.log.close()

    def test_concurrent_joins_get_unique_sequential_numbers(self):
        n = 40
        barrier = threading.Barrier(n)
        results = []

        def worker(ws):
            barrier.wait()
            t, _ = self.q.join(ws, "S%d" % ws, "p")
            results.append(t.number)

        threads = [threading.Thread(target=worker, args=(100 + i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), list(range(1, n + 1)))
        with open(self.path, encoding="utf-8") as fh:
            joins = [json.loads(line)["ticket"] for line in fh if '"JOIN"' in line]
        self.assertEqual(joins, list(range(1, n + 1)), "log order == ticket order")

    def test_queue_full(self):
        q = QueueManager(EventLog(os.path.join(self.tmp, "small.jsonl")), clock=self.clock, max_active=2)
        q.load()
        q.join(1, "a", "p")
        q.join(2, "b", "p")
        with self.assertRaises(QueueError) as cm:
            q.join(3, "c", "p")
        self.assertEqual(cm.exception.code, "QUEUE_FULL")
        q.log.close()


if __name__ == "__main__":
    unittest.main()
