"""Pre-flight check for every saved credential.

Run this before a demo. It exercises each configured provider once — transcribe
a generated tone, complete one prompt, synthesize one line — so a bad or expired
key surfaces in seconds instead of as silence on a live call.

Synthesized audio is written to `preflight/` so you can listen to each voice.

Run:  .venv/Scripts/python.exe scripts/check_providers.py
"""

import asyncio
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from app import db, registry  # noqa: E402
from app.audio import mulaw_to_pcm, pcm_to_wav_bytes  # noqa: E402
from app.models import AgentConfig  # noqa: E402
from app.registry import MissingCredential  # noqa: E402

OUT_DIR = Path("preflight")
SAMPLE_TEXT = "नमस्ते, मैं वाणी बोल रही हूँ।"

OK, FAIL, SKIP = "  [ok]  ", "  [FAIL]", "  [skip]"


def tone_wav(seconds: float = 1.0, rate: int = 16000, freq: int = 440) -> bytes:
    """A 16kHz WAV tone — enough to prove the STT endpoint accepts and answers."""
    frames = [
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(int(rate * seconds))
    ]
    return pcm_to_wav_bytes(b"".join(frames), rate)


async def check_stt() -> list[bool]:
    print("\nSpeech to text")
    results = []
    for entry in registry.CATALOG["stt"]:
        agent = AgentConfig(stt_provider=entry["provider"], stt_model=entry["models"][0])
        try:
            provider = registry.build_stt(agent)
        except MissingCredential as exc:
            print(f"{SKIP} {entry['label']}: {exc}")
            continue
        try:
            text, language = await provider.transcribe(tone_wav(), None)
            print(f"{OK} {entry['label']} ({entry['models'][0]}) "
                  f"-> transcript={text!r} language={language}")
            results.append(True)
        except Exception as exc:
            print(f"{FAIL} {entry['label']}: {exc}")
            results.append(False)
        finally:
            await provider.close()
    return results


async def check_llm() -> list[bool]:
    print("\nLanguage models")
    results = []
    for entry in registry.CATALOG["llm"]:
        agent = AgentConfig(llm_provider=entry["provider"], llm_model=entry["models"][0])
        try:
            provider = registry.build_llm(agent)
        except MissingCredential as exc:
            print(f"{SKIP} {entry['label']}: {exc}")
            continue
        try:
            reply = await provider.complete(
                [{"role": "user", "content": "Say 'ready' and nothing else."}],
                system="You are a test harness. Answer in one word.",
                tools=None, max_output_tokens=20,
            )
            if reply.text:
                print(f"{OK} {entry['label']} ({entry['models'][0]}) -> {reply.text!r}")
                results.append(True)
            else:
                print(f"{FAIL} {entry['label']}: empty response (check key or model name)")
                results.append(False)
        except Exception as exc:
            print(f"{FAIL} {entry['label']}: {exc}")
            results.append(False)
        finally:
            await provider.close()
    return results


async def check_tts() -> list[bool]:
    print("\nText to speech")
    OUT_DIR.mkdir(exist_ok=True)
    results = []
    for entry in registry.CATALOG["tts"]:
        voice = entry["voices"][0]["id"]
        agent = AgentConfig(tts_provider=entry["provider"], tts_voice=voice,
                            language="hi-IN")
        try:
            provider = registry.build_tts(agent)
        except MissingCredential as exc:
            print(f"{SKIP} {entry['label']}: {exc}")
            continue
        try:
            mulaw = await provider.synthesize(SAMPLE_TEXT, "hi-IN")
            if not mulaw:
                print(f"{FAIL} {entry['label']}: returned no audio")
                results.append(False)
                continue
            path = OUT_DIR / f"{entry['provider']}_{voice}.wav"
            path.write_bytes(pcm_to_wav_bytes(mulaw_to_pcm(mulaw), 8000))
            print(f"{OK} {entry['label']} (voice {voice}) -> "
                  f"{len(mulaw) / 8000:.1f}s  saved {path}")
            results.append(True)
        except Exception as exc:
            note = ""
            if entry["provider"] == "bhashini":
                note = "  (Bhashini returns MP3 — this needs ffmpeg on PATH)"
            print(f"{FAIL} {entry['label']}: {exc}{note}")
            results.append(False)
        finally:
            await provider.close()
    return results


def check_twilio() -> bool:
    print("\nTelephony")
    creds = db.get_credential_json("twilio")
    if not creds.get("account_sid"):
        print(f"{SKIP} Twilio: not configured")
        return False
    try:
        from twilio.rest import Client

        account = Client(creds["account_sid"], creds["auth_token"]).api.accounts(
            creds["account_sid"]
        ).fetch()
        print(f"{OK} Twilio account {account.friendly_name!r} status={account.status} "
              f"from={creds.get('from_number')}")
        return True
    except Exception as exc:
        print(f"{FAIL} Twilio: {exc}")
        return False


async def main():
    db.init()
    print("Pre-flight check\n" + "=" * 60)

    results = []
    results += await check_stt()
    results += await check_llm()
    results += await check_tts()
    results.append(check_twilio())

    print("\n" + "=" * 60)
    failed = results.count(False)
    if failed:
        print(f"{failed} check(s) FAILED — fix these before demoing.")
        sys.exit(1)
    if not results:
        print("Nothing configured yet. Add keys on the Credentials page first.")
        sys.exit(1)
    print(f"All {len(results)} configured provider(s) responded. "
          f"Listen to {OUT_DIR}/ to pick a voice.")


if __name__ == "__main__":
    asyncio.run(main())
