"""OpenAI-compatible glossary-injection proxy in front of an Ollama backend.

PlayTranslate (or any OpenAI-format client) talks to this proxy; the proxy
rewrites each request into the official Sakura-GalTransl v3.7 prompt format,
injecting only the per-game glossary terms that actually occur in the source
text, then forwards to Ollama's OpenAI-compatible endpoint. The model's
Simplified output is converted to Taiwan Traditional (OpenCC s2twp) at the
proxy's output boundary, so the result is correct regardless of PlayTranslate's
own render-time conversion setting.

Environment:
    UPSTREAM_URL      Ollama base URL (default http://localhost:11434)
    OLLAMA_MODEL      local model name (default sakura-galtransl-v3.7)
    GLOSSARY_PATH     per-game gpt_dict file; unset disables glossary injection
    GEMINI_API_KEY    enables the cloud-first chain (unset = local only)
    CLOUD_MODELS      comma-separated cloud model ids tried in order
    CLOUD_URL         OpenAI-compatible cloud base URL (default: Gemini)
    CONTINUATION_JOIN "0" disables cross-box sentence joining
    STARTUP_SMOKE     "0" skips the startup cloud smoke tests
    TRANSLATION_CACHE_SIZE  exact-prompt response cache entries; 0 disables

Run (from the repo root, LAN-reachable):
    GLOSSARY_PATH=glossaries/pokemon-oras.txt \
    uv run uvicorn server.proxy.main:app --host 0.0.0.0 --port 8000

Limitations (v1):
    - Non-streaming only: an incoming ``"stream": true`` is forced to false.
    - Incoming sampling parameters and model name are ignored; the proxy
      always applies the GalTransl-recommended profile and OLLAMA_MODEL.
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from opencc import OpenCC
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import cloud, sakura
from .glossary import Glossary

# uvicorn configures only its own loggers; without this, our INFO-level
# startup diagnostics (cloud smoke tests, glossary load, keep-alive pin)
# never reach the console.
#
# LOG_FILE (when set) adds a size-capped rotating handler. The launchers
# append the console stream to proxy.log, which nothing ever truncates - on a
# handheld that runs for months, TRACE lines would fill the flash. Rotation
# belongs here rather than in the shell because ">>" cannot reclaim its own
# file: the console stream keeps only startup banners and crash tracebacks.
LOG_FILE = os.environ.get("LOG_FILE", "")
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUPS = int(os.environ.get("LOG_BACKUPS", "3"))

_handlers: list[logging.Handler] = [logging.StreamHandler()]
if LOG_FILE:
    _handlers.append(
        RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    )
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger("proxy")

# Output boundary: the model emits Simplified; convert to Taiwan Traditional
# with TW vocabulary (s2twp) so correctness does not depend on PlayTranslate's
# render-time conversion setting (its variant pref fail-safes to Simplified).
# PT re-converting already-Traditional text is an identity operation.
_s2twp = OpenCC("s2twp")
# Input boundary: PT sends our Traditional output back as context; the model
# is trained on Simplified, so history is normalized back with tw2sp.
_tw2sp = OpenCC("tw2sp")

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "sakura-galtransl-v3.7")
GLOSSARY_PATH = os.environ.get("GLOSSARY_PATH", "")
# Join a sentence split across dialogue boxes back together before
# translating (see sakura.join_continuation). Set to "0" for games whose
# text boxes routinely end without sentence-final punctuation.
CONTINUATION_JOIN = os.environ.get("CONTINUATION_JOIN", "1") != "0"
# "0" skips the per-model cloud smoke tests at startup: each probe costs one
# request against that model's daily quota, and Android restarts the service
# often enough for that to add up (see boot-start.sh).
STARTUP_SMOKE = os.environ.get("STARTUP_SMOKE", "1") != "0"

# Cloud-first, local-fallback chain. With GEMINI_API_KEY set, each request
# tries the CLOUD_MODELS in order (free-tier quota exhaustion returns 429 and
# the chain slides to the next model); the local Sakura backend is always the
# last resort, so an unreachable/exhausted cloud degrades instead of failing.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
CLOUD_URL = os.environ.get(
    "CLOUD_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
)
# Order = preference. gemini-3.1-flash-lite leads because it is the one
# Google documents as translation-optimized AND it measured healthiest on
# device (0.7 s vs 3.5-flash-lite stalling at 60-80 s on 2026-08-03); the
# ailing model stays in the chain as spare quota, last.
CLOUD_MODELS = [
    model.strip()
    for model in os.environ.get(
        "CLOUD_MODELS", "gemini-3.1-flash-lite,gemma-4-26b-a4b-it,gemini-3.5-flash-lite"
    ).split(",")
    if model.strip()
]

UPSTREAM_ERROR_EXCERPT = 300  # chars of upstream body carried into error messages
MAX_COMPLETION_TOKENS = 4096

# Cloud read timeouts, measured on device: healthy single-line replies land in
# 0.5-2.3 s and batches in 3-5 s, while stalls jump straight to 12 s+. Giving a
# stalled call more patience than that only delays the fallback, which itself
# costs ~1 s - so cut losses early and let the next model in the chain answer.
CLOUD_READ_TIMEOUT_SINGLE = float(os.environ.get("CLOUD_READ_TIMEOUT_SINGLE", "2.5"))
CLOUD_READ_TIMEOUT_BATCH = float(os.environ.get("CLOUD_READ_TIMEOUT_BATCH", "6.0"))

# TRACE=1 logs one line per translation with the pieces needed to diagnose a
# bad result after the fact: what arrived, what continuation join did to it,
# which glossary terms matched, and what came back. Off by default - the game
# script would otherwise end up in a plaintext log.
TRACE = os.environ.get("TRACE", "") == "1"

# Sources this proxy translated within the last few seconds, used to tell a
# genuinely preceding utterance from a long-lived on-screen caption that
# PlayTranslate also keeps in its context block. Seconds, not minutes: two
# halves of one sentence arrive back to back.
CONTINUATION_WINDOW_S = float(os.environ.get("CONTINUATION_WINDOW_S", "20"))
_recent_sources: dict[str, float] = {}


def _remember_source(text: str, now: float) -> None:
    _recent_sources[text] = now
    for source, at in list(_recent_sources.items()):
        if now - at > CONTINUATION_WINDOW_S:
            del _recent_sources[source]


def _recent_source_set(now: float) -> set[str]:
    return {
        source
        for source, at in _recent_sources.items()
        if now - at <= CONTINUATION_WINDOW_S
    }

# Fails fast (GlossaryError) on a configured-but-unreadable glossary file.
glossary = Glossary(Path(GLOSSARY_PATH)) if GLOSSARY_PATH else Glossary(None)
if not GLOSSARY_PATH:
    logger.info(
        "GLOSSARY_PATH not set - no default glossary; injection applies only "
        "when the client's model field names a file in GLOSSARY_DIR"
    )

# Per-game glossaries selectable per request (see _resolve_glossary).
GLOSSARY_DIR = os.environ.get("GLOSSARY_DIR", "glossaries")
_game_glossaries: dict[str, Glossary] = {}

# Sticky failover: whichever cloud model last answered leads the chain, and
# keeps leading until it fails - then the model that rescued the request takes
# over as leader. No probing, no cooldown timers: a sick model is simply
# demoted the moment something else proves healthier, and only gets tried
# again if the current leader also starts failing.
_cloud_leader: str | None = None
# ...reset daily: free-tier quotas refill at midnight Pacific, so crossing
# that boundary clears the leader and the preferred model leads again. The
# reason for the original demotion is deliberately NOT tracked - if the
# preferred model is still sick, one request pays the timeout and the chain
# re-demotes it, which is cheaper than the logic to tell the cases apart.
_leader_quota_day: int | None = None
QUOTA_RESET_TZ = timezone(timedelta(hours=-8))  # US/Pacific standard time


def _quota_day() -> int:
    """Ordinal of the current quota day (Pacific midnight boundaries)."""
    return datetime.now(QUOTA_RESET_TZ).date().toordinal()


def _cloud_chain() -> list[str]:
    """CLOUD_MODELS ordered with the current leader first."""
    global _cloud_leader, _leader_quota_day
    if _cloud_leader is not None and _leader_quota_day != _quota_day():
        logger.info(
            "Quota day rolled over - resetting cloud leader (was %s)", _cloud_leader
        )
        _cloud_leader = None
    if _cloud_leader is None or _cloud_leader not in CLOUD_MODELS:
        return list(CLOUD_MODELS)
    return [_cloud_leader] + [m for m in CLOUD_MODELS if m != _cloud_leader]


# Negative memory. Sticky failover reorders the chain but never removes a
# model: when EVERY cloud model is failing (an all-429 day, or an all-stall
# outage) no success ever reassigns the leader, so each request re-pays a
# probe per model - and a stalled probe is billed upstream for its full
# input even though we abandon the read. Two bounded skip-lists close that:
# a PerDay-evidence quota mark lasting until the Pacific refill, and a short
# cooldown after consecutive transport errors.
#
# Refill clock: Google resets RPD at midnight *Pacific wall clock* (DST-aware,
# UTC-7 in summer), while marks expire on the same fixed UTC-8 ordinal as the
# leader clock (_quota_day). Expiry therefore lags the true refill by up to an
# hour in summer - deliberately the safe direction: a still-benched model
# receives no traffic, so it can never collect a post-refill 429 that would
# re-mark it for the whole fresh day. Expiring EARLY would risk exactly that
# poisoning (probe lands in the pre-refill hour, 429s, re-marks on the new
# ordinal, model benched ~23h of refilled quota).
_model_exhausted_day: dict[str, int] = {}
_unrecognized_429_day: dict[str, int] = {}

CLOUD_STRIKE_LIMIT = int(os.environ.get("CLOUD_STRIKE_LIMIT", "3"))
CLOUD_STRIKE_COOLDOWN_S = float(os.environ.get("CLOUD_STRIKE_COOLDOWN_S", "60"))
CLOUD_STRIKE_DECAY_S = 300.0  # a lone strike from an old blip must not linger
_model_strikes: dict[str, int] = {}
_model_strike_at: dict[str, float] = {}
_model_cooldown_until: dict[str, float] = {}


def _note_cloud_response(model: str, resp: httpx.Response, attempt_day: int) -> None:
    """Learn PerDay quota exhaustion from a 429 body.

    attempt_day is snapshotted before the call: the 429 is evidence about the
    day Google evaluated the request, and a reply landing after the refill
    boundary must not poison the fresh day. Only an unambiguous PerDay
    violation marks the model (a PerMinute burst that also mentions the daily
    limit must not bench it for hours); anything else fails open but is
    logged once per model per day so an unexpected body shape stays
    diagnosable.
    """
    if resp.status_code != 429:
        return
    body = resp.text
    if "PerDay" in body and "PerMinute" not in body:
        if _model_exhausted_day.get(model) != attempt_day:
            _model_exhausted_day[model] = attempt_day
            logger.info(
                "Cloud model %s: PerDay quota exhausted - skipping until the Pacific refill",
                model,
            )
    elif _unrecognized_429_day.get(model) != attempt_day:
        _unrecognized_429_day[model] = attempt_day
        excerpt = " ".join(body[:UPSTREAM_ERROR_EXCERPT].split())
        logger.warning(
            "Cloud model %s: 429 without PerDay evidence (not marking): %s", model, excerpt
        )


def _note_cloud_transport_error(model: str, started: float) -> None:
    """Count a connect/timeout failure toward the model's strike cooldown.

    Only attempts started after the previous strike count: overlapping
    requests felled by one network blip must not bench a model that had no
    chance to recover between them. Strikes decay after a quiet spell.
    """
    last = _model_strike_at.get(model, 0.0)
    if started < last:
        return
    now = time.monotonic()
    strikes = 1 if now - last > CLOUD_STRIKE_DECAY_S else _model_strikes.get(model, 0) + 1
    _model_strike_at[model] = now
    if strikes >= CLOUD_STRIKE_LIMIT:
        _model_strikes[model] = 0
        _model_cooldown_until[model] = now + CLOUD_STRIKE_COOLDOWN_S
        logger.info(
            "Cloud model %s: %d consecutive transport errors - cooling down for %.0fs",
            model,
            strikes,
            CLOUD_STRIKE_COOLDOWN_S,
        )
    else:
        _model_strikes[model] = strikes


def _cloud_available(model: str, attempt_day: int) -> bool:
    """False while the model is known exhausted today or cooling down."""
    marked = _model_exhausted_day.get(model)
    if marked == attempt_day:
        return False
    if marked is not None:
        del _model_exhausted_day[model]  # previous quota day - refilled since
    until = _model_cooldown_until.get(model)
    if until is not None:
        if time.monotonic() < until:
            return False
        del _model_cooldown_until[model]
    return True


# Exact-prompt response cache. The key covers everything that shapes the
# upstream prompt (batch flag, joined+escaped input, history pairs, matched
# glossary lines), so a hit is provably prompt-equivalent: identical client
# retries and unchanged-screen re-OCR are absorbed, while the same line under
# different context always re-translates. Entries store the rendered response
# bytes; usage replays verbatim (PT's meter counts served responses, not
# upstream spend). All TTL/cooldown clocks here and above use time.monotonic()
# like _recent_sources - "awake seconds" on a device that suspends; keep the
# clocks consistent rather than fixing one to wall time.
TRANSLATION_CACHE_SIZE = int(os.environ.get("TRANSLATION_CACHE_SIZE", "256"))
TRANSLATION_CACHE_TTL_S = float(os.environ.get("TRANSLATION_CACHE_TTL_S", "3600"))
# Local (Sakura-quality) answers expire fast so cloud recovery shows through.
TRANSLATION_CACHE_TTL_LOCAL_S = float(os.environ.get("TRANSLATION_CACHE_TTL_LOCAL_S", "120"))
TRANSLATION_CACHE_ENTRY_MAX_BYTES = 16 * 1024
_translation_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
# hits vs repeat_misses (same source seconds apart, exact key missed) decides
# whether a looser key or in-flight coalescing is worth building later.
_cache_stats = {"requests": 0, "hits": 0, "repeat_misses": 0}


def _cache_key(is_batch: bool, input_text: str, history: list[tuple[str, str]], entries) -> str:
    hist = "\x1f".join(f"{source}\x1e{translation}" for source, translation in history)
    terms = "\x1f".join(f"{entry.src}\x1e{entry.dst}\x1e{entry.note or ''}" for entry in entries)
    return "\x1d".join(("b" if is_batch else "s", input_text, hist, terms))


def _cache_get(key: str) -> bytes | None:
    entry = _translation_cache.get(key)
    if entry is None:
        return None
    expires, body = entry
    if time.monotonic() >= expires:
        del _translation_cache[key]
        return None
    _translation_cache.move_to_end(key)
    return body


def _cache_put(key: str, body: bytes, ttl: float) -> None:
    if len(body) > TRANSLATION_CACHE_ENTRY_MAX_BYTES:
        return
    now = time.monotonic()
    _translation_cache[key] = (now + ttl, body)
    _translation_cache.move_to_end(key)
    # Shed expired entries from the cold end, then enforce the size cap.
    while _translation_cache:
        oldest = next(iter(_translation_cache))
        if oldest == key or _translation_cache[oldest][0] > now:
            break
        del _translation_cache[oldest]
    while len(_translation_cache) > TRANSLATION_CACHE_SIZE:
        _translation_cache.popitem(last=False)


def _resolve_glossary(model_name) -> Glossary:
    """Pick the glossary for this request via the client's model field.

    The proxy ignores the client's model for actual model selection, so the
    field is free to act as a game selector: if it names a file in
    GLOSSARY_DIR (model "pokemon-oras" → glossaries/pokemon-oras.txt), that
    per-game glossary is used; anything else falls back to the GLOSSARY_PATH
    default. Lets the user switch games from PlayTranslate's service settings
    without touching the server.
    """
    if isinstance(model_name, str):
        name = model_name.strip().lower()
        # Conservative charset: no path separators, no traversal.
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            if name not in _game_glossaries:
                path = Path(GLOSSARY_DIR) / f"{name}.txt"
                if path.is_file():
                    _game_glossaries[name] = Glossary(path)
                    logger.info("Per-game glossary active: %s", path)
            if name in _game_glossaries:
                return _game_glossaries[name]
    return glossary

_client: httpx.AsyncClient | None = None
_cloud_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Long read timeout: a cold request includes Ollama's model load time.
        _client = httpx.AsyncClient(
            base_url=UPSTREAM_URL,
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        )
    return _client


def _get_cloud_client() -> httpx.AsyncClient:
    global _cloud_client
    if _cloud_client is None:
        _cloud_client = httpx.AsyncClient(
            base_url=CLOUD_URL,
            headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
            # Per-request read timeouts are set at the call site; this is only
            # the ceiling for anything that forgets to pass one.
            timeout=httpx.Timeout(connect=5.0, read=CLOUD_READ_TIMEOUT_BATCH, write=10.0, pool=5.0),
        )
    return _cloud_client


async def _startup_checks() -> None:
    """Best-effort diagnostics, run in the background so the port opens
    immediately (the Gemma smoke test alone can take ~40 s cold)."""
    # Preload the local model and pin it in memory. keep_alive is not settable
    # through /v1, so this native call is the only way to prevent the 5-minute
    # idle unload without reconfiguring the Ollama server itself.
    try:
        resp = await _get_client().post(
            "/api/generate", json={"model": OLLAMA_MODEL, "keep_alive": -1}
        )
        if resp.status_code == 200:
            logger.info("Pinned %s in Ollama memory (keep_alive=-1)", OLLAMA_MODEL)
        else:
            logger.warning(
                "Could not pin %s (HTTP %d): %s", OLLAMA_MODEL, resp.status_code, resp.text[:200]
            )
    except httpx.HTTPError as exc:
        logger.warning("Could not pin %s (Ollama unreachable?): %r", OLLAMA_MODEL, exc)
    # Smoke-test each configured cloud model so a wrong model id or rejected
    # parameter shows up in the log instead of silently sliding every request
    # down to the local fallback.
    if GEMINI_API_KEY and not STARTUP_SMOKE:
        logger.info("STARTUP_SMOKE=0 - cloud smoke tests skipped")
    elif GEMINI_API_KEY:
        for model in CLOUD_MODELS:
            attempt_day = _quota_day()
            if not _cloud_available(model, attempt_day):
                logger.info("Cloud model %s: skipped (marked unavailable)", model)
                continue
            try:
                resp = await post_cloud(
                    {
                        "model": model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    }
                )
                if resp.status_code == 200:
                    logger.info("Cloud model %s: OK", model)
                else:
                    # Seed the skip table: a restart on an exhausted day pays
                    # these probes once instead of re-learning per request.
                    _note_cloud_response(model, resp, attempt_day)
                    logger.warning(
                        "Cloud model %s: HTTP %d: %s", model, resp.status_code, resp.text[:200]
                    )
            except httpx.HTTPError as exc:
                logger.warning("Cloud model %s: unreachable: %r", model, exc)
    else:
        logger.info("GEMINI_API_KEY not set - cloud chain disabled, local Sakura only")


@asynccontextmanager
async def _lifespan(app: Starlette):
    checks = asyncio.create_task(_startup_checks())
    yield
    if not checks.done():
        checks.cancel()
    if _client is not None:
        await _client.aclose()
    if _cloud_client is not None:
        await _cloud_client.aclose()


async def post_upstream(path: str, payload: dict) -> httpx.Response:
    return await _get_client().post(path, json=payload)


async def post_cloud(payload: dict, read_timeout: float | None = None) -> httpx.Response:
    client = _get_cloud_client()
    if read_timeout is None:
        return await client.post("/chat/completions", json=payload)
    return await client.post(
        "/chat/completions",
        json=payload,
        timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=10.0, pool=5.0),
    )


def _protect_glossary_terms(text: str, entries) -> str:
    """Undo s2twp phrase rewrites that clobber injected glossary targets.

    The local model echoes glossary targets in Simplified (項目 → 项目), and
    the output-side s2twp pass may then phrase-map them to a different Taiwan
    word (项目 → 專案). Restore the exact glossary form for matched entries.
    """
    for entry in entries:
        variant = _s2twp.convert(_tw2sp.convert(entry.dst))
        if variant != entry.dst:
            text = text.replace(variant, entry.dst)
    return text


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message}})


def _upstream_error(resp: httpx.Response) -> JSONResponse:
    excerpt = " ".join(resp.text[:UPSTREAM_ERROR_EXCERPT].split())
    return _error(502, f"Upstream {UPSTREAM_URL} returned {resp.status_code}: {excerpt}")


def _last_user_content(body: dict) -> str | None:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and isinstance(message.get("content"), str)
        ):
            return message["content"]
    return None


async def chat_completions(request: Request) -> JSONResponse:
    global _cloud_leader, _leader_quota_day
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "Request body is not valid JSON")

    user_content = _last_user_content(body)
    if user_content is None:
        return _error(400, "No user message with string content in request")

    context_pairs, payload = sakura.split_pt_user_message(user_content)

    # PlayTranslate marks its batch path (several OCR regions in one request)
    # by attaching response_format; single requests never carry it.
    is_batch = "response_format" in body
    was_recent = False
    if is_batch:
        texts = sakura.extract_batch_texts(payload)
        if texts is None:
            # A 400 on the batch path makes PT retry each text individually.
            return _error(400, "Could not extract a JSON string array from batch request")
        input_text = "\n".join(sakura.escape_line(text) for text in texts)
        match_basis = "\n".join(texts)
        history = list(context_pairs)
    else:
        texts = None
        # Batch regions are separate on-screen groups, so sentence joining
        # applies only to the single-box path.
        if CONTINUATION_JOIN:
            raw_input, history = sakura.join_continuation(
                context_pairs, payload, _recent_source_set(time.monotonic())
            )
        else:
            raw_input = payload
            history = list(context_pairs)
        # Escape real newlines like the batch path: to Sakura, "\n" separates
        # independent texts, so an unescaped newline would split one dialogue
        # box into unrelated fragments.
        input_text = sakura.escape_line(raw_input)
        match_basis = raw_input
        now_mono = time.monotonic()
        was_recent = payload in _recent_source_set(now_mono)
        _remember_source(payload, now_mono)
    entries = _resolve_glossary(body.get("model")).match(match_basis)
    max_tokens = min(MAX_COMPLETION_TOKENS, max(128, 2 * len(input_text)))

    cache_key = None
    if TRANSLATION_CACHE_SIZE > 0:
        cache_key = _cache_key(is_batch, input_text, history, entries)
        _cache_stats["requests"] += 1
        # Emitted before the hit/miss early-return so century marks are never
        # skipped - a hit-heavy stretch is exactly when the numbers matter.
        if _cache_stats["requests"] % 100 == 0:
            logger.info(
                "Cache stats: %d requests, %d hits, %d repeated-source misses",
                _cache_stats["requests"],
                _cache_stats["hits"],
                _cache_stats["repeat_misses"],
            )
        cached = _cache_get(cache_key)
        if cached is not None:
            _cache_stats["hits"] += 1
            if TRACE:
                logger.info("TRACE cache-hit | in=%r", input_text)
            return Response(cached, media_type="application/json")
        if was_recent:
            # Same source seconds apart yet the exact key missed: a repeat
            # arriving under different context. Counted (and TRACE-logged) to
            # decide whether a looser key or in-flight coalescing is worth it.
            _cache_stats["repeat_misses"] += 1
            if TRACE:
                logger.info("TRACE repeat-miss | in=%r", input_text)

    # Cloud-first, local-last attempt chain: any failure (unreachable, 429
    # quota, bad completion, batch line-count mismatch) slides to the next
    # backend, so the request degrades in quality instead of erroring. Models
    # known exhausted or cooling down are dropped before they cost a probe.
    attempt_day = _quota_day()
    attempts = (
        [("cloud", model) for model in _cloud_chain() if _cloud_available(model, attempt_day)]
        if GEMINI_API_KEY
        else []
    )
    attempts.append(("local", OLLAMA_MODEL))
    failures: list[str] = []
    for kind, model in attempts:
        if kind == "cloud":
            request_body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": cloud.SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": cloud.build_user_prompt(entries, history, input_text),
                    },
                ],
                # v1 proxy is non-streaming: incoming "stream": true is forced off.
                "stream": False,
                "max_tokens": max_tokens,
                **cloud.SAMPLING,
            }
        else:
            # The Sakura model is trained on Simplified; its history block is
            # normalized back from our Traditional output (tw2sp).
            # Only the Chinese side is script-converted; the Japanese source
            # must reach the model untouched.
            local_history = [
                (source, _tw2sp.convert(translation)) for source, translation in history
            ]
            request_body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sakura.SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": sakura.build_user_prompt(entries, local_history, input_text),
                    },
                ],
                "stream": False,
                # GalTransl heuristic: budget roughly 2 output tokens per input char.
                "max_tokens": max_tokens,
                **sakura.SAMPLING,
            }

        started = time.monotonic()
        try:
            if kind == "cloud":
                # Impatient with every model but the last one: a stalled call
                # is worth abandoning while another backend can still answer,
                # but the final attempt has nobody left to hand off to.
                is_last = (kind, model) == attempts[-1]
                read_timeout = None if is_last else (
                    CLOUD_READ_TIMEOUT_BATCH if is_batch else CLOUD_READ_TIMEOUT_SINGLE
                )
                resp = await post_cloud(request_body, read_timeout)
            else:
                resp = await post_upstream("/v1/chat/completions", request_body)
        except httpx.HTTPError as exc:
            if kind == "cloud":
                _note_cloud_transport_error(model, started)
            failures.append(f"{model}: unreachable ({exc!r})")
            continue
        if resp.status_code != 200:
            if kind == "cloud":
                _note_cloud_response(model, resp, attempt_day)
            excerpt = " ".join(resp.text[:120].split())
            failures.append(f"{model}: HTTP {resp.status_code} {excerpt}")
            continue
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError):
            failures.append(f"{model}: malformed completion")
            continue

        if is_batch:
            lines = sakura.split_output_lines(content)
            if len(lines) != len(texts):
                failures.append(f"{model}: line count {len(lines)} != {len(texts)}")
                continue
            # Cloud models emit Taiwan Traditional directly; only the local
            # Sakura output needs the OpenCC pass.
            if kind == "local":
                lines = [
                    _protect_glossary_terms(_s2twp.convert(line), entries) for line in lines
                ]
            # Judged on the raw lines: the JSON wrapper below is never empty,
            # so a safety-filtered blank region would otherwise get pinned.
            cacheable = all(line.strip() for line in lines)
            data["choices"][0]["message"]["content"] = json.dumps(
                {"translations": lines}, ensure_ascii=False
            )
        else:
            output = sakura.unescape_line(content)
            if kind == "local":
                output = _protect_glossary_terms(_s2twp.convert(output), entries)
            cacheable = bool(output.strip())
            data["choices"][0]["message"]["content"] = output

        if kind == "cloud" and model != _cloud_leader:
            logger.info("Cloud leader is now %s (was %s)", model, _cloud_leader)
            _cloud_leader = model
        if kind == "cloud":
            _leader_quota_day = _quota_day()
            _model_strikes.pop(model, None)
            _model_strike_at.pop(model, None)
            _model_cooldown_until.pop(model, None)
        if TRACE:
            logger.info(
                "TRACE %s | joined=%s | in=%r | hist=%r | terms=%s | out=%r",
                model,
                not is_batch and input_text != sakura.escape_line(payload),
                input_text,
                history,
                [entry.src for entry in entries],
                data["choices"][0]["message"]["content"],
            )
        if failures:
            logger.warning("Served by %s after: %s", model, "; ".join(failures))
        # Unchanged fields (usage, id, ...) pass through — PT feeds its token
        # meter from usage. Rendered by hand (byte-identical to JSONResponse)
        # so cache hits and misses serve the same bytes.
        body_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        if cache_key is not None and cacheable:
            # Empty (safety-filtered) replies pass through but are never
            # pinned; local-quality entries expire fast so cloud recovery
            # shows up within minutes.
            ttl = TRANSLATION_CACHE_TTL_LOCAL_S if kind == "local" else TRANSLATION_CACHE_TTL_S
            _cache_put(cache_key, body_bytes, ttl)
        return Response(body_bytes, media_type="application/json")

    detail = "; ".join(failures)
    if is_batch and any("line count" in failure for failure in failures):
        # A 400 triggers PT's per-text retry path.
        return _error(400, f"All backends failed batch translation: {detail}")
    return _error(502, f"All translation backends failed: {detail}")


async def models(request: Request) -> JSONResponse:
    # Not real auth (the proxy is LAN-only): rejecting keyless probes makes
    # PlayTranslate's key check (which probes with AND without the key, and
    # expects the keyless probe to fail) report the endpoint as OK.
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return _error(401, "Missing bearer token")
    # The "models" this proxy offers ARE the per-game glossaries (the model
    # field is the game selector — see _resolve_glossary), so PlayTranslate's
    # native model picker doubles as the game-switching menu.
    names = []
    glossary_dir = Path(GLOSSARY_DIR)
    if glossary_dir.is_dir():
        names = sorted(path.stem for path in glossary_dir.glob("*.txt"))
    data = [
        {"id": name, "object": "model", "created": 0, "owned_by": "thor-proxy"}
        for name in names
    ]
    return JSONResponse({"object": "list", "data": data})


# Plain Starlette (not FastAPI): keeps every dependency pure Python so the
# proxy also pip-installs on Termux/Android without a compiler toolchain.
app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
    ],
    lifespan=_lifespan,
)
