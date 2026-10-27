"""Spoken-command grammar: dictionary, phrases, edits, formatters, planning.

Pure Python: no Windows API, no audio, so it can be tested off-line.
"""
import re

DEFAULT_PUNCTUATION = {
    ",": ["comma", "add comma", "punctuation comma"],
    ".": ["period", "full stop", "add period", "punctuation period"],
    ";": ["semicolon", "add semicolon", "punctuation semicolon"],
    ":": ["colon", "add colon", "punctuation colon"],
    "?": ["question mark", "add question mark"],
    "!": ["exclamation mark", "exclamation point", "add exclamation mark"],
    "-": ["dash", "hyphen", "add dash"],
    "(": ["open bracket", "open parenthesis"],
    ")": ["close bracket", "close parenthesis"],
    "'": ["apostrophe", "single quote"],
    '"': ["quote", "double quote"],
}

_cfg: dict = {}


def configure(cfg: dict) -> None:
    """Give this module the live config (load_phrases/apply_dictionary need it)."""
    global _cfg
    _cfg = cfg


def _last_sentence_words(text: str) -> int:
    parts = [part for part in re.split(r"(?<=[.!?])\s+", text.rstrip()) if part.strip()]
    return len(parts[-1].split()) if parts else 0
def _clean_spacing(text: str) -> str:
    """Whisper sometimes emits "word ." - pull punctuation back onto the word."""
    text = re.sub(r"\s+([,.;:!?%)\]}])", r"\1", text)
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()
def _is_word(token: str) -> bool:
    return any(ch.isalnum() for ch in token)
def _tokens_for_words(text: str, words: int) -> int:
    """How many whitespace tokens the last `words` words plus trailing punctuation span."""
    if words <= 0:
        return 0
    span = 0
    seen = 0
    for token in reversed(text.rstrip().split()):
        span += 1
        if _is_word(token):
            seen += 1
            if seen >= words:
                break
    return span if seen else 0
def _trim_tokens(text: str, span: int) -> str:
    tokens = text.rstrip().split()
    if span >= len(tokens):
        return ""
    return " ".join(tokens[: len(tokens) - span])
def apply_dictionary(text: str) -> str:
    """Fix the nouns whisper reliably mishears (deterministic, no model)."""
    table = _cfg.get("dictionary") or {}
    if not table:
        return text
    for spoken in sorted(table, key=len, reverse=True):
        replacement = table[spoken]
        pattern = r"(?<!\w)" + re.escape(spoken) + r"(?!\w)"
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text
FORMAT_STYLES = {
    "snake": "snake",
    "kebab": "kebab",
    "camel": "camel",
    "title": "title",
    "upper": "upper",
    "lower": "lower",
}
def format_words(text: str, style: str) -> str:
    words = [word for word in re.split(r"[^A-Za-z0-9]+", text) if word]
    if not words:
        return ""
    if style == "snake":
        return "_".join(word.lower() for word in words)
    if style == "kebab":
        return "-".join(word.lower() for word in words)
    if style == "camel":
        return words[0].lower() + "".join(word.capitalize() for word in words[1:])
    if style == "title":
        return " ".join(word.capitalize() for word in words)
    if style == "upper":
        return " ".join(word.upper() for word in words)
    if style == "lower":
        return " ".join(word.lower() for word in words)
    return " ".join(words)
def parse_format(text: str) -> tuple[str, str] | None:
    """`snake case hello world` -> ('format:snake', 'hello world')."""
    match = re.match(r"^\s*(snake|kebab|camel|title|upper|lower)\s+case\s+(.+)$", text, re.IGNORECASE)
    if not match:
        return None
    style = match.group(1).lower()
    if style not in FORMAT_STYLES:
        return None
    return ("format:" + style, match.group(2).strip())
def classify(text: str) -> str | None:
    """Map a spoken phrase to a command action, if the whole phrase is one."""
    return _PHRASES.get(_normalise(text))
def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]
NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_EDIT_VERBS = r"(?:delete|remove|undo|cut)"
_EDIT_NUM_WORDS = re.compile(
    _EDIT_VERBS + r"\s+(?:the\s+)?(?:last\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:words?)?"
)
_EDIT_LAST_WORD = re.compile(_EDIT_VERBS + r"\s+(?:the\s+)?last\s+word")
_EDIT_LAST_SENTENCE = re.compile(_EDIT_VERBS + r"\s+(?:the\s+)?last\s+sentence")
_PHRASES: dict[str, str] = {}
def load_phrases() -> None:
    """Flatten compose_commands and punctuation into phrase -> action, longest first."""
    _PHRASES.clear()
    table = _cfg.get("compose_commands") or {}
    for action, phrases in table.items():
        for phrase in phrases:
            normalised = _normalise(phrase)
            if normalised:
                _PHRASES[normalised] = action

    punctuation = dict(DEFAULT_PUNCTUATION)
    for char, phrases in (_cfg.get("punctuation_commands") or {}).items():
        punctuation[char] = phrases
    for char, phrases in punctuation.items():
        for phrase in phrases:
            normalised = _normalise(phrase)
            if normalised:
                _PHRASES.setdefault(normalised, "punct:" + char)
def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())
def _collapse(actions: list[str]) -> list[str]:
    out: list[str] = []
    for action in actions:
        if not out or out[-1] != action:
            out.append(action)
    return out
def _consume_all(normalised: str) -> list[str] | None:
    """True only if the whole utterance is a sequence of command phrases."""
    tokens = normalised.split()
    ordered = sorted(_PHRASES, key=lambda p: len(p.split()), reverse=True)
    actions: list[str] = []
    index = 0
    while index < len(tokens):
        for phrase in ordered:
            words = phrase.split()
            if tokens[index:index + len(words)] == words:
                actions.append(_PHRASES[phrase])
                index += len(words)
                break
        else:
            return None
    return actions or None
def _leading_command(text: str) -> tuple[str, str] | None:
    """A multi-word command phrase at the start of a longer utterance.

    Single-word phrases are excluded on purpose: "send me the file" and
    "copy the path" are prose, not commands.
    """
    ordered = sorted((p for p in _PHRASES if len(p.split()) >= 2), key=len, reverse=True)
    for phrase in ordered:
        match = re.match(r"^\s*" + re.escape(phrase) + r"\b[\s,.!?]*", text, re.IGNORECASE)
        if match:
            return _PHRASES[phrase], text[match.end():].strip()
    return None
def _trailing_command(text: str) -> tuple[str, str] | None:
    """A command phrase at the very end of a longer utterance."""
    ordered = sorted(_PHRASES, key=len, reverse=True)
    for phrase in ordered:
        match = re.search(r"(?:^|\s)" + re.escape(phrase) + r"[\s.!?]*$", text, re.I)
        if match:
            return text[:match.start()].strip(), _PHRASES[phrase]
    return None
def parse_edit(text: str) -> tuple[str, str] | None:
    """Whole-utterance edit commands, tolerant of punctuation and missing words."""
    norm = _normalise(text)
    if _EDIT_LAST_SENTENCE.fullmatch(norm):
        return ("delete_sentence", "")
    if _EDIT_LAST_WORD.fullmatch(norm):
        return ("delete_words", "1")
    match = _EDIT_NUM_WORDS.fullmatch(norm)
    if match:
        token = match.group(1)
        count = int(token) if token.isdigit() else NUMBERS.get(token, 0)
        if count:
            return ("delete_words", str(count))
    return None
def plan_transcript(text: str) -> list[tuple[str, str]]:
    """Split into ordered steps: prose runs, spoken commands, word deletions."""
    whole = _consume_all(_normalise(text))
    if whole:
        return [(action, "") for action in _collapse(whole)]

    steps: list[tuple[str, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            steps.append(("add", " ".join(buffer)))
            buffer.clear()

    for sentence in split_sentences(text):
        edit = parse_edit(sentence)
        fmt = None if edit else parse_format(sentence)
        action = None if (edit or fmt) else classify(sentence)
        if edit:
            flush()
            steps.append(edit)
        elif fmt:
            flush()
            steps.append(fmt)
        elif action:
            flush()
            if not steps or steps[-1][0] != action:
                steps.append((action, ""))
        else:
            buffer.append(sentence)

    if buffer:
        joined = " ".join(buffer)
        edit = parse_edit(joined)
        leading = None if edit else _leading_command(joined)
        trailing = None if (edit or leading) else _trailing_command(joined)
        if edit:
            steps.append(edit)
        elif leading:
            action, rest = leading
            if not steps or steps[-1][0] != action:
                steps.append((action, ""))
            if rest:
                steps.append(("add", rest))
        elif trailing:
            head, action = trailing
            if head:
                steps.append(("add", head))
            if not steps or steps[-1][0] != action:
                steps.append((action, ""))
        else:
            steps.append(("add", joined))
    return steps
