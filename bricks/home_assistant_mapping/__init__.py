"""HomeAssistantMapping — Custom Arduino App Lab brick for local Home Assistant integration.

Connects the local LLM pipeline to a local Home Assistant Docker instance via REST API.
Provides:
1. Live discovery of `light.*` and light-like `switch.*` entities (`GET /api/states`) with
   automatic URL fallback across Docker/Linux host addresses and fallback caching.
2. Prompt construction & single-pass JSON output parsing for low-latency NPU inference.
3. Execution of `light.turn_on`, `light.turn_off`, `light.toggle` (and `switch.*` or
   `homeassistant.*` equivalents) including brightness (`brightness_pct`), color (`rgb_color`,
   `color_name`), and color temperature (`color_temp_kelvin`).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

# NOTE: `arduino.app_utils` is pre-installed inside the Arduino Ventuno Q App Lab
# runtime container and is not available in local Windows development environments.
from arduino.app_utils import Logger, brick  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]

log = Logger("HomeAssistantMapping")

# Candidate Home Assistant URLs to probe on Linux / Docker if the primary URL fails
CANDIDATE_HA_URLS: list[str] = [
    "http://172.17.0.1:8123",
    "http://host.docker.internal:8123",
    "http://localhost:8123",
    "http://127.0.0.1:8123",
    "http://homeassistant:8123",
]

# Named RGB color presets for natural voice commands if the LLM outputs a color name
NAMED_RGB_COLORS: dict[str, list[int]] = {
    "red": [255, 0, 0],
    "green": [0, 255, 0],
    "blue": [0, 0, 255],
    "white": [255, 255, 255],
    "warm white": [255, 197, 143],
    "soft white": [255, 214, 170],
    "cool white": [212, 235, 255],
    "daylight": [255, 250, 244],
    "yellow": [255, 255, 0],
    "orange": [255, 165, 0],
    "purple": [160, 32, 240],
    "violet": [238, 130, 238],
    "pink": [255, 105, 180],
    "magenta": [255, 0, 255],
    "cyan": [0, 255, 255],
    "turquoise": [64, 224, 208],
    "gold": [255, 215, 0],
}


def _detect_linux_default_gateway() -> str | None:
    """Detect the Docker bridge gateway IP inside a Linux container from /proc/net/route."""
    try:
        route_file = Path("/proc/net/route")
        if not route_file.is_file():
            return None
        for line in route_file.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == "00000000":
                gw_hex = parts[2]
                if len(gw_hex) == 8:
                    octets = [int(gw_hex[i : i + 2], 16) for i in (6, 4, 2, 0)]
                    ip = ".".join(str(o) for o in octets)
                    if ip != "0.0.0.0":
                        return f"http://{ip}:8123"
    except Exception:
        pass
    return None


def load_dotenv_file(dotenv_path: str | Path | None = None) -> dict[str, str]:
    """Load key=value pairs from a `.env` file into `os.environ` without external deps."""
    loaded: dict[str, str] = {}
    candidates: list[Path] = []

    if dotenv_path:
        candidates.append(Path(dotenv_path))
    else:
        cwd = Path.cwd()
        candidates.extend([
            cwd / ".env",
            cwd / "python" / ".env",
            cwd.parent / ".env",
            Path(__file__).resolve().parent / ".env",
            Path(__file__).resolve().parent.parent / ".env",
            Path(__file__).resolve().parent.parent.parent / ".env",
            Path("/app/.env"),
            Path("/app/python/.env"),
        ])
        app_home = os.getenv("APP_HOME")
        if app_home:
            candidates.append(Path(app_home) / ".env")
            candidates.append(Path(app_home) / "python" / ".env")

    for path in candidates:
        if path.is_file():
            try:
                for raw_line in path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'").strip('"')
                    if key and val and not os.getenv(key):
                        os.environ[key] = val
                    if key and val:
                        loaded[key] = val
                if loaded:
                    print(f"📄 [HA Mapping] Loaded .env from {path}")
                    break
            except Exception as exc:
                log.warning(f"Failed reading .env at {path}: {exc}")
    return loaded


@brick
class HomeAssistantMapping:
    """Client and LLM mapper for controlling Home Assistant lights & switches locally."""

    DEFAULT_HA_URL = "http://172.17.0.1:8123"

    SYSTEM_PROMPT = (
        "/no_think\n"
        "You are a local smart home voice assistant running on an Arduino Ventuno Q.\n"
        "You MUST ALWAYS respond with a single valid JSON object and nothing else (no markdown, no extra text).\n"
        "JSON format:\n"
        '{"actions": [{"service": "light.turn_on", "entity_id": "light.example", "brightness_pct": 80, "rgb_color": [255, 0, 0]}], "speech": "Your spoken response here."}\n\n'
        "Rules:\n"
        "1. If the user wants to control one or more lights, populate \"actions\" using ONLY the exact entity_id values listed in [Available Lights].\n"
        "2. Supported services: \"light.turn_on\", \"light.turn_off\", \"light.toggle\" (or \"switch.turn_on\", \"switch.turn_off\", \"switch.toggle\" if the entity starts with switch.).\n"
        "3. Optional fields for turning on a `light.*` entity:\n"
        "   - \"brightness_pct\": integer from 1 to 100.\n"
        "   - \"rgb_color\": [R, G, B] array of 3 integers (0-255).\n"
        "   - \"color_temp_kelvin\": integer (e.g. 2700 for warm, 4000 for neutral, 6500 for cool daylight).\n"
        "4. If the user asks about current light status or asks a general knowledge / conversational question, set \"actions\": [] and answer in \"speech\".\n"
        "5. Keep \"speech\" brief, natural, and maximum two sentences. Write out symbols and units as words (for example 80% as 80 percent) and never use emojis."
    )

    def __init__(
        self,
        ha_url: str | None = None,
        ha_token: str | None = None,
        dotenv_path: str | None = None,
        request_timeout: float = 3.0,
        include_switches: bool = True,
    ):
        load_dotenv_file(dotenv_path)
        raw_url = ha_url or os.getenv("HA_URL") or self.DEFAULT_HA_URL
        self.ha_url = raw_url.rstrip("/")
        self.ha_token = (ha_token or os.getenv("HA_TOKEN") or "").strip()
        self.request_timeout = float(
            os.getenv("HA_TIMEOUT_SEC", str(request_timeout))
        )
        self.include_switches = include_switches
        self._cached_lights: list[dict[str, Any]] = []
        self._last_fetch_ts: float = 0.0
        self._url_verified: bool = False

    # ─────────────────── Lifecycle ───────────────────

    def start(self) -> None:
        """Verify Home Assistant connectivity at startup and warm entity cache."""
        if not self.ha_token:
            log.warning(
                "HA_TOKEN is not set! Configure HA_URL and HA_TOKEN in .env to enable Home Assistant control."
            )
            return
        lights = self.get_lights(use_cache_on_error=True, log_discovered=True)
        print(
            f"🏠 [HA Mapping] Active URL: {self.ha_url} | Discovered {len(lights)} controllable light/switch entity(s)."
        )

    def stop(self) -> None:
        pass

    # ─────────────────── HTTP Helpers ───────────────────

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.ha_token:
            headers["Authorization"] = f"Bearer {self.ha_token}"
        return headers

    def _get_from_url(self, base_url: str, path: str, timeout: float | None = None) -> Any:
        url = f"{base_url.rstrip('/')}{path}"
        req = urlrequest.Request(url, headers=self._headers(), method="GET")
        with urlrequest.urlopen(req, timeout=timeout or self.request_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str, timeout: float | None = None) -> Any:
        """GET from `self.ha_url`, or automatically probe fallback Linux/Docker URLs if unreachable."""
        if self._url_verified:
            return self._get_from_url(self.ha_url, path, timeout=timeout)

        # Build candidate URL list starting with configured self.ha_url, then Linux gateway, then defaults
        candidates: list[str] = [self.ha_url]
        gw_url = _detect_linux_default_gateway()
        if gw_url and gw_url not in candidates:
            candidates.append(gw_url)
        for url in CANDIDATE_HA_URLS:
            if url not in candidates:
                candidates.append(url)

        last_exc: Exception | None = None
        for candidate in candidates:
            try:
                data = self._get_from_url(candidate, path, timeout=timeout or 2.0)
                if candidate != self.ha_url:
                    print(
                        f"🔄 [HA Mapping] '{self.ha_url}' was unreachable; auto-switched to working Home Assistant URL: '{candidate}'"
                    )
                    self.ha_url = candidate
                self._url_verified = True
                return data
            except urlerror.HTTPError as http_exc:
                # If we got an HTTP status (even 401/403), the host is reachable!
                if candidate != self.ha_url:
                    self.ha_url = candidate
                self._url_verified = True
                raise http_exc
            except Exception as exc:
                last_exc = exc

        if last_exc:
            raise last_exc
        raise RuntimeError("No Home Assistant URL candidates succeeded.")

    def _post(
        self, path: str, payload: dict[str, Any], timeout: float | None = None
    ) -> Any:
        url = f"{self.ha_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            url, data=data, headers=self._headers(), method="POST"
        )
        with urlrequest.urlopen(req, timeout=timeout or self.request_timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}

    # ─────────────────── Entity Discovery ───────────────────

    def get_lights(
        self, use_cache_on_error: bool = True, log_discovered: bool = False
    ) -> list[dict[str, Any]]:
        """Fetch live `light.*` (and `switch.*`) states from Home Assistant `/api/states`."""
        if not self.ha_token:
            return self._cached_lights

        try:
            states = self._get("/api/states")
            lights: list[dict[str, Any]] = []
            switches: list[dict[str, Any]] = []
            all_domains: dict[str, int] = {}

            for item in states:
                entity_id = item.get("entity_id", "")
                if "." in entity_id:
                    dom = entity_id.split(".", 1)[0]
                    all_domains[dom] = all_domains.get(dom, 0) + 1

                is_light = entity_id.startswith("light.")
                is_switch = self.include_switches and entity_id.startswith("switch.")
                if not (is_light or is_switch):
                    continue

                attrs = item.get("attributes") or {}
                friendly_name = str(attrs.get("friendly_name") or entity_id)
                raw_brightness = attrs.get("brightness")
                brightness_pct = (
                    round((float(raw_brightness) / 255.0) * 100)
                    if isinstance(raw_brightness, (int, float))
                    else None
                )

                entity_info: dict[str, Any] = {
                    "entity_id": entity_id,
                    "name": friendly_name,
                    "state": item.get("state", "unknown"),
                }
                if brightness_pct is not None:
                    entity_info["brightness_pct"] = brightness_pct
                if attrs.get("rgb_color"):
                    entity_info["rgb_color"] = list(attrs["rgb_color"])
                if attrs.get("color_temp_kelvin"):
                    entity_info["color_temp_kelvin"] = attrs["color_temp_kelvin"]

                if is_light:
                    lights.append(entity_info)
                else:
                    switches.append(entity_info)

            # Include all `light.*` entities plus any `switch.*` entities (especially garage lights on smart switches)
            combined = lights + switches

            if log_discovered:
                print(
                    f"🔍 [HA Discovery] Total HA entities: {len(states)} | Domains: {all_domains}"
                )
                if combined:
                    for ent in combined:
                        print(
                            f"   💡 Found: {ent['entity_id']} (\"{ent['name']}\") -> state={ent['state']}"
                        )
                else:
                    print(
                        "   ⚠️ No `light.*` or `switch.*` entities found in Home Assistant `/api/states`!"
                    )

            self._cached_lights = combined
            self._last_fetch_ts = time.monotonic()
            return combined

        except Exception as exc:
            print(f"⚠️ [HA Mapping] Failed to fetch live states from {self.ha_url}: {exc}")
            if use_cache_on_error and self._cached_lights:
                print(
                    f"ℹ️ [HA Mapping] Using cached light states ({len(self._cached_lights)} entities)."
                )
                return self._cached_lights
            return []

    def format_lights_context(self, lights: list[dict[str, Any]] | None = None) -> str:
        """Format discovered light states into a compact prompt block for the LLM."""
        if lights is None:
            lights = self.get_lights()

        if not lights:
            return "[Available Lights]\nNone discovered (or Home Assistant unreachable)."

        lines = ["[Available Lights]"]
        for lgt in lights:
            details = [f"state={lgt['state']}"]
            if "brightness_pct" in lgt:
                details.append(f"brightness={lgt['brightness_pct']}%")
            if "rgb_color" in lgt:
                details.append(f"rgb={lgt['rgb_color']}")
            if "color_temp_kelvin" in lgt:
                details.append(f"kelvin={lgt['color_temp_kelvin']}")
            detail_str = ", ".join(details)
            lines.append(f'- {lgt["entity_id"]} ("{lgt["name"]}"): {detail_str}')
        return "\n".join(lines)

    # ─────────────────── Prompt & Output Parsing ───────────────────

    def build_enriched_prompt(
        self, user_text: str, formatted_time: str
    ) -> tuple[str, list[dict[str, Any]]]:
        """Fetch live light entities and build the context-injected user prompt."""
        lights = self.get_lights()
        lights_block = self.format_lights_context(lights)
        print(f"\n📋 [HA Context]\n{lights_block}")

        prompt = (
            f"[System info: Current local time is {formatted_time}]\n"
            f"{lights_block}\n\n"
            f"User command: {user_text}\n"
            "Respond ONLY with a valid JSON object containing \"actions\" and \"speech\"."
        )
        return prompt, lights

    @staticmethod
    def parse_llm_response(raw_response: str) -> tuple[list[dict[str, Any]], str]:
        """Parse the LLM's response into `(actions, speech)`."""
        cleaned = raw_response.strip()

        # Strip optional <think>...</think> reasoning blocks
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

        # Strip markdown fences if present
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        else:
            first_brace = cleaned.find("{")
            last_brace = cleaned.rfind("}")
            if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                cleaned = cleaned[first_brace : last_brace + 1]

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                raw_actions = parsed.get("actions") or []
                if isinstance(raw_actions, dict):
                    raw_actions = [raw_actions]
                actions = [a for a in raw_actions if isinstance(a, dict)]
                speech = str(parsed.get("speech") or "").strip()
                if not speech and actions:
                    speech = "Done."
                return actions, speech
        except json.JSONDecodeError:
            pass

        fallback_speech = re.sub(r"<think>.*?</think>", "", raw_response, flags=re.DOTALL).strip()
        return [], fallback_speech

    # ─────────────────── Service Execution ───────────────────

    def execute_actions(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute parsed Home Assistant light/switch actions and log each call to stdout."""
        results: list[dict[str, Any]] = []
        if not actions:
            return results

        for action in actions:
            entity_id = str(action.get("entity_id") or "").strip()
            if not entity_id:
                print(f"⚠️ [HA Action Skipped] Missing entity_id in action: {action}")
                results.append({"action": action, "ok": False, "error": "Missing entity_id"})
                continue

            entity_domain = entity_id.split(".", 1)[0] if "." in entity_id else "light"
            service_full = str(action.get("service") or f"{entity_domain}.turn_on").strip().lower()
            if "." in service_full:
                _, service = service_full.split(".", 1)
            else:
                service = service_full

            # Always route to the actual entity's domain (`light` or `switch`) so `light.turn_on` on a `switch.garage_lights` still works
            domain = entity_domain if entity_domain in ("light", "switch", "homeassistant") else "light"

            if service not in ("turn_on", "turn_off", "toggle"):
                print(
                    f"⚠️ [HA Action Skipped] Unsupported service '{service_full}'. Supported: turn_on, turn_off, toggle."
                )
                results.append(
                    {"action": action, "ok": False, "error": f"Unsupported service: {service_full}"}
                )
                continue

            payload: dict[str, Any] = {"entity_id": entity_id}

            if service == "turn_on" and domain == "light":
                # Brightness (percentage 1..100 or raw 1..255)
                if "brightness_pct" in action and action["brightness_pct"] is not None:
                    try:
                        pct = max(0, min(100, int(round(float(action["brightness_pct"])))))
                        payload["brightness_pct"] = pct
                    except (ValueError, TypeError):
                        pass
                elif "brightness" in action and action["brightness"] is not None:
                    try:
                        bri = max(0, min(255, int(round(float(action["brightness"])))))
                        payload["brightness"] = bri
                    except (ValueError, TypeError):
                        pass

                # RGB Color ([R, G, B]) or named color
                rgb = action.get("rgb_color")
                color_name = action.get("color_name") or action.get("color")
                if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
                    try:
                        payload["rgb_color"] = [
                            max(0, min(255, int(c))) for c in rgb
                        ]
                    except (ValueError, TypeError):
                        pass
                elif isinstance(color_name, str) and color_name.strip():
                    cname = color_name.strip().lower()
                    if cname in NAMED_RGB_COLORS:
                        payload["rgb_color"] = NAMED_RGB_COLORS[cname]
                    else:
                        payload["color_name"] = cname

                # Color temperature (Kelvin)
                if "color_temp_kelvin" in action and action["color_temp_kelvin"] is not None:
                    try:
                        payload["color_temp_kelvin"] = int(action["color_temp_kelvin"])
                    except (ValueError, TypeError):
                        pass

            endpoint = f"/api/services/{domain}/{service}"
            print(f"⚡ [HA Tool Call] POST {self.ha_url}{endpoint} -> {json.dumps(payload)}")

            if not self.ha_token:
                err_msg = "HA_TOKEN not configured"
                print(f"❌ [HA Tool Error] {err_msg}")
                results.append({"action": action, "ok": False, "error": err_msg})
                continue

            try:
                resp = self._post(endpoint, payload)
                print(f"✅ [HA Tool Success] {domain}.{service} executed on {entity_id}")
                results.append({"action": action, "ok": True, "response": resp})
            except urlerror.HTTPError as exc:
                err_body = ""
                try:
                    err_body = exc.read().decode("utf-8")
                except Exception:
                    pass
                print(f"❌ [HA HTTP Error {exc.code}] {endpoint}: {err_body or exc.reason}")
                results.append(
                    {"action": action, "ok": False, "error": f"HTTP {exc.code}: {err_body or exc.reason}"}
                )
            except Exception as exc:
                print(f"❌ [HA Connection Error] {endpoint}: {exc}")
                results.append({"action": action, "ok": False, "error": str(exc)})

        return results
