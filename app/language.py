"""Language stability across a call.

`language_mode="auto"` used to mean "believe the STT's language guess on every
single utterance". Sarvam guesses per utterance with no memory of the call, so a
1.5-second Marathi fragment came back `pa-IN`, and the agent answered the next
turn in Punjabi — script, voice and all — in the middle of a Marathi call. The
caller then says "this isn't Marathi", which is itself a short utterance, and
the call spirals.

The fix is not to distrust detection, it is to require *agreement*. A new
language has to be detected on consecutive turns before the agent follows it.
An optional allow-list bounds the whole thing: a Marathi/Hindi/English agent
should never be dragged into Punjabi no matter how many times detection says so.

Duration alone turned out to be the wrong reliability test. Gating on
`min_seconds` meant a caller who switched to English in short phrases — "Hello",
"Yes, you are talking to" — never cast a single vote, so the agent answered in
Hindi no matter how many times they used English. The stronger signal is the
*script of the transcript*: `en-IN` on Latin text corroborates itself, and
`pa-IN` on Devanagari text contradicts itself, both regardless of clip length.
Duration is now only the fallback for a transcript whose script says nothing
(digits, punctuation, a transliterated fragment).

Above all of it sits what the caller says in words. "Can you talk in English?"
is not a detection to be debounced, it is an instruction, and it switches the
call on the turn it is heard. Debouncing that request is what left a caller
asking for English three times and being answered in Hindi each time.

`language_mode="fixed"` bypasses all of this and always speaks the configured
language — still the right choice for a single-language campaign.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Below this, a clip is too short for either the transcript or the language
# guess to mean anything, whatever script came back.
MIN_VOTE_SECONDS = 0.25

# Unicode blocks per script. Latin is deliberately narrow (Basic Latin letters
# plus the Latin-1/Extended-A letters) so punctuation and digits do not vote.
_SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "latin": ((0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F)),
    "devanagari": ((0x0900, 0x097F),),
    "bengali": ((0x0980, 0x09FF),),
    "gurmukhi": ((0x0A00, 0x0A7F),),
    "gujarati": ((0x0A80, 0x0AFF),),
    "odia": ((0x0B00, 0x0B7F),),
    "tamil": ((0x0B80, 0x0BFF),),
    "telugu": ((0x0C00, 0x0C7F),),
    "kannada": ((0x0C80, 0x0CFF),),
    "malayalam": ((0x0D00, 0x0D7F),),
}

# The script each language is actually written in, keyed by the primary subtag.
_LANGUAGE_SCRIPT: dict[str, str] = {
    "en": "latin",
    "hi": "devanagari", "mr": "devanagari", "ne": "devanagari", "sa": "devanagari",
    "bn": "bengali", "as": "bengali",
    "pa": "gurmukhi",
    "gu": "gujarati",
    "or": "odia",
    "ta": "tamil",
    "te": "telugu",
    "kn": "kannada",
    "ml": "malayalam",
}

# Share of a transcript's letters that must sit in one script before that script
# is treated as the transcript's own. Below it the text is a mix — Hinglish, a
# brand name inside Devanagari — and says nothing either way.
_SCRIPT_MAJORITY = 0.6

# A reply is only treated as written in the wrong script when the language's own
# script is this close to absent, and the script that replaced it is more than a
# stray word. See `conflicting_script`.
_OWN_SCRIPT_MIN_SHARE = 0.10
_FOREIGN_SCRIPT_MIN_CHARS = 4

# Human-readable names, for telling the model which language to write in. A
# BCP-47 code in a prompt is a token a small model happily ignores; "English" is
# not.
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English", "hi": "Hindi", "mr": "Marathi", "bn": "Bengali",
    "pa": "Punjabi", "gu": "Gujarati", "or": "Odia", "ta": "Tamil",
    "te": "Telugu", "kn": "Kannada", "ml": "Malayalam", "ne": "Nepali",
    "as": "Assamese", "sa": "Sanskrit",
}

_SCRIPT_LABELS: dict[str, str] = {
    "latin": "the Latin alphabet", "devanagari": "the Devanagari script",
    "bengali": "the Bengali script", "gurmukhi": "the Gurmukhi script",
    "gujarati": "the Gujarati script", "odia": "the Odia script",
    "tamil": "the Tamil script", "telugu": "the Telugu script",
    "kannada": "the Kannada script", "malayalam": "the Malayalam script",
}

# The language to speak when all we know is the script a piece of text is
# written in. Devanagari is shared, so this is a default and not a deduction —
# callers of `language_for_script` pass the codes they would rather have.
_SCRIPT_DEFAULT_LANGUAGE: dict[str, str] = {
    "latin": "en-IN", "devanagari": "hi-IN", "bengali": "bn-IN",
    "gurmukhi": "pa-IN", "gujarati": "gu-IN", "odia": "or-IN",
    "tamil": "ta-IN", "telugu": "te-IN", "kannada": "kn-IN",
    "malayalam": "ml-IN",
}

# A caller who says "can you speak in English?" has given a far stronger signal
# than any detector: they named the language themselves, in words. Making them
# earn `confirmations` more turns before the agent follows is what produced the
# worst artefact on the recordings — the caller asks for English and gets
# another Hindi sentence, twice.
_LANGUAGE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("en-IN", ("english", "englis", "angrezi", "angreji", "इंग्लिश", "अंग्रेजी", "अंग्रेज़ी")),
    ("hi-IN", ("hindi", "हिंदी", "हिन्दी")),
    ("mr-IN", ("marathi", "मराठी")),
    ("bn-IN", ("bengali", "bangla", "বাংলা")),
    ("pa-IN", ("punjabi", "ਪੰਜਾਬੀ")),
    ("gu-IN", ("gujarati", "ગુજરાતી")),
    ("ta-IN", ("tamil", "தமிழ்")),
    ("te-IN", ("telugu", "తెలుగు")),
    ("kn-IN", ("kannada", "ಕನ್ನಡ")),
    ("ml-IN", ("malayalam", "മലയാളം")),
)

# The language name alone is not a request — "my English is not good" names one
# without asking for it. One of these has to be in the sentence too.
_REQUEST_CUES: tuple[str, ...] = (
    "speak", "talk", "say", "switch", "change", "prefer", "understand",
    "in english", "in hindi", "bol", "boliye", "bolo", "baat", "karo",
    "kijiye", "mein", "me ", " me", "बोल", "बात", "में", "कर", "समझ",
)


def script_counts(text: str) -> dict[str, int]:
    """How many letters of each script the text contains. Digits and
    punctuation are in no script and are not counted."""
    counts: dict[str, int] = {}
    for char in text or "":
        code = ord(char)
        for script, ranges in _SCRIPT_RANGES.items():
            if any(low <= code <= high for low, high in ranges):
                counts[script] = counts.get(script, 0) + 1
                break
    return counts


def dominant_script(text: str) -> str | None:
    """The script a transcript is written in, or None if it is mixed or empty."""
    counts = script_counts(text)
    total = sum(counts.values())
    if not total:
        return None
    script, count = max(counts.items(), key=lambda item: item[1])
    return script if count / total >= _SCRIPT_MAJORITY else None


def conflicting_script(text: str, language: str | None) -> str | None:
    """The script `text` is really in, when `language` cannot be read in it.

    Deliberately not `dominant_script`. An Indian agent writes Hinglish, and
    "जी Krishna जी, आपके account में margin shortfall है" has more Latin letters
    than Devanagari ones while still being a Hindi sentence that only a Hindi
    voice can pronounce — a majority vote hands it to an English voice and the
    caller hears mangled nonsense. So the question asked here is not "which
    script wins" but "is the language's own script missing entirely": a reply
    with any real amount of Devanagari in it belongs to the Devanagari voice,
    and only a reply with none at all is spoken as something else.
    """
    wanted = expected_script(language)
    counts = script_counts(text)
    total = sum(counts.values())
    if not wanted or not total:
        return None
    if counts.get(wanted, 0) / total >= _OWN_SCRIPT_MIN_SHARE:
        return None
    script, count = max(counts.items(), key=lambda item: item[1])
    return script if count >= _FOREIGN_SCRIPT_MIN_CHARS else None


def expected_script(language: str | None) -> str | None:
    """The script a BCP-47 code should be transcribed in, if we know it."""
    if not language:
        return None
    return _LANGUAGE_SCRIPT.get(language.split("-")[0].lower())


def language_name(language: str | None) -> str:
    """"en-IN" -> "English". Falls back to the code, which is better than
    dropping the instruction entirely."""
    if not language:
        return ""
    return _LANGUAGE_NAMES.get(language.split("-")[0].lower(), language)


def script_label(language: str | None) -> str:
    """A spellable name for the script a language is written in, for prompts."""
    return _SCRIPT_LABELS.get(expected_script(language) or "", "")


def language_for_script(script: str | None, *preferred: str | None) -> str | None:
    """A language code written in `script`, preferring the codes given.

    Devanagari does not identify a language on its own, so the caller passes the
    languages this call might plausibly be in — the agent's own and the current
    one — and only falls through to the default when neither fits.
    """
    if not script:
        return None
    for code in preferred:
        if code and expected_script(code) == script:
            return code
    return _SCRIPT_DEFAULT_LANGUAGE.get(script)


def detect_language_request(text: str) -> str | None:
    """The language the caller asked for out loud, if they asked for one.

    Deliberately conservative: the sentence must both name a language and read
    like a request, so "my Hindi is weak" does not switch the call to Hindi.
    """
    lowered = (text or "").lower()
    if not lowered:
        return None
    if not any(cue in lowered for cue in _REQUEST_CUES):
        return None
    for code, words in _LANGUAGE_WORDS:
        if any(word in lowered for word in words):
            return code
    return None


@dataclass
class LanguageDecision:
    """What the agent should speak this turn, and why it did or didn't move."""

    language: str
    switched: bool = False
    # Set when a detection was seen but deliberately not followed. Logged and
    # surfaced in the UI so a wrong lock is diagnosable rather than mysterious.
    ignored: str = ""


class LanguageTracker:
    """Debounces per-utterance language detection into a stable call language."""

    def __init__(
        self,
        primary: str,
        *,
        mode: str = "auto",
        allowed: list[str] | None = None,
        confirmations: int = 2,
        min_seconds: float = 1.0,
    ) -> None:
        self._mode = mode
        self._primary = primary
        self._current = primary
        self._allowed = {code for code in (allowed or []) if code}
        # The agent's own language is always allowed, whatever the list says.
        if self._allowed:
            self._allowed.add(primary)
        self._confirmations = max(int(confirmations), 1)
        self._min_seconds = max(float(min_seconds), 0.0)

        self._candidate: str | None = None
        self._votes = 0

    @property
    def current(self) -> str:
        return self._current

    def update(
        self, detected: str | None, speech_secs: float, transcript: str = ""
    ) -> LanguageDecision:
        if self._mode == "fixed":
            return LanguageDecision(self._primary)

        # An explicit request outranks detection, the confirmation count and the
        # clip length all at once. The caller said the word.
        asked = detect_language_request(transcript)
        if asked:
            if self._allowed and asked not in self._allowed:
                return LanguageDecision(
                    self._current, ignored=f"{asked} asked for but not allowed"
                )
            self._candidate = None
            self._votes = 0
            if asked != self._current:
                previous = self._current
                self._current = asked
                logger.info(
                    "Call language switched %s -> %s (the caller asked for it)",
                    previous, asked,
                )
                return LanguageDecision(asked, switched=True)
            return LanguageDecision(self._current)

        if not detected:
            return LanguageDecision(self._current)

        if detected == self._current:
            # Agreement with the status quo also clears a half-formed switch:
            # two Marathi turns either side of one Punjabi blip should not
            # eventually add up to a move to Punjabi.
            self._candidate = None
            self._votes = 0
            return LanguageDecision(self._current)

        if self._allowed and detected not in self._allowed:
            return LanguageDecision(
                self._current, ignored=f"{detected} not in allowed languages"
            )

        if speech_secs < MIN_VOTE_SECONDS:
            return LanguageDecision(
                self._current, ignored=f"{detected} on a {speech_secs:.1f}s clip"
            )

        script = dominant_script(transcript)
        wanted = expected_script(detected)
        if script and wanted and script != wanted:
            # STT named a language its own transcript is not written in — a
            # Devanagari sentence labelled pa-IN. The label is the wrong half.
            return LanguageDecision(
                self._current,
                ignored=f"{detected} but the transcript is {script}",
            )

        corroborated = bool(script and wanted and script == wanted)
        if not corroborated and speech_secs < self._min_seconds:
            # Nothing in the text backs the guess up, and the clip is too short
            # for the guess to stand on its own. No vote.
            return LanguageDecision(
                self._current, ignored=f"{detected} on a {speech_secs:.1f}s clip"
            )

        if detected == self._candidate:
            self._votes += 1
        else:
            self._candidate = detected
            self._votes = 1

        # A long clip whose script backs its own label up is worth as much as two
        # short agreements. Three seconds of Latin text labelled en-IN is not a
        # guess waiting to be corrected, and making the caller say a second full
        # English sentence before the agent follows is the delay they experience
        # as "it can't work out what language I'm speaking". Short corroborated
        # clips still need the full count: "Hello" is Latin on every Hindi call
        # ever made, and switching on it is the flip-flop the debounce exists for.
        needed = 1 if corroborated and speech_secs >= self._min_seconds else self._confirmations

        if self._votes >= needed:
            previous = self._current
            self._current = detected
            self._candidate = None
            self._votes = 0
            logger.info("Call language switched %s -> %s", previous, detected)
            return LanguageDecision(detected, switched=True)

        return LanguageDecision(
            self._current,
            ignored=f"{detected} needs {needed - self._votes} more turn(s)",
        )


def parse_allowed(raw: str | None) -> list[str]:
    """Parse the comma-separated allow-list stored on the agent row."""
    return [code.strip() for code in (raw or "").split(",") if code.strip()]
