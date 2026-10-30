import json
import os
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
from arduino.app_bricks.keyword_spotting import KeywordSpotting  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
from kokoro_tts import KokoroTTS  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
from home_assistant_mapping import HomeAssistantMapping, load_dotenv_file  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]

# Load configuration from .env file before initializing bricks
load_dotenv_file()

TIMEZONE_NAME = os.getenv("TIMEZONE", "America/Los_Angeles")
ALSA_DEVICE = os.getenv("ALSA_PLAYBACK_DEVICE", "plughw:1,0")
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "1.0"))
KOKORO_LANG = os.getenv("KOKORO_LANG", "en-us")
KEYWORD_CONFIDENCE = float(os.getenv("KEYWORD_CONFIDENCE", "0.90"))
KEYWORD_DEBOUNCE_SEC = float(os.getenv("KEYWORD_DEBOUNCE_SEC", "2.0"))

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
llm.with_memory(5)

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

mic_spotter = Microphone(device=Microphone.USB_MIC_1, shared=True)
mic_asr = Microphone(device=Microphone.USB_MIC_1, shared=True)
asr = AutomaticSpeechRecognition(mic_asr)

app_state = "IDLE"
user_text = ""

spotter = KeywordSpotting(
    mic=mic_spotter,
    confidence=KEYWORD_CONFIDENCE,
    debounce_sec=KEYWORD_DEBOUNCE_SEC,
)

# Automatically configure wake word from the selected model's labels
available_classes = getattr(spotter.model_info, "labels", []) or []
valid_keywords = [
    c
    for c in available_classes
    if c.lower()
    not in ["noise", "unknown", "_noise", "_unknown", "z-openset", "background"]
]
if not valid_keywords:
    model_name = getattr(spotter.model_info, "name", "")
    valid_keywords = [model_name] if model_name else ["Hey Arduino"]

target_keyword = valid_keywords[0]


def on_keyword_detected():
    global app_state
    if app_state == "IDLE":
        print(f"\n✨ Wake word '{target_keyword}' detected!")
        app_state = "LISTENING"


for kw in valid_keywords:
    spotter.on_detect(kw, on_keyword_detected)

mic_spotter.start()
asr.start()
spotter.start()

# STATE 0: IDLE
Bridge.call("set_state", 0)
print(f"\n✅ All systems online! 💤 Listening for '{target_keyword}'...")


def loop():
    global app_state, user_text

    if app_state == "IDLE":
        time.sleep(0.1)
        return

    elif app_state == "LISTENING":
        user_text = ""
        partial_text = ""

        # STATE 1: LISTENING
        Bridge.call("set_state", 1)

        # Play audio cue when the assistant starts listening
        if BOOP_SOUND_PATH and os.path.exists(BOOP_SOUND_PATH):
            os.system(f"aplay -D {ALSA_DEVICE} {BOOP_SOUND_PATH} -q")

        mic_asr.start()
        time.sleep(0.1)

        print("\n🟢 ASR ACTIVE: Tell me your command! (Speak now)...")

        try:
            with asr.transcribe_stream(duration=7) as stream:
                for chunk in stream:
                    match chunk.type:
                        case "partial_text":
                            print(f"\r👂 Listening: {chunk.data}", end="", flush=True)
                            partial_text = chunk.data.strip()
                        case "full_text":
                            user_text = chunk.data.strip()
                            break

        # --- ASR EXCEPTION HANDLING ---
        except (ReadTimeout, ConnectionError) as e:
            print(f"\n⚠️ Container Connection Timeout: {e}")
            user_text = ""
        except RuntimeError as e:
            print(f"\n⚠️ ASR Container Crashed (Timeout): {e}")
            user_text = ""
        except Exception as e:
            if partial_text and not user_text:
                user_text = partial_text
            else:
                print(f"\n⚠️ Unexpected ASR Error: {e}")
                user_text = ""
        # ------------------------------

        if partial_text and not user_text:
            user_text = partial_text

        time.sleep(1.5)  # Allow ASR hardware resources to release safely
        mic_asr.stop()

        if user_text:
            print(f"\n🗣️ You said: {user_text}")
            # STATE 2: PROCESSING
            Bridge.call("set_state", 2)
            app_state = "PROCESSING"
        else:
            print("\n🤷 No command heard or ASR timeout. Returning to sleep...")
            Bridge.call("set_state", 0)  # Turn off LED matrix
            app_state = "IDLE"
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

            time.sleep(0.5)  # Allow NPU to release memory bus

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
        app_state = "IDLE"


try:
    App.run(user_loop=loop)
except (KeyboardInterrupt, SystemExit):
    print("\nStopping application...")
finally:
    spotter.stop()
    mic_spotter.stop()
    mic_asr.stop()
    asr.stop()
    ha_mapper.stop()

    try:
        Bridge.call("set_state", 0)  # Turn off LED matrix on exit
    except Exception:
        pass
