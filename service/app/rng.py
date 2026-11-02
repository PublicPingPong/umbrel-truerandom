"""Unverzerrte Umwandlung von echten Zufallsbytes in Zahlen, Permutationen usw.

Grundregeln (bitte nicht aufweichen):
  * Kein PRNG. Jedes Ergebnis entsteht direkt aus frischen Bytes der Quelle.
  * Kein Modulo-Bias: Zahlenbereiche per Rejection Sampling.
  * Bytes werden genau einmal verwendet; Reste eines Requests werden verworfen.
"""

from __future__ import annotations

from typing import Callable

TakeFn = Callable[[int], bytes]

MAX_INT_SPAN = 1 << 64


class Drawer:
    """Liest Bytes blockweise aus dem Pool und gibt sie einzeln weiter.

    Holt kleine Blöcke (64 Bytes), um den Pool-Lock nicht pro Zahl anzufassen. Was am
    Ende übrig bleibt, wird verworfen (nie an einen anderen Request gegeben).
    """

    def __init__(self, take: TakeFn, block: int = 64):
        self._take = take
        self._block = block
        self._buf = b""
        self._pos = 0
        self.bytes_consumed = 0  # aus dem Pool entnommen (inkl. verworfener Reste)

    def read(self, n: int) -> bytes:
        if n <= 0:
            return b""
        out = bytearray()
        while len(out) < n:
            if self._pos >= len(self._buf):
                chunk = self._take(max(self._block, n - len(out)))
                self.bytes_consumed += len(chunk)
                self._buf, self._pos = chunk, 0
            need = n - len(out)
            piece = self._buf[self._pos : self._pos + need]
            self._pos += len(piece)
            out += piece
        return bytes(out)

    def close(self) -> None:
        # Rest wegwerfen und überschreiben.
        self._buf, self._pos = b"", 0


def randbelow(d: Drawer, n: int) -> int:
    """Gleichverteilte ganze Zahl in [0, n) per Rejection Sampling."""
    if n <= 0:
        raise ValueError("n muss > 0 sein")
    if n == 1:
        return 0
    k = (n - 1).bit_length()
    nbytes = (k + 7) // 8
    mask = (1 << k) - 1
    while True:
        x = int.from_bytes(d.read(nbytes), "big") & mask
        if x < n:
            return x


def randint(d: Drawer, lo: int, hi: int) -> int:
    """Gleichverteilt in [lo, hi] (beide inklusive)."""
    if hi < lo:
        raise ValueError("max muss >= min sein")
    span = hi - lo + 1
    if span > MAX_INT_SPAN:
        raise ValueError("Bereich zu groß (max. 2^64 Werte)")
    return lo + randbelow(d, span)


def shuffle(d: Drawer, n: int, start: int = 0) -> list[int]:
    """Zufällige Permutation von start..start+n-1 (Fisher-Yates)."""
    a = list(range(start, start + n))
    for i in range(n - 1, 0, -1):
        j = randbelow(d, i + 1)
        a[i], a[j] = a[j], a[i]
    return a


def choice(d: Drawer, n: int, k: int, start: int = 0) -> list[int]:
    """k verschiedene Werte aus start..start+n-1 ohne Zurücklegen, in Ziehungsreihenfolge."""
    if not 0 <= k <= n:
        raise ValueError("k muss zwischen 0 und n liegen")
    # Partielles Fisher-Yates mit Dict, damit große n wenig Speicher brauchen.
    swapped: dict[int, int] = {}
    out = []
    for i in range(k):
        j = i + randbelow(d, n - i)
        vi, vj = swapped.get(i, i), swapped.get(j, j)
        swapped[j] = vi
        out.append(start + vj)
    return out


def uniform_float(d: Drawer) -> float:
    """Gleichverteilt in [0, 1) mit voller 53-Bit-Auflösung."""
    return (int.from_bytes(d.read(7), "big") >> 3) / float(1 << 53)
