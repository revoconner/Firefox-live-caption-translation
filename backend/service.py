"""Live caption backend: captures Firefox playback audio, runs streaming ASR with language detection, translates non English finals, and streams caption events to the extension over a local WebSocket.

Run: capvenv\\Scripts\\python.exe backend\\service.py [--port 8765] [--device auto|gpu|cpu] [--home DIR] [--no-tray] [--quit] (see --help for the rest)

The models are loaded while firefox.exe is running and unloaded after it has been gone for the grace period, so a backend started at login costs nothing until Firefox appears.

Protocol (JSON text frames, one object per frame):
  client -> server: {"type": "activate"} | {"type": "deactivate"} | {"type": "ping"} | {"type": "quit"}
  server -> client: {"type": "status", "state": "idle|capturing|error", "detail": str}
                    {"type": "partial", "text": str}
                    {"type": "final", "id": int, "text": str, "lang": str, "needs_translation": bool}
                    {"type": "translation", "id": int, "text": str, "lang": str}
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import psutil
import websockets
from websockets.asyncio.server import ServerConnection, serve

FROZEN = bool(getattr(sys, "frozen", False))  # running as the PyInstaller build
ROOT = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent  # _internal next to the exe, or the backend folder
sys.path.insert(0, str(ROOT))
import nemo_ffi  # noqa: E402
import tray  # noqa: E402

__version__ = "0.1.0"
log = logging.getLogger("captions")

# The application home holds bin (nemo-speech runtime), models and native (proc_loopback.exe). It is _internal in the PyInstaller build, or the backend folder in the dev checkout, and can be pointed elsewhere with LCT_HOME or --home. A home without bin falls back to the developer install of the runtime.
HOME = ROOT
MODELS = ROOT / "models"
ASR_MODEL = MODELS / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
NMT_MODEL = MODELS / "riva-translate-4b-instruct-v2-q8_0.gguf"
CAPTURE_EXE = ROOT / "native" / "proc_loopback.exe"


def set_home(home: Path) -> None:
    global HOME, MODELS, ASR_MODEL, NMT_MODEL, CAPTURE_EXE
    HOME = home.resolve()
    MODELS = HOME / "models"
    ASR_MODEL = MODELS / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
    NMT_MODEL = MODELS / "riva-translate-4b-instruct-v2-q8_0.gguf"
    CAPTURE_EXE = HOME / "native" / "proc_loopback.exe"


def default_log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "LiveCaptionTranslate" / "logs"
RATE = 16000
CHUNK_MS = 80
CHUNK_BYTES = RATE * CHUNK_MS // 1000 * 2
TERMINAL_PUNCT = ".?!。！？"  # sentence enders, Latin and CJK
FORCE_EOU_S = 0.3  # a partial ending in sentence punctuation and unchanged this long is cut early
MERGE_WINDOW_S = 12.0  # a final without terminal punctuation waits this long to be joined with the next
LIVE_MIN_INTERVAL_S = 0.4  # floor between two live translations of the in progress line
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "no", "jr", "sr", "prof", "inc", "ltd", "co", "e.g", "i.e", "sra", "sr", "hr", "fr", "bzw", "ca", "usw", "z.b"}
NO_SPACE_LANGS = ("ja", "zh", "th")


ELLIPSIS_CHAR = chr(0x2026)  # the single character ellipsis the model sometimes emits


def trailing_ellipsis(text: str) -> bool:
    return text.endswith("...") or text.endswith(ELLIPSIS_CHAR)


def ends_sentence(text: str) -> bool:
    """A trailing ellipsis is the model's own marker for speech that stopped mid sentence, so it does not count."""
    text = text.rstrip()
    return bool(text) and text[-1] in TERMINAL_PUNCT and not trailing_ellipsis(text)


def strip_ellipsis(text: str) -> str:
    text = text.rstrip()
    while trailing_ellipsis(text):
        text = text[:-1] if text.endswith(ELLIPSIS_CHAR) else text[:-3]
        text = text.rstrip()
    return text


def looks_complete(text: str) -> bool:
    """Whether a partial ending in sentence punctuation is safe to cut: not an abbreviation, not a number, not a two word stub."""
    if not ends_sentence(text):
        return False
    body = text[:-1].rstrip()
    if not body or body[-1].isdigit():
        return False
    if script_family(text) in ("ja", "zh", "ko", "th"):
        return len(body) >= 4
    words = body.split()
    if len(words) < 3:
        return False
    last = words[-1].strip("\"'()[]").lower().rstrip(".")
    return len(last) > 1 and last not in ABBREVIATIONS


def split_complete_sentences(text: str) -> tuple[list[str], str]:
    """Split off every sentence that is finished and already followed by more speech. Returns the finished sentences and the remainder still in progress."""
    done: list[str] = []
    rest = text
    while True:
        cut = -1
        for i, ch in enumerate(rest):
            if ch not in TERMINAL_PUNCT:
                continue
            head = rest[: i + 1]
            after = rest[i + 1:]
            if trailing_ellipsis(head) or (i + 1 < len(rest) and rest[i + 1] in TERMINAL_PUNCT):
                continue  # inside an ellipsis or a run of punctuation like ?!
            if not after.strip():
                break  # nothing spoken after it yet, the pause rule handles this case
            cjk = script_family(head) in ("ja", "zh", "ko", "th")
            if not cjk and not after[0].isspace():
                continue  # decimal point or similar
            if looks_complete(head):
                cut = i + 1
                break
        if cut < 0:
            return done, rest
        done.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()


def common_prefix_len(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def join_fragments(a: str, b: str, lang: str) -> str:
    a = strip_ellipsis(a)
    if nmt_code(lang) in NO_SPACE_LANGS:
        return a + b
    if b[:1].isupper() and not b[:2].isupper():  # the model capitalises every final; only a sentence start should be
        b = b[0].lower() + b[1:]
    return a + " " + b


def common_word_prefix(a: str, b: str) -> str:
    aw, bw = a.split(), b.split()
    n = 0
    while n < min(len(aw), len(bw)) and aw[n] == bw[n]:
        n += 1
    return " ".join(aw[:n])


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

    def __init__(self, loop: asyncio.AbstractEventLoop, events: asyncio.Queue, right_context: int, eou_ms: int, unload_grace: float = 20.0, watch_interval: float = 5.0, keep_loaded: bool = False, device: str = "auto") -> None:
        self.loop = loop
        self.events = events
        self.right_context = right_context
        self.eou_ms = eou_ms
        self.device = device  # auto, gpu or cpu
        self.on_status = None  # optional callback receiving a one line state for the tray tooltip
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
        # Live translation of the in progress line: newest partial wins, one worker, rate limited.
        self.live_translation = True
        self.live_cv = threading.Condition()
        self.live_pending: tuple[int, str, str] | None = None  # (utterance sequence, text, lang)
        self.live_prev = ""  # previous live output for the current utterance, for the stable prefix
        self.utt_seq = 0
        self.open_fragment: dict | None = None  # a final without terminal punctuation waiting to be merged
        self.stream_active = False  # a recognition stream exists; unloading the models under it would crash the runtime
        threading.Thread(target=self._watch_firefox, name="firefox-watch", daemon=True).start()
        threading.Thread(target=self._live_worker, name="live-nmt", daemon=True).start()

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
        nemo_ffi.load(nemo_ffi.resolve_bin(HOME))
        rec = self._create(lambda gpu: nemo_ffi.Recognizer(ASR_MODEL, gpu=gpu, rnnt_right_context=self.right_context, endpointing=True, eou_ms=self.eou_ms))
        self.nmt = self._create(lambda gpu: nemo_ffi.Translator(NMT_MODEL, gpu=gpu))
        self.rec = rec  # assigned last so `loaded` only turns true once both models are in
        log.info("models loaded in %.1fs", time.perf_counter() - t)

    def _create(self, factory):
        """Build a model on the chosen device. With auto, a GPU failure (no NVIDIA card or driver, no free VRAM) retries on the CPU."""
        if self.device == "cpu":
            return factory(-1)
        try:
            return factory(0)
        except nemo_ffi.NemoError as e:
            if self.device != "auto":
                raise
            log.warning("gpu model creation failed (%s), retrying on cpu", e)
            return factory(-1)
        self.emit({"type": "status", "state": "idle", "detail": "models loaded"})

    def _unload(self) -> bool:
        if self.rec is None:
            return True
        if self.proc is not None or self.inflight or self.stream_active:
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

    def infer_lang(self, text: str) -> str:
        """Best guess for untagged text: the last tagged language when the script agrees, else the script's default language, else unknown."""
        fam = script_family(text)
        if not fam:
            return ""
        last_fam = lang_family(self.last_lang) if self.last_lang else ""
        if last_fam == fam or (fam == "zh" and last_fam == "ja"):
            return self.last_lang
        return FAMILY_DEFAULT.get(fam, "")

    def resolve_lang(self, tagged: str, text: str) -> str:
        """The model only emits its language tag after terminal punctuation, so untagged finals fall back to infer_lang."""
        if tagged:
            self.last_lang = tagged
            return tagged
        return self.infer_lang(text)

    def emit(self, msg: dict) -> None:
        self.loop.call_soon_threadsafe(self.events.put_nowait, msg)
        if msg.get("type") == "status" and self.on_status:
            self.on_status(f'{msg["state"]}: {msg.get("detail", "")}')

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
        self.proc = subprocess.Popen([str(CAPTURE_EXE), "--pid", str(pid), "--rate", str(RATE)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW)
        self.stop_flag.clear()
        self.last_lang = ""
        self.open_fragment = None
        self._end_utterance()
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

        def read_exact(n: int) -> bytes:
            parts = bytearray()
            while len(parts) < n:
                piece = proc.stdout.read(n - len(parts))
                if not piece:
                    break
                parts += piece
            return bytes(parts)

        try:
            self.run_stream(read_exact)
        except Exception as e:  # noqa: BLE001, a dead ASR thread must be reported, not silent
            log.exception("asr loop failed")
            self.emit({"type": "status", "state": "error", "detail": str(e)})
        finally:
            if not self.stop_flag.is_set():
                self.emit({"type": "status", "state": "idle", "detail": "capture ended"})

    def run_stream(self, read_exact) -> None:
        """Feed PCM16 chunks from read_exact into one recognition stream until it returns short. Separate from the capture process so tests can drive it from a file."""
        rec = self.rec
        assert rec is not None
        self.stream_active = True
        stream = rec.stream(language="auto", interim=True)
        # Sentence level finals are cut from the text, not the audio: the recogniser runs on undisturbed while finished
        # sentences are emitted as finals as soon as the next word appears (or after a short pause), and the runtime's
        # own final at the next silence only contributes whatever was not emitted yet. Tokens are never retracted, so
        # the committed text stays a prefix of what the runtime reports.
        committed = ""
        last_rest = ""
        rest_since = 0.0
        try:
            while not self.stop_flag.is_set():
                buf = read_exact(CHUNK_BYTES)
                if len(buf) < 2:
                    break
                samples = np.frombuffer(buf[: len(buf) & ~1], dtype=np.int16).astype(np.float32) / 32768.0
                stream.push(np.ascontiguousarray(samples), RATE)
                for r in stream.drain():
                    text = r.text.strip()
                    tagged = r.languages[0] if r.languages else ""
                    if r.final:
                        rest = self._remainder(committed, text)
                        committed = ""
                        last_rest = ""
                        self._end_utterance()
                        if rest:
                            self._on_final(rest, tagged)
                        elif tagged:
                            self.last_lang = tagged
                        continue
                    if not text:
                        continue
                    rest = self._remainder(committed, text)
                    if self.infer_lang(rest):  # unknown language means wait for the runtime's tagged final
                        done, rest = split_complete_sentences(rest)
                        for sentence in done:
                            committed = text[: text.index(sentence, len(committed)) + len(sentence)]
                            self._end_utterance()
                            self._on_final(sentence, "")
                            last_rest = ""
                    if rest != last_rest:
                        last_rest = rest
                        rest_since = time.monotonic()
                        if rest:
                            self._on_partial(rest)
                # A finished sentence with nothing after it yet becomes a final after a short pause rather than after the full silence threshold.
                if last_rest and looks_complete(last_rest) and self.infer_lang(last_rest) and time.monotonic() - rest_since >= FORCE_EOU_S:
                    committed = committed + (" " if committed else "") + last_rest if committed else last_rest
                    self._end_utterance()
                    self._on_final(last_rest, "")
                    last_rest = ""
        finally:
            stream.close()
            self.stream_active = False

    @staticmethod
    def _remainder(committed: str, text: str) -> str:
        """The part of the runtime's text that has not been emitted as a sentence final yet."""
        if not committed:
            return text
        if text.startswith(committed):
            return text[len(committed):].strip()
        n = common_prefix_len(committed, text)
        if n >= len(committed) - 3:  # the runtime touched up the tail, for example added an ellipsis
            return text[len(committed):].strip()
        log.warning("runtime revised committed text, showing the divergent tail: %r vs %r", committed[:40], text[:40])
        return text[n:].strip()

    def _end_utterance(self) -> None:
        with self.live_cv:
            self.utt_seq += 1
            self.live_pending = None
            self.live_prev = ""

    def _on_partial(self, text: str) -> None:
        lang = self.infer_lang(text)
        self.emit({"type": "partial", "text": text, "lang": lang})
        if not self.live_translation or self.nmt is None:
            return
        if not lang or lang.lower().startswith("en"):
            return
        with self.live_cv:
            self.live_pending = (self.utt_seq, text, lang)
            self.live_cv.notify()

    def _live_worker(self) -> None:
        """Translates the newest in progress line, at most every LIVE_MIN_INTERVAL_S, and reports which leading words agree with the previous attempt."""
        last_run = 0.0
        while not self.closing.is_set():
            with self.live_cv:
                while self.live_pending is None and not self.closing.is_set():
                    self.live_cv.wait(1.0)
                if self.closing.is_set():
                    return
                seq, text, lang = self.live_pending
                self.live_pending = None
            wait = LIVE_MIN_INTERVAL_S - (time.monotonic() - last_run)
            if wait > 0:
                time.sleep(wait)
                with self.live_cv:  # a newer partial may have arrived while waiting
                    if self.live_pending is not None and self.live_pending[0] == seq:
                        seq, text, lang = self.live_pending
                        self.live_pending = None
            nmt = self.nmt
            if nmt is None or seq != self.utt_seq:
                continue
            last_run = time.monotonic()
            try:
                out = nmt.translate([text], nmt_code(lang), "en")[0]
            except nemo_ffi.NemoError as e:
                log.error("live nmt error (%s): %s", lang, e)
                continue
            with self.live_cv:
                if seq != self.utt_seq:
                    continue
                stable = common_word_prefix(self.live_prev, out)
                self.live_prev = out
            self.emit({"type": "live_translation", "text": out, "stable": stable, "lang": lang})

    def _on_final(self, text: str, tagged: str) -> None:
        self.final_id += 1
        fid = self.final_id
        lang = self.resolve_lang(tagged, text)
        needs = bool(lang) and not lang.lower().startswith("en")
        replaces: list[int] = []
        frag = self.open_fragment
        self.open_fragment = None
        if frag and needs and frag["lang"] == lang and time.monotonic() - frag["t"] <= MERGE_WINDOW_S:
            # The previous final stopped mid sentence; translate the whole sentence and let this caption replace it.
            text = join_fragments(frag["text"], text, lang)
            replaces = [frag["id"]]
        self.emit({"type": "final", "id": fid, "text": text, "lang": lang, "needs_translation": needs, "replaces": replaces})
        if needs:
            if not ends_sentence(text) and not replaces:  # one merge at most, so a rambling speaker cannot defer forever
                self.open_fragment = {"id": fid, "text": text, "lang": lang, "t": time.monotonic()}
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
    def __init__(self, engine: Engine, events: asyncio.Queue, stop: asyncio.Event) -> None:
        self.engine = engine
        self.events = events
        self.stop = stop
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
                elif kind == "config":
                    if "live_translation" in msg:
                        self.engine.live_translation = bool(msg["live_translation"])
                        log.info("live translation %s", "on" if self.engine.live_translation else "off")
                elif kind == "quit":  # only local processes can connect, so this is the --quit flag or a tool
                    log.info("quit requested by a client")
                    self.stop.set()
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
    stop = asyncio.Event()
    engine = Engine(loop, events, a.right_context, a.eou_ms, unload_grace=a.unload_grace, watch_interval=a.watch_interval, keep_loaded=a.keep_loaded, device=a.device)
    server = Server(engine, events, stop)
    try:
        ws_server = await serve(server.handler, "127.0.0.1", a.port, max_size=1 << 20)
    except OSError as e:
        log.info("port %d is taken, another instance is probably running: %s", a.port, e)
        engine.close()
        return
    icon = None
    if not a.no_tray:
        icon_path = ROOT / "icon.ico"
        icon = tray.TrayIcon(f"Live Caption Translate {__version__}", icon_path, on_quit=lambda: loop.call_soon_threadsafe(stop.set), log_dir=a.log_dir)
        engine.on_status = icon.set_status
        icon.start()
        icon.set_status("waiting for firefox")
    log.info("listening on ws://127.0.0.1:%d", a.port)
    pump = asyncio.create_task(server.broadcast())
    try:
        await stop.wait()
    finally:
        pump.cancel()
        ws_server.close()
        try:
            await asyncio.wait_for(ws_server.wait_closed(), 5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        engine.close()
        if icon:
            icon.stop()
        log.info("stopped")


def send_quit(port: int) -> int:
    """Ask a running instance to exit. Returns 0 when it accepted, 1 when nothing was listening."""
    from websockets.sync.client import connect
    try:
        with connect(f"ws://127.0.0.1:{port}", open_timeout=3) as ws:
            ws.send(json.dumps({"type": "quit"}))
    except (OSError, websockets.exceptions.WebSocketException) as e:
        log.info("no instance on port %d: %s", port, e)
        return 1
    log.info("quit sent to port %d", port)
    return 0


_mutex = None


def hold_mutex() -> None:
    """A named mutex held for the process lifetime. The installer polls it to know when a running instance has exited."""
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _mutex = kernel32.CreateMutexW(None, False, "LiveCaptionTranslate")


def setup_logging(log_dir: Path) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    if sys.stderr is not None:  # a windowed exe has no console
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        root.addHandler(h)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "service.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        log.warning("file logging disabled: %s", e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version", version=f"LiveCaptionTranslate {__version__}")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("LCT_HOME", str(ROOT))), help="folder holding bin, models and native (default: LCT_HOME or the program folder)")
    ap.add_argument("--device", choices=("auto", "gpu", "cpu"), default="auto", help="auto tries the GPU and falls back to the CPU")
    ap.add_argument("--log-dir", type=Path, default=default_log_dir(), help="rotating service.log location")
    ap.add_argument("--no-tray", action="store_true", help="run without the notification area icon")
    ap.add_argument("--quit", action="store_true", help="tell the running instance on --port to exit, then exit")
    ap.add_argument("--eou-ms", type=int, default=800)
    ap.add_argument("--right-context", type=int, default=1, help="rnnt right context frames: 1 low latency, -1 model max")
    ap.add_argument("--unload-grace", type=float, default=20.0, help="seconds Firefox must be absent before the models are unloaded")
    ap.add_argument("--watch-interval", type=float, default=5.0, help="seconds between checks for firefox.exe")
    ap.add_argument("--keep-loaded", action="store_true", help="never unload the models once loaded")
    a = ap.parse_args()
    if a.quit:  # stays out of the log file the running instance is writing
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr or open(os.devnull, "w"))
        sys.exit(send_quit(a.port))
    setup_logging(a.log_dir)
    hold_mutex()
    set_home(a.home)
    log.info("LiveCaptionTranslate %s, home %s, runtime %s, device %s", __version__, HOME, nemo_ffi.resolve_bin(HOME), a.device)
    try:
        asyncio.run(main_async(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
