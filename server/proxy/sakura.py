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


def build_user_prompt(
    entries: list[GlossaryEntry],
    history: list[str],
    input_text: str,
) -> str:
    """Assemble the official GalTransl v3 user prompt.

    ``history`` holds previous *translated* lines (v3 passes history inside the
    single user message as ``历史翻译：``, not as multi-turn chat).
    """
    history_block = "历史翻译：" + "\n".join(history) + "\n" if history else ""
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
