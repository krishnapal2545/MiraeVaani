"""Smart conversational fillers.

The silence between a caller finishing their sentence and the agent starting to
speak is `STT (0.3-0.8s) + LLM (0.4-0.9s) + TTS TTFB (0.3-0.7s)` — roughly one
to two and a half seconds. Callers read that as a dropped line and start talking
again, which trips barge-in and makes the turn worse.

The filler covers that gap. Two constraints shape the design:

- You cannot classify anything until STT returns, but once it does the whole
  LLM + TTS window is still ahead. So the filler is chosen *after* STT and raced
  against the real response.
- A filler that needs its own TTS round-trip is not a filler. Every clip is
  synthesized once at session start and cached as mu-law bytes.
"""

import asyncio
import logging
import random
import re

from app.providers.base import TTSProvider

logger = logging.getLogger(__name__)

# Category -> language -> candidate phrases.
#
# A filler in the wrong language is worse than no filler: it is the agent
# audibly changing language for half a second and changing back. So a language
# with no entry here gets silence, never a Hindi clip — see `FillerBank.get`.
FILLER_BANK: dict[str, dict[str, list[str]]] = {
    "lookup": {
        "hi-IN": ["एक सेकंड, मैं चेक कर रही हूँ।", "जी, अभी देखती हूँ।"],
        "en-IN": ["One second, let me check that.", "Sure, just looking that up."],
        "mr-IN": ["एक सेकंद, मी बघते.", "हो, मी आत्ता बघते."],
        "gu-IN": ["એક સેકન્ડ, હું જોઈ રહી છું.", "હા, હું અત્યારે જોઉં છું."],
        "bn-IN": ["এক সেকেন্ড, আমি দেখছি।", "হ্যাঁ, এখনই দেখছি।"],
        "ta-IN": ["ஒரு நிமிடம், நான் பார்க்கிறேன்.", "சரி, இப்போது பார்க்கிறேன்."],
        "te-IN": ["ఒక సెకను, నేను చూస్తున్నాను.", "అలాగే, ఇప్పుడే చూస్తాను."],
        "kn-IN": ["ಒಂದು ಕ್ಷಣ, ನಾನು ನೋಡುತ್ತಿದ್ದೇನೆ.", "ಸರಿ, ಈಗಲೇ ನೋಡುತ್ತೇನೆ."],
        "ml-IN": ["ഒരു നിമിഷം, ഞാൻ നോക്കുന്നു.", "ശരി, ഇപ്പോൾ നോക്കാം."],
        "pa-IN": ["ਇੱਕ ਸਕਿੰਟ, ਮੈਂ ਵੇਖ ਰਹੀ ਹਾਂ।", "ਜੀ, ਹੁਣੇ ਵੇਖਦੀ ਹਾਂ।"],
    },
    "thinking": {
        "hi-IN": ["जी, एक मिनट।", "हाँ जी।"],
        "en-IN": ["Right, one moment.", "Okay."],
        "mr-IN": ["हो, एक मिनिट.", "हो जी."],
        "gu-IN": ["જી, એક મિનિટ.", "હા જી."],
        "bn-IN": ["জি, এক মিনিট।", "হ্যাঁ জি।"],
        "ta-IN": ["சரி, ஒரு நிமிடம்.", "ஆம் சார்."],
        "te-IN": ["సరే, ఒక నిమిషం.", "అవును సార్."],
        "kn-IN": ["ಸರಿ, ಒಂದು ನಿಮಿಷ.", "ಹೌದು ಸರ್."],
        "ml-IN": ["ശരി, ഒരു മിനിറ്റ്.", "അതെ സർ."],
        "pa-IN": ["ਜੀ, ਇੱਕ ਮਿੰਟ।", "ਹਾਂ ਜੀ।"],
    },
    "empathy": {
        "hi-IN": ["जी, मैं समझ सकती हूँ।", "जी, बिलकुल समझती हूँ।"],
        "en-IN": ["I completely understand.", "I see, I understand."],
        "mr-IN": ["हो, मी समजू शकते.", "हो, मला नक्की समजतंय."],
        "gu-IN": ["જી, હું સમજી શકું છું.", "હા, હું બરાબર સમજું છું."],
        "bn-IN": ["জি, আমি বুঝতে পারছি।", "হ্যাঁ, আমি একদম বুঝছি।"],
        "ta-IN": ["ஆம், நான் புரிந்துகொள்கிறேன்.", "சரி, எனக்குப் புரிகிறது."],
        "te-IN": ["అవును, నేను అర్థం చేసుకోగలను.", "సరే, నాకు అర్థమైంది."],
        "kn-IN": ["ಹೌದು, ನಾನು ಅರ್ಥಮಾಡಿಕೊಳ್ಳಬಲ್ಲೆ.", "ಸರಿ, ನನಗೆ ಅರ್ಥವಾಗಿದೆ."],
        "ml-IN": ["അതെ, എനിക്ക് മനസ്സിലാകുന്നു.", "ശരി, എനിക്ക് നന്നായി മനസ്സിലായി."],
        "pa-IN": ["ਜੀ, ਮੈਂ ਸਮਝ ਸਕਦੀ ਹਾਂ।", "ਹਾਂ ਜੀ, ਮੈਂ ਬਿਲਕੁਲ ਸਮਝਦੀ ਹਾਂ।"],
    },
    "confirm": {
        "hi-IN": ["जी बिलकुल।", "ठीक है जी।"],
        "en-IN": ["Absolutely.", "Understood."],
        "mr-IN": ["हो नक्कीच.", "ठीक आहे."],
        "gu-IN": ["જી ચોક્કસ.", "ઠીક છે જી."],
        "bn-IN": ["জি অবশ্যই।", "ঠিক আছে জি।"],
        "ta-IN": ["கண்டிப்பாக.", "சரி."],
        "te-IN": ["తప్పకుండా.", "సరే."],
        "kn-IN": ["ಖಂಡಿತ.", "ಸರಿ."],
        "ml-IN": ["തീർച്ചയായും.", "ശരി."],
        "pa-IN": ["ਜੀ ਬਿਲਕੁਲ।", "ਠੀਕ ਹੈ ਜੀ।"],
    },
}

# Both transliterated and native script: STT returns Devanagari for a Hindi or
# Marathi caller, so a Latin-only pattern set never fires on the majority of
# real calls and every turn falls through to "no filler".
_LOOKUP_PATTERNS = re.compile(
    r"kitna|kitne|kitni|balance|amount|paisa|paise|rupay|rupaye|account|"
    r"statement|shortfall|margin|date|kab|kaunsa|status|detail|"
    r"how much|how many|when|what is my|my account|my balance|"
    r"किती|कितना|कितने|रुपये|रक्कम|राशि|खाते|खाता|तारीख|तारीक|शिल्लक|बॅलन्स|बैलेंस",
    re.IGNORECASE,
)
_EMPATHY_PATTERNS = re.compile(
    r"pareshan|problem|dikkat|galat|shikayat|naraz|gussa|bekar|kharab|nahi hua|"
    r"complaint|issue|wrong|angry|upset|frustrat|not working|worst|bad|"
    r"अडचण|त्रास|चूक|चुकीच|तक्रार|परेशान|दिक्कत|गलत|शिकायत|नाराज|खराब",
    re.IGNORECASE,
)
_CONFIRM_PATTERNS = re.compile(
    r"^(haan|han|ha|ji|ok|okay|theek|thik|sahi|yes|yeah|sure|done|kar dunga|"
    r"kar dungi|karta hoon|karti hoon)\b",
    re.IGNORECASE,
)
_QUESTION_PATTERNS = re.compile(
    r"kaise|kyun|kyu|kya|matlab|samajh|batao|bataiye|"
    r"how do|why|what|explain|tell me|can you|"
    r"कसं|कसे|कशी|काय|का\b|क्या|कैसे|क्यों|म्हणजे|मतलब|सांगा|समजाव|बताओ|बताइए",
    re.IGNORECASE,
)


def classify(transcript: str) -> str | None:
    """Pick a filler category from the transcript. Keywords only — deliberately.

    Calling an LLM to choose a filler would reintroduce the very latency the
    filler exists to hide.
    """
    text = (transcript or "").strip()
    if not text:
        return None

    # Very short replies ("haan", "ok") get answered fast anyway; a filler in
    # front of them sounds robotic.
    if len(text.split()) <= 2:
        return None

    if _EMPATHY_PATTERNS.search(text):
        return "empathy"
    if _LOOKUP_PATTERNS.search(text):
        return "lookup"
    if _CONFIRM_PATTERNS.search(text):
        return "confirm"
    if _QUESTION_PATTERNS.search(text) or text.endswith("?"):
        return "thinking"
    return None


class FillerBank:
    """Pre-synthesized filler audio for one call, keyed by (category, language)."""

    def __init__(self, tts: TTSProvider, languages: list[str]) -> None:
        self._tts = tts
        self._languages = languages or ["hi-IN"]
        self._clips: dict[tuple[str, str], list[bytes]] = {}
        self._warmed: set[str] = set()
        self._warming: dict[str, asyncio.Task] = {}
        self._ready = asyncio.Event()

    async def warm(self) -> None:
        """Synthesize the starting languages' clips while the greeting plays."""
        await asyncio.gather(*(self._warm_language(lang) for lang in self._languages))
        self._ready.set()
        logger.info("Filler bank warmed: %d entries", len(self._clips))

    async def _warm_language(self, language: str) -> None:
        """Render every category for one language.

        Fired concurrently: these are a handful of very short requests, and
        doing them serially would risk the bank not being ready by the time the
        caller's first question needs it.
        """
        if language in self._warmed:
            return
        self._warmed.add(language)

        async def render(category: str, phrase: str):
            try:
                return category, await self._tts.synthesize(phrase, language)
            except Exception:
                logger.warning("Filler synthesis failed: %s [%s]", phrase, language)
                return category, b""

        jobs = [
            render(category, phrase)
            for category, by_language in FILLER_BANK.items()
            for phrase in by_language.get(language, [])
        ]
        if not jobs:
            return
        for category, audio in await asyncio.gather(*jobs):
            if audio:
                self._clips.setdefault((category, language), []).append(audio)

    def ensure_language(self, language: str | None) -> None:
        """Start warming a language the call has just switched into.

        Nothing awaits this: the current turn gets no filler, later turns in the
        new language do. Blocking a live turn on filler synthesis would defeat
        the entire point of a filler.
        """
        if not language or language in self._warmed:
            return
        if language not in FILLER_BANK["thinking"]:
            self._warmed.add(language)  # no phrases authored; don't retry
            return
        task = asyncio.create_task(self._warm_language(language))
        self._warming[language] = task
        task.add_done_callback(lambda _t, lang=language: self._warming.pop(lang, None))

    def cancel_warming(self) -> None:
        for task in list(self._warming.values()):
            task.cancel()
        self._warming.clear()

    def get(self, category: str, language: str | None) -> bytes | None:
        if not self._ready.is_set():
            return None
        lang = language or self._languages[0]
        clips = self._clips.get((category, lang))
        # No cross-language fallback on purpose: a Hindi filler dropped into a
        # Marathi call is the agent switching language for half a second, which
        # is precisely the artefact this release exists to remove. Silence is
        # the better failure.
        return random.choice(clips) if clips else None


class FillerController:
    """Decides whether a filler plays this turn, and which one."""

    def __init__(self, bank: FillerBank, *, enabled: bool = True) -> None:
        self._bank = bank
        self._enabled = enabled
        self._last_turn_played = False
        self._turns = 0
        self._played = 0

    @property
    def bank(self) -> FillerBank:
        return self._bank

    def choose(self, transcript: str, language: str | None) -> tuple[str, bytes] | None:
        """Returns (category, mulaw_audio) or None if no filler should play."""
        self._turns += 1

        if not self._enabled:
            return None

        # Never two turns in a row — that is what makes a bot sound like a bot.
        if self._last_turn_played:
            self._last_turn_played = False
            return None

        # Cap at roughly a third of turns.
        if self._turns > 3 and self._played / self._turns > 0.34:
            return None

        category = classify(transcript)
        if not category:
            return None

        audio = self._bank.get(category, language)
        if not audio:
            return None

        self._last_turn_played = True
        self._played += 1
        return category, audio
