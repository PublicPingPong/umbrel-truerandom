"""Prüft die Zufallslogik auf Verzerrung (mit os.urandom als Testquelle)."""
import math, os, sys, unittest
from collections import Counter
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import rng  # noqa: E402


def chi2_ok(counts, expected, df):
    chi2 = sum((c - expected) ** 2 / expected for c in counts)
    # grobe obere Schranke ~ df + 5*sqrt(2df)
    return chi2 < df + 5 * math.sqrt(2 * df), chi2


class RngTest(unittest.TestCase):
    def d(self):
        return rng.Drawer(os.urandom)

    def test_randint_uniform_d6(self):
        d = self.d(); N = 60000
        c = Counter(rng.randint(d, 1, 6) for _ in range(N))
        self.assertEqual(set(c), set(range(1, 7)))
        ok, chi = chi2_ok(c.values(), N / 6, 5); self.assertTrue(ok, chi)

    def test_no_modulo_bias_awkward_range(self):
        # 0..170: mit Modulo auf ein Byte wären 0..84 deutlich häufiger
        d = self.d(); N = 171 * 400
        c = Counter(rng.randbelow(d, 171) for _ in range(N))
        low = sum(c[i] for i in range(85)) / 85; high = sum(c[i] for i in range(85, 171)) / 86
        self.assertLess(abs(low - high) / high, 0.03)
        ok, chi = chi2_ok([c[i] for i in range(171)], N / 171, 170); self.assertTrue(ok, chi)

    def test_bounds(self):
        d = self.d()
        for _ in range(2000):
            self.assertTrue(-5 <= rng.randint(d, -5, 5) <= 5)
        self.assertEqual(rng.randint(d, 7, 7), 7)
        big = rng.randint(d, 0, 2**64 - 1); self.assertTrue(0 <= big < 2**64)
        with self.assertRaises(ValueError): rng.randint(d, 0, 2**64)
        with self.assertRaises(ValueError): rng.randint(d, 5, 4)

    def test_shuffle_permutation_and_uniform(self):
        d = self.d()
        self.assertEqual(sorted(rng.shuffle(d, 100)), list(range(100)))
        N = 24000
        c = Counter(tuple(rng.shuffle(d, 4)) for _ in range(N))
        self.assertEqual(len(c), 24)
        ok, chi = chi2_ok(c.values(), N / 24, 23); self.assertTrue(ok, chi)

    def test_choice(self):
        d = self.d()
        for _ in range(200):
            s = rng.choice(d, 40, 6, start=1)
            self.assertEqual(len(set(s)), 6); self.assertTrue(all(1 <= x <= 40 for x in s))
        N = 30000
        c = Counter(x for _ in range(N) for x in rng.choice(d, 10, 3))
        ok, chi = chi2_ok(c.values(), N * 3 / 10, 9); self.assertTrue(ok, chi)
        self.assertEqual(sorted(rng.choice(d, 5, 5)), list(range(5)))

    def test_float(self):
        d = self.d(); xs = [rng.uniform_float(d) for _ in range(20000)]
        self.assertTrue(all(0 <= x < 1 for x in xs))
        self.assertAlmostEqual(sum(xs) / len(xs), 0.5, delta=0.01)

    def test_bytes_never_reused(self):
        # Quelle, die fortlaufende Zähler liefert: jedes Byte darf nur einmal auftauchen
        state = {"i": 0}
        def take(n):
            out = bytes((state["i"] + k) % 256 for k in range(n)); state["i"] += n; return out
        d = rng.Drawer(take, block=16)
        a = d.read(10); b = d.read(10)
        self.assertEqual(list(a + b), list(range(20)))
        d.close(); self.assertEqual(d.bytes_consumed, 32)


if __name__ == "__main__":
    unittest.main()
