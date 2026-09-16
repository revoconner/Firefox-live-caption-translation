# CLAUDE.md
This file describes the goal of this project along with guidelines as needed. Global rules and restrictions apply.

## Goal

The project aims to provide real time caption generation for audio content (playback not recording) for any video being played inside firefox. If the audio is **NOT** in English then the audio to text will be translated to English as well before being shown, in real time. All processing must happen locally and not using any external API.

No part of the project should be tied to Windows Live caption API. Other windows API can be used as needed.

The project is divided into two parts:
- A backend that runs in the background on Windows 11 24H2  26100 LTSC enterprise. (D:\My OpenSource\Firefox Extensions\ff-live-caption-translate\backend)
- A frontend webextension for Firefox that takes the data from backend and generates the user facing content. (D:\My OpenSource\Firefox Extensions\ff-live-caption-translate\extension)

If python is used to develop the backend the folder `D:\My OpenSource\Firefox Extensions\ff-live-caption-translate\capvenv` for venv will be used. Venv is already setup (Python 3.11.9, currently empty apart from pip). Dependencies are listed in `requirements.txt` at the project root. Rev installs packages, Claude does not.

### End goal

- While the backend may start with windows or user login, no processing will be done until activated for a specific tab inside firefox.
- The firefox caption generated must respect the video player position and size inside firefox and should be available only on the current playing video (not paused) with audio.
- UI of the caption should be adjustable from within the extension if possible.
- Tab activation model (decided 16 Sep 2026, revised same day): the toolbar button toggles captions for one tab, and the tab stays on until clicked again, across reloads and navigation. Several tabs can be on at once. Windows audio capture cannot isolate a single tab, so capture only runs for the one enabled tab that is in the foreground and has a playing video, and captions only go to that tab. Switching tabs hands the stream over with no clicks. This is what keeps a background tab playing music from stealing the captions of the video being watched.

## Hardware Specification
- **GPU**: RTX 4090 24GB Vram (driver 616.56)
- **CPU**: Threadripper 7960x 48 vCPU
- **Memory**: 128GB DDR5 Registered

## Software available
- Firefox Developer Edition (as of 16 sep 2026, the version is 157)
- web-ext.exe 10.6.0
- Python 3.11.9 (as needed)
    - Python 3.10.11 is also available (not in the venv) and is on path but shadowed by the 3.11.9 version. If needed it can be accessed by `runver 2 python [command]` such as `runver 2 python --version`
- gcc (MinGW-W64 x86_64-ucrt-mcf-seh) 15.1.0 (as needed)
- MSVC 2026 (not preferred)
- clang 21.1.7 (as needed)
- NASM version 3.02 (as needed)
- cmake 4.2.1
- CUDA Toolkit 12.6 (nvcc on path)
- NeMo-Speech.cpp 0.1.0 (see Runtime below)
- Rev has said any of these tools may be used as the task requires: C++, NASM, Python, or a mix.

## Architecture decision (16 Sep 2026)

Cascade, not a single end to end model: streaming ASR with automatic language detection, then text translation into English for non English utterances. Cascaded systems won the IWSLT 2026 simultaneous speech translation task, and no open model does streaming multilingual speech into English text on its own.

Inference runs in process through the NeMo-Speech.cpp **C ABI**, loaded with ctypes in `backend\nemo_ffi.py`. The `nemo-speech serve` HTTP path was built and tested first but abandoned: its realtime WebSocket does not report the detected language on finals, while the C ABI does (`nemo_speech_asr_result_language_code`). One process, no localhost hop for the models.

Pipeline:
1. Backend captures Firefox playback audio with WASAPI process loopback (ActivateAudioInterfaceAsync with AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, include mode on the root firefox.exe PID, which also captures its child processes). Available since Windows 10 build 20348, so fine on 24H2. Python has no maintained package for per process loopback, so this is a small native helper, `backend\native\proc_loopback.cpp`, built by its `build.ps1` with clang, that emits mono PCM16 at 16 kHz on stdout. PyAudioWPatch only does whole device loopback and is a fallback, not the plan.
2. Backend pushes float32 mono frames into a streaming recognizer with language `auto` and reads partials and finals with their detected language. Finals are sentence level and cut from the text, never the audio: a finished sentence followed by more speech is emitted at once, a finished sentence with nothing after it after 300 ms, and the runtime's own final at the next silence contributes only what was not emitted yet. Never use a forced endpoint for this, it cuts the audio mid word for a fast speaker.
3. For finals whose language is not English, a worker thread translates to English through the NMT translator. A final that stopped mid sentence (no sentence punctuation, or the model's own trailing ellipsis) is merged with the next one and re-translated as a whole sentence, replacing the fragment on screen. The in progress line is also translated live, throttled, with a stable prefix computed by agreement with the previous attempt.
4. Backend pushes caption events (partial, live_translation, final with optional replaces, translation) to the extension over a WebSocket on 127.0.0.1:8765. The extension sends `config` with the live translation toggle.
5. Extension overlays captions on the largest playing unmuted video of the streaming tab.

Run the backend with `capvenv\Scripts\python.exe backend\service.py`. Flags: `--port`, `--home`, `--device auto|gpu|cpu`, `--log-dir`, `--no-tray`, `--quit`, `--eou-ms`, `--right-context`, `--unload-grace`, `--watch-interval`, `--keep-loaded`. It shows a notification area icon (status, log folder, Quit) and writes a rotating log to `%LocalAppData%\LiveCaptionTranslate\logs\service.log`; a second instance exits quietly when the port is taken.

Model lifetime (decided 16 Sep 2026): the models are loaded while `firefox.exe` is running and unloaded after it has been gone for the grace period (20 s default), decided by a watcher thread polling the process list. Rev chose this over load on activate and unload on idle because the loaded but idle cost is only VRAM and reloading on every activation would add latency. An activate that arrives before the watcher has loaded loads the models itself. Unload waits for any in flight translation. Verified: 5.1 GB of VRAM taken while Firefox runs, released to about 0.4 GB (the CUDA context, freed only at process exit) after it closes, and reloaded within the poll interval when it returns.

## Models

### ASR: NVIDIA Nemotron 3.5 ASR Streaming 0.6B (chosen)
- Repo `nvidia/nemotron-3.5-asr-streaming-0.6b`, released on Hugging Face 4 Jun 2026, license OpenMDW 1.1 (commercial use allowed).
- Cache aware FastConformer RNNT, 600M parameters, language ID prompt conditioning, punctuation and capitalization built in.
- Chunk sizes 80 ms, 160 ms, 320 ms, 560 ms, 1120 ms selected at runtime. Accuracy improves with larger chunks.
- `language=auto` detects the spoken language and appends a `<xx-XX>` tag after the terminal punctuation, so no separate language ID model is needed.
- 40 locales in three tiers. Transcription ready (19): en-US en-GB es-US es-ES fr-FR fr-CA it-IT pt-BR pt-PT nl-NL de-DE tr-TR ru-RU ar-AR hi-IN ja-JP ko-KR vi-VN uk-UA. Broad coverage (13): pl-PL sv-SE cs-CZ nb-NO da-DK bg-BG fi-FI hr-HR sk-SK zh-CN hu-HU ro-RO et-EE. Adaptation ready (8, need fine tuning): el-GR lt-LT lv-LV mt-MT sl-SI he-IL th-TH nn-NO.
- Rev confirmed the transcription ready tier covers every language needed. Chinese is not a priority.
- Known issues: Mandarin quality is reported poor (NeMo Speech issue 15809). The NeMo Python toolkit is not needed and must not be installed, it fails to build on native Windows and has a Python 3.10 syntax bug (issues 15797, 15820). The Transformers 5.13 path exists but is unnecessary given the C++ runtime.
- The q8_0 GGUF used by the runtime is 742 MB and is pulled automatically on first use.

### Translation tier 1: NVIDIA Riva-Translate-4B-Instruct-v2 through nemo-speech NMT (default)
- Repo `nvidia/Riva-Translate-4B-Instruct-v2`, trained Nov 2025 to May 2026, NVIDIA Open Model License. 4B parameters, Mistral NeMo architecture, runs through the llama.cpp engine bundled inside nemo-speech.
- 37 languages, every pair has English on one side, which is exactly the project need. It covers all 31 non English Nemotron ready and broad coverage locales (nb-NO maps to `no`, fr-CA to `fr`) plus el lt lv sl th id zh-TW.
- Language pair tags are `xx-en`, for example `de-en`, `ja-en`, `pt-br-en`, `es-us-en`, `zh-cn-en`. The nemo-speech API accepts two codes (`source_language`, `target_language`) or the full tag.
- FLORES-101 average into English: sacreBLEU 37.76. Sentence level translation is the sweet spot, quality drops on very long inputs.
- GGUF: NVIDIA publishes no GGUF for v2. Options are a community q8_0 conversion (for example `darrowoflykos/Riva-Translate-4B-Instruct-v2-Q8_0-GGUF` or `shivnathtathe/nvidia_Riva-Translate-4B-Instruct-v2-GGUF`) or converting from a NeMo-Speech.cpp source checkout with `convert_model.py` and the pinned llama.cpp converter (needs git clone and pip installs, so Rev does that). q8_0 is about 4.5 GB and is the tested precision.
- Chosen as default because it runs inside the same server as ASR with zero Python ML dependencies. Rev originally picked Hy-MT2 with a fallback; Riva covers the fallback gap natively, and Hy-MT2 stays available as tier 2 below if translation quality turns out insufficient.

### Translation tier 2 (optional quality upgrade, not wired by default)
- Tencent Hy-MT2 1.8B or 7B: released 21 May 2026, Apache 2.0, 33 languages, official GGUF and FP8 builds, Transformers 5.6+. Dedicated translation model, 1.8B claims to beat commercial APIs. Misses sv nb da fi bg hr sk hu ro et.
- Google TranslateGemma 4B / 12B / 27B: released 15 Jan 2026, Gemma license (gated download), 55 languages, GGUF builds exist. Fallback for languages Hy-MT2 lacks.
- Preferred mechanism if this tier is enabled: a llama.cpp server (CUDA build) hosting the GGUF with its OpenAI compatible HTTP API, so the Python backend still needs no torch. Routing: Hy-MT2 first, TranslateGemma for its missing languages.

### Rejected or deferred
- Qwen3-ASR 1.7B / 0.6B (Jan 2026, Apache 2.0, 52 languages incl. 22 Chinese dialects): best option if Chinese becomes a priority, but streaming works only through vLLM which has no native Windows support. Deferred.
- Voxtral Mini 4B Realtime (Feb 2026, Apache 2.0, 13 languages): realtime path is vLLM only, llama.cpp support unfinished. Rejected.
- Canary 1B v2 (Aug 2025): direct speech translation for 25 European languages but offline only. Rejected.
- Whisper family: per the pitfalls section below.

## Runtime: NeMo-Speech.cpp 0.1.0

- Install prefix `C:\PortableProgs\nemo-speech-0.1.0`, symlinked into the project as `nemo-speech-0.1.0`. `bin` is on PATH for PowerShell. In Git Bash the command is not on PATH, use the full path `/c/PortableProgs/nemo-speech-0.1.0/bin/nemo-speech.exe`.
- `nemo-speech doctor` reports features: asr, backend_cuda, diarization, http, integrated_vad, model_pull, punctuation, realtime_websocket, speech_translation, translation, tts. Devices: RTX 4090 and CPU. curl is available for model pulls.
- Bundled docs live in `share\doc\nemo-speech\docs` (api.md, server.md, cli.md, sdk.md, asr\configuration.md, nmt\configuration.md, nmt\models.md, model-conversion.md, troubleshooting.md). Read these before guessing behaviour. Example YAML configs are in `share\nemo-speech\config`.
- Model cache: `%LOCALAPPDATA%\NeMoSpeech\models`, override with `NEMO_SPEECH_MODEL_DIR`. Pull ahead with `nemo-speech pull nemotron-3.5`.
- Stable C ABI headers in `include\nemo_speech` (asr.h, nmt.h, diar.h, tts.h) with import libs in `lib`, Apache 2.0. In process embedding from C++ is possible but the HTTP/WebSocket server is the planned integration.
- Text normalization (ITN) is not supported on Windows builds. Not needed for captions.

### Server usage
- Start: `nemo-speech serve --asr-model nemotron-3.5 --nmt-model <path to riva gguf> --port 8080`, or `--config <yaml>`. Binds 127.0.0.1 only. Use `--cors-origin` only if the extension talks to it directly (current plan is the Python backend talks to it, the extension talks to the backend).
- Settings precedence: built in defaults, then `--config` YAML, then `NEMO_SPEECH_<KEY>` env, then CLI. Unknown YAML keys are hard errors.
- Streaming latency knob: `asr.streaming.rnnt_right_context` (1 = low latency preset, -1 = model max, values map to the 80 ms to 1120 ms chunks). `asr.batching.enabled` defaults true in serve, disable it to minimize single stream latency.
- Endpointing: `asr.endpointing.enable` with `stop_history_eou_ms` (default 800) finalizes utterances on silence mid stream. Optional Silero VAD GGUF via `asr.vad.model_path` (must be converted, no index entry).
- WebSocket `/v1/realtime`: server sends `session.created`; client may send one `session.update` (fields: sample_rate, language, automatic_punctuation, verbatim, word_timestamps, endpointing_ms, speech_contexts) before audio; then binary little endian PCM16 frames; `input_audio_buffer.commit` to finish. Server events: `conversation.item.input_audio_transcription.delta` (partials), `.completed` (finals), `input_audio_buffer.committed`, `error`. This is not the OpenAI Realtime protocol.
- `POST /v1/translations` JSON body with `input` (string or array), `source_language`, `target_language`; response is a `translations` array of objects with `text`.
- `POST /v1/audio/translations` does ASR then NMT on a WAV upload (not streaming). Useful for tests only.
- `GET /ready` and `GET /health` for startup checks. `nemo-speech health` from the CLI.
- Cumulative audio on one realtime socket is capped by `--max-upload-mb` (512 MiB default, about 4.6 hours of 16 kHz PCM16). Reconnect periodically or on silence for long sessions.

### Verified on 16 Sep 2026
- The realtime WebSocket accepts `language: "auto"` but its finals carry only `transcript` and `audio_processed`, no language. The C ABI does expose the language, which is why the project uses it. See SCRATCHPAD.md.
- The model only appends its `<xx-XX>` language tag after terminal punctuation, so finals cut off mid sentence have no language. The backend compensates; see SCRATCHPAD.md.
- The NMT engine rejects some locale forms (`pt-PT` fails, `es-ES` works), so codes are normalized to bare ones before translating.
- Measured on the 4090 with `backend\tools\bench_resources.py`: ASR runs 28x realtime in process, the ASR model loads in 0.4 s and the translator in 2.0 s, and one sentence translates in about 290 ms. Compute is negligible either way; the real cost is the 5 GB of VRAM held while the models are loaded. Full numbers in SCRATCHPAD.md.

## Working notes
`SCRATCHPAD.md` at the project root holds bugs encountered, workarounds, measurements and other running notes. Write those there, not here. This file stays limited to goals, decisions and stable facts.

## Extension conventions
- **Bump the patch version in `extension\manifest.json` on every change to the extension** (0.1.2 becomes 0.1.3 and so on). Rev installs the built add-on as an update, which requires a higher version.
- Firefox clears per-tab badge text and title when a tab navigates, so `tabs.onUpdated` repaints them. Losing that is what once made a reload look like a deactivation.
- Firefox suspends the MV3 event page after about 30 s without an extension event, and an open WebSocket does not count as one. The streaming tab's content script sends a `keepalive` message every 10 s, and the enabled tab set is mirrored in `storage.session` so a restarted event page recovers.
- `tests\extension_background.test.mjs` drives `background.js` against a stubbed WebExtension API and asserts the capture state machine (tab handover, reload, pause debounce, event page restart). Run it with `node tests\extension_background.test.mjs` after touching the background script. It lives outside `extension\` so it is never packaged.
- Check the extension with `web-ext lint --source-dir extension`. One warning about `strict_min_version` and Firefox for Android is expected and irrelevant to this desktop project.
- User settings are on the options page in the add-on manager (`options.html`, `options.js`), never a toolbar popup, since the toolbar click toggles the tab. Defaults and normalization live in `settings.js`, shared by the options page and the content script. Add new settings there first.
- Display toggles (decided 16 Sep 2026): `showNativeLive`, default off, governs everything shown in the source language for non English speech (live line and untranslated sentences); `englishLive`, default on, governs the live line for English speech, which is styled like a finished caption. Finished English always shows. Rev chose this pair over per feature toggles because it matches how a viewer thinks.
- Extension icons are the `icon-*.png` files generated from Rev's `icon.ico`; regenerate with System.Drawing if the artwork changes (see SCRATCHPAD.md for the edge halo caveat).

Model files live in `backend\models`: `nemotron-3.5-asr-streaming-0.6b.q8_0.gguf` (ASR) and `riva-translate-4b-instruct-v2-q8_0.gguf` (NMT, community q8_0 conversion, verified runtime_compatible by `nemo-speech model info`). The `.nemo` archive there is not used by the runtime.

## Packaging (decided 16 Sep 2026, installer not written yet)
The installer covers the backend only: the PyInstaller build, the nemo-speech runtime DLLs and licenses, proc_loopback.exe, the two models, and an autostart entry. The extension is not part of it; Rev will publish it on addons.mozilla.org separately.

- Rev installed PyInstaller 6.22 into capvenv and chose it over the embeddable Python. The spec is `packaging\LiveCaptionTranslate.spec` (onedir, windowed, icon `backend\icon.ico`). `packaging\build.ps1` builds proc_loopback.exe if missing, runs PyInstaller into `packaging\dist\LiveCaptionTranslate` and then Inno Setup on `packaging\installer.iss` once that file exists. `packaging\build`, `dist` and `out` are git ignored.
- The dist folder is self contained and portable (decided by Rev 16 Sep 2026): the spec copies the nemo-speech `bin`, the two GGUFs into `models`, proc_loopback.exe into `native` and the runtime licenses into `licenses`, all inside `_internal`, so a zip of the dist folder runs anywhere and the installer ships the dist folder wholesale. About 5.1 GB.
- Application home: `_internal` in the build, the `backend` folder in the dev checkout. It holds `bin`, `models` and `native`. `LCT_HOME` or `--home` overrides it; a home without `bin` (the dev checkout) falls back to the developer install in `C:\PortableProgs`.
- `backend\tray.py` is the notification area icon, written against the Win32 API with ctypes so there is no pystray or Pillow dependency. Quit goes through the same path as `--quit`, which sends `{"type": "quit"}` over the WebSocket.
- `--device auto` retries model creation on the CPU when the GPU attempt fails. Rev considers this unnecessary for the target machine; it stays because it is a few lines. Untested on a machine without an NVIDIA card.
- Inno Setup 6 is installed on this machine (`C:\Program Files (x86)\Inno Setup 6\ISCC.exe`). The GGUFs go in with `Flags: nocompression`. Build notes and verification are in SCRATCHPAD.md.

## Python backend dependencies
Listed in `requirements.txt` at the project root. Kept minimal on purpose: websockets, httpx, numpy, psutil, pytest for tests. No torch, no NeMo toolkit. Anything ML related is served by nemo-speech or a llama.cpp server as a separate process.

## Pitfalls to avoid
- Avoid older models like Whisper which had bugs even in 2025.
- Search online for newer model specifications, documents and bugs before planning work.
- Do not install `nemo_toolkit`. It is not needed and breaks on native Windows.
- Do not plan on vLLM. It has no native Windows support.
- Firefox has no `tabCapture` API, so audio must come from the OS side.

## Git
Do not bother with git or commits, unless specifically asked for.
