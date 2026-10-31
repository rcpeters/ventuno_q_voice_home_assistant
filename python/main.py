import json
import os
import re
import subprocess
import threading
import time
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
from requests.exceptions import ReadTimeout, ConnectionError

# NOTE: `arduino.*` packages and local custom bricks are pre-installed/mounted inside
# the Arduino Ventuno Q App Lab runtime container and are not present in Windows dev environments.
from arduino.app_utils import App, Bridge  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
from arduino.app_bricks.llm import LargeLanguageModel  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
from arduino.app_bricks.asr import AutomaticSpeechRecognition  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
from arduino.app_peripherals.microphone import Microphone  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
from kokoro_tts import KokoroTTS  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
from home_assistant_mapping import HomeAssistantMapping, load_dotenv_file  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]

# Load configuration from .env file before initializing bricks
load_dotenv_file()

TIMEZONE_NAME = os.getenv("TIMEZONE", "America/Los_Angeles")
WAKE_WORD = os.getenv("WAKE_WORD", "Hey Arduino")
ALSA_DEVICE = os.getenv("ALSA_PLAYBACK_DEVICE", "plughw:1,0")
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "1.0"))
KOKORO_LANG = os.getenv("KOKORO_LANG", "en-us")

# Sound assets configuration
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_sound_file(filename: str) -> str | None:
    candidates = [
        os.path.join(BASE_DIR, "assets", filename),
        os.path.join(BASE_DIR, "assests", filename),
    ]
    if filename.endswith(".wav"):
        candidates.append(os.path.join(BASE_DIR, "assets", filename + "e"))
        candidates.append(os.path.join(BASE_DIR, "assests", filename + "e"))
    elif filename.endswith(".wave"):
        stem = filename[:-1]
        candidates.append(os.path.join(BASE_DIR, "assets", stem))
        candidates.append(os.path.join(BASE_DIR, "assests", stem))

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


BOOP_SOUND_PATH = _resolve_sound_file("boop.wav")

# Common phonetic transcriptions Whisper Small produces for "Hey Arduino"
WAKE_PHONETIC_VARIANTS = {
    "hey arduino",
    "arduino",
    "ardino",
    "arduina",
    "adreno",
    "audrino",
    "alduino",
    "hardwino",
    "ardwino",
    "ardweeno",
    "eduino",
    "erduino",
    "are you know",
    "or do we know",
    "how do we know",
}


def match_wake_word(text: str, target_wake_word: str) -> tuple[bool, str]:
    """Check if `text` contains the wake word and return `(matched, inline_command_after_wake_word)`."""
    cleaned = re.sub(r"\[.*?\]|\(.*?\)|\*.*?\*", " ", text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return False, ""

    norm = re.sub(r"[^a-zA-Z0-9\s]", " ", cleaned).strip().lower()
    norm = re.sub(r"\s+", " ", norm)
    if not norm:
        return False, ""

    variants = set(WAKE_PHONETIC_VARIANTS)
    target_norm = re.sub(r"[^a-zA-Z0-9\s]", " ", target_wake_word).strip().lower()
    target_norm = re.sub(r"\s+", " ", target_norm)
    if target_norm:
        variants.add(target_norm)

    # Sort longest first so "hey arduino" matches before "arduino"
    for variant in sorted(variants, key=len, reverse=True):
        pattern = rf"(?:^|\b)(?:hey\s+)?{re.escape(variant)}[\s,.:;!?-]*(.*)$"
        m = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if m:
            trailing_command = m.group(1).strip()
            return True, trailing_command
        if variant in norm:
            return True, ""

    return False, ""


print("=" * 50)
print("🏠 PHASE 0: Initializing Home Assistant Mapping...")
print("=" * 50)

ha_mapper = HomeAssistantMapping()
ha_mapper.start()

print("\n" + "=" * 50)
print("🚀 PHASE 1: Loading LOCAL LLM into RAM...")
print("=" * 50)

llm = LargeLanguageModel(
    system_prompt=HomeAssistantMapping.SYSTEM_PROMPT
)
llm.with_memory(2)

try:
    print("⏳ Moving ~8GB of data to NPU. Please wait a moment...")
    llm.chat("Respond 'ok'")
    print("✅ Local LLM Loaded.")
except Exception as e:
    print(f"❌ Critical error loading LLM: {e}")

print("\n" + "=" * 50)
print("🎙️ INITIALIZING AUDIO & KOKORO TTS")
print("=" * 50)

# Initialize and warmup Kokoro TTS
tts = KokoroTTS(
    default_voice=KOKORO_VOICE,
    default_lang=KOKORO_LANG,
    default_speed=KOKORO_SPEED,
)
print("⏳ Warming Up Kokoro TTS...")
tts.warmup()
print("✅ Kokoro TTS ready.")

mic = Microphone(device=Microphone.USB_MIC_1, shared=True)
asr = AutomaticSpeechRecognition(mic, language="en")

app_state = "IDLE"
user_text = ""

mic.start()
asr.start()

def _wait_for_asr_session_close(timeout_sec: float = 2.0) -> None:
    """Wait until the active ASR session has finished closing so the NPU is free for LLM inference."""
    deadline = time.monotonic() + timeout_sec
    while asr.is_transcribing() and time.monotonic() < deadline:
        time.sleep(0.05)


# Lower VAD hangover from 700ms to 400ms and flush interval from 5s to 2s so Whisper emits transcripts much faster
asr._DEFAULT_VAD_MS = 400
asr._FLUSH_INTERVAL_SECONDS = 2
# STATE 0: IDLE
Bridge.call("set_state", 0)
print(f"\n✅ All systems online! 💤 Whisper Small is listening for '{WAKE_WORD}'...")


def loop():
    global app_state, user_text

    if app_state == "IDLE":
        try:
            # Keep a single continuous ASR session open across wake word AND command!
            # When the wake word is heard, we flip to LISTENING and play `boop.wav` IN-PLACE
            # without closing/re-opening the ASR session (zero session teardown/startup delay).
            user_text = ""
            in_command_mode = False
            cmd_deadline = 0.0
            last_partial_ts = 0.0
            wake_prefix_text = ""
            partial_text = ""

            with asr.transcribe_stream(duration=20) as stream:
                for chunk in stream:
                    text = chunk.data.strip()
                    if not text:
                        continue

                    if not in_command_mode:
                        matched, inline_cmd = match_wake_word(text, WAKE_WORD)
                        if matched:
                            print(f"\n✨ Wake word detected by Whisper ('{text}')!")
                            if inline_cmd and len(inline_cmd.split()) >= 2:
                                user_text = inline_cmd
                                print(f"🗣️ Inline command captured: {user_text}")
                                Bridge.call("set_state", 2)
                                app_state = "PROCESSING"
                                asr.cancel()
                            else:
                                # Transition to LISTENING immediately inside the SAME ASR stream!
                                in_command_mode = True
                                wake_prefix_text = text if chunk.type == "partial_text" else ""
                                Bridge.call("set_state", 1)
                                if BOOP_SOUND_PATH and os.path.exists(BOOP_SOUND_PATH):
                                    subprocess.Popen(
                                        ["aplay", "-D", ALSA_DEVICE, BOOP_SOUND_PATH, "-q"],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                    )
                                print("\n🟢 ASR ACTIVE: Tell me your command! (Speak now)...")
                                # Schedule a background watchdog that cancels ASR as soon as
                                # 0.7s of silence elapses after the last partial_text (or 7s max timeout)
                                cmd_deadline = time.monotonic() + 7.0

                                def _command_watchdog():
                                    while in_command_mode and asr.is_transcribing():
                                        now_ts = time.monotonic()
                                        if now_ts >= cmd_deadline:
                                            asr.cancel()
                                            return
                                        if partial_text and last_partial_ts > 0.0 and (now_ts - last_partial_ts) >= 0.7:
                                            asr.cancel()
                                            return
                                        time.sleep(0.05)

                                threading.Thread(target=_command_watchdog, daemon=True).start()
                    else:
                        # Strip the wake word if Whisper includes it in the same utterance
                        _, stripped_cmd = match_wake_word(text, WAKE_WORD)
                        cmd_candidate = stripped_cmd if stripped_cmd else (
                            "" if text.lower() == wake_prefix_text.lower() else text
                        )
                        if not cmd_candidate:
                            continue

                        match chunk.type:
                            case "partial_text":
                                print(f"\r👂 Listening: {cmd_candidate}", end="", flush=True)
                                if cmd_candidate != partial_text:
                                    partial_text = cmd_candidate
                                    last_partial_ts = time.monotonic()
                            case "full_text":
                                user_text = cmd_candidate
                                in_command_mode = False
                                asr.cancel()

            if in_command_mode or partial_text or user_text:
                in_command_mode = False
                if partial_text and not user_text:
                    user_text = partial_text
                if user_text:
                    print(f"\n🗣️ You said: {user_text}")
                    Bridge.call("set_state", 2)
                    app_state = "PROCESSING"
                else:
                    print("\n🤷 No command heard or ASR timeout. Returning to sleep...")
                    Bridge.call("set_state", 0)
                    app_state = "IDLE"

        except (ReadTimeout, ConnectionError, RuntimeError):
            time.sleep(0.2)
        except Exception as e:
            print(f"\n⚠️ ASR stream error: {e}")
            time.sleep(0.2)
        return

    elif app_state == "PROCESSING":
        raw_llm_output = ""

        try:
            try:
                time_zone = ZoneInfo(TIMEZONE_NAME)
                local_time = datetime.now(time_zone)
            except Exception:
                local_time = datetime.now()

            formatted_time = local_time.strftime("%I:%M %p, %A, %B %d, %Y")

            # Fetch live Home Assistant light states and build context-enriched prompt
            enriched_prompt, _ = ha_mapper.build_enriched_prompt(
                user_text=user_text,
                formatted_time=formatted_time,
            )

            print("🧠 AI Thinking (Local JSON output): ", end="", flush=True)
            for chunk in llm.chat_stream(enriched_prompt):
                print(chunk, end="", flush=True)
                raw_llm_output += chunk
            print()

            # Parse structured JSON into Home Assistant actions + spoken response
            actions, speech_response = ha_mapper.parse_llm_response(raw_llm_output)
            print(f"🛠️ Parsed HA Actions: {json.dumps(actions)}")
            print(f"💬 Spoken Response: {speech_response}")

            # Execute Home Assistant light service calls if any were requested
            if actions:
                exec_results = ha_mapper.execute_actions(actions)
                failed = [r for r in exec_results if not r.get("ok")]
                if failed and len(failed) == len(exec_results):
                    speech_response = (
                        "I could not reach Home Assistant to control the lights."
                    )

            if speech_response:
                print("🔊 Speaking via Kokoro...")
                # STATE 3: SPEAKING
                Bridge.call("set_state", 3)

                # --- KOKORO TTS INTEGRATION ---
                # 1. Generate audio bytes
                wav_bytes = tts.synth(
                    speech_response,
                    voice=KOKORO_VOICE,
                    speed=KOKORO_SPEED,
                    lang=KOKORO_LANG,
                )

                # 2. Save temporary WAV file
                wav_path = "/tmp/assistant_response.wav"
                with open(wav_path, "wb") as f:
                    f.write(wav_bytes)

                # 3. Play audio forcing output to specific hardware
                os.system(f"aplay -D {ALSA_DEVICE} {wav_path} -q")
                # ------------------------------

        # --- LLM / TTS EXCEPTION HANDLING ---
        except (ReadTimeout, ConnectionError, RuntimeError) as e:
            print(f"\n⚠️ Container Error during AI/TTS Processing: {e}")
        except urllib.error.HTTPError as e:
            print(f"❌ HTTP Error {e.code}")
            try:
                error_body = e.read().decode("utf-8")
                print(error_body)
            except Exception as parse_error:
                print(f"Couldn't read the error: {parse_error}")
        # ------------------------------------

        print("\n🔄 Returning to sleep mode...")
        Bridge.call("set_state", 0)  # Turn off LED matrix
        user_text = ""
        app_state = "IDLE"


try:
    App.run(user_loop=loop)
except (KeyboardInterrupt, SystemExit):
    print("\nStopping application...")
finally:
    asr.stop()
    mic.stop()
    ha_mapper.stop()

    try:
        Bridge.call("set_state", 0)  # Turn off LED matrix on exit
    except Exception:
        pass
