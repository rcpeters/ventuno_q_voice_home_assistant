# Ventuno Q Smart Home Voice Assistant (Local AI + Home Assistant + Kokoro TTS)

A 100% local, privacy-first smart home voice assistant built with **Arduino App Lab** for the **Arduino Ventuno Q**. It combines keyword spotting, automatic speech recognition (ASR), a local Large Language Model (`genie:qwen3-4b` on the NPU), a custom **Home Assistant Mapping Brick** (`home_assistant_mapping`), and high-fidelity **Kokoro TTS**.

## Project Structure

- [`app.yaml`](app.yaml): Arduino App Lab project manifest declaring built-in and custom bricks.
- [`assets/`](assets/): Audio cues and media assets (e.g. `boop.wav` listening cue).
- [`.env.example`](.env.example) / [`.env`](.env): Local configuration for Home Assistant URL, Long-Lived Access Token, timezone, and audio hardware.
- [`bricks/home_assistant_mapping/`](bricks/home_assistant_mapping/__init__.py): Custom pure-Python brick that fetches live `light.*` entity states from Home Assistant, injects context into the local LLM, parses single-pass JSON output, and executes Home Assistant REST service calls (`light.turn_on`, `light.turn_off`, `light.toggle`).
- [`bricks/kokoro_tts/`](bricks/kokoro_tts/__init__.py): Custom containerized brick running the local Kokoro-82M ONNX TTS engine.
- [`python/main.py`](python/main.py): Main orchestrator loop managing wake word detection, streaming ASR, Home Assistant context injection, LLM inference, light control execution, and TTS playback.
- [`sketch/sketch.ino`](sketch/sketch.ino): Microcontroller C++ firmware driving the 104-LED matrix animations for Idle (`0`), Listening (`1`), Processing (`2`), and Speaking (`3`) states.

## Setup & Configuration

1. **Ensure Home Assistant is running in Docker on the Ventuno Q** (default port `8123`) and generate a **Long-Lived Access Token** from your Home Assistant Profile page.
2. **Configure `.env`**:
   Edit `.env` in the project root:
   ```ini
   HA_URL=http://host.docker.internal:8123
   HA_TOKEN=your_long_lived_access_token_here
   HA_TIMEOUT_SEC=3.0
   TIMEZONE=America/Santo_Domingo
   ALSA_PLAYBACK_DEVICE=plughw:1,0
   ```
   *(Tip: If `host.docker.internal` does not resolve on your Linux Docker setup, set `HA_URL` to your Ventuno Q's local LAN IP or Docker bridge IP, e.g. `http://172.17.0.1:8123`.)*

3. **Deploy to `~/ArduinoApps` on the Ventuno Q** and run via Arduino App Lab:

   1. Copy the repository directory to the board's `~/ArduinoApps` folder:
      ```bash
      scp -r ../ventuno_q_voice_home_assistant arduino@<board-ip-or-hostname>:~/ArduinoApps
      ```

   2. SSH into the board:
      ```bash
      ssh arduino@<board-ip-or-hostname>
      ```

   3. Build the Docker image for the custom `kokoro_tts` brick (this also downloads the ONNX model if not already present):
      ```bash
      docker build -t ventuno_q_voice_home_assistant-kokoro_tts:latest ~/ArduinoApps/ventuno_q_voice_home_assistant/bricks/kokoro_tts/container
      ```
      *(Note: `home_assistant_mapping` is a pure-Python brick with zero external dependencies that runs directly inside the main application process, so it does not require a `docker build` step.)*

   4. Configure the built-in ASR and Keyword Spotting bricks:
      ```bash
      arduino-app-cli app brick config user:ventuno_q_voice_home_assistant arduino:asr --model whisper-small-quantized
      arduino-app-cli app brick config user:ventuno_q_voice_home_assistant arduino:keyword_spotting --model keyword-spotting-hey-arduino
      ```

   5. Start the application:
      ```bash
      arduino-app-cli app start user:ventuno_q_voice_home_assistant
      ```

## Supported Voice Commands

- **Turn lights on/off/toggle**:
  - *"Turn on the kitchen light"*
  - *"Turn off all the living room lights"*
- **Brightness & Color control**:
  - *"Dim the bedroom light to 30 percent"*
  - *"Set the desk lamp to warm white"* or *"Change the living room light to blue"*
- **State queries & General Knowledge**:
  - *"Which lights are currently on?"*
  - *"What time is it?"* or any general conversational prompt.
