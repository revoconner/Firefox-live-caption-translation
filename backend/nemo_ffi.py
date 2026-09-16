"""ctypes bindings for the NeMo-Speech.cpp stable C ABI (asr.h and nmt.h).

Only the parts this backend needs are exposed: a streaming recognizer with detected language codes on finals, and a batched text translator. Struct layouts mirror include/nemo_speech/*.h of nemo-speech 0.1.0; every config struct is size prefixed so newer runtimes default any fields we do not set.
"""
from __future__ import annotations

import ctypes as C
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BIN = Path(r"C:\PortableProgs\nemo-speech-0.1.0\bin")

_asr: C.CDLL | None = None
_nmt: C.CDLL | None = None


def load(bin_dir: Path = DEFAULT_BIN) -> None:
    global _asr, _nmt
    if _asr is not None:
        return
    os.add_dll_directory(str(bin_dir))
    _asr = C.CDLL(str(bin_dir / "nemo_speech_asr_c.dll"))
    _nmt = C.CDLL(str(bin_dir / "nemo_speech_nmt_c.dll"))
    _bind_asr(_asr)
    _bind_nmt(_nmt)


class AsrBackendConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("gpu", C.c_int32)]


class AsrModelConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("path", C.c_char_p), ("name", C.c_char_p)]


class AsrStreamingConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("chunk_size", C.c_float), ("ctc_left_padding", C.c_float), ("ctc_right_padding", C.c_float), ("rnnt_right_context", C.c_int32)]


class AsrDecoderConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("kind", C.c_int), ("flashlight_lm", C.c_char_p), ("flashlight_lexicon", C.c_char_p), ("flashlight_tokenizer", C.c_char_p), ("beam_size", C.c_int32), ("beam_size_token", C.c_int32), ("beam_threshold", C.c_double), ("lm_weight", C.c_double), ("word_insertion_score", C.c_double), ("max_boost", C.c_double)]


class AsrVadConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("model_path", C.c_char_p), ("enable_masking", C.c_bool), ("onset", C.c_float), ("offset", C.c_float)]


class AsrEndpointingConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("enable", C.c_bool), ("vad_based", C.c_bool), ("stop_history_eou_ms", C.c_int32)]


class AsrPostprocConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("profanity_list_path", C.c_char_p), ("itn_model_dir", C.c_char_p), ("pnc_model_path", C.c_char_p)]


class AsrDiarConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("model_path", C.c_char_p), ("chunk_frames", C.c_int32), ("right_context_frames", C.c_int32), ("left_context_frames", C.c_int32), ("fifo_frames", C.c_int32), ("spkcache_frames", C.c_int32), ("update_period_frames", C.c_int32)]


class AsrBatchingConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("enable", C.c_bool), ("max_batch_size", C.c_int32), ("max_queue_delay_us", C.c_int32), ("max_queue_depth", C.c_int32), ("ingress_cohort_delay_us", C.c_int32), ("state_arena_slots", C.c_int32)]


class AsrRecognizerConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("backend", C.POINTER(AsrBackendConfig)), ("model", C.POINTER(AsrModelConfig)), ("streaming", C.POINTER(AsrStreamingConfig)), ("decoder", C.POINTER(AsrDecoderConfig)), ("vad", C.POINTER(AsrVadConfig)), ("endpointing", C.POINTER(AsrEndpointingConfig)), ("postproc", C.POINTER(AsrPostprocConfig)), ("diar", C.POINTER(AsrDiarConfig)), ("batching", C.POINTER(AsrBatchingConfig))]


class AsrSpeechContext(C.Structure):
    _fields_ = [("size", C.c_size_t), ("phrases", C.POINTER(C.c_char_p)), ("phrase_count", C.c_size_t), ("boost", C.c_float)]


class AsrRecognitionOptions(C.Structure):
    _fields_ = [("size", C.c_size_t), ("request_id", C.c_char_p), ("language_code", C.c_char_p), ("interim_results", C.c_bool), ("enable_word_time_offsets", C.c_bool), ("enable_automatic_punctuation", C.c_bool), ("verbatim_transcripts", C.c_bool), ("profanity_filter", C.c_bool), ("stop_history_eou_ms", C.c_int32), ("speech_contexts", C.POINTER(AsrSpeechContext)), ("speech_context_count", C.c_size_t), ("max_alternatives", C.c_int32), ("enable_speaker_diarization", C.c_bool), ("max_speaker_count", C.c_int32)]


class NmtBackendConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("gpu", C.c_int32)]


class NmtModelConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("path", C.c_char_p), ("n_ctx", C.c_int32)]


class NmtGenerationConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("max_new_tokens", C.c_int32)]


class NmtPoolConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("contexts", C.c_int32)]


class NmtTranslatorConfig(C.Structure):
    _fields_ = [("size", C.c_size_t), ("backend", C.POINTER(NmtBackendConfig)), ("model", C.POINTER(NmtModelConfig)), ("generation", C.POINTER(NmtGenerationConfig)), ("pool", C.POINTER(NmtPoolConfig))]


def _bind_asr(lib: C.CDLL) -> None:
    P = C.c_void_p
    lib.nemo_speech_asr_recognition_options_default.restype = AsrRecognitionOptions
    lib.nemo_speech_asr_recognition_options_default.argtypes = []
    lib.nemo_speech_asr_create.restype = C.c_int
    lib.nemo_speech_asr_create.argtypes = [C.POINTER(AsrRecognizerConfig), C.POINTER(P)]
    lib.nemo_speech_asr_destroy.restype = None
    lib.nemo_speech_asr_destroy.argtypes = [P]
    lib.nemo_speech_asr_streaming_recognize.restype = C.c_int
    lib.nemo_speech_asr_streaming_recognize.argtypes = [P, C.POINTER(AsrRecognitionOptions), C.POINTER(P)]
    lib.nemo_speech_asr_stream_push_f32.restype = C.c_int
    lib.nemo_speech_asr_stream_push_f32.argtypes = [P, C.POINTER(C.c_float), C.c_size_t, C.c_int32]
    lib.nemo_speech_asr_stream_force_endpoint.restype = C.c_int
    lib.nemo_speech_asr_stream_force_endpoint.argtypes = [P]
    lib.nemo_speech_asr_stream_finish.restype = C.c_int
    lib.nemo_speech_asr_stream_finish.argtypes = [P]
    lib.nemo_speech_asr_stream_next.restype = C.c_int
    lib.nemo_speech_asr_stream_next.argtypes = [P, C.POINTER(P)]
    lib.nemo_speech_asr_stream_close.restype = None
    lib.nemo_speech_asr_stream_close.argtypes = [P]
    lib.nemo_speech_asr_result_is_final.restype = C.c_bool
    lib.nemo_speech_asr_result_is_final.argtypes = [P]
    lib.nemo_speech_asr_result_audio_processed.restype = C.c_float
    lib.nemo_speech_asr_result_audio_processed.argtypes = [P]
    lib.nemo_speech_asr_result_alternative_count.restype = C.c_size_t
    lib.nemo_speech_asr_result_alternative_count.argtypes = [P]
    lib.nemo_speech_asr_result_transcript.restype = C.c_char_p
    lib.nemo_speech_asr_result_transcript.argtypes = [P, C.c_size_t]
    lib.nemo_speech_asr_result_language_count.restype = C.c_size_t
    lib.nemo_speech_asr_result_language_count.argtypes = [P, C.c_size_t]
    lib.nemo_speech_asr_result_language_code.restype = C.c_char_p
    lib.nemo_speech_asr_result_language_code.argtypes = [P, C.c_size_t, C.c_size_t]
    lib.nemo_speech_asr_result_destroy.restype = None
    lib.nemo_speech_asr_result_destroy.argtypes = [P]
    lib.nemo_speech_asr_last_error.restype = C.c_char_p
    lib.nemo_speech_asr_last_error.argtypes = []
    lib.nemo_speech_asr_version.restype = C.c_char_p
    lib.nemo_speech_asr_version.argtypes = []


def _bind_nmt(lib: C.CDLL) -> None:
    P = C.c_void_p
    lib.nemo_speech_nmt_create.restype = C.c_int
    lib.nemo_speech_nmt_create.argtypes = [C.POINTER(NmtTranslatorConfig), C.POINTER(P)]
    lib.nemo_speech_nmt_destroy.restype = None
    lib.nemo_speech_nmt_destroy.argtypes = [P]
    lib.nemo_speech_nmt_translate.restype = C.c_int
    lib.nemo_speech_nmt_translate.argtypes = [P, C.POINTER(C.c_char_p), C.c_size_t, C.c_char_p, C.c_char_p, C.POINTER(P)]
    lib.nemo_speech_nmt_result_count.restype = C.c_size_t
    lib.nemo_speech_nmt_result_count.argtypes = [P]
    lib.nemo_speech_nmt_result_text.restype = C.c_char_p
    lib.nemo_speech_nmt_result_text.argtypes = [P, C.c_size_t]
    lib.nemo_speech_nmt_result_language.restype = C.c_char_p
    lib.nemo_speech_nmt_result_language.argtypes = [P, C.c_size_t]
    lib.nemo_speech_nmt_result_destroy.restype = None
    lib.nemo_speech_nmt_result_destroy.argtypes = [P]
    lib.nemo_speech_nmt_last_error.restype = C.c_char_p
    lib.nemo_speech_nmt_last_error.argtypes = []
    lib.nemo_speech_nmt_version.restype = C.c_char_p
    lib.nemo_speech_nmt_version.argtypes = []


class NemoError(RuntimeError):
    pass


def _s(b: bytes | None) -> str:
    return b.decode("utf-8", "replace") if b else ""


@dataclass
class AsrResult:
    final: bool
    text: str
    audio_processed: float
    languages: list[str]


class Recognizer:
    """One loaded ASR model. Create streams with stream()."""

    def __init__(self, model_path: Path, gpu: int = 0, rnnt_right_context: int = 1, endpointing: bool = True, eou_ms: int = 800) -> None:
        load()
        self._path = str(model_path).encode()
        self._backend = AsrBackendConfig(C.sizeof(AsrBackendConfig), gpu)
        self._model = AsrModelConfig(C.sizeof(AsrModelConfig), self._path, None)
        self._streaming = AsrStreamingConfig(C.sizeof(AsrStreamingConfig), 0.16, 1.92, 1.92, rnnt_right_context)
        self._endpointing = AsrEndpointingConfig(C.sizeof(AsrEndpointingConfig), endpointing, False, eou_ms)
        self._batching = AsrBatchingConfig(C.sizeof(AsrBatchingConfig), False, 0, 0, 0, 0, 0)
        cfg = AsrRecognizerConfig(C.sizeof(AsrRecognizerConfig), C.pointer(self._backend), C.pointer(self._model), C.pointer(self._streaming), None, None, C.pointer(self._endpointing), None, None, C.pointer(self._batching))
        self._cfg = cfg
        handle = C.c_void_p()
        if _asr.nemo_speech_asr_create(C.byref(cfg), C.byref(handle)) != 0:
            raise NemoError(_s(_asr.nemo_speech_asr_last_error()))
        self._h = handle

    def close(self) -> None:
        if self._h:
            _asr.nemo_speech_asr_destroy(self._h)
            self._h = None

    def stream(self, language: str = "auto", interim: bool = True, punctuation: bool = True) -> "Stream":
        opts = _asr.nemo_speech_asr_recognition_options_default()
        opts.language_code = language.encode() if language else None
        opts.interim_results = interim
        opts.enable_automatic_punctuation = punctuation
        handle = C.c_void_p()
        if _asr.nemo_speech_asr_streaming_recognize(self._h, C.byref(opts), C.byref(handle)) != 0:
            raise NemoError(_s(_asr.nemo_speech_asr_last_error()))
        return Stream(handle, opts)


class Stream:
    """Drive from a single thread: push(), then drain next() until it returns None."""

    def __init__(self, handle: C.c_void_p, opts: AsrRecognitionOptions) -> None:
        self._h = handle
        self._opts = opts  # keep the language string alive for the stream lifetime

    def push(self, samples_f32, sample_rate: int = 16000) -> None:
        """samples_f32: contiguous numpy float32 array of mono samples."""
        n = len(samples_f32)
        if n == 0:
            return
        ptr = samples_f32.ctypes.data_as(C.POINTER(C.c_float))
        if _asr.nemo_speech_asr_stream_push_f32(self._h, ptr, n, sample_rate) != 0:
            raise NemoError(_s(_asr.nemo_speech_asr_last_error()))

    def force_endpoint(self) -> None:
        _asr.nemo_speech_asr_stream_force_endpoint(self._h)

    def finish(self) -> None:
        if _asr.nemo_speech_asr_stream_finish(self._h) != 0:
            raise NemoError(_s(_asr.nemo_speech_asr_last_error()))

    def next(self) -> AsrResult | None:
        out = C.c_void_p()
        if _asr.nemo_speech_asr_stream_next(self._h, C.byref(out)) != 0:
            raise NemoError(_s(_asr.nemo_speech_asr_last_error()))
        if not out:
            return None
        try:
            final = _asr.nemo_speech_asr_result_is_final(out)
            processed = _asr.nemo_speech_asr_result_audio_processed(out)
            text = ""
            langs: list[str] = []
            if _asr.nemo_speech_asr_result_alternative_count(out) > 0:
                text = _s(_asr.nemo_speech_asr_result_transcript(out, 0))
                n = _asr.nemo_speech_asr_result_language_count(out, 0)
                langs = [_s(_asr.nemo_speech_asr_result_language_code(out, 0, i)) for i in range(n)]
            return AsrResult(final, text, processed, langs)
        finally:
            _asr.nemo_speech_asr_result_destroy(out)

    def drain(self) -> list[AsrResult]:
        results = []
        while (r := self.next()) is not None:
            results.append(r)
        return results

    def close(self) -> None:
        if self._h:
            _asr.nemo_speech_asr_stream_close(self._h)
            self._h = None


class Translator:
    def __init__(self, model_path: Path, gpu: int = 0, n_ctx: int = 1024, max_new_tokens: int = 256, contexts: int = 1) -> None:
        load()
        self._path = str(model_path).encode()
        self._backend = NmtBackendConfig(C.sizeof(NmtBackendConfig), gpu)
        self._model = NmtModelConfig(C.sizeof(NmtModelConfig), self._path, n_ctx)
        self._gen = NmtGenerationConfig(C.sizeof(NmtGenerationConfig), max_new_tokens)
        self._pool = NmtPoolConfig(C.sizeof(NmtPoolConfig), contexts)
        cfg = NmtTranslatorConfig(C.sizeof(NmtTranslatorConfig), C.pointer(self._backend), C.pointer(self._model), C.pointer(self._gen), C.pointer(self._pool))
        self._cfg = cfg
        handle = C.c_void_p()
        if _nmt.nemo_speech_nmt_create(C.byref(cfg), C.byref(handle)) != 0:
            raise NemoError(_s(_nmt.nemo_speech_nmt_last_error()))
        self._h = handle

    def translate(self, texts: list[str], source: str, target: str = "en") -> list[str]:
        arr = (C.c_char_p * len(texts))(*[t.encode() for t in texts])
        out = C.c_void_p()
        if _nmt.nemo_speech_nmt_translate(self._h, arr, len(texts), source.encode(), target.encode(), C.byref(out)) != 0:
            raise NemoError(_s(_nmt.nemo_speech_nmt_last_error()))
        try:
            n = _nmt.nemo_speech_nmt_result_count(out)
            return [_s(_nmt.nemo_speech_nmt_result_text(out, i)) for i in range(n)]
        finally:
            _nmt.nemo_speech_nmt_result_destroy(out)

    def close(self) -> None:
        if self._h:
            _nmt.nemo_speech_nmt_destroy(self._h)
            self._h = None


def versions() -> tuple[str, str]:
    load()
    return _s(_asr.nemo_speech_asr_version()), _s(_nmt.nemo_speech_nmt_version())
