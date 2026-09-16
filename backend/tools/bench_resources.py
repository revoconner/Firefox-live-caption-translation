"""Measure what the backend costs while idle versus while transcribing and translating.

Runs the real engines through nemo_ffi in defined phases and samples process and GPU counters throughout,
so idle cost, active cost and the cost of merely having the models loaded can be told apart.

Usage:
  capvenv\\Scripts\\python.exe backend\\tools\\bench_resources.py [--device gpu|cpu] [--clip FILE] [--short]

Nothing else should be using the GPU heavily while this runs, since VRAM and GPU utilization are only
readable system wide on Windows (WDDM hides per process VRAM from nvidia-smi).
"""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nemo_ffi  # noqa: E402

MODELS = Path(__file__).resolve().parents[1] / "models"
ASR = MODELS / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
NMT = MODELS / "riva-translate-4b-instruct-v2-q8_0.gguf"
CHUNK_MS = 80
NCPU = psutil.cpu_count()


def gpu_query() -> dict[str, float]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True).stdout.strip().splitlines()[0]
        util, mem, power, temp = [float(x.strip()) for x in out.split(",")]
        return {"gpu_util": util, "vram_mb": mem, "power_w": power, "temp_c": temp}
    except Exception:
        return {}


@dataclass
class Phase:
    name: str
    note: str = ""
    proc: list[tuple[float, float, float, int]] = field(default_factory=list)  # rss, uss, cpu_pct, threads
    gpu: list[dict[str, float]] = field(default_factory=list)
    extra: dict[str, float] = field(default_factory=dict)


class Sampler(threading.Thread):
    """Samples this process every 250 ms and the GPU every second, tagging samples with the active phase."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.me = psutil.Process()
        self.me.cpu_percent(None)
        self.phase: Phase | None = None
        self.stop_flag = threading.Event()

    def run(self) -> None:
        last_gpu = 0.0
        while not self.stop_flag.wait(0.25):
            p = self.phase
            if p is None:
                self.me.cpu_percent(None)
                continue
            try:
                mi = self.me.memory_full_info()
                cpu = self.me.cpu_percent(None)
                p.proc.append((mi.rss / 1e6, getattr(mi, "uss", mi.rss) / 1e6, cpu, self.me.num_threads()))
            except psutil.Error:
                pass
            now = time.monotonic()
            if now - last_gpu >= 1.0:
                last_gpu = now
                g = gpu_query()
                if g:
                    p.gpu.append(g)

    def begin(self, phase: Phase, settle: float = 1.0) -> Phase:
        self.phase = None
        time.sleep(settle)  # let the previous phase's work drain before attributing samples
        self.me.cpu_percent(None)
        self.phase = phase
        return phase

    def end(self) -> None:
        self.phase = None


def load_f32(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype=np.float32)
    else:
        raise SystemExit(f"unsupported sample width {width}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32), rate


def stream_realtime(rec, audio: np.ndarray, rate: int, seconds: float, translator=None) -> dict[str, float]:
    """Feed looping audio at 1x for `seconds`, mimicking the live service, and count the work done."""
    chunk = rate * CHUNK_MS // 1000
    st = rec.stream(language="auto", interim=True)
    pool = ThreadPoolExecutor(max_workers=1) if translator else None
    counts = {"partials": 0, "finals": 0, "translations": 0}
    nmt_times: list[float] = []

    def translate(text: str, lang: str) -> None:
        t = time.perf_counter()
        try:
            translator.translate([text], nmt_code(lang), "en")
        except nemo_ffi.NemoError:
            return
        nmt_times.append(time.perf_counter() - t)
        counts["translations"] += 1

    t0 = time.monotonic()
    i = 0
    n = 0
    try:
        while time.monotonic() - t0 < seconds:
            start = (i * chunk) % len(audio)
            block = audio[start:start + chunk]
            if len(block) < chunk:
                block = np.ascontiguousarray(np.concatenate([block, audio[: chunk - len(block)]]))
            st.push(block, rate)
            for r in st.drain():
                if r.final:
                    if r.text.strip():
                        counts["finals"] += 1
                        lang = r.languages[0] if r.languages else ""
                        if pool and lang and not lang.lower().startswith("en"):
                            pool.submit(translate, r.text, lang)
                else:
                    counts["partials"] += 1
            i += 1
            n += len(block)
            target = t0 + i * CHUNK_MS / 1000
            d = target - time.monotonic()
            if d > 0:
                time.sleep(d)
    finally:
        st.close()
        if pool:
            pool.shutdown(wait=True)
    out = {**counts, "audio_s": n / rate}
    if nmt_times:
        out["nmt_mean_ms"] = statistics.mean(nmt_times) * 1000
        out["nmt_max_ms"] = max(nmt_times) * 1000
    return out


def nmt_code(lang: str) -> str:
    low = lang.lower()
    if low.startswith("zh"):
        return "zh-tw" if low.endswith("tw") else "zh"
    if low in ("nb-no", "nn-no", "nb", "nn"):
        return "no"
    return low.split("-")[0]


def measure_rtf(rec, audio: np.ndarray, rate: int) -> float:
    """Unpaced throughput: how many seconds of audio the recognizer handles per wall second."""
    chunk = rate * CHUNK_MS // 1000
    st = rec.stream(language="auto", interim=True)
    t0 = time.perf_counter()
    for i in range(0, len(audio), chunk):
        st.push(np.ascontiguousarray(audio[i:i + chunk]), rate)
        st.drain()
    st.finish()
    st.drain()
    st.close()
    return (len(audio) / rate) / (time.perf_counter() - t0)


def summarize(p: Phase) -> dict[str, float]:
    if not p.proc:
        return {}
    rss = [s[0] for s in p.proc]
    uss = [s[1] for s in p.proc]
    cpu = [s[2] for s in p.proc]
    thr = [s[3] for s in p.proc]
    out = {
        "samples": len(p.proc),
        "rss_mb": statistics.median(rss),
        "uss_mb": statistics.median(uss),
        "cpu_cores": statistics.median(cpu) / 100,
        "cpu_cores_max": max(cpu) / 100,
        "threads": max(thr),
    }
    if p.gpu:
        out["gpu_util"] = statistics.median(g["gpu_util"] for g in p.gpu)
        out["gpu_util_max"] = max(g["gpu_util"] for g in p.gpu)
        out["vram_mb"] = statistics.median(g["vram_mb"] for g in p.gpu)
        out["power_w"] = statistics.median(g["power_w"] for g in p.gpu)
        out["temp_c"] = statistics.median(g["temp_c"] for g in p.gpu)
    return out


def row(name: str, s: dict[str, float], base_vram: float | None) -> str:
    if not s:
        return f"{name:<26} no samples"
    vram = ""
    if "vram_mb" in s:
        d = s["vram_mb"] - base_vram if base_vram is not None else 0.0
        vram = f"{s['vram_mb']:>8.0f} {d:>+8.0f}"
    gpu = f"{s.get('gpu_util', 0):>5.0f} {s.get('gpu_util_max', 0):>5.0f}" if "gpu_util" in s else " " * 11
    pw = f"{s.get('power_w', 0):>7.0f}" if "power_w" in s else " " * 7
    return (f"{name:<26} {s['rss_mb']:>8.0f} {s['uss_mb']:>8.0f} "
            f"{s['cpu_cores']:>6.2f} {s['cpu_cores_max']:>6.2f} {s['threads']:>4.0f} {vram} {gpu} {pw}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", choices=["gpu", "cpu"], default="gpu")
    ap.add_argument("--clip", default=None, help="non English WAV used for the transcribe plus translate phase")
    ap.add_argument("--en-clip", default=None, help="English WAV used for the transcribe only phase")
    ap.add_argument("--short", action="store_true", help="shorter phases for a quick check")
    ap.add_argument("--no-nmt", action="store_true", help="skip the translator entirely")
    ap.add_argument("--cores", type=int, default=0, help="pin to N cores to approximate a weaker machine")
    a = ap.parse_args()

    if a.cores:
        psutil.Process().cpu_affinity(list(range(a.cores)))
        print(f"pinned to {a.cores} cores")

    gpu = 0 if a.device == "gpu" else -1
    idle_s = 6 if a.short else 12
    active_s = 10 if a.short else 25

    print(f"device={a.device} cpus={NCPU} ram={psutil.virtual_memory().total / 1e9:.0f}GB")
    print("rss/uss in MB, cpu in cores (1.00 = one core fully busy), vram in MB with delta vs no models\n")

    sampler = Sampler()
    sampler.start()
    phases: list[Phase] = []
    extras: dict[str, dict] = {}

    p = sampler.begin(Phase("1 process only"), settle=0.5)
    time.sleep(4)
    phases.append(p)
    base = summarize(p)
    base_vram = base.get("vram_mb")

    t = time.perf_counter()
    rec = nemo_ffi.Recognizer(ASR, gpu=gpu)
    asr_load = time.perf_counter() - t
    p = sampler.begin(Phase("2 ASR loaded, idle"))
    time.sleep(idle_s)
    phases.append(p)

    nmt = None
    nmt_load = 0.0
    if not a.no_nmt:
        t = time.perf_counter()
        nmt = nemo_ffi.Translator(NMT, gpu=gpu)
        nmt_load = time.perf_counter() - t
        p = sampler.begin(Phase("3 ASR+NMT loaded, idle"))
        time.sleep(idle_s)
        phases.append(p)

    en = Path(a.en_clip) if a.en_clip else None
    es = Path(a.clip) if a.clip else None

    if en and en.exists():
        audio, rate = load_f32(en)
        p = sampler.begin(Phase("4 transcribing English"))
        extras["4 transcribing English"] = stream_realtime(rec, audio, rate, active_s)
        sampler.end()
        phases.append(p)

    if es and es.exists():
        audio, rate = load_f32(es)
        p = sampler.begin(Phase("5 transcribe + translate"))
        extras["5 transcribe + translate"] = stream_realtime(rec, audio, rate, active_s, translator=nmt)
        sampler.end()
        phases.append(p)
        rtf = measure_rtf(rec, audio, rate)
    else:
        rtf = None

    sampler.stop_flag.set()

    hdr = (f"{'phase':<26} {'rss':>8} {'uss':>8} {'cpu':>6} {'peak':>6} {'thr':>4} "
           f"{'vram':>8} {'delta':>8} {'gpu%':>5} {'peak':>5} {'watt':>7}")
    print(hdr)
    print("-" * len(hdr))
    for p in phases:
        print(row(p.name, summarize(p), base_vram))
    print()
    print(f"load time: asr {asr_load:.2f}s" + (f", nmt {nmt_load:.2f}s" if nmt else ""))
    if rtf:
        print(f"asr throughput: {rtf:.0f}x realtime unpaced (one stream)")
    for name, e in extras.items():
        bits = ", ".join(f"{k}={v:.0f}" if isinstance(v, float) else f"{k}={v}" for k, v in e.items())
        print(f"{name}: {bits}")

    if nmt:
        nmt.close()
    rec.close()


if __name__ == "__main__":
    main()
