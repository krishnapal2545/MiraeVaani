"""Live credential checks.

A wrong API key used to surface only mid-call — the agent answered to silence
and the reason sat in the server log. Every key is therefore probed against the
provider before it is written to disk, using the cheapest authenticated request
each provider offers.

Every probe collapses onto three states:

    ok       the provider accepted the key
    invalid  the provider rejected it — the key is not saved
    unknown  network trouble, or the key works but something around it does not
             (an API not enabled, a phone number not on the account). The key is
             saved and the message is shown as a warning.

Google is the awkward one: a bad API key comes back as HTTP 400 rather than 401,
so the body has to be read to tell "key is wrong" from "request was wrong".
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0, connect=8.0)

OK = "ok"
INVALID = "invalid"
UNKNOWN = "unknown"


@dataclass
class VerifyResult:
    status: str
    message: str

    @property
    def rejected(self) -> bool:
        return self.status == INVALID

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "message": self.message}


def _body_snippet(response: httpx.Response, limit: int = 200) -> str:
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:limit]
    if isinstance(error, str):
        return error[:limit]
    return response.text[:limit].replace("\n", " ").strip()


def _classify(response: httpx.Response, ok_message: str) -> VerifyResult:
    """Shared mapping for providers that answer 401/403 on a bad key."""
    if response.status_code < 300:
        return VerifyResult(OK, ok_message)
    if response.status_code in (401, 403):
        return VerifyResult(INVALID, f"Rejected by the provider: {_body_snippet(response)}")
    if response.status_code in (400, 422):
        # The key got past auth; the probe payload is what the provider disliked.
        return VerifyResult(OK, "Key accepted.")
    return VerifyResult(
        UNKNOWN, f"Could not verify (HTTP {response.status_code}): {_body_snippet(response)}"
    )


def _classify_google(response: httpx.Response, ok_message: str) -> VerifyResult:
    """Google returns 400/API_KEY_INVALID for a bad key and 403 when an API is off."""
    if response.status_code < 300:
        return VerifyResult(OK, ok_message)

    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    message = str(error.get("message") or _body_snippet(response))
    reasons = {
        str(d.get("reason", "")) for d in (error.get("details") or []) if isinstance(d, dict)
    }

    if "API_KEY_INVALID" in reasons or "api key not valid" in message.lower():
        return VerifyResult(INVALID, "Google rejected this API key.")
    if "API_KEY_SERVICE_BLOCKED" in reasons or "api_key_http_referrer_blocked" in message.lower():
        return VerifyResult(INVALID, f"Key is restricted: {message}")
    if "SERVICE_DISABLED" in reasons or "has not been used in project" in message.lower():
        return VerifyResult(UNKNOWN, f"Key is valid but the API is not enabled: {message}")
    if "api keys are not supported" in message.lower():
        # The key itself parses; this project only accepts OAuth for this API,
        # and every provider here authenticates with ?key=, so it cannot work.
        return VerifyResult(
            INVALID,
            "This project rejects API-key auth for the API (it wants OAuth). "
            "Use a key from a project where the API accepts API keys.",
        )
    if response.status_code in (401, 403):
        return VerifyResult(INVALID, f"Rejected by Google: {message}")
    return VerifyResult(UNKNOWN, f"Could not verify (HTTP {response.status_code}): {message}")


# ---------------------------------------------------------------------------
# Per-provider probes
# ---------------------------------------------------------------------------
async def _verify_sarvam(client: httpx.AsyncClient, secret: str) -> VerifyResult:
    response = await client.post(
        "https://api.sarvam.ai/text-to-speech",
        headers={"api-subscription-key": secret},
        json={"text": "ok", "target_language_code": "en-IN"},
    )
    return _classify(response, "Sarvam accepted the key.")


async def _verify_bhashini(client: httpx.AsyncClient, secret: str) -> VerifyResult:
    response = await client.post(
        "https://tts.bhashini.ai/v2/synthesize",
        headers={"X-API-KEY": secret},
        json={"text": "ok", "language": "Hindi", "voiceName": "hi-f3", "voiceStyle": "Neutral"},
    )
    return _classify(response, "Bhashini accepted the key.")


async def _verify_google(client: httpx.AsyncClient, secret: str) -> VerifyResult:
    """One key, two APIs — the call needs both, so both are probed."""
    tts = await client.get(
        "https://texttospeech.googleapis.com/v1/voices",
        params={"key": secret, "languageCode": "en-IN"},
    )
    tts_result = _classify_google(tts, "Text-to-Speech reachable.")
    if tts_result.rejected:
        return tts_result

    # An empty recognition request is free: 400 INVALID_ARGUMENT means the key
    # and the API are both fine and only the (deliberately empty) audio is not.
    stt = await client.post(
        "https://speech.googleapis.com/v1/speech:recognize",
        params={"key": secret},
        json={"config": {"languageCode": "en-IN"}, "audio": {"content": ""}},
    )
    stt_result = _classify_google(stt, "Speech-to-Text reachable.")
    if stt_result.rejected:
        return VerifyResult(INVALID, f"Speech-to-Text: {stt_result.message}")

    warnings = [
        f"Text-to-Speech: {tts_result.message}" if tts_result.status != OK else "",
        f"Speech-to-Text: {stt_result.message}" if stt_result.status != OK else "",
    ]
    warnings = [w for w in warnings if w]
    if warnings:
        return VerifyResult(UNKNOWN, " ".join(warnings))
    return VerifyResult(OK, "Speech-to-Text and Text-to-Speech both reachable.")


async def _verify_gemini(client: httpx.AsyncClient, secret: str) -> VerifyResult:
    response = await client.get(
        "https://generativelanguage.googleapis.com/v1beta/models", params={"key": secret}
    )
    return _classify_google(response, "Gemini accepted the key.")


async def _verify_groq(client: httpx.AsyncClient, secret: str) -> VerifyResult:
    if secret.startswith("xai-"):
        return VerifyResult(
            INVALID, "That is an xAI (Grok) key. Groq keys start with 'gsk_' — "
                     "paste this one into the xAI Grok field instead."
        )
    response = await client.get(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {secret}"},
    )
    return _classify(response, "Groq accepted the key.")


async def _verify_xai(client: httpx.AsyncClient, secret: str) -> VerifyResult:
    """/v1/api-key answers even for a team with no credits, so it says more
    than /v1/models: it reports whether the key or its team is blocked."""
    if secret.startswith("gsk_"):
        return VerifyResult(
            INVALID, "That is a Groq key. xAI keys start with 'xai-' — "
                     "paste this one into the Groq (Llama) field instead."
        )

    response = await client.get(
        "https://api.x.ai/v1/api-key", headers={"Authorization": f"Bearer {secret}"}
    )
    if response.status_code >= 300:
        # xAI answers a wrong key with 400 invalid-argument, not 401.
        message = _body_snippet(response)
        if "incorrect api key" in message.lower() or response.status_code in (400, 401, 403):
            return VerifyResult(INVALID, f"xAI rejected this key: {message}")
        return VerifyResult(UNKNOWN, f"Could not verify (HTTP {response.status_code}): {message}")

    info = response.json()
    if info.get("api_key_blocked") or info.get("api_key_disabled"):
        return VerifyResult(INVALID, "This xAI key is blocked or disabled in the console.")
    if info.get("team_blocked"):
        return VerifyResult(
            UNKNOWN,
            "Key is valid, but the xAI team is blocked — usually no credits yet. "
            "Buy credits at console.x.ai or calls will fail with permission-denied.",
        )
    return VerifyResult(OK, "xAI accepted the key.")


VERIFIERS: dict[str, Callable[[httpx.AsyncClient, str], Awaitable[VerifyResult]]] = {
    "sarvam": _verify_sarvam,
    "bhashini": _verify_bhashini,
    "google": _verify_google,
    "gemini": _verify_gemini,
    "groq": _verify_groq,
    "xai": _verify_xai,
}


async def verify_provider(key: str, secret: str) -> VerifyResult:
    probe = VERIFIERS.get(key)
    if probe is None:
        return VerifyResult(UNKNOWN, "No check available for this provider.")
    if not secret.strip():
        return VerifyResult(INVALID, "Key is empty.")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return await probe(client, secret.strip())
    except httpx.HTTPError as exc:
        logger.warning("Credential check for %s failed: %s", key, exc)
        return VerifyResult(UNKNOWN, f"Could not reach the provider: {exc}")


async def verify_twilio(account_sid: str, auth_token: str, from_number: str) -> VerifyResult:
    if not (account_sid and auth_token):
        return VerifyResult(INVALID, "Account SID and auth token are both required.")
    if not account_sid.startswith("AC"):
        return VerifyResult(INVALID, "An Account SID starts with 'AC'.")

    base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, auth=(account_sid, auth_token)) as client:
            account = await client.get(f"{base}.json")
            if account.status_code in (401, 403):
                return VerifyResult(INVALID, "Twilio rejected this SID / auth token pair.")
            if account.status_code == 404:
                return VerifyResult(INVALID, "No Twilio account with that SID.")
            if account.status_code >= 300:
                return VerifyResult(
                    UNKNOWN,
                    f"Could not verify (HTTP {account.status_code}): {_body_snippet(account)}",
                )

            status = (account.json().get("status") or "").lower()
            if status and status != "active":
                return VerifyResult(UNKNOWN, f"Credentials are valid but the account is {status}.")

            if not from_number:
                return VerifyResult(UNKNOWN, "Credentials are valid; no from-number set.")

            owned = await client.get(
                f"{base}/IncomingPhoneNumbers.json", params={"PhoneNumber": from_number}
            )
            if owned.status_code < 300 and not (owned.json().get("incoming_phone_numbers") or []):
                return VerifyResult(
                    UNKNOWN,
                    f"Credentials are valid, but {from_number} is not a number on this account.",
                )
    except httpx.HTTPError as exc:
        logger.warning("Twilio credential check failed: %s", exc)
        return VerifyResult(UNKNOWN, f"Could not reach Twilio: {exc}")

    return VerifyResult(OK, "Twilio credentials and from-number look good.")


async def verify_many(secrets: dict[str, str]) -> dict[str, VerifyResult]:
    """Probe several provider keys at once — they are independent HTTP calls."""
    keys = list(secrets)
    results = await asyncio.gather(
        *(verify_provider(key, secrets[key]) for key in keys), return_exceptions=True
    )

    out: dict[str, VerifyResult] = {}
    for key, result in zip(keys, results):
        if isinstance(result, BaseException):
            logger.warning("Credential check for %s raised: %s", key, result)
            out[key] = VerifyResult(UNKNOWN, f"Check failed: {result}")
        else:
            out[key] = result
    return out


def summarize(results: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {key: result.as_dict() for key, result in results.items()}


# ---------------------------------------------------------------------------
# Live model lists
# ---------------------------------------------------------------------------
# Providers list everything they host — speech, image, music, safety, robotics.
# None of those can hold a phone conversation, and offering them in the agent
# builder only produces a call that fails on the first turn.
_NON_CHAT = (
    "whisper", "prompt-guard", "orpheus", "tts", "embed", "guard", "distil",
    "image", "banana", "lyria", "-clip", "robotics", "computer-use",
    "transcribe", "omni", "deep-research", "antigravity", "veo", "imagen",
)

# Chat models that cannot do *this* job. `allam-2-7b` is a 7B Arabic/English
# model: it has no real Devanagari, it ignores a long call-flow prompt and
# reverts to generic assistant behaviour ("How may I assist you?") on an
# outbound script, it degenerates into repeated fragments when pushed to write
# Hindi, and Groq rejects tool calls for it — which silently costs the agent
# `end_call` and `record_outcome` for the whole call.
_WRONG_JOB = ("allam",)

# The dropdown lands on whichever id sorts first, so plain alphabetical order
# chose the account's worst model. Better models sort first now; anything
# unrecognised keeps its alphabetical place after them rather than ahead.
_PREFERRED = (
    "gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash",
    "grok-4-fast", "grok-4", "grok-3",
    "openai/gpt-oss-20b", "openai/gpt-oss-120b",
    "qwen", "llama", "gemma", "groq/compound-mini", "groq/compound",
)


def _rank(model_id: str) -> int:
    lowered = model_id.lower()
    for position, marker in enumerate(_PREFERRED):
        if lowered.startswith(marker) or marker in lowered:
            return position
    return len(_PREFERRED)


def _chat_only(ids: list[str]) -> list[str]:
    usable = [
        i for i in ids
        if not any(bad in i.lower() for bad in _NON_CHAT + _WRONG_JOB)
    ]
    return sorted(usable, key=lambda i: (_rank(i), i))


async def list_llm_models(provider: str, secret: str) -> list[str] | None:
    """What this account can actually run, or None if the provider won't say.

    Hardcoded model lists rot: Groq retired the llama-3.x ids this app shipped
    with, and the only symptom was every turn of a live call falling back to
    "Sorry, kya aap dobara bol sakte hain?" after a 404.
    """
    if not secret:
        return None

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if provider == "groq":
                response = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {secret}"},
                )
                if response.status_code >= 300:
                    return None
                return _chat_only([m["id"] for m in response.json().get("data", [])])

            if provider == "xai":
                response = await client.get(
                    "https://api.x.ai/v1/models",
                    headers={"Authorization": f"Bearer {secret}"},
                )
                if response.status_code >= 300:
                    return None
                return _chat_only([m["id"] for m in response.json().get("data", [])])

            if provider == "gemini":
                response = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": secret},
                )
                if response.status_code >= 300:
                    return None
                return _chat_only([
                    model["name"].removeprefix("models/")
                    for model in response.json().get("models", [])
                    if "generateContent" in (model.get("supportedGenerationMethods") or [])
                ])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Could not list %s models: %s", provider, exc)

    return None
