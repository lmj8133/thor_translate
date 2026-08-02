"""Post sample dialogue to the proxy and report translations plus latency.

Simulates PlayTranslate's default single-text request format (default system
prompt, context block of previous pairs, wrapper line), so each round trip
exercises the proxy's request parsing, glossary injection, and Sakura prompt
construction end to end.

Usage:
    uv run python tools/quality_check.py --endpoint http://localhost:8000/v1

Exit codes: 0 = all lines translated, 1 = any request failed.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

# PlayTranslate's default system prompt with Japanese/Chinese substituted in.
PT_SYSTEM = (
    "You are a professional Japanese (ja) to Chinese (zh) translator. "
    "Your goal is to accurately convey the meaning and nuances of the original "
    "Japanese text while adhering to Chinese grammar, vocabulary, and cultural "
    "sensitivities.\n\n"
    "Produce only the Chinese translation, without any additional explanations "
    "or commentary."
)
MAX_CONTEXT_PAIRS = 3  # mirrors PlayTranslate's ContextRing cap


def build_user_message(text: str, context_pairs: list[tuple[str, str]]) -> str:
    """Mimic PT's default user message: optional context block + wrapper + text."""
    parts = []
    if context_pairs:
        parts.append("Recent dialogue lines, for context only:\n")
        for source, translation in context_pairs[-MAX_CONTEXT_PAIRS:]:
            # PT's CJK OCR pipeline joins group lines without separators, so
            # real PT context pairs never contain newlines — mirror that here.
            source_line = source.replace("\r\n", "").replace("\n", "")
            translation_line = translation.replace("\r\n", "").replace("\n", "")
            parts.append(f"- {source_line} → {translation_line}\n")
        parts.append("\n")
    parts.append(f"Please translate the following Japanese text into Chinese:\n\n{text}")
    return "".join(parts)


def percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile on an ascending list."""
    return ordered[round(fraction * (len(ordered) - 1))]


def main() -> int:
    parser = argparse.ArgumentParser(description="Translation quality and latency spot check")
    parser.add_argument(
        "--endpoint",
        default="http://localhost:8000/v1",
        help="OpenAI-compatible base URL of the proxy (default: %(default)s)",
    )
    parser.add_argument(
        "--dialogue",
        default=str(Path(__file__).with_name("sample_dialogue.jsonl")),
        help="JSONL test set with a 'ja' field per line (default: %(default)s)",
    )
    args = parser.parse_args()

    dialogue_path = Path(args.dialogue)
    if not dialogue_path.exists():
        print(f"Dialogue file not found: {dialogue_path}", file=sys.stderr)
        return 1
    lines = [
        json.loads(raw)
        for raw in dialogue_path.read_text(encoding="utf-8").splitlines()
        if raw.strip()
    ]

    base = args.endpoint.rstrip("/")
    latencies: list[float] = []
    context_pairs: list[tuple[str, str]] = []
    failures = 0
    timeout = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)
    with httpx.Client(timeout=timeout) as client:
        for index, item in enumerate(lines, start=1):
            text = item["ja"]
            body = {
                "model": "quality-check",
                "messages": [
                    {"role": "system", "content": PT_SYSTEM},
                    {"role": "user", "content": build_user_message(text, context_pairs)},
                ],
            }
            start = time.perf_counter()
            try:
                resp = client.post(f"{base}/chat/completions", json=body)
                elapsed = time.perf_counter() - start
                resp.raise_for_status()
                translation = resp.json()["choices"][0]["message"]["content"]
            except (httpx.HTTPError, ValueError, LookupError) as exc:
                elapsed = time.perf_counter() - start
                print(f"[{index}/{len(lines)}] FAILED after {elapsed:.2f}s: {exc}")
                failures += 1
                continue
            latencies.append(elapsed)
            context_pairs.append((text, translation))
            print(f"[{index}/{len(lines)}] 原文: {text!r}")
            print(f"           譯文: {translation}")
            print(f"           耗時: {elapsed:.2f}s")

    if latencies:
        ordered = sorted(latencies)
        print(
            f"\nn={len(latencies)}  "
            f"p50={percentile(ordered, 0.50):.2f}s  "
            f"p95={percentile(ordered, 0.95):.2f}s"
        )
    if failures:
        print(f"{failures} line(s) failed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
