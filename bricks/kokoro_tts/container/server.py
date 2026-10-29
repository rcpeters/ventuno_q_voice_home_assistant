"""Kokoro-82M ONNX TTS HTTP server — the body of the `kokoro_tts` brick.

Endpoints
---------
GET  /health                       -> {"status": "ok"|"loading", "loaded": bool}
GET  /voices                       -> {"voices": [...]}
POST /warmup                       -> 202, kicks background load if not loaded
POST /speak       {text, voice?, speed?, lang?}
                                   -> 200 audio/wav (full WAV bytes)
POST /speak_stream {text, ...}     -> 200 application/x-ndjson, one JSON
                                      object per chunk:
                                        {seq, last, text, wav_b64,
                                         synth_s, audio_s, rtf}

Perf knobs (preserved from the in-tree app)
-------------------------------------------
- CPU affinity pinned to {0,1,2,3} (A78C "big" cluster on QCS8300) BEFORE
  numpy/onnxruntime spawn their threadpools.
- Custom ORT SessionOptions: intra_op=4, inter_op=1, ORT_ENABLE_ALL,
  sequential exec, CPU mem arena + mem pattern.
- FP16 model variant by default. Override via KOKORO_MODEL=int8|fp32.
- Eager background warmup at boot so the first user request hits a hot
  session.
"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

# Pin to big cores BEFORE numpy/onnxruntime spawn their threadpools.
try:
    os.sched_setaffinity(0, {0, 1, 2, 3})
except Exception:
    pass

import numpy as np
import onnxruntime as ort
import soundfile as sf
from aiohttp import web
from kokoro_onnx import Kokoro

log = logging.getLogger("kokoro-tts")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MODEL_DIR = Path(os.getenv("KOKORO_MODEL_DIR", "/models"))
PORT = int(os.getenv("KOKORO_PORT", "9100"))

_VARIANT = os.getenv("KOKORO_MODEL", "fp16").lower()
_VARIANT_TO_FILE = {
    "fp16": "kokoro-v1.0.fp16.onnx",
    "int8": "kokoro-v1.0.int8.onnx",
    "fp32": "kokoro-v1.0.onnx",
}
MODEL_FILE = _VARIANT_TO_FILE.get(_VARIANT, _VARIANT_TO_FILE["fp16"])
MODEL_PATH = MODEL_DIR / MODEL_FILE
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"

INTRA_OP_THREADS = int(os.getenv("KOKORO_THREADS", "4"))
DEFAULT_VOICE = os.getenv("KOKORO_VOICE", "af_heart")
WARMUP_TEXT = "Hello."

_kokoro: Kokoro | None = None
_load_lock = threading.Lock()
_synth_lock = threading.Lock()  # one ORT session — serialise inferences


def _make_session(model_path: Path) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.intra_op_num_threads = INTRA_OP_THREADS
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.enable_cpu_mem_arena = True
    so.enable_mem_pattern = True
    return ort.InferenceSession(
        str(model_path),
        sess_options=so,
        providers=["CPUExecutionProvider"],
    )


def get_kokoro() -> Kokoro:
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    with _load_lock:
        if _kokoro is None:
            if not MODEL_PATH.exists():
                raise FileNotFoundError(
                    f"Model {MODEL_PATH} not found. The image should bake "
                    f"models into {MODEL_DIR}; check the Dockerfile.")
            if not VOICES_PATH.exists():
                raise FileNotFoundError(f"Voices file {VOICES_PATH} not found.")
            log.info("Loading Kokoro %s with intra_op=%d, affinity=%s",
                     _VARIANT, INTRA_OP_THREADS,
                     sorted(os.sched_getaffinity(0)))
            t0 = time.perf_counter()
            sess = _make_session(MODEL_PATH)
            _kokoro = Kokoro.from_session(sess, str(VOICES_PATH))
            log.info("Kokoro loaded in %.2fs", time.perf_counter() - t0)
            try:
                t1 = time.perf_counter()
                _kokoro.create(WARMUP_TEXT, voice=DEFAULT_VOICE, lang="en-us")
                log.info("Warmup synth: %.3fs", time.perf_counter() - t1)
            except Exception:
                log.exception("warmup failed (non-fatal)")
    return _kokoro


# Aggressive splitter — same as in the original app.
_BREAK_RE = re.compile(r"(.+?[.!?,;:])(?:\s+|$)|(.+?$)", re.DOTALL)


def split_for_streaming(text: str, min_chars: int = 24,
                        max_chars: int = 220) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    pos = 0
    buf = ""
    while pos < len(text):
        m = _BREAK_RE.match(text, pos)
        if not m:
            buf += text[pos:]
            pos = len(text)
            break
        piece = (m.group(1) or m.group(2) or "").strip()
        pos = m.end()
        if not piece:
            continue
        buf = piece if not buf else (buf + " " + piece).strip()
        if (
            piece.endswith((".", "!", "?"))
            or len(buf) >= max_chars
            or (len(buf) >= min_chars and piece.endswith((",", ";", ":")))
        ):
            chunks.append(buf)
            buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def _synth_to_wav(text: str, voice: str, speed: float,
                  lang: str) -> tuple[bytes, float, float]:
    k = get_kokoro()
    with _synth_lock:
        t0 = time.perf_counter()
        samples, sr = k.create(text, voice=voice, speed=speed, lang=lang)
        synth_s = time.perf_counter() - t0
    audio_s = len(samples) / sr if sr else 0.0
    samples = np.asarray(samples, dtype=np.float32)
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue(), synth_s, audio_s


# ───────────────────────── HTTP handlers ─────────────────────────

async def health(_req: web.Request) -> web.Response:
    loaded = _kokoro is not None
    return web.json_response({
        "status": "ok" if loaded else "loading",
        "loaded": loaded,
        "variant": _VARIANT,
    })


async def voices(_req: web.Request) -> web.Response:
    loop = asyncio.get_running_loop()
    try:
        k = await loop.run_in_executor(None, get_kokoro)
        vs = sorted(k.get_voices())
    except Exception as e:
        log.exception("voices failed")
        return web.json_response({"error": str(e), "voices": []}, status=500)
    return web.json_response({"voices": vs})


async def warmup(_req: web.Request) -> web.Response:
    threading.Thread(target=get_kokoro, daemon=True).start()
    return web.json_response({"status": "warming"}, status=202)


def _parse_speak_body(data: dict) -> tuple[str, str, float, str]:
    text = (data or {}).get("text", "").strip()
    voice = (data or {}).get("voice") or DEFAULT_VOICE
    try:
        speed = float((data or {}).get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    lang = (data or {}).get("lang") or "en-us"
    return text, voice, speed, lang


async def speak(req: web.Request) -> web.Response:
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    text, voice, speed, lang = _parse_speak_body(data)
    if not text:
        return web.json_response({"error": "text is empty"}, status=400)
    loop = asyncio.get_running_loop()
    try:
        wav, synth_s, audio_s = await loop.run_in_executor(
            None, _synth_to_wav, text, voice, speed, lang)
    except Exception as e:
        log.exception("speak failed")
        return web.json_response({"error": str(e)}, status=500)
    rtf = synth_s / audio_s if audio_s else 0.0
    log.info("speak %.2fs synth / %.2fs audio (RTF %.3f, voice=%s, %d chars)",
             synth_s, audio_s, rtf, voice, len(text))
    return web.Response(
        body=wav,
        content_type="audio/wav",
        headers={
            "X-Synth-Seconds": f"{synth_s:.3f}",
            "X-Audio-Seconds": f"{audio_s:.3f}",
            "X-RTF": f"{rtf:.3f}",
        },
    )


async def speak_stream(req: web.Request) -> web.StreamResponse:
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    text, voice, speed, lang = _parse_speak_body(data)
    if not text:
        return web.json_response({"error": "text is empty"}, status=400)

    chunks = split_for_streaming(text)
    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "application/x-ndjson; charset=utf-8"},
    )
    await resp.prepare(req)
    if not chunks:
        await resp.write_eof()
        return resp

    log.info("speak_stream split into %d chunks (voice=%s)", len(chunks), voice)
    loop = asyncio.get_running_loop()
    total_synth = 0.0
    total_audio = 0.0
    try:
        for i, chunk in enumerate(chunks):
            try:
                wav, synth_s, audio_s = await loop.run_in_executor(
                    None, _synth_to_wav, chunk, voice, speed, lang)
            except Exception as e:
                log.exception("chunk synth failed")
                line = json.dumps({"error": str(e), "seq": i, "last": True})
                await resp.write((line + "\n").encode("utf-8"))
                break
            total_synth += synth_s
            total_audio += audio_s
            last = (i == len(chunks) - 1)
            payload = {
                "seq": i,
                "last": last,
                "text": chunk,
                "wav_b64": base64.b64encode(wav).decode("ascii"),
                "synth_s": round(synth_s, 3),
                "audio_s": round(audio_s, 3),
                "rtf": round(synth_s / audio_s, 3) if audio_s else 0.0,
            }
            await resp.write((json.dumps(payload) + "\n").encode("utf-8"))
    except (ConnectionResetError, asyncio.CancelledError):
        log.info("speak_stream client disconnected mid-stream")
        return resp

    log.info("speak_stream done: %.2fs synth / %.2fs audio (RTF %.3f)",
             total_synth, total_audio,
             total_synth / total_audio if total_audio else 0)
    await resp.write_eof()
    return resp


def make_app() -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_get("/voices", voices)
    app.router.add_post("/warmup", warmup)
    app.router.add_post("/speak", speak)
    app.router.add_post("/speak_stream", speak_stream)
    return app


def main() -> None:
    # Eager background warmup at boot.
    threading.Thread(target=get_kokoro, name="kokoro-boot-warmup",
                     daemon=True).start()
    log.info("kokoro_tts brick listening on 0.0.0.0:%d (variant=%s)",
             PORT, _VARIANT)
    web.run_app(make_app(), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
