"""Vet a candidate cloud endpoint before adding it to CLOUD_ENDPOINTS.

Runs the full gauntlet that today's chain members went through, using the
proxy's real prompt construction and output guards, so a PASS here means the
entry can be pasted into env.sh as-is:

    uv run python tools/vet_endpoint.py 'https://api.groq.com/openai/v1|<key>|qwen/qwen3.6-27b'

Stages per model:
    1. reachability  - 1-token probe; 401/402/404/429 get a plain diagnosis
    2. translation   - the glossary sample set through cloud.SYSTEM_PROMPT +
                       request_tweaks + strip_reasoning/echo/U+3000 guards;
                       measures latency and counts every failure mode the
                       chain has met in the wild (thinking leaks, echoes,
                       empties, fullwidth spaces, Simplified leakage)
    3. batch         - one multi-line batch, line-count contract
    4. remedies      - on thinking leaks with no REQUEST_TWEAKS row, tries
                       the known off-switch params and prints the row to add

Exit codes: 0 = every model passed, 1 = at least one WARN, 2 = any FAIL.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from opencc import OpenCC

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.proxy import cloud, sakura  # noqa: E402
from server.proxy.glossary import Glossary  # noqa: E402

_s2twp = OpenCC("s2twp")

# Candidate thinking off-switches, tried in order when leaks are detected on
# a host that has no REQUEST_TWEAKS row yet (mirrors cloud.REQUEST_TWEAKS).
CANDIDATE_TWEAKS: list[dict] = [
    {"reasoning_effort": "none"},
    {"thinking": {"type": "disabled"}},
]

STATUS_DIAGNOSIS = {
    401: "金鑰無效或格式錯誤",
    402: "帳號要求付款設定（免費層未啟用？）",
    403: "金鑰權限不足",
    404: "模型 id 不存在（用 /models 查現行清單）",
    429: "額度已滿（稍後再驗，或本 key 已在別處使用）",
}


def post(url: str, key: str, body: dict, timeout: float) -> tuple[httpx.Response | None, float, Exception | None]:
    t0 = time.monotonic()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        resp = httpx.post(f"{url}/chat/completions", json=body, headers=headers, timeout=timeout)
        return resp, time.monotonic() - t0, None
    except Exception as exc:  # noqa: BLE001 - report, don't crash the vetting
        return None, time.monotonic() - t0, exc


def translation_body(model: str, entries, input_text: str, extra: dict) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": cloud.SYSTEM_PROMPT},
            {"role": "user", "content": cloud.build_user_prompt(entries, [], input_text)},
        ],
        "stream": False,
        "max_tokens": 256,
        **cloud.SAMPLING,
        **extra,
    }


def vet_model(url: str, key: str, model: str, lines: list[dict], glossary: Glossary, budget: float, pace: float = 0.0) -> str:
    """Run every stage for one model; returns 'PASS' / 'WARN' / 'FAIL'."""
    print(f"\n===== {model} =====")
    tweaks = cloud.request_tweaks(url, model)

    # Stage 1: reachability.
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1, **tweaks}
    resp, dt, exc = post(url, key, body, timeout=15.0)
    if exc is not None:
        print(f"[連通] 失敗：{exc!r}")
        return "FAIL"
    if resp.status_code != 200:
        hint = STATUS_DIAGNOSIS.get(resp.status_code, "")
        print(f"[連通] HTTP {resp.status_code} {hint}\n      {resp.text[:150]}")
        return "FAIL"
    print(f"[連通] OK ({dt:.2f}s)")

    # Stage 2: real translations with the proxy's guards.
    stats = {"ok": 0, "empty": 0, "echo": 0, "leak": 0, "space": 0, "simp": 0, "err": 0, "cut": 0}
    times: list[float] = []
    outputs: list[str] = []
    for item in lines:
        if pace:
            time.sleep(pace)  # free tiers meter RPM; a vet must not 429 itself
        ja = item["ja"]
        entries = glossary.match(ja)
        resp, dt, exc = post(url, key, translation_body(model, entries, ja, tweaks), timeout=20.0)
        if exc is not None or resp.status_code != 200:
            stats["err"] += 1
            outputs.append(f"[{item['id']:2}] ERR {exc!r:.40}" if exc else f"[{item['id']:2}] HTTP {resp.status_code}")
            continue
        times.append(dt)
        choice = resp.json()["choices"][0]
        msg = choice["message"]
        # Thinking models silently spend max_tokens on reasoning and hand
        # back a mid-sentence stump - finish_reason is the reliable tell.
        truncated = choice.get("finish_reason") == "length"
        if truncated:
            stats["cut"] += 1
        raw = msg.get("content") or ""
        if "<think" in raw or "<thought" in raw or msg.get("reasoning_content"):
            stats["leak"] += 1
        served = cloud.strip_reasoning(raw)
        if not served:
            stats["empty"] += 1
            outputs.append(f"[{item['id']:2}] {dt:4.1f}s (空→滑棒)")
            continue
        if cloud.is_untranslated_echo(ja, served):
            stats["echo"] += 1
            outputs.append(f"[{item['id']:2}] {dt:4.1f}s (原文回聲→滑棒)")
            continue
        if "　" in served:
            stats["space"] += 1
        served = served.replace("　", "")
        if _s2twp.convert(served) != served:
            stats["simp"] += 1
        stats["ok"] += 1
        mark = "（截斷）" if truncated else ""
        outputs.append(f"[{item['id']:2}] {dt:4.1f}s {served[:44]}{mark}")
    for line in outputs:
        print(line)

    # Stage 3: batch line-count contract.
    if pace:
        time.sleep(pace)
    batch_src = [item["ja"] for item in lines[:3]]
    joined = "\n".join(sakura.escape_line(t) for t in batch_src)
    entries = glossary.match("\n".join(batch_src))
    resp, dt, exc = post(url, key, translation_body(model, entries, joined, tweaks), timeout=20.0)
    batch_ok = False
    if exc is None and resp.status_code == 200:
        content = cloud.strip_reasoning(resp.json()["choices"][0]["message"].get("content") or "")
        batch_ok = len(sakura.split_output_lines(content)) == len(batch_src)
    print(f"[批次] {'OK' if batch_ok else '行數契約不符（批次請求會滑棒，僅靠單句路徑）'}")

    # Stage 4: remedy suggestions for thinking leaks on an unshimmed host.
    if (stats["leak"] or stats["empty"] > len(lines) // 3) and not tweaks:
        for candidate in CANDIDATE_TWEAKS:
            probe_ja = lines[0]["ja"]
            resp, _, exc = post(url, key,
                                translation_body(model, glossary.match(probe_ja), probe_ja, candidate),
                                timeout=20.0)
            if exc is None and resp.status_code == 200:
                served = cloud.strip_reasoning(resp.json()["choices"][0]["message"].get("content") or "")
                if served and not cloud.is_untranslated_echo(probe_ja, served):
                    host = url.split("/")[2]
                    print(f"[藥方] thinking 洩漏可用此參數關閉 → cloud.py REQUEST_TWEAKS 加一行：")
                    print(f"       (\"{host}\", {candidate!r}),")
                    break
        else:
            print("[藥方] 已知的關閉參數都無效——需人工研究此供應商的 thinking 開關")

    # Verdict.
    times.sort()
    p50 = times[len(times) // 2] if times else 0.0
    p95 = times[int(len(times) * 0.95)] if times else 0.0
    over = sum(1 for t in times if t > budget)
    total = len(lines)
    bad = stats["empty"] + stats["echo"] + stats["err"]
    print(f"[延遲] p50={p50:.2f}s p95={p95:.2f}s 超過預算{budget}s：{over}/{len(times)}")
    print(f"[統計] 成功 {stats['ok']}/{total}｜空 {stats['empty']}｜回聲 {stats['echo']}｜"
          f"截斷 {stats['cut']}｜thinking洩漏 {stats['leak']}｜全形空格 {stats['space']}｜"
          f"疑似簡體 {stats['simp']}")
    bad += stats["cut"]  # a mid-sentence stump gets SERVED - as bad as empty
    if bad > total * 0.3 or (times and p95 > budget * 2):
        verdict = "FAIL"
    elif bad or stats["simp"] > total * 0.3 or over > len(times) * 0.2 or not batch_ok:
        verdict = "WARN"
    else:
        verdict = "PASS"
    print(f"[判決] {verdict}"
          + ("（請人工複審上方逐句輸出的翻譯品質——機器只驗得了格式）" if verdict != "FAIL" else ""))
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("entry", help="url|key|model1;model2 - CLOUD_ENDPOINTS entry format")
    parser.add_argument("--lines", default=str(Path(__file__).with_name("sample_dialogue.jsonl")))
    parser.add_argument("--glossary", default="glossaries/pokemon-oras.txt")
    parser.add_argument("--budget", type=float, default=3.5,
                        help="single-line latency budget in seconds (default: %(default)s)")
    parser.add_argument("--pace", type=float, default=0.0,
                        help="seconds between requests, for low-RPM free tiers (default: none)")
    args = parser.parse_args()

    fields = [f.strip() for f in args.entry.split("|", 2)]
    if len(fields) != 3 or not fields[0] or not fields[2]:
        print("條目格式錯誤：需要 url|key|model1;model2（與 CLOUD_ENDPOINTS 相同）", file=sys.stderr)
        return 2
    url, key, models_field = fields
    models = [m.strip() for m in models_field.split(";") if m.strip()]

    lines = [json.loads(l) for l in open(args.lines, encoding="utf-8")]
    glossary = Glossary(Path(args.glossary))

    verdicts = [
        vet_model(url, key, model, lines, glossary, args.budget, args.pace)
        for model in models
    ]
    passed = [m for m, v in zip(models, verdicts) if v == "PASS"]
    if passed:
        print(f"\n可貼進 env.sh 的條目（僅含 PASS 的模型）：")
        print(f"  {url}|<key>|{';'.join(passed)}")
    if "FAIL" in verdicts:
        return 2
    return 1 if "WARN" in verdicts else 0


if __name__ == "__main__":
    sys.exit(main())
