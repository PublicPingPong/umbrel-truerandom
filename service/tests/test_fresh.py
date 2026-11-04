"""Prüft den Fresh-Modus: nur Bytes aus Blöcken, die nach der Anfrage entstanden sind."""
import os, sys, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
from source import EntropySource  # noqa: E402

FAKE = [sys.executable, os.path.join(HERE, "fake_infnoise.py")]


class FreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = EntropySource(FAKE, "TEST", mode="fresh", fresh_max_age=1.0)
        cls.src.start()
        time.sleep(1.5)

    def test_only_after_request(self):
        for _ in range(20):
            t0 = time.time(); meta = {}
            b = self.src.take(500, after=t0, meta=meta)
            self.assertEqual(len(b), 500)
            self.assertGreater(meta["first"], t0)
            # Vorgängerblock muss ebenfalls nach t0 angekommen sein
            self.assertLess(meta["last"] - t0, 1.0)

    def test_old_data_expires(self):
        time.sleep(0.2)
        self.assertLess(self.src.pool_size(), 60000)  # max ~1 s bei ~43 KB/s

    def test_no_reuse_between_requests(self):
        t0 = time.time()
        a = self.src.take(2000, after=t0); b = self.src.take(2000, after=t0)
        self.assertNotEqual(a, b)
        self.assertNotIn(a[:32], b)


if __name__ == "__main__":
    unittest.main()
