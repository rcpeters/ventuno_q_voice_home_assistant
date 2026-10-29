# Ventuno Q Smart Home Voice Assistant (Local AI + Home Assistant + Kokoro TTS)

A 100% local, privacy-first smart home voice assistant built with **Arduino App Lab** for the **Arduino Ventuno Q**. It combines keyword spotting, automatic speech recognition (ASR), a local Large Language Model (`genie:qwen3-4b` on the NPU), a custom **Home Assistant Mapping Brick** (`home_assistant_mapping`), and high-fidelity **Kokoro TTS**.

## Project Structure

- [`app.yaml`](file:///C:/Users/info/Documents/Antigravity/Venturo%20Home%20Assistent/app.yaml): Arduino App Lab project manifest declaring built-in and custom bricks.
- [`.env.example`](file:///C:/Users/info/Documents/Antigravity/Venturo%20Home%20Assistent/.env.example) / [`.env`](file:///C:/Users/info/Documents/Antigravity/Venturo%20Home%20Assistent/.env): Local configuration for Home Assistant URL, Long-Lived Access Token, timezone, and audio hardware.
- [`bricks/home_assistant_mapping/`](file:///C:/Users/info/Documents/Antigravity/Venturo%20Home%20Assistent/bricks/home_assistant_mapping/__init__.py): Custom pure-Python brick that fetches live `light.*` entity states from Home Assistant, injects context into the local LLM, parses single-pass JSON output, and executes Home Assistant REST service calls (`light.turn_on`, `light.turn_off`, `light.toggle`).
- [`bricks/kokoro_tts/`](file:///C:/Users/info/Documents/Antigravity/Venturo%20Home%20Assistent/bricks/kokoro_tts/__init__.py): Custom containerized brick running the local Kokoro-82M ONNX TTS engine.
- [`python/main.py`](file:///C:/Users/info/Documents/Antigravity/Venturo%20Home%20Assistent/python/main.py): Main orchestrator loop managing wake word detection, streaming ASR, Home Assistant context injection, LLM inference, light control execution, and TTS playback.
- [`sketch/sketch.ino`](file:///C:/Users/info/Documents/Antigravity/Venturo%20Home%20Assistent/sketch/sketch.ino): Microcontroller C++ firmware driving the 104-LED matrix animations for Idle (`0`), Listening (`1`), Processing (`2`), and Speaking (`3`) states.

## Setup & Configuration

1. **Ensure Home Assistant is running in Docker on the Ventuno Q** (default port `8123`) and generate a **Long-Lived Access Token** from your Home Assistant Profile page.
2. **Configure `.env`**:
   Edit `.env` in the project root:
   ```ini
   HA_URL=http://host.docker.internal:8123
   HA_TOKEN=your_long_lived_access_token_here
   HA_TIMEOUT_SEC=3.0
   TIMEZONE=America/Santo_Domingo
   WAKE_WORD=Ventuno
   ALSA_PLAYBACK_DEVICE=plughw:1,0
   ```
   *(Tip: If `host.docker.internal` does not resolve on your Linux Docker setup, set `HA_URL` to your Ventuno Q's local LAN IP or Docker bridge IP, e.g. `http://172.17.0.1:8123`.)*

3. **Deploy to `~/ArduinoApps` on the Ventuno Q** and run via Arduino App Lab.

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
