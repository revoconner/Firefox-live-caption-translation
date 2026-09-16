"""Live caption backend: captures Firefox playback audio, runs streaming ASR with language detection, translates non English finals, and streams caption events to the extension over a local WebSocket.

Run: capvenv\\Scripts\\python.exe backend\\service.py [--port 8765] [--eou-ms 800] [--right-context 1] [--unload-grace 20] [--keep-loaded]

The models are loaded while firefox.exe is running and unloaded after it has been gone for the grace period, so a backend started at login costs nothing until Firefox appears.

Protocol (JSON text frames, one object per frame):
  client -> server: {"type": "activate"} | {"type": "deactivate"} | {"type": "ping"}
  server -> client: {"type": "status", "state": "idle|capturing|error", "detail": str}
                    {"type": "partial", "text": str}
                    {"type": "final", "id": int, "text": str, "lang": str, "needs_translation": bool}
                    {"type": "translation", "id": int, "text": str, "lang": str}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import psutil
import websockets
from websockets.asyncio.server import ServerConnection, serve

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nemo_ffi  # noqa: E402

log = logging.getLogger("captions")

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
ASR_MODEL = MODELS / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
NMT_MODEL = MODELS / "riva-translate-4b-instruct-v2-q8_0.gguf"
CAPTURE_EXE = ROOT / "native" / "proc_loopback.exe"
RATE = 16000
CHUNK_MS = 80
CHUNK_BYTES = RATE * CHUNK_MS // 1000 * 2


def nmt_code(lang: str) -> str:
    """Riva pair tags use bare codes (ja, pt, ko) with a few variants; the runtime rejects forms like pt-PT."""
    low = lang.lower()
    if low.startswith("zh"):
        return "zh-tw" if low.endswith("tw") else "zh"
    if low in ("nb-no", "nn-no", "nb", "nn"):
        return "no"
    return low.split("-")[0]


SCRIPT_RANGES = [
    ("ja", (0x3040, 0x30FF)),
    ("ko", (0xAC00, 0xD7AF)),
    ("ko", (0x1100, 0x11FF)),
    ("zh", (0x4E00, 0x9FFF)),
    ("cyr", (0x0400, 0x04FF)),
    ("ar", (0x0600, 0x06FF)),
    ("th", (0x0E00, 0x0E7F)),
    ("he", (0x0590, 0x05FF)),
    ("el", (0x0370, 0x03FF)),
    ("hi", (0x0900, 0x097F)),
]
FAMILY_DEFAULT = {"ja": "ja", "ko": "ko", "zh": "zh", "cyr": "ru", "ar": "ar", "th": "th", "he": "he", "el": "el", "hi": "hi"}


def script_family(text: str) -> str:
    """Which writing system dominates the text. Kana wins over ideographs so Japanese is not mistaken for Chinese."""
    counts: dict[str, int] = {}
    for ch in text:
        o = ord(ch)
        for fam, (lo, hi) in SCRIPT_RANGES:
            if lo <= o <= hi:
                counts[fam] = counts.get(fam, 0) + 1
                break
        else:
            if ch.isalpha():
                counts["latin"] = counts.get("latin", 0) + 1
    if not counts:
        return ""
    if counts.get("ja") and counts.get("zh"):
        counts["ja"] += counts.pop("zh")
    return max(counts, key=counts.get)


def lang_family(lang: str) -> str:
    base = nmt_code(lang)
    if base in ("ru", "uk", "bg"):
        return "cyr"
    if base.startswith("zh"):
        return "zh"
    if base in FAMILY_DEFAULT:
        return base
    return "latin"


def firefox_root_pid() -> int | None:
    """The firefox.exe whose parent is not firefox.exe. Process loopback on it captures the whole tree."""
    for p in psutil.process_iter(["pid", "name", "ppid"]):
        if (p.info["name"] or "").lower() != "firefox.exe":
            continue
        try:
            parent = psutil.Process(p.info["ppid"]).name().lower()
        except psutil.Error:
            parent = ""
        if parent != "firefox.exe":
            return p.info["pid"]
    return None


class Engine:
    """Owns the ASR recognizer, the NMT translator, the capture subprocess and the ASR thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, events: asyncio.Queue, right_context: int, eou_ms: int, unload_grace: float = 20.0, watch_interval: float = 5.0, keep_loaded: bool = False) -> None:
        self.loop = loop
        self.events = events
        self.right_context = right_context
        self.eou_ms = eou_ms
        self.unload_grace = unload_grace
        self.watch_interval = watch_interval
        self.keep_loaded = keep_loaded
        self.rec: nemo_ffi.Recognizer | None = None
        self.nmt: nemo_ffi.Translator | None = None
        self.nmt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nmt")
        self.inflight = 0  # translations queued or running, so unload never pulls the model from under one
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()  # start, stop, load and unload run on worker threads and must not interleave
        self.final_id = 0
        self.last_lang = ""
        self.closing = threading.Event()
        threading.Thread(target=self._watch_firefox, name="firefox-watch", daemon=True).start()

    @property
    def loaded(self) -> bool:
        return self.rec is not None

    def load(self) -> None:
        with self.lock:
            self._load()

    def unload(self) -> bool:
        with self.lock:
            return self._unload()

    def _load(self) -> None:
        if self.rec is not None:
            return
        t = time.perf_counter()
        self.emit({"type": "status", "state": "loading", "detail": "loading models"})
        rec = nemo_ffi.Recognizer(ASR_MODEL, rnnt_right_context=self.right_context, endpointing=True, eou_ms=self.eou_ms)
        self.nmt = nemo_ffi.Translator(NMT_MODEL)
        self.rec = rec  # assigned last so `loaded` only turns true once both models are in
        log.info("models loaded in %.1fs", time.perf_counter() - t)
        self.emit({"type": "status", "state": "idle", "detail": "models loaded"})

    def _unload(self) -> bool:
        if self.rec is None:
            return True
        if self.proc is not None or self.inflight:
            return False
        if self.nmt:
            self.nmt.close()
            self.nmt = None
        self.rec.close()
        self.rec = None
        log.info("models unloaded")
        self.emit({"type": "status", "state": "unloaded", "detail": "firefox not running"})
        return True

    def _watch_firefox(self) -> None:
        """Keep the models loaded exactly while Firefox is running. Unloading waits out a grace period so a Firefox restart does not pay the load twice."""
        absent_since: float | None = None
        while not self.closing.wait(self.watch_interval):
            running = firefox_root_pid() is not None
            if running:
                absent_since = None
                if not self.loaded:
                    try:
                        self.load()
                    except nemo_ffi.NemoError as e:
                        log.error("model load failed: %s", e)
                        self.emit({"type": "status", "state": "error", "detail": f"model load failed: {e}"})
            elif self.loaded and not self.keep_loaded:
                if absent_since is None:
                    absent_since = time.monotonic()
                elif time.monotonic() - absent_since >= self.unload_grace:
                    if self.proc is not None:
                        self.stop()  # capture cannot outlive the process it was capturing
                    if self.unload():
                        absent_since = None

    def resolve_lang(self, tagged: str, text: str) -> str:
        """The model only emits its language tag after terminal punctuation, so untagged finals fall back to the last tagged language when the script agrees, else to the script's default language."""
        if tagged:
            self.last_lang = tagged
            return tagged
        fam = script_family(text)
        if not fam:
            return ""
        last_fam = lang_family(self.last_lang) if self.last_lang else ""
        if last_fam == fam or (fam == "zh" and last_fam == "ja"):
            return self.last_lang
        return FAMILY_DEFAULT.get(fam, "")

    def emit(self, msg: dict) -> None:
        self.loop.call_soon_threadsafe(self.events.put_nowait, msg)

    def start(self) -> None:
        with self.lock:
            self._start()

    def stop(self) -> None:
        with self.lock:
            self._stop()

    def _start(self) -> None:
        if self.proc is not None:
            self.emit({"type": "status", "state": "capturing", "detail": "already capturing"})
            return
        pid = firefox_root_pid()
        if pid is None:
            self.emit({"type": "status", "state": "error", "detail": "firefox.exe not found"})
            return
        self._load()  # no-op once the watcher has done it; covers an activate that beats the watcher
        self.proc = subprocess.Popen([str(CAPTURE_EXE), "--pid", str(pid), "--rate", str(RATE)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.stop_flag.clear()
        self.last_lang = ""
        self.thread = threading.Thread(target=self._asr_loop, name="asr", daemon=True)
        self.thread.start()
        threading.Thread(target=self._drain_stderr, name="capture-stderr", daemon=True).start()
        self.emit({"type": "status", "state": "capturing", "detail": f"firefox pid {pid}"})
        log.info("capture started on pid %d", pid)

    def _stop(self) -> None:
        if self.proc is None:
            return
        self.stop_flag.set()
        try:
            self.proc.kill()
        except OSError:
            pass
        if self.thread:
            self.thread.join(timeout=5)
        self.proc = None
        self.thread = None
        self.emit({"type": "status", "state": "idle", "detail": "capture stopped"})
        log.info("capture stopped")

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            log.info("capture: %s", line.decode("utf-8", "replace").rstrip())

    def _asr_loop(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        rec = self.rec
        assert rec is not None
        stream = rec.stream(language="auto", interim=True)
        last_partial = ""

        def read_exact(n: int) -> bytes:
            parts = bytearray()
            while len(parts) < n:
                piece = proc.stdout.read(n - len(parts))
                if not piece:
                    break
                parts += piece
            return bytes(parts)

        try:
            while not self.stop_flag.is_set():
                buf = read_exact(CHUNK_BYTES)
                if len(buf) < 2:
                    break
                samples = np.frombuffer(buf[: len(buf) & ~1], dtype=np.int16).astype(np.float32) / 32768.0
                stream.push(np.ascontiguousarray(samples), RATE)
                for r in stream.drain():
                    text = r.text.strip()
                    if r.final:
                        last_partial = ""
                        if text:
                            self._on_final(text, r.languages[0] if r.languages else "")
                    elif text and text != last_partial:
                        last_partial = text
                        self.emit({"type": "partial", "text": text})
        except Exception as e:  # noqa: BLE001, a dead ASR thread must be reported, not silent
            log.exception("asr loop failed")
            self.emit({"type": "status", "state": "error", "detail": str(e)})
        finally:
            stream.close()
            if not self.stop_flag.is_set():
                self.emit({"type": "status", "state": "idle", "detail": "capture ended"})

    def _on_final(self, text: str, tagged: str) -> None:
        self.final_id += 1
        fid = self.final_id
        lang = self.resolve_lang(tagged, text)
        needs = bool(lang) and not lang.lower().startswith("en")
        self.emit({"type": "final", "id": fid, "text": text, "lang": lang, "needs_translation": needs})
        if needs:
            self.inflight += 1
            self.nmt_pool.submit(self._translate, fid, text, lang)

    def _translate(self, fid: int, text: str, lang: str) -> None:
        t = time.perf_counter()
        try:
            nmt = self.nmt
            if nmt is None:
                return
            out = nmt.translate([text], nmt_code(lang), "en")[0]
        except nemo_ffi.NemoError as e:
            log.error("nmt error (%s): %s", lang, e)
            return
        finally:
            self.inflight -= 1
        log.info("nmt %s %.2fs: %s", lang, time.perf_counter() - t, out)
        self.emit({"type": "translation", "id": fid, "text": out, "lang": lang})

    def close(self) -> None:
        self.closing.set()
        self.stop()
        self.nmt_pool.shutdown(wait=True)
        self.unload()


class Server:
    def __init__(self, engine: Engine, events: asyncio.Queue) -> None:
        self.engine = engine
        self.events = events
        self.clients: set[ServerConnection] = set()
        self.active_clients: set[ServerConnection] = set()

    async def broadcast(self) -> None:
        while True:
            msg = await self.events.get()
            data = json.dumps(msg, ensure_ascii=False)
            if msg.get("type") != "partial":
                log.info("event %s", data[:200])
            for ws in list(self.clients):
                try:
                    await ws.send(data)
                except websockets.ConnectionClosed:
                    self.clients.discard(ws)

    async def handler(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        state = "capturing" if self.engine.proc else "idle"
        await ws.send(json.dumps({"type": "status", "state": state, "detail": "connected"}))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = msg.get("type")
                if kind == "activate":
                    self.active_clients.add(ws)
                    await asyncio.to_thread(self.engine.start)
                elif kind == "deactivate":
                    self.active_clients.discard(ws)
                    if not self.active_clients:
                        await asyncio.to_thread(self.engine.stop)
                elif kind == "ping":
                    await ws.send(json.dumps({"type": "pong", "state": "capturing" if self.engine.proc else "idle"}))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            self.active_clients.discard(ws)
            if not self.active_clients and self.engine.proc:
                await asyncio.to_thread(self.engine.stop)


async def main_async(a: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    engine = Engine(loop, events, a.right_context, a.eou_ms, unload_grace=a.unload_grace, watch_interval=a.watch_interval, keep_loaded=a.keep_loaded)
    server = Server(engine, events)
    async with serve(server.handler, "127.0.0.1", a.port, max_size=1 << 20):
        log.info("listening on ws://127.0.0.1:%d", a.port)
        try:
            await server.broadcast()
        finally:
            engine.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--eou-ms", type=int, default=800)
    ap.add_argument("--right-context", type=int, default=1, help="rnnt right context frames: 1 low latency, -1 model max")
    ap.add_argument("--unload-grace", type=float, default=20.0, help="seconds Firefox must be absent before the models are unloaded")
    ap.add_argument("--watch-interval", type=float, default=5.0, help="seconds between checks for firefox.exe")
    ap.add_argument("--keep-loaded", action="store_true", help="never unload the models once loaded")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        asyncio.run(main_async(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
