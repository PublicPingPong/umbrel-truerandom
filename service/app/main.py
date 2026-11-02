"""True Random – HTTP-Dienst für echten Zufall aus dem Infinite Noise TRNG.

Nur Python-Standardbibliothek, damit der Code klein und prüfbar bleibt.

Öffentlich (mit API-Key, an Umbrel-Login vorbei):   /v1/*
Verwaltung (hinter Umbrel-Login):                   /, /api/*
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import shlex
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))

import rng  # noqa: E402
from source import (  # noqa: E402
    DESIGN_ENTROPY_PER_BIT,
    EntropySource,
    Unavailable,
    mcv_min_entropy_per_bit,
    ones_ratio,
)

VERSION = "0.1.0"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PORT = int(os.environ.get("PORT", "8080"))
SOURCE_CMD = shlex.split(os.environ.get("SOURCE_CMD", "infnoise --debug"))
RAW_CMD = shlex.split(os.environ.get("RAW_CMD", "infnoise --raw --debug"))
SOURCE_LABEL = os.environ.get("SOURCE_LABEL", "infnoise")
STATIC = Path(__file__).parent / "static"

LIMITS = {"bytes": 65536, "count": 10000, "shuffle": 10000, "choice_n": 1 << 32}

DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
RAW_DIR = DATA_DIR / "raw"
RAW_DIR.mkdir(exist_ok=True)


# ------------------------------------------------------------------ Config
def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    if not cfg.get("api_key"):
        cfg["api_key"] = secrets.token_urlsafe(32)
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


CONFIG = load_config()
SOURCE = EntropySource(SOURCE_CMD, SOURCE_LABEL)
STATS = {"requests": 0, "errors": 0, "by_endpoint": {}}
RAW_LOCK = threading.Lock()
RAW_RESULT: dict = {}


# ------------------------------------------------------------------ Helpers
class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def q_int(q: dict, name: str, default=None, lo=None, hi=None) -> int:
    if name not in q:
        if default is None:
            raise ApiError(400, f"Parameter '{name}' fehlt")
        return default
    try:
        v = int(q[name][0])
    except ValueError:
        raise ApiError(400, f"Parameter '{name}' muss eine ganze Zahl sein")
    if lo is not None and v < lo:
        raise ApiError(400, f"'{name}' muss >= {lo} sein")
    if hi is not None and v > hi:
        raise ApiError(400, f"'{name}' muss <= {hi} sein")
    return v


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run_draw(fn):
    """Führt eine Ziehung mit eigenem Drawer aus und liefert Ergebnis + Metadaten."""
    d = rng.Drawer(SOURCE.take)
    try:
        result = fn(d)
    except Unavailable as e:
        raise ApiError(503, str(e))
    except ValueError as e:
        raise ApiError(400, str(e))
    finally:
        d.close()
    return {
        "data": result,
        "source": SOURCE.label,
        "id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "bytes_used": d.bytes_consumed,
    }


# ------------------------------------------------------------------ v1 API
def v1_bytes(q):
    n = q_int(q, "n", 32, 1, LIMITS["bytes"])
    fmt = q.get("format", ["hex"])[0]
    if fmt not in ("hex", "base64"):
        raise ApiError(400, "format muss hex oder base64 sein")

    def f(d):
        b = d.read(n)
        return b.hex() if fmt == "hex" else base64.b64encode(b).decode()

    return run_draw(f)


def v1_int(q):
    lo = q_int(q, "min", 0)
    hi = q_int(q, "max", None)
    count = q_int(q, "count", 1, 1, LIMITS["count"])
    return run_draw(lambda d: [rng.randint(d, lo, hi) for _ in range(count)])


def v1_float(q):
    count = q_int(q, "count", 1, 1, LIMITS["count"])
    return run_draw(lambda d: [rng.uniform_float(d) for _ in range(count)])


def v1_shuffle(q):
    n = q_int(q, "n", None, 1, LIMITS["shuffle"])
    start = q_int(q, "start", 0)
    return run_draw(lambda d: rng.shuffle(d, n, start))


def v1_choice(q):
    n = q_int(q, "n", None, 1, LIMITS["choice_n"])
    k = q_int(q, "k", 1, 0, min(n, LIMITS["count"]))
    start = q_int(q, "start", 0)
    return run_draw(lambda d: rng.choice(d, n, k, start))


def v1_health(q):
    return {
        "ok": SOURCE.state == "ok",
        "state": SOURCE.state,
        "source": SOURCE.label,
        "pool_bytes": SOURCE.pool_size(),
        "timestamp": now_iso(),
    }


V1 = {
    "/v1/bytes": v1_bytes,
    "/v1/int": v1_int,
    "/v1/float": v1_float,
    "/v1/shuffle": v1_shuffle,
    "/v1/choice": v1_choice,
    "/v1/health": v1_health,
}


# ------------------------------------------------------------------ Admin
def api_status():
    return {
        "version": VERSION,
        "source": SOURCE.label,
        "state": SOURCE.state,
        "last_message": SOURCE.last_error,
        "pool_bytes": SOURCE.pool_size(),
        "pool_max": SOURCE.pool_max,
        "rate_bytes_per_s": round(SOURCE.rate(), 1),
        "bytes_in": SOURCE.bytes_in,
        "bytes_out": SOURCE.bytes_out,
        "restarts": SOURCE.restarts,
        "uptime_s": int(time.time() - SOURCE.started_at),
        "health": SOURCE.stats,
        "history": list(SOURCE.stats_history),
        "design_entropy_per_bit": round(DESIGN_ENTROPY_PER_BIT, 4),
        "api_key": CONFIG["api_key"],
        "requests": STATS,
        "raw_test": RAW_RESULT,
        "limits": LIMITS,
    }


def api_rotate_key():
    CONFIG["api_key"] = secrets.token_urlsafe(32)
    save_config(CONFIG)
    return {"api_key": CONFIG["api_key"]}


def api_rawtest(q):
    mb = q_int(q, "mb", 1, 1, 16)
    if not RAW_LOCK.acquire(blocking=False):
        raise ApiError(409, "Ein Rohdaten-Test läuft bereits")
    try:
        RAW_RESULT.clear()
        RAW_RESULT.update(running=True, started=now_iso(), mb=mb)
        data, stats = SOURCE.capture_raw(RAW_CMD, mb * 1024 * 1024)
        name = f"raw-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.bin"
        if data:
            (RAW_DIR / name).write_bytes(data)
        # nur die letzten 5 Rohdateien behalten
        for old in sorted(RAW_DIR.glob("raw-*.bin"))[:-5]:
            old.unlink(missing_ok=True)
        RAW_RESULT.clear()
        RAW_RESULT.update(
            running=False,
            finished=now_iso(),
            bytes=len(data),
            file=name if data else None,
            infnoise=stats,
            mcv_min_entropy_per_bit=mcv_min_entropy_per_bit(data),
            ones_ratio=ones_ratio(data),
        )
        return RAW_RESULT
    finally:
        RAW_LOCK.release()


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"TrueRandom/{VERSION}"
    sys_version = ""

    def log_message(self, fmt, *args):  # knappes Log, keine Query-Strings (Key!)
        sys.stderr.write(f"{self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, cors: bool = False):
        extra = {"Access-Control-Allow-Origin": "*"} if cors else None
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _authorized(self, q) -> bool:
        key = self.headers.get("X-API-Key", "")
        auth = self.headers.get("Authorization", "")
        if not key and auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        if not key:
            key = q.get("key", [""])[0]
        return bool(key) and hmac.compare_digest(key.encode(), CONFIG["api_key"].encode())

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain", {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization, X-API-Key",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
        })

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch(post=True)

    def _dispatch(self, post: bool = False):
        url = urlparse(self.path)
        path, q = url.path.rstrip("/") or "/", parse_qs(url.query)
        try:
            if path.startswith("/v1/"):
                fn = V1.get(path)
                if not fn:
                    raise ApiError(404, "Unbekannter Endpunkt")
                if not self._authorized(q):
                    raise ApiError(401, "API-Key fehlt oder ist falsch")
                STATS["requests"] += 1
                STATS["by_endpoint"][path] = STATS["by_endpoint"].get(path, 0) + 1
                return self._json(200, fn(q), cors=True)

            # --- Verwaltung (Umbrel-Login schützt diese Pfade) ---
            if path == "/":
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(200, api_status())
            if path == "/api/rotate-key" and post:
                return self._json(200, api_rotate_key())
            if path == "/api/rawtest" and post:
                return self._json(200, api_rawtest(q))
            if path.startswith("/api/raw/"):
                name = Path(path).name
                f = RAW_DIR / name
                if not name.startswith("raw-") or not f.is_file():
                    raise ApiError(404, "Datei nicht gefunden")
                return self._send(200, f.read_bytes(), "application/octet-stream",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            raise ApiError(404, "Nicht gefunden")
        except ApiError as e:
            STATS["errors"] += 1
            self._json(e.status, {"error": e.msg, "state": SOURCE.state}, cors=path.startswith("/v1/"))
        except BrokenPipeError:
            pass


def main():
    SOURCE.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"True Random {VERSION} auf Port {PORT}, Quelle: {' '.join(SOURCE_CMD)}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
