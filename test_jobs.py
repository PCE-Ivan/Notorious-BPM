#!/usr/bin/env python3
"""Unit tests for jobs.py -- the shared background-job runner. Pure Python,
no Flask/app import, nothing touched on disk.

Run manually:

    python3 test_jobs.py
"""
import threading
import time
import unittest

import jobs


def _wait_until(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class JobTest(unittest.TestCase):
    def setUp(self):
        self._names = []

    def tearDown(self):
        for n in self._names:
            jobs.REGISTRY.pop(n, None)

    def make(self, name, **kw):
        self._names.append(name)
        return jobs.Job(name, name.title(), **kw)

    def test_runs_and_records_result(self):
        job = self.make("t_result", progress_extra="found", found=0)

        def worker():
            for i in range(3):
                job.progress(i + 1, 3, extra=i)
            job.state["result"] = {"ok": True}

        self.assertTrue(job.start(worker))
        self.assertTrue(_wait_until(lambda: not job.running))
        self.assertEqual(job.state["result"], {"ok": True})
        self.assertEqual((job.state["done"], job.state["total"], job.state["found"]), (3, 3, 2))
        self.assertIsNone(job.state["error"])
        self.assertFalse(job.state["cancelled"])
        self.assertIsNotNone(job.state["finished_at"])

    def test_second_start_while_running_is_refused(self):
        job = self.make("t_busy")
        gate = threading.Event()
        self.assertTrue(job.start(gate.wait))
        self.assertFalse(job.start(lambda: None))
        gate.set()
        self.assertTrue(_wait_until(lambda: not job.running))
        self.assertTrue(job.start(lambda: None))  # free again

    def test_worker_exception_becomes_error_not_a_crash(self):
        job = self.make("t_error")

        def worker():
            raise RuntimeError("boom")

        job.start(worker)
        self.assertTrue(_wait_until(lambda: not job.running))
        self.assertEqual(job.state["error"], "boom")

    def test_cancel_stops_at_next_progress_tick(self):
        job = self.make("t_cancel")
        reached = []
        started = threading.Event()

        def worker():
            for i in range(1000):
                started.set()
                time.sleep(0.005)
                job.progress(i + 1, 1000)
                reached.append(i)

        job.start(worker)
        self.assertTrue(started.wait(2))
        self.assertTrue(job.cancel())
        self.assertTrue(_wait_until(lambda: not job.running))
        self.assertTrue(job.state["cancelled"])
        self.assertIsNone(job.state["error"])
        self.assertLess(len(reached), 1000)

    def test_cancel_is_not_swallowed_by_per_item_except_exception(self):
        # Workers routinely wrap each item in `except Exception: pass`;
        # a cancel must punch straight through those.
        job = self.make("t_swallow")
        started = threading.Event()

        def worker():
            for i in range(1000):
                try:
                    started.set()
                    time.sleep(0.005)
                    job.progress(i + 1, 1000)
                except Exception:
                    pass

        job.start(worker)
        self.assertTrue(started.wait(2))
        job.cancel()
        self.assertTrue(_wait_until(lambda: not job.running))
        self.assertTrue(job.state["cancelled"])

    def test_non_cancellable_job_refuses_cancel(self):
        job = self.make("t_fixed", cancellable=False)
        gate = threading.Event()
        job.start(gate.wait)
        self.assertFalse(job.cancel())
        self.assertFalse(job.snapshot()["can_cancel"])
        gate.set()

    def test_state_dict_identity_is_stable_across_runs(self):
        # app.py aliases `_xxx_state = job.state`; a rebind would orphan it.
        job = self.make("t_alias")
        alias = job.state
        job.start(lambda: None)
        _wait_until(lambda: not job.running)
        job.start(lambda: None)
        _wait_until(lambda: not job.running)
        self.assertIs(alias, job.state)

    def test_guard_lock_is_held_around_start_and_prepare_runs_under_it(self):
        job = self.make("t_guard")
        guard = threading.Lock()
        seen = []

        def prepare():
            seen.append(guard.locked())

        job.start(lambda: None, guard=guard, prepare=prepare)
        self.assertEqual(seen, [True])
        self.assertFalse(guard.locked())

    def test_extras_reset_between_runs(self):
        job = self.make("t_extras", moved=0)

        def first():
            job.state["moved"] = 5

        job.start(first)
        _wait_until(lambda: not job.running)
        self.assertEqual(job.state["moved"], 5)
        job.start(lambda: None)
        _wait_until(lambda: not job.running)
        self.assertEqual(job.state["moved"], 0)

    def test_tray_listing_shows_running_and_recent_not_never_started(self):
        job = self.make("t_tray")
        self.assertNotIn("t_tray", [j["name"] for j in jobs.list_jobs()])
        gate = threading.Event()
        job.start(gate.wait)
        listed = {j["name"]: j for j in jobs.list_jobs()}
        self.assertTrue(listed["t_tray"]["running"])
        gate.set()
        _wait_until(lambda: not job.running)
        self.assertIn("t_tray", [j["name"] for j in jobs.list_jobs()])
        job.dismiss()
        self.assertNotIn("t_tray", [j["name"] for j in jobs.list_jobs()])

    def test_summary_only_on_clean_finish(self):
        job = self.make("t_sum", summarize=lambda s: f"did {s['result']}")

        def worker():
            job.state["result"] = 7

        job.start(worker)
        _wait_until(lambda: not job.running)
        self.assertEqual(job.snapshot()["summary"], "did 7")


if __name__ == "__main__":
    unittest.main()
