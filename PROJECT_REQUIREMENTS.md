# Project Requirements: Smart Home Assistant with Local AI on Arduino Ventuno Q

## 1. Project Overview
Build a privacy-first, locally hosted smart home voice assistant running on an **Arduino Ventuno Q**. The system combines **Home Assistant** for device automation with a local voice pipeline (**Wake Word + ASR + Local LLM + Kokoro TTS**) built in **Arduino App Lab**, enabling natural-language voice commands to control smart home devices without relying on cloud services.

---

## 2. System Architecture

### Hardware
- **Compute Platform:** Arduino Ventuno Q
- **Audio Input/Output:** Microphone (for wake word & voice commands) and speaker (for TTS responses)

### Software Stack
- **Home Automation Hub:** Home Assistant running locally in a Docker container on the Ventuno Q
- **AI & Voice Pipeline (Arduino App Lab):**
  - Based on the [`keyword-asr-local-llm-kokoro-tts`](https://docs.arduino.cc/tutorials/ventuno-q/local-ai-assistant/) application architecture
  - Combines existing Arduino App Lab Bricks with a custom/modified Brick for Home Assistant integration

```mermaid
flowchart LR
    Mic["Microphone"] --> WakeASR["Keyword Spotting & Local ASR"]
    WakeASR --> LLM["Custom Local LLM Brick"]
    LLM <-->|Local API / Tool Calls| HA["Home Assistant (Docker)"]
    LLM --> TTS["Kokoro TTS Brick"]
    TTS --> Speaker["Speaker"]
```

---

## 3. Functional Requirements

### 3.1 Home Assistant Core
- [ ] Run Home Assistant locally inside a Docker container on the Arduino Ventuno Q.
- [ ] Expose local API endpoints (REST API / WebSocket API / Long-Lived Access Token) for local inter-container communication.

### 3.2 Voice Input & Speech-to-Text (ASR)
- [ ] Continuously listen for a local wake word / keyword.
- [ ] Transcribe spoken user commands locally into text using the ASR brick.

### 3.3 Custom Local LLM Brick (Home Assistant Integration)
- [ ] Base the custom brick on the `keyword-asr-local-llm-kokoro-tts` LLM pipeline.
- [ ] Provide the LLM with context of available Home Assistant entities, states, and services (e.g., lights, switches, sensors, climate).
- [ ] Parse user intent and execute commands against the local Home Assistant instance (via tool calling / structured JSON output / Home Assistant REST API).
- [ ] Return a concise natural-language confirmation or status response to be spoken aloud.
- [ ] Support general-knowledge and conversational queries when the user's prompt does not target a smart home device.
- [ ] Log transcribed user prompts, triggered Home Assistant tool calls/commands, and generated LLM responses to the console (`stdout`) for real-time debugging and observability.

### 3.4 Text-to-Speech (TTS)
- [ ] Synthesize the LLM's response into speech locally using the `kokoro_tts` Docker container/brick.
- [ ] Play the generated audio response back to the user with low latency.

---

## 4. Non-Functional Requirements
- **100% Local Execution:** Voice recognition, LLM inference, TTS synthesis, and Home Assistant control must operate on the local network/device without external cloud dependencies.
- **Reproducible Deployment:** Custom brick containers must be buildable and deployable on the Ventuno Q (`~/ArduinoApps`).
- **Low Latency:** Keep prompts, tool definitions, and TTS responses concise to minimize end-to-end voice response time on edge hardware.