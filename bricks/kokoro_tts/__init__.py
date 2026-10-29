"""KokoroTTS — main-container client for the local `kokoro_tts` brick.

The companion container at service hostname `kokoro_tts` (port 9100) hosts a
self-contained Kokoro-82M ONNX TTS engine with the model weights baked into
the Docker image. This client wraps the brick's HTTP API so the main app can
synthesize speech with one method call.

Typical usage from `python/main.py`:

    from kokoro_tts import KokoroTTS

    tts = KokoroTTS()
    tts.warmup()                                  # non-blocking, optional

    wav = tts.synth("Hello world", voice="af_heart")
    # wav is a WAV-formatted bytes blob (PCM16, 24 kHz mono).

    for chunk in tts.synth_stream("First sentence. Second sentence."):
        # chunk = {"seq", "last", "text", "wav_b64", "synth_s", "audio_s", "rtf"}
        send_to_browser(chunk)
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterator
from urllib import error as urlerror
from urllib import request as urlrequest

# NOTE: `arduino.app_utils` is pre-installed inside the Arduino Ventuno Q App Lab
# runtime container and is not available in local Windows development environments.
from arduino.app_utils import Logger, brick  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]

log = Logger("KokoroTTS")


@brick
class KokoroTTS:
    SERVICE_HOST = "kokoro_tts"
    PORT = 9100

    def __init__(self,
                 host: str | None = None,
                 port: int | None = None,
                 default_voice: str = "af_heart",
                 default_lang: str = "en-us",
                 default_speed: float = 1.0):
        self._base = f"http://{host or self.SERVICE_HOST}:{port or self.PORT}"
        self.default_voice = default_voice
        self.default_lang = default_lang
        self.default_speed = default_speed

    # ─────────────────── lifecycle ───────────────────

    def start(self) -> None:
        # Best-effort: tell the brick to start loading the model so the first
        # synth is hot. Don't block app startup if the brick isn't up yet.
        try:
            self.warmup()
        except Exception as e:
            log.info(f"warmup deferred ({e})")

    def stop(self) -> None:
        pass

    # ─────────────────── HTTP helpers ───────────────────

    def _post(self, path: str, body: dict | None = None,
              timeout: float = 60.0):
        data = json.dumps(body or {}).encode("utf-8")
        req = urlrequest.Request(
            self._base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urlrequest.urlopen(req, timeout=timeout)

    def _get(self, path: str, timeout: float = 5.0):
        return urlrequest.urlopen(self._base + path, timeout=timeout)

    # ─────────────────── public API ───────────────────

    def health(self) -> dict:
        with self._get("/health") as r:
            return json.loads(r.read())

    def wait_ready(self, timeout: float = 60.0, poll: float = 1.0) -> bool:
        """Block until the brick reports loaded=True or timeout elapses."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.health().get("loaded"):
                    return True
            except urlerror.URLError:
                pass
            time.sleep(poll)
        return False

    def list_voices(self) -> list[str]:
        with self._get("/voices", timeout=120.0) as r:
            return json.loads(r.read()).get("voices", [])

    def warmup(self) -> None:
        with self._post("/warmup", timeout=5.0):
            pass

    def synth(self, text: str, voice: str | None = None,
              speed: float | None = None, lang: str | None = None,
              timeout: float = 120.0) -> bytes:
        """Synthesize the whole text in one shot. Returns WAV bytes."""
        body = {
            "text": text,
            "voice": voice or self.default_voice,
            "speed": speed if speed is not None else self.default_speed,
            "lang": lang or self.default_lang,
        }
        with self._post("/speak", body=body, timeout=timeout) as r:
            return r.read()

    def synth_stream(self, text: str, voice: str | None = None,
                     speed: float | None = None, lang: str | None = None,
                     timeout: float = 600.0) -> Iterator[dict]:
        """Stream chunked synthesis. Yields one dict per chunk; the final
        chunk has ``last=True``. Each dict has keys:
        ``seq, last, text, wav_b64, synth_s, audio_s, rtf``.
        """
        body = {
            "text": text,
            "voice": voice or self.default_voice,
            "speed": speed if speed is not None else self.default_speed,
            "lang": lang or self.default_lang,
        }
        with self._post("/speak_stream", body=body, timeout=timeout) as r:
            for raw in r:
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    log.warning(f"bad ndjson chunk: {line[:80]!r}")
                    continue
