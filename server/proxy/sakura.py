"""Sakura-GalTransl v3.7 prompt construction and PlayTranslate request parsing.

Prompt format mirrors, verbatim, the official model card
(huggingface.co/SakuraLLM/Sakura-GalTransl-7B-v3.7) and GalTransl's
``Prompts.py`` (GalTransl_SYSTEM_PROMPT / GalTransl_TRANS_PROMPT_V3) and
``SakuraTranslate.py`` (history injection, newline escaping, sampling).
PlayTranslate default templates come from its ``LlmPromptTemplates.kt`` and
``ContextRing.kt`` (v3.0.1).
"""

import json
import re

from .glossary import GlossaryEntry

SYSTEM_PROMPT = (
    "你是一个视觉小说翻译模型，可以通顺地使用给定的术语表以指定的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，注意不要混淆使役态和被动态的主语和宾语，"
    "不要擅自添加原文中没有的特殊符号，也不要擅自增加或减少换行。"
)

# GalTransl "precise" sampling profile recommended for GalTransl v3.x models.
SAMPLING = {"temperature": 0.3, "top_p": 0.8, "frequency_penalty": 0.1}

# PlayTranslate's context block header and pair line format (ContextRing.kt);
# the pair separator is space + U+2192 RIGHTWARDS ARROW + space.
_PT_CONTEXT_HEADER = "Recent dialogue lines, for context only:\n"
_PT_CONTEXT_PAIR = re.compile(r"^- (.*) \u2192 (.*)$")
# PlayTranslate's default single-text wrapper line (LlmPromptTemplates.kt).
_PT_SINGLE_WRAPPER = re.compile(r"^Please translate the following .+ text into .+:\n\n")


def split_pt_user_message(content: str) -> tuple[list[tuple[str, str]], str]:
    """Split a PlayTranslate user message into (context pairs, payload).

    Understands PT's default templates; with a minimal custom template
    ("{context}{text}") the whole remainder is returned as the payload.
    """
    pairs: list[tuple[str, str]] = []
    rest = content
    if rest.startswith(_PT_CONTEXT_HEADER):
        block, sep, tail = rest[len(_PT_CONTEXT_HEADER):].partition("\n\n")
        if sep:
            parsed = [_PT_CONTEXT_PAIR.match(line) for line in block.split("\n")]
            if parsed and all(parsed):
                # A surplus " → " (source or translation containing the arrow)
                # always lands in group(1) under greedy matching; such a pair
                # is ambiguous — drop it rather than guess a wrong split.
                pairs = [
                    (match.group(1), match.group(2))
                    for match in parsed
                    if " → " not in match.group(1)
                ]
                rest = tail
    wrapper = _PT_SINGLE_WRAPPER.match(rest)
    if wrapper:
        rest = rest[wrapper.end():]
    return pairs, rest


def extract_batch_texts(payload: str) -> list[str] | None:
    """Extract the JSON string array from a PT batch payload (its final line)."""
    tail = payload.rsplit("\n", 1)[-1].strip()
    for candidate in (tail, payload.strip()):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return parsed
    return None


def escape_line(text: str) -> str:
    """Escape real newlines to the literal two-character sequence GalTransl uses."""
    return text.replace("\r\n", "\\n").replace("\n", "\\n")


def unescape_line(text: str) -> str:
    """Reverse :func:`escape_line` on a model output line."""
    return text.replace("\\n", "\n")


# A dialogue box cut mid-sentence ends on a particle or a conjugating form -
# grammar that cannot end an utterance. On-screen LABELS (place names, menu
# captions, signposts) end on a noun instead, which is exactly what made
# ワカバタウン ("Wakaba Town", a permanent map caption) glue itself onto every
# line of dialogue. Requiring one of these endings keeps labels out.
_CONTINUATION_ENDINGS = (
    # case/binding particles
    "を", "に", "へ", "と", "が", "は", "も", "や", "で", "から", "まで", "より",
    # te-form and conjunctive endings that demand a continuation
    "て", "で", "り", "し", "ば", "たら", "なら", "けど", "けれど", "ので", "のに",
    # comma-like pauses
    "、", "，",
)

# Sentence-final characters: a box ending with one of these is a finished
# sentence; anything else is treated as continuing into the next box.
_COMPLETE_ENDINGS = tuple("。．｡!！?？…‥♪」』）)")
# Sentence-final grammatical forms: in-game manual/menu pages often end
# sentences WITHOUT punctuation (e.g. せつめいします / えらんでください), and
# treating those as continuations would snowball joins across every page.
_COMPLETE_FORMS = (
    "ます", "です", "ました", "ません", "でした", "ましょう",
    "ください", "である", "だ", "よ", "ね",
    # Casual sentence-final particles/forms common in Pokémon dialogue; safe
    # because true continuations end in 、 or case particles (を/に/て...),
    # never in these. Deliberately NOT included: な (prenominal おおきな
    # is a genuine continuation), の (noun-phrase の likewise).
    "さ", "ぞ", "ぜ", "わ", "かな", "かい", "だろう", "でしょう", "くれ",
)


def is_sentence_complete(text: str) -> bool:
    """True when a dialogue-box text ends a sentence (or is empty)."""
    stripped = text.rstrip()
    return (
        not stripped
        or stripped.endswith(_COMPLETE_ENDINGS)
        or stripped.endswith(_COMPLETE_FORMS)
    )


def continues_into_next_box(text: str) -> bool:
    """True when ``text`` is an utterance that was cut off mid-sentence.

    Stricter than ``not is_sentence_complete(text)``: the text must actually
    END on grammar that demands a continuation. Screen labels and captions
    are neither complete sentences nor continuations, so they are excluded
    from joining.
    """
    stripped = text.rstrip()
    if not stripped or is_sentence_complete(stripped):
        return False
    return stripped.endswith(_CONTINUATION_ENDINGS)


def join_continuation(
    context_pairs: list[tuple[str, str]],
    current: str,
    recent_sources: set[str] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Prepend unfinished previous box sources to the current text.

    Japanese is verb-final, so the leading half of a sentence split across
    dialogue boxes often carries no verb and translates badly in isolation.
    Consecutive trailing context pairs whose SOURCE does not end a sentence
    are treated as the current sentence's earlier boxes: their sources join
    the input (so the model translates the whole sentence, displayed on the
    current box) and their translations leave the history (the model would
    otherwise treat that part as already translated).

    ``recent_sources`` (when given) limits joining to lines this proxy
    translated moments ago. PlayTranslate's context holds whatever was on
    screen, including long-lived captions like a map's place name, which are
    not the previous utterance no matter how recently they were recorded.

    Returns (joined input, remaining history pairs). Trade-off: a false
    positive re-displays the previous box's meaning on the current box —
    mildly redundant, never wrong.
    """
    consumed = 0
    for source, _ in reversed(context_pairs):
        if not continues_into_next_box(source):
            break
        if recent_sources is not None and source not in recent_sources:
            break
        consumed += 1
    if not consumed:
        return current, list(context_pairs)
    prefix = "".join(source for source, _ in context_pairs[-consumed:])
    joined = prefix + _drop_overlap(prefix, current)
    return joined, list(context_pairs[:-consumed])


# Seams shorter than this only count when they end at a line boundary: a
# coincidental one-character match (て/て) must never eat real text, while a
# genuinely re-captured short line always ends where its newline was.
_MIN_OVERLAP = 4


def _drop_overlap(prefix: str, current: str) -> str:
    """Drop current's head where it repeats prefix's tail (scrolled boxes).

    A dialogue box that scrolls by one line re-captures the surviving line:
    the previous payload's tail reappears verbatim at the current payload's
    head, and joining without deduplication would translate that line twice
    (and display it twice). Longest exact overlap wins; OCR jitter that
    changes the re-captured text falls back to the old duplicated join,
    which is redundant but never wrong.
    """
    for k in range(min(len(prefix), len(current)), 0, -1):
        if not prefix.endswith(current[:k]):
            continue
        if k >= _MIN_OVERLAP or k == len(current) or current[k] == "\n":
            return current[k:]
    return current


def build_user_prompt(
    entries: list[GlossaryEntry],
    history: list[tuple[str, str]],
    input_text: str,
) -> str:
    """Assemble the official GalTransl v3 user prompt.

    ``history`` holds (source, translation) pairs of previous lines. GalTransl
    v3 puts history inside the single user message as ``历史翻译：`` (not as
    multi-turn chat) and passes translations only; the source is included
    alongside because translation-only history hides WHO was speaking, which
    made the model flip first person into third across dialogue boxes.
    """
    history_block = (
        "历史翻译：\n"
        + "\n".join(f"{source} → {translation}" for source, translation in history)
        + "\n"
        if history
        else ""
    )
    glossary_block = "\n".join(entry.to_line() for entry in entries)
    return (
        f"{history_block}\n"
        f"参考以下术语表（可为空，格式为src->dst #备注）：\n"
        f"{glossary_block}\n"
        f"根据以上术语表的对应关系和备注，结合历史剧情和上下文，将下面的文本从日文翻译成简体中文：\n"
        f"{input_text}"
    )


def split_output_lines(content: str) -> list[str]:
    """Split a batch completion into per-input lines (one line per input text)."""
    stripped = content.strip("\n")
    if not stripped:
        return []
    return [unescape_line(line) for line in stripped.split("\n")]
