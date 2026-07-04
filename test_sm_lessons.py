"""Gated learning — propose can ONLY set PROPOSED; bank sets BANKED. Stdlib unittest
(source-shape gate + a functional check on a mock driver)."""
import inspect
import unittest

import sm_lessons


class _Result:
    def single(self):
        return {"st": "BANKED"}


class _Session:
    def __init__(self, log):
        self.log = log
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def run(self, cy, **kw):
        self.log.append((cy, kw))
        return _Result()


class _Driver:
    def __init__(self, log):
        self.log = log
    def session(self, **k):
        return _Session(self.log)


class LessonGateTest(unittest.TestCase):
    def test_propose_cannot_write_BANKED(self):
        self.assertNotIn("status='BANKED'", inspect.getsource(sm_lessons.propose))
        self.assertIn("status='PROPOSED'", inspect.getsource(sm_lessons.propose))

    def test_bank_is_the_only_BANKED_writer(self):
        self.assertIn("status='BANKED'", inspect.getsource(sm_lessons.bank))

    def test_propose_writes_PROPOSED_via_driver(self):
        log = []
        sm_lessons._driver_cache = _Driver(log)
        try:
            sm_lessons.propose("a lesson", lesson_id="lid")
            merged = [cy for cy, _ in log if "SMLesson" in cy]
            self.assertTrue(merged and "PROPOSED" in merged[0] and "BANKED" not in merged[0])
        finally:
            sm_lessons._driver_cache = None


if __name__ == "__main__":
    unittest.main()
