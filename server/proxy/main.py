"""OpenAI-compatible glossary-injection proxy in front of an Ollama backend.

PlayTranslate (or any OpenAI-format client) talks to this proxy; the proxy
rewrites each request into the official Sakura-GalTransl v3.7 prompt format,
injecting only the per-game glossary terms that actually occur in the source
text, then forwards to Ollama's OpenAI-compatible endpoint. Output stays
Simplified Chinese by design — PlayTranslate converts to Traditional (Taiwan)
at render time.

Environment:
    UPSTREAM_URL   Ollama base URL (default http://localhost:11434)
    OLLAMA_MODEL   model name requested upstream (default sakura-galtransl-v3.7)
    GLOSSARY_PATH  per-game gpt_dict file; unset disables glossary injection

Run (from the repo root, LAN-reachable):
    GLOSSARY_PATH=glossaries/pokemon-oras.txt \
    uv run uvicorn server.proxy.main:app --host 0.0.0.0 --port 8000

Limitations (v1):
    - Non-streaming only: an incoming ``"stream": true`` is forced to false.
    - Incoming sampling parameters and model name are ignored; the proxy
      always applies the GalTransl-recommended profile and OLLAMA_MODEL.
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import sakura
from .glossary import Glossary

logger = logging.getLogger("proxy")

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "sakura-galtransl-v3.7")
GLOSSARY_PATH = os.environ.get("GLOSSARY_PATH", "")

UPSTREAM_ERROR_EXCERPT = 300  # chars of upstream body carried into error messages
MAX_COMPLETION_TOKENS = 4096

# Fails fast (GlossaryError) on a configured-but-unreadable glossary file.
glossary = Glossary(Path(GLOSSARY_PATH)) if GLOSSARY_PATH else Glossary(None)
if not GLOSSARY_PATH:
    logger.warning("GLOSSARY_PATH not set - glossary injection disabled")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Long read timeout: a cold request includes Ollama's model load time.
        _client = httpx.AsyncClient(
            base_url=UPSTREAM_URL,
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        )
    return _client


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Best effort: preload the model and pin it in memory. keep_alive is not
    # settable through /v1, so this native call is the only way to prevent the
    # 5-minute idle unload without reconfiguring the Ollama server itself.
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
    yield
    if _client is not None:
        await _client.aclose()


app = FastAPI(title="thor-translation glossary proxy", lifespan=_lifespan)


async def post_upstream(path: str, payload: dict) -> httpx.Response:
    return await _get_client().post(path, json=payload)


async def get_upstream(path: str) -> httpx.Response:
    return await _get_client().get(path)


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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "Request body is not valid JSON")

    user_content = _last_user_content(body)
    if user_content is None:
        return _error(400, "No user message with string content in request")

    context_pairs, payload = sakura.split_pt_user_message(user_content)
    history = [translation for _, translation in context_pairs]

    # PlayTranslate marks its batch path (several OCR regions in one request)
    # by attaching response_format; single requests never carry it.
    is_batch = "response_format" in body
    if is_batch:
        texts = sakura.extract_batch_texts(payload)
        if texts is None:
            # A 400 on the batch path makes PT retry each text individually.
            return _error(400, "Could not extract a JSON string array from batch request")
        input_text = "\n".join(sakura.escape_line(text) for text in texts)
        match_basis = "\n".join(texts)
    else:
        texts = None
        # Escape real newlines like the batch path: to Sakura, "\n" separates
        # independent texts, so an unescaped newline would split one dialogue
        # box into unrelated fragments.
        input_text = sakura.escape_line(payload)
        match_basis = payload

    entries = glossary.match(match_basis)
    upstream_body = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": sakura.SYSTEM_PROMPT},
            {"role": "user", "content": sakura.build_user_prompt(entries, history, input_text)},
        ],
        # v1 proxy is non-streaming: incoming "stream": true is forced off.
        "stream": False,
        # GalTransl heuristic: budget roughly 2 output tokens per input char.
        "max_tokens": min(MAX_COMPLETION_TOKENS, max(128, 2 * len(input_text))),
        **sakura.SAMPLING,
    }

    try:
        resp = await post_upstream("/v1/chat/completions", upstream_body)
    except httpx.HTTPError as exc:
        return _error(502, f"Upstream {UPSTREAM_URL} unreachable: {exc!r}")
    if resp.status_code != 200:
        return _upstream_error(resp)
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, LookupError, TypeError):
        excerpt = " ".join(resp.text[:UPSTREAM_ERROR_EXCERPT].split())
        return _error(502, f"Upstream returned a malformed completion: {excerpt}")

    if is_batch:
        lines = sakura.split_output_lines(content)
        if len(lines) != len(texts):
            # Unrecoverable here; a 400 triggers PT's per-text retry path.
            return _error(
                400,
                f"Batch line count mismatch: sent {len(texts)} lines, model returned {len(lines)}",
            )
        data["choices"][0]["message"]["content"] = json.dumps(
            {"translations": lines}, ensure_ascii=False
        )
    else:
        data["choices"][0]["message"]["content"] = sakura.unescape_line(content)

    # Unchanged fields (usage, id, ...) pass through — PT feeds its token
    # meter from usage.
    return JSONResponse(data)


@app.get("/v1/models")
async def models(request: Request) -> JSONResponse:
    # Not real auth (the proxy is LAN-only): rejecting keyless probes makes
    # PlayTranslate's key check (which probes with AND without the key, and
    # expects the keyless probe to fail) report the endpoint as OK.
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return _error(401, "Missing bearer token")
    try:
        resp = await get_upstream("/v1/models")
    except httpx.HTTPError as exc:
        return _error(502, f"Upstream {UPSTREAM_URL} unreachable: {exc!r}")
    if resp.status_code != 200:
        return _upstream_error(resp)
    try:
        return JSONResponse(resp.json())
    except ValueError:
        excerpt = " ".join(resp.text[:UPSTREAM_ERROR_EXCERPT].split())
        return _error(502, f"Upstream returned malformed JSON for /v1/models: {excerpt}")
