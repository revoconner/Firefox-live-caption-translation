"""Stream a WAV file to the nemo-speech realtime WebSocket and print every event with timing.

Usage: python realtime_smoke.py FILE.wav [--language auto] [--realtime] [--url ws://127.0.0.1:8080/v1/realtime]

--realtime paces the audio at 1x so latency numbers are meaningful. Without it the file is pushed as fast as possible.
"""
import argparse
import asyncio
import json
import sys
import time
import wave

import numpy as np
import websockets

CHUNK_MS = 80


def load_pcm16_mono(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        samples = np.frombuffer(raw, dtype=np.int16)
    elif width == 4:
        samples = (np.frombuffer(raw, dtype=np.float32) * 32767).astype(np.int16)
    else:
        raise SystemExit(f"unsupported sample width {width}")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return samples.tobytes(), rate


async def run(path: str, language: str, realtime: bool, url: str, words: bool) -> None:
    pcm, rate = load_pcm16_mono(path)
    chunk_bytes = rate * CHUNK_MS // 1000 * 2
    total_s = len(pcm) / 2 / rate
    print(f"file={path} rate={rate} duration={total_s:.2f}s chunk={CHUNK_MS}ms language={language}")

    async with websockets.connect(url, max_size=None) as ws:
        t0 = time.perf_counter()

        async def reader():
            async for msg in ws:
                t = time.perf_counter() - t0
                if isinstance(msg, bytes):
                    print(f"[{t:7.3f}] binary {len(msg)} bytes")
                    continue
                ev = json.loads(msg)
                kind = ev.get("type")
                if kind in ("session.created", "session.updated"):
                    print(f"[{t:7.3f}] {kind}: {json.dumps(ev)[:400]}")
                elif kind == "conversation.item.input_audio_transcription.delta":
                    print(f"[{t:7.3f}] delta: {ev.get('delta')!r}")
                elif kind == "conversation.item.input_audio_transcription.completed":
                    print(f"[{t:7.3f}] FINAL: {json.dumps(ev, ensure_ascii=False)}")
                else:
                    print(f"[{t:7.3f}] {kind}: {json.dumps(ev, ensure_ascii=False)[:600]}")
                if kind == "input_audio_buffer.committed":
                    return

        reader_task = asyncio.create_task(reader())

        session = {"sample_rate": rate, "word_timestamps": words}
        if language:
            session["language"] = language
        await ws.send(json.dumps({"type": "session.update", "session": session}))

        sent = 0
        idx = 0
        while sent < len(pcm):
            await ws.send(pcm[sent:sent + chunk_bytes])
            sent += chunk_bytes
            idx += 1
            if realtime:
                target = t0 + idx * CHUNK_MS / 1000
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
        t_sent = time.perf_counter() - t0
        print(f"[{t_sent:7.3f}] all audio sent")
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        try:
            await asyncio.wait_for(reader_task, timeout=30)
        except asyncio.TimeoutError:
            print("timed out waiting for committed event")
        print(f"[{time.perf_counter() - t0:7.3f}] done")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--language", default="auto")
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--url", default="ws://127.0.0.1:8080/v1/realtime")
    ap.add_argument("--words", action="store_true", help="request word timestamps on finals")
    a = ap.parse_args()
    asyncio.run(run(a.file, a.language, a.realtime, a.url, a.words))


if __name__ == "__main__":
    sys.exit(main())
