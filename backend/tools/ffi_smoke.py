"""In-process smoke test: stream a WAV through the nemo-speech C ABI, print partials and finals with detected languages, translate non English finals.

Usage: python ffi_smoke.py FILE.wav [--realtime] [--no-nmt]
"""
import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nemo_ffi  # noqa: E402

MODELS = Path(__file__).resolve().parents[1] / "models"
ASR = MODELS / "nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
NMT = MODELS / "riva-translate-4b-instruct-v2-q8_0.gguf"
CHUNK_MS = 80


def load_f32(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype=np.float32)
    else:
        raise SystemExit(f"unsupported width {width}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32), rate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--no-nmt", action="store_true")
    ap.add_argument("--language", default="auto")
    a = ap.parse_args()

    print("versions:", nemo_ffi.versions())
    t = time.perf_counter()
    rec = nemo_ffi.Recognizer(ASR)
    print(f"asr loaded in {time.perf_counter() - t:.2f}s")
    nmt = None
    if not a.no_nmt:
        t = time.perf_counter()
        nmt = nemo_ffi.Translator(NMT)
        print(f"nmt loaded in {time.perf_counter() - t:.2f}s")

    audio, rate = load_f32(a.file)
    chunk = rate * CHUNK_MS // 1000
    print(f"file={a.file} rate={rate} duration={len(audio) / rate:.2f}s")
    st = rec.stream(language=a.language)
    t0 = time.perf_counter()
    finals = []
    for i in range(0, len(audio), chunk):
        st.push(audio[i:i + chunk], rate)
        for r in st.drain():
            el = time.perf_counter() - t0
            if r.final:
                print(f"[{el:7.3f}] FINAL lang={r.languages} processed={r.audio_processed:.2f}s: {r.text}")
                finals.append(r)
            else:
                print(f"[{el:7.3f}] partial: {r.text}")
        if a.realtime:
            target = t0 + (i // chunk + 1) * CHUNK_MS / 1000
            d = target - time.perf_counter()
            if d > 0:
                time.sleep(d)
    st.finish()
    for r in st.drain():
        el = time.perf_counter() - t0
        print(f"[{el:7.3f}] {'FINAL' if r.final else 'partial'} lang={r.languages} processed={r.audio_processed:.2f}s: {r.text}")
        if r.final:
            finals.append(r)
    st.close()
    print(f"stream done in {time.perf_counter() - t0:.2f}s")

    if nmt:
        for r in finals:
            if not r.text.strip():
                continue
            lang = r.languages[0] if r.languages else ""
            if lang.lower().startswith("en") or not lang:
                print(f"skip translate ({lang or 'no lang'}): {r.text}")
                continue
            t = time.perf_counter()
            out = nmt.translate([r.text], lang, "en")
            print(f"translated {lang} in {time.perf_counter() - t:.2f}s: {out[0]}")
        nmt.close()
    rec.close()


if __name__ == "__main__":
    main()
