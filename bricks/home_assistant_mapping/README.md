# `home_assistant_mapping` Brick

Pure-Python custom Arduino App Lab brick that bridges the local LLM (`arduino:llm`) with an external **Home Assistant** Docker container running on the Arduino Ventuno Q.

## Features

- **Live Entity Discovery (`GET /api/states`)**: Queries Home Assistant right before each LLM inference (with automatic fallback caching) to inject all `light.*` entities, their friendly names, on/off states, brightness percentages, and color settings into the prompt.
- **Single-Pass Structured JSON Output**: Instructs the local LLM (`genie:qwen3-4b`) to return both the Home Assistant service call(s) and the spoken confirmation in a single JSON response (`{"actions": [...], "speech": "..."}`) for minimal latency on the NPU.
- **Supported Services**:
  - `light.turn_on` (supports `brightness_pct`, `rgb_color`, `color_name`, `color_temp_kelvin`)
  - `light.turn_off`
  - `light.toggle`
- **Observability**: Logs discovered light entities, parsed JSON tool calls, REST API payloads, and spoken output directly to `stdout`.

## Configuration (`.env`)

Configure your Home Assistant connection in the project root `.env` file:

```ini
HA_URL=http://host.docker.internal:8123
HA_TOKEN=your_long_lived_access_token_here
HA_TIMEOUT_SEC=3.0
```
