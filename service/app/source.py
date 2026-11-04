"""Liest den Infinite Noise TRNG über das offizielle `infnoise`-Tool.

`infnoise` (ohne --multiplier!) gibt nur so viele Bits aus, wie es an Entropie
gemessen hat, und nur, solange der eingebaute Health-Check besteht. Mit
`--debug` schreibt es etwa alle 2^20 Rohbits eine Statuszeile nach stderr, die
wir für die Gesundheitsanzeige auswerten.

Zwei Betriebsarten:

  fresh (Standard)  Zufall entsteht NACH Eingang der Anfrage. Der Dienst liest
                    den TRNG ständig mit, verwirft aber alles, was älter ist
                    als die Anfrage. Verwendet werden nur Datenblöcke, deren
                    Vorgängerblock schon nach der Anfrage ankam – damit sind
                    auch die Rohbits nachweislich erst danach gemessen worden.
  pool              Klassischer Vorrat (bis pool_max Bytes) für hohen
                    Durchsatz. Bytes können dann Minuten bis Tage alt sein.

`infnoise` läuft unter `stdbuf -o0`, damit es jeden Block (~400 Bytes) sofort
schreibt statt 4–8 KB zu sammeln. Das hält die Wartezeit bei einigen
Millisekunden.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
from collections import Counter, deque

STATS_RE = re.compile(
    r"Generated (\d+) bits\.\s+(OK|NOT OK) to use data\.\s+"
    r"Estimated entropy per bit: ([\d.]+), estimated K: ([\d.]+)"
)
ONES_RE = re.compile(
    r"num1s:([\d.]+)%, even misfires:([\d.]+)%, odd misfires:([\d.]+)%"
)

# Theoretischer Wert laut Design (K = 1.82  ->  log2(1.82) ≈ 0.864 Bit/Bit)
DESIGN_ENTROPY_PER_BIT = math.log2(1.82)


class Unavailable(Exception):
    """Es gibt gerade keinen echten Zufall. Niemals durch PRNG ersetzen."""


class EntropySource:
    def __init__(self, cmd: list[str], label: str, mode: str = "fresh",
                 pool_max: int = 4 * 1024 * 1024, fresh_max_age: float = 2.0):
        if mode not in ("fresh", "pool"):
            raise ValueError("mode muss fresh oder pool sein")
        self.cmd = cmd
        self.label = label
        self.mode = mode
        self.pool_max = pool_max
        self.fresh_max_age = fresh_max_age

        # Blöcke: [ankunft, ankunft_vorgänger, daten, gelesen_bis]
        self._chunks: deque = deque()
        self._size = 0
        self._last_arrival = 0.0
        self._cond = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._paused = threading.Event()
        self._idle = threading.Event()  # gesetzt, wenn kein Prozess läuft
        self._idle.set()
        self._stop = threading.Event()

        self.state = "starting"  # starting | ok | no_device | health_failed | error | paused
        self.last_error = ""
        self.started_at = time.time()
        self.bytes_in = 0
        self.bytes_out = 0
        self.restarts = 0
        self.stats: dict = {}
        self.stats_history: deque = deque(maxlen=120)
        self._rate_window: deque = deque(maxlen=60)  # (zeit, bytes_in)

    # ------------------------------------------------------------------ Lauf
    def start(self) -> None:
        threading.Thread(target=self._run, name="entropy-source", daemon=True).start()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():
                self.state = "paused"
                time.sleep(0.2)
                continue
            try:
                self._run_once()
            except Exception as e:  # pragma: no cover
                self.state, self.last_error = "error", str(e)
            if self._paused.is_set() or self._stop.is_set():
                continue
            self.restarts += 1
            time.sleep(5)

    def _run_once(self) -> None:
        env = dict(os.environ)
        env.pop("INFNOISE_MULTIPLIER", None)  # Sicherheitsnetz: niemals strecken
        self._idle.clear()
        try:
            proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, bufsize=0
            )
        except FileNotFoundError as e:
            self.state, self.last_error = "error", f"Programm nicht gefunden: {e}"
            self._idle.set()
            return
        self._proc = proc
        self.state = "starting"
        err_thread = threading.Thread(target=self._read_stderr, args=(proc,), daemon=True)
        err_thread.start()
        try:
            while True:
                # Pool-Modus: Gegendruck, wenn voll (infnoise blockiert dann im Pipe).
                # Fresh-Modus: immer weiterlesen, Altes wird verworfen.
                if self.mode == "pool":
                    with self._cond:
                        while self._size >= self.pool_max and not self._paused.is_set():
                            self._cond.wait(0.5)
                if self._paused.is_set():
                    break
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                if self.state not in ("health_failed",):
                    self.state = "ok"
                now = time.time()
                with self._cond:
                    self._chunks.append([now, self._last_arrival, chunk, 0])
                    self._last_arrival = now
                    self._size += len(chunk)
                    self.bytes_in += len(chunk)
                    self._expire(now)
                    self._cond.notify_all()
                self._rate_window.append((time.time(), self.bytes_in))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            err_thread.join(2)
            code = proc.returncode
            if not self._paused.is_set() and self.state == "ok" and code not in (0, None):
                self.state = "error"
            if code not in (0, None) and not self.last_error:
                self.last_error = f"Exit-Code {code}"
            self._proc = None
            self._idle.set()

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            m = STATS_RE.search(line)
            if m:
                ok = m.group(2) == "OK"
                self.stats.update(
                    raw_bits=int(m.group(1)),
                    ok=ok,
                    entropy_per_bit=float(m.group(3)),
                    k=float(m.group(4)),
                    at=time.time(),
                )
                self.stats_history.append(
                    {"t": time.time(), "entropy_per_bit": float(m.group(3)), "ok": ok}
                )
                if not ok:
                    self.state = "health_failed"
                elif self.state == "health_failed":
                    self.state = "ok"
                continue
            m = ONES_RE.search(line)
            if m:
                self.stats.update(
                    ones_pct=float(m.group(1)),
                    even_misfires_pct=float(m.group(2)),
                    odd_misfires_pct=float(m.group(3)),
                )
                continue
            low = line.lower()
            if "can't find" in low or "can't open" in low or "cannot find" in low or "no devices" in low or "unable to open" in low:
                self.state, self.last_error = "no_device", line
            elif "health check" in low or "exceeded" in low:
                self.state, self.last_error = "health_failed", line
            elif line.startswith("Error"):
                self.last_error = line
                if self.state != "no_device":
                    self.state = "error"
            else:
                self.last_error = line

    # --------------------------------------------------------------- Entnahme
    def _expire(self, now: float) -> None:
        """Verwirft Blöcke, die im Fresh-Modus zu alt sind (Lock muss gehalten werden)."""
        if self.mode != "fresh":
            return
        limit = now - self.fresh_max_age
        while self._chunks and self._chunks[0][0] < limit:
            c = self._chunks.popleft()
            self._size -= len(c[2]) - c[3]

    def take(self, n: int, timeout: float = 30.0, after: float | None = None,
             meta: dict | None = None) -> bytes:
        """Entnimmt genau n Bytes. Entnommene Bytes verlassen den Puffer für immer.

        after: nur Bytes verwenden, die nachweislich nach diesem Zeitpunkt
               entstanden sind (Vorgängerblock kam nach `after` an).
        meta:  wird mit first/last (Ankunftszeiten der verwendeten Blöcke) gefüllt.
        """
        deadline = time.time() + timeout
        out = bytearray()
        with self._cond:
            while len(out) < n:
                self._expire(time.time())
                got = False
                for c in self._chunks:
                    arrival, prev, data, pos = c
                    if pos >= len(data):
                        continue
                    if after is not None and prev <= after:
                        continue  # könnte vor der Anfrage entstanden sein
                    k = min(n - len(out), len(data) - pos)
                    out += data[pos : pos + k]
                    c[3] = pos + k
                    self._size -= k
                    self.bytes_out += k
                    if meta is not None:
                        meta.setdefault("first", arrival)
                        meta["last"] = arrival
                    got = True
                    if len(out) >= n:
                        break
                while self._chunks and self._chunks[0][3] >= len(self._chunks[0][2]):
                    self._chunks.popleft()
                if got:
                    self._cond.notify_all()
                    continue
                if self.state in ("no_device", "health_failed", "error", "paused"):
                    raise Unavailable(self._unavailable_msg())
                left = deadline - time.time()
                if left <= 0:
                    raise Unavailable("Zeitüberschreitung: Quelle liefert gerade nicht schnell genug")
                self._cond.wait(min(left, 0.5))
        return bytes(out)

    def _unavailable_msg(self) -> str:
        return {
            "no_device": "TRNG nicht gefunden (USB prüfen)",
            "health_failed": "Health-Check des TRNG fehlgeschlagen",
            "paused": "Quelle pausiert (Rohdaten-Test läuft)",
        }.get(self.state, f"Quelle nicht verfügbar: {self.last_error}")

    def pool_size(self) -> int:
        with self._cond:
            self._expire(time.time())
            return self._size

    def rate(self) -> float:
        w = list(self._rate_window)
        if len(w) < 2 or w[-1][0] - w[0][0] < 1:
            return 0.0
        return (w[-1][1] - w[0][1]) / (w[-1][0] - w[0][0])

    # ------------------------------------------------------------ Rohdaten
    def capture_raw(self, raw_cmd: list[str], nbytes: int, timeout: float = 120.0) -> tuple[bytes, dict]:
        """Hält den Normalbetrieb an und nimmt Rohdaten (ungewhitet) auf.

        Rohdaten sind NUR zur Prüfung da und gehen nie in den Pool.
        """
        self._paused.set()
        with self._cond:
            self._cond.notify_all()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        self._idle.wait(10)
        stats: dict = {}
        data = bytearray()
        try:
            p = subprocess.Popen(raw_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

            def errs():
                for raw in iter(p.stderr.readline, b""):
                    m = STATS_RE.search(raw.decode("utf-8", "replace"))
                    if m:
                        stats.update(entropy_per_bit=float(m.group(3)), k=float(m.group(4)), ok=m.group(2) == "OK")
                    elif raw.strip():
                        stats["message"] = raw.decode("utf-8", "replace").strip()

            t = threading.Thread(target=errs, daemon=True)
            t.start()
            deadline = time.time() + timeout
            while len(data) < nbytes and time.time() < deadline:
                chunk = p.stdout.read(min(65536, nbytes - len(data)))
                if not chunk:
                    break
                data += chunk
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
            t.join(2)
        finally:
            self._paused.clear()
        return bytes(data), stats


def mcv_min_entropy_per_bit(data: bytes) -> float | None:
    """Most-Common-Value-Schätzer (NIST SP 800-90B, 6.3.1) auf 8-Bit-Samples.

    Nur ein grober Indikator. Für eine belastbare Schätzung die Rohdatei mit
    NIST `ea_non_iid` prüfen.
    """
    L = len(data)
    if L < 1000:
        return None
    p_hat = max(Counter(data).values()) / L
    p_u = min(1.0, p_hat + 2.576 * math.sqrt(p_hat * (1 - p_hat) / (L - 1)))
    return -math.log2(p_u) / 8


def ones_ratio(data: bytes) -> float | None:
    if not data:
        return None
    ones = sum(bin(b).count("1") for b in data)
    return ones / (len(data) * 8)
