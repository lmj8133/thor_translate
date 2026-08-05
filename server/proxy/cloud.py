"""Generic cloud-model prompt construction (OpenAI-compatible providers).

Unlike the Sakura path (sakura.py), cloud models are general instruction
followers: they are asked for Taiwan Traditional output directly (so no
OpenCC pass on their output) and receive the glossary and prior context as
plain instructions. The line-count contract matches the Sakura path so the
batch splitting logic in main.py works unchanged for both.
"""

import re
from urllib.parse import urlsplit

from .glossary import GlossaryEntry

# The trailing /no_think is Qwen's soft switch for its hybrid-reasoning mode
# (a 2.5 s translation budget cannot afford visible chain-of-thought). Other
# model families treat it as stray system-prompt text - measured harmless on
# Gemini; providers with a real off-switch parameter get it via
# request_tweaks() below instead.
SYSTEM_PROMPT = (
    "你是遊戲文本翻譯引擎，將日文遊戲文本翻譯成台灣正體中文。規則：\n"
    "1. 只輸出譯文，不要任何解釋、註記、拼音或引號包裹。\n"
    "2. 輸出行數必須與輸入行數完全一致，逐行對應；行內的「\\n」字面序列原樣保留。\n"
    "3. 提供術語表時，原文中出現的術語必須使用指定譯名。\n"
    "4. 原文常以平假名書寫（兒童向文本），請依上下文正確判讀詞義。\n"
    "5. 用語採台灣習慣，對話語氣自然口語。\n"
    "/no_think"
)

SAMPLING = {"temperature": 0.2}

# Request tweaks that switch hybrid-reasoning models into plain answer mode
# (measured 2026-08-04/05: without these, the reasoning burns the token
# budget and the content arrives empty or truncated mid-sentence). The chain
# stays provider-neutral - these are compatibility shims keyed by the host
# that needs each field AND a model substring ("" = every model there),
# because acceptance is inconsistent even within one provider: Gemini's
# compat layer takes reasoning_effort "minimal" on 3.6-flash but 400s on
# "none" there, while 3.5-flash-lite rejects the field entirely. A new
# provider or model quirk is one row.
REQUEST_TWEAKS: list[tuple[str, str, dict]] = [
    ("api.groq.com", "", {"reasoning_effort": "none"}),
    ("api.z.ai", "", {"thinking": {"type": "disabled"}}),
    ("generativelanguage.googleapis.com", "gemini-3.6-flash", {"reasoning_effort": "minimal"}),
]

# Some models leak their reasoning into content as <think>/<thought> blocks
# (Gemma has no off-switch at all). Closed blocks or an unclosed block that
# runs to the end of the text are both dropped.
_REASONING_BLOCK = re.compile(r"<(think|thought)>.*?(</\1>|\Z)", re.DOTALL)


def request_tweaks(base_url: str, model: str = "") -> dict:
    """Extra request-body fields this (provider, model) needs, {} otherwise."""
    host = urlsplit(base_url).netloc
    for suffix, model_part, extra in REQUEST_TWEAKS:
        if (host == suffix or host.endswith("." + suffix)) and model_part in model:
            return dict(extra)
    return {}


def strip_reasoning(text: str) -> str:
    """Remove leaked reasoning blocks; the caller treats leftovers of "" as
    a failed attempt so the chain slides instead of serving garbage."""
    return _REASONING_BLOCK.sub("", text).strip()


_KANA = re.compile(r"[぀-ヿ]")


def is_untranslated_echo(input_text: str, output: str) -> bool:
    """True when the model handed the Japanese input back unchanged.

    Only fires when the input actually contains kana: a symbol-only line
    ("……") translating to itself is correct, not an echo. Whitespace and
    U+3000 are ignored so a space-stripped echo still counts.
    """
    if not _KANA.search(input_text):
        return False

    def normalize(text: str) -> str:
        return text.replace("　", "").strip()

    return normalize(output) == normalize(input_text)


def build_user_prompt(
    entries: list[GlossaryEntry],
    history: list[tuple[str, str]],
    input_text: str,
) -> str:
    """Assemble the cloud user prompt from matched terms, history, and input.

    History is passed as source→translation pairs: translation-only history
    hides who was speaking, which made the model turn a self-introduction
    ("大家都叫我...") into the third person ("大家都尊稱他為...").
    """
    parts = []
    if history:
        pairs = "\n".join(f"{source} → {translation}" for source, translation in history)
        parts.append(f"前文（原文 → 譯文，僅供參考，不要重譯）：\n{pairs}\n")
    if entries:
        lines = "\n".join(
            f"{entry.src} → {entry.dst}" + (f"（{entry.note}）" if entry.note else "")
            for entry in entries
        )
        parts.append(f"術語表（原文出現時必須採用）：\n{lines}\n")
    parts.append(f"翻譯以下文本：\n{input_text}")
    return "\n".join(parts)
