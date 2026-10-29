# `kokoro_tts` brick

Local Kokoro-82M ONNX TTS as a self-contained Arduino App custom brick.

To reuse: copy this whole directory into another app's `bricks/`, add
`- kokoro_tts: {}` to that app's `app.yaml`, then:

```python
from kokoro_tts import KokoroTTS
tts = KokoroTTS()
wav = tts.synth("Hello world")
```

See the parent app's `README.md` for the full HTTP API, env vars, and
performance notes.
