"""Unit tests for the queue store. Run: python -m pytest tests  (or python tests/test_store.py)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib_prompt_queue.store import DONE, FAILED, RUNNING, WAITING, QueueStore  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = QueueStore(self.dir)

    def add(self, prompt, copies=1):
        return self.s.add("txt2img", ["task(x)", prompt, 20], ["Textbox:p"], {"prompt": prompt}, copies)

    def test_add_and_order(self):
        self.add("a"); self.add("b", copies=2)
        self.assertEqual([j["summary"]["prompt"] for j in self.s.waiting()], ["a", "b", "b"])
        self.assertEqual(self.s.load_args(self.s.waiting()[0]["id"])[1], "a")

    def test_claim_respects_pause(self):
        self.add("a")
        self.s.set_paused(True)
        self.assertIsNone(self.s.claim_next())
        self.s.set_paused(False)
        self.assertEqual(self.s.claim_next()["status"], RUNNING)
        self.assertIsNone(self.s.claim_next())

    def test_time_starts_when_gpu_is_free(self):
        self.add("a")
        j = self.s.claim_next()
        claimed = j["started"]
        import time
        time.sleep(0.02)
        self.s.mark_started(j["id"])
        self.assertGreater(self.s.get(j["id"])["started"], claimed)

    def test_finish_keeps_gallery_info(self):
        self.add("a")
        j = self.s.claim_next()
        self.s.finish(j["id"], DONE, images=[], gen_info='{"seed": 1}', html_info="<p>x</p>")
        self.assertEqual(self.s.get(j["id"])["gen_info"], '{"seed": 1}')

    def test_move(self):
        a, = self.add("a"); b, = self.add("b"); c, = self.add("c")
        self.s.move(c["id"], 0)
        self.assertEqual([j["summary"]["prompt"] for j in self.s.waiting()], ["c", "a", "b"])
        self.s.move(c["id"], 99)
        self.assertEqual([j["summary"]["prompt"] for j in self.s.waiting()], ["a", "b", "c"])
        self.s.move(a["id"], 1)
        self.assertEqual([j["summary"]["prompt"] for j in self.s.waiting()], ["b", "a", "c"])

    def test_remove_undo_keeps_place(self):
        a, = self.add("a"); b, = self.add("b"); c, = self.add("c")
        token = self.s.remove([b["id"]])
        self.assertEqual(len(self.s.waiting()), 2)
        self.assertEqual(self.s.undo(token), 1)
        self.assertEqual([j["summary"]["prompt"] for j in self.s.waiting()], ["a", "b", "c"])
        self.assertEqual(self.s.undo(token), 0)

    def test_running_job_cannot_be_removed(self):
        self.add("a")
        j = self.s.claim_next()
        self.assertIsNone(self.s.remove([j["id"]]))

    def test_requeue_and_clear(self):
        self.add("a")
        j = self.s.claim_next()
        self.s.finish(j["id"], DONE)
        new = self.s.requeue(j["id"])
        self.assertEqual(new["status"], WAITING)
        self.assertEqual(self.s.load_args(new["id"])[1], "a")
        token, n = self.s.clear("finished")
        self.assertEqual(n, 1)
        self.assertEqual(len(self.s.jobs), 1)
        self.s.undo(token)
        self.assertEqual(len(self.s.jobs), 2)

    def test_restart_survival(self):
        self.add("a"); self.add("b")
        self.s.claim_next()  # "a" is running when Forge dies
        s2 = QueueStore(self.dir)
        self.assertEqual(s2.jobs[0]["status"], FAILED)
        self.assertIn("Retry", s2.jobs[0]["error"])
        self.assertEqual(s2.jobs[1]["status"], WAITING)
        self.assertFalse(s2.paused)  # Auto is always on after a restart
        self.assertIn("carry on", s2.notice)

    def test_broken_file_is_kept_not_lost(self):
        with open(os.path.join(self.dir, "jobs.json"), "w") as f:
            f.write("{not json")
        s2 = QueueStore(self.dir)
        self.assertEqual(s2.jobs, [])
        self.assertTrue(any(n.startswith("jobs.json.broken") for n in os.listdir(self.dir)))

    def test_copies_capped(self):
        self.assertEqual(len(self.add("a", copies=500)), 100)


if __name__ == "__main__":
    unittest.main()
