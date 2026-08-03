"""Generic cloud-model prompt construction (Gemini OpenAI-compatible API).

Unlike the Sakura path (sakura.py), cloud models are general instruction
followers: they are asked for Taiwan Traditional output directly (so no
OpenCC pass on their output) and receive the glossary and prior context as
plain instructions. The line-count contract matches the Sakura path so the
batch splitting logic in main.py works unchanged for both.
"""

from .glossary import GlossaryEntry

SYSTEM_PROMPT = (
    "你是遊戲文本翻譯引擎，將日文遊戲文本翻譯成台灣正體中文。規則：\n"
    "1. 只輸出譯文，不要任何解釋、註記、拼音或引號包裹。\n"
    "2. 輸出行數必須與輸入行數完全一致，逐行對應；行內的「\\n」字面序列原樣保留。\n"
    "3. 提供術語表時，原文中出現的術語必須使用指定譯名。\n"
    "4. 原文常以平假名書寫（兒童向文本），請依上下文正確判讀詞義。\n"
    "5. 用語採台灣習慣，對話語氣自然口語。"
)

SAMPLING = {"temperature": 0.2}


def build_user_prompt(
    entries: list[GlossaryEntry],
    history: list[str],
    input_text: str,
) -> str:
    """Assemble the cloud user prompt from matched terms, history, and input."""
    parts = []
    if history:
        parts.append("前文譯文（僅供參考，不要重譯）：\n" + "\n".join(history) + "\n")
    if entries:
        lines = "\n".join(
            f"{entry.src} → {entry.dst}" + (f"（{entry.note}）" if entry.note else "")
            for entry in entries
        )
        parts.append(f"術語表（原文出現時必須採用）：\n{lines}\n")
    parts.append(f"翻譯以下文本：\n{input_text}")
    return "\n".join(parts)
