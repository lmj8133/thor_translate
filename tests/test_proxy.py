"""Proxy route tests: glossary injection, PT request parsing, error mapping.

Upstream (Ollama) calls are intercepted by monkeypatching
``main.post_upstream`` / ``main.get_upstream``; the app is exercised through
``httpx.ASGITransport`` without a network.
"""

import json
from pathlib import Path

import httpx
import pytest

from server.proxy import main, sakura
from server.proxy.glossary import Glossary, GlossaryError

GLOSSARY_TEXT = "ダイゴ->大吾 #人名\nポケモン->寶可夢\nこうもく->項目\n"


class FakeUpstream:
    """Records the forwarded payload and serves a configurable reply."""

    def __init__(self):
        self.payload: dict | None = None
        self.path: str | None = None
        self.status = 200
        self.content = "大吾先生在哪里？"
        self.raw_body: str | None = None

    async def post(self, path: str, payload: dict) -> httpx.Response:
        self.path = path
        self.payload = payload
        if self.raw_body is not None:
            return httpx.Response(self.status, text=self.raw_body)
        return httpx.Response(
            self.status,
            json={
                "id": "chatcmpl-test",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": self.content}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )


class FakeCloud:
    """Records cloud payloads; replies per configured (status, content) queue."""

    def __init__(self):
        self.payloads: list[dict] = []
        self.replies: list[tuple[int, str]] = [(200, "雲端譯文")]

    async def post(self, payload: dict) -> httpx.Response:
        self.payloads.append(payload)
        status, content = self.replies[min(len(self.payloads) - 1, len(self.replies) - 1)]
        if status != 200:
            return httpx.Response(status, text="quota exceeded")
        return httpx.Response(
            status,
            json={
                "id": "chatcmpl-cloud",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}}
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            },
        )


@pytest.fixture(autouse=True)
def _local_only_by_default(monkeypatch):
    # Keep the pre-cloud tests deterministic regardless of the dev machine's
    # environment; cloud tests opt in via the `cloud_chain` fixture.
    monkeypatch.setattr(main, "GEMINI_API_KEY", "")


@pytest.fixture
def cloud_chain(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["cloud-a", "cloud-b"])
    monkeypatch.setattr(main, "post_cloud", fake.post)
    return fake


@pytest.fixture
def upstream(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(main, "post_upstream", fake.post)
    return fake


@pytest.fixture
def glossary_file(tmp_path, monkeypatch):
    path = tmp_path / "glossary.txt"
    path.write_text(GLOSSARY_TEXT, encoding="utf-8")
    monkeypatch.setattr(main, "glossary", Glossary(path))
    return path


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def pt_request(text: str, context: list[tuple[str, str]] | None = None) -> dict:
    """A request shaped like PlayTranslate's default single-text template."""
    content = ""
    if context:
        content += "Recent dialogue lines, for context only:\n"
        for source, translation in context:
            content += f"- {source} → {translation}\n"
        content += "\n"
    content += f"Please translate the following Japanese text into Chinese:\n\n{text}"
    return {
        "model": "chat-latest",
        "messages": [
            {"role": "system", "content": "You are a professional Japanese (ja) to Chinese (zh) translator."},
            {"role": "user", "content": content},
        ],
    }


def pt_batch_request(texts: list[str]) -> dict:
    """A request shaped like PlayTranslate's default batch template."""
    request = pt_request("")
    request["messages"][1]["content"] = (
        f"Translate each of these {len(texts)} strings:\n"
        + json.dumps(texts, ensure_ascii=False, separators=(",", ":"))
    )
    request["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": "translations", "strict": True, "schema": {}},
    }
    return request


async def test_glossary_hit_injected(client, upstream, glossary_file):
    resp = await client.post("/v1/chat/completions", json=pt_request("ダイゴさんは　どこ？"))
    assert resp.status_code == 200
    assert upstream.path == "/v1/chat/completions"
    prompt = upstream.payload["messages"][1]["content"]
    assert "ダイゴ->大吾 #人名" in prompt
    assert "寶可夢" not in prompt  # unmatched term stays out
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nダイゴさんは　どこ？")
    assert upstream.payload["messages"][0]["content"] == sakura.SYSTEM_PROMPT
    assert upstream.payload["model"] == main.OLLAMA_MODEL
    assert upstream.payload["temperature"] == sakura.SAMPLING["temperature"]
    data = resp.json()
    # Simplified 哪里 from the model comes back as Taiwan Traditional 哪裡.
    assert data["choices"][0]["message"]["content"] == "大吾先生在哪裡？"
    assert data["usage"]["completion_tokens"] == 5  # usage passes through


async def test_no_match_no_injection(client, upstream, glossary_file):
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    # The glossary slot between the two instruction lines stays empty.
    assert "参考以下术语表（可为空，格式为src->dst #备注）：\n\n根据以上术语表" in prompt
    assert "大吾" not in prompt


async def test_missing_glossary_file_raises():
    missing = Path("/nonexistent/glossary.txt")
    with pytest.raises(GlossaryError) as excinfo:
        Glossary(missing)
    assert str(missing) in str(excinfo.value)


async def test_output_converted_to_taiwan_traditional(client, upstream, glossary_file):
    # s2twp is phrase-level TW localization, not just glyphs: 软件 → 軟體.
    upstream.content = "这个软件不能用了"
    resp = await client.post("/v1/chat/completions", json=pt_request("このソフトはもう使えない"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "這個軟體不能用了"


async def test_local_s2twp_does_not_clobber_glossary_terms(client, upstream, glossary_file):
    # The model echoes the injected 項目 as Simplified 项目; a naive s2twp pass
    # would phrase-map that to 專案 — the protection restores the glossary form.
    upstream.content = "请选择想看的项目"
    resp = await client.post(
        "/v1/chat/completions", json=pt_request("みたい　こうもくを　えらんで　ください")
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "請選擇想看的項目"


async def test_history_normalized_back_to_simplified(client, upstream, glossary_file):
    # PT echoes our Traditional output as context; the model is trained on
    # Simplified, so the 历史翻译 block must be normalized with tw2sp.
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request("つづき", context=[("ソフトのはなし。", "這個軟體不能用了")]),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.startswith("历史翻译：这个软件不能用了\n")


async def test_context_becomes_history(client, upstream, glossary_file):
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request("ダイゴさんは　どこ？", context=[("こんにちは！", "你好"), ("行くぞ！", "走吧")]),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.startswith("历史翻译：你好\n走吧\n")
    assert "Recent dialogue lines" not in prompt
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nダイゴさんは　どこ？")


async def test_incomplete_previous_box_joins_input(client, upstream, glossary_file):
    # The previous box ends mid-sentence (no sentence-final punctuation), so
    # its source joins the input and its translation leaves the history.
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request(
            "きみを　むかえに　きたんだ！",
            context=[("ダイゴさんに　たのまれて", "被大吾先生拜託")],
        ),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.endswith(
        "将下面的文本从日文翻译成简体中文：\nダイゴさんに　たのまれてきみを　むかえに　きたんだ！"
    )
    assert "历史翻译" not in prompt
    assert "ダイゴ->大吾 #人名" in prompt  # glossary matches over the joined text


async def test_continuation_chain_keeps_older_history(client, upstream, glossary_file):
    # Two trailing incomplete boxes join; the older finished pair stays as history.
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request(
            "きたんだ！",
            context=[
                ("こんにちは！", "你好"),
                ("ダイゴさんに　たのまれて", "被大吾先生拜託"),
                ("きみを　むかえに", "來迎接你"),
            ],
        ),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.startswith("历史翻译：你好\n")
    assert prompt.endswith(
        "将下面的文本从日文翻译成简体中文：\nダイゴさんに　たのまれてきみを　むかえにきたんだ！"
    )


async def test_continuation_join_can_be_disabled(client, upstream, glossary_file, monkeypatch):
    monkeypatch.setattr(main, "CONTINUATION_JOIN", False)
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request("きたんだ！", context=[("たのまれて", "被拜託")]),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.startswith("历史翻译：被拜托\n")
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nきたんだ！")


async def test_custom_minimal_template_passthrough(client, upstream, glossary_file):
    # A user-customized PT template "{context}{text}" delivers the bare text.
    body = {"model": "m", "messages": [{"role": "user", "content": "ポケモンだいすき"}]}
    resp = await client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert "ポケモン->寶可夢" in prompt
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nポケモンだいすき")


async def test_single_path_escapes_and_unescapes_newlines(client, upstream, glossary_file):
    # To Sakura "\n" separates independent texts, so a multi-line single text
    # must round-trip through the literal backslash-n escape.
    upstream.content = "第一行\\n第二行"
    body = {"model": "m", "messages": [{"role": "user", "content": "いちぎょうめ\nにぎょうめ"}]}
    resp = await client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nいちぎょうめ\\nにぎょうめ")
    assert resp.json()["choices"][0]["message"]["content"] == "第一行\n第二行"


async def test_ambiguous_context_pair_dropped(client, upstream, glossary_file):
    # A context line whose source/translation contains " → " cannot be split
    # unambiguously — it is dropped while the rest of the block still parses.
    content = (
        "Recent dialogue lines, for context only:\n"
        "- こうげき → ぼうぎょ → 攻击 → 防御\n"
        "- こんにちは！ → 你好\n"
        "\n"
        "Please translate the following Japanese text into Chinese:\n\nたたかう"
    )
    body = {"model": "m", "messages": [{"role": "user", "content": content}]}
    resp = await client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.startswith("历史翻译：你好\n")
    assert "防御" not in prompt
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nたたかう")


async def test_stream_forced_false(client, upstream, glossary_file):
    body = pt_request("こんにちは")
    body["stream"] = True
    resp = await client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200
    assert upstream.payload["stream"] is False


async def test_empty_content_passes_through(client, upstream, glossary_file):
    upstream.content = ""
    resp = await client.post("/v1/chat/completions", json=pt_request("……"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == ""


async def test_upstream_error_maps_to_502(client, upstream, glossary_file):
    upstream.status = 500
    upstream.raw_body = "internal server error\nwith details"
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 502
    message = resp.json()["error"]["message"]
    assert "500" in message
    assert "internal server error" in message


async def test_upstream_unreachable_maps_to_502(client, glossary_file, monkeypatch):
    async def raise_connect_error(path, payload):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(main, "post_upstream", raise_connect_error)
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["error"]["message"]


async def test_invalid_json_body(client, upstream):
    resp = await client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


async def test_no_user_message(client, upstream):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "system", "content": "hi"}]},
    )
    assert resp.status_code == 400


async def test_batch_translates_line_per_text(client, upstream, glossary_file):
    upstream.content = "大吾先生\n你好"
    resp = await client.post(
        "/v1/chat/completions", json=pt_batch_request(["ダイゴさん", "こんにちは"])
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nダイゴさん\nこんにちは")
    assert "ダイゴ->大吾 #人名" in prompt
    content = resp.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {"translations": ["大吾先生", "你好"]}


async def test_batch_escapes_inner_newlines(client, upstream, glossary_file):
    upstream.content = "第一行\\n第二行\n你好"
    resp = await client.post(
        "/v1/chat/completions", json=pt_batch_request(["いちぎょうめ\nにぎょうめ", "こんにちは"])
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nいちぎょうめ\\nにぎょうめ\nこんにちは")
    content = resp.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {"translations": ["第一行\n第二行", "你好"]}


async def test_batch_count_mismatch_maps_to_400(client, upstream, glossary_file):
    upstream.content = "只有一行"
    resp = await client.post(
        "/v1/chat/completions", json=pt_batch_request(["ダイゴさん", "こんにちは"])
    )
    assert resp.status_code == 400
    assert "line count" in resp.json()["error"]["message"]


async def test_batch_unparsable_payload_maps_to_400(client, upstream, glossary_file):
    request = pt_batch_request(["placeholder"])
    request["messages"][1]["content"] = "Translate each of these 1 strings:\nnot an array"
    resp = await client.post("/v1/chat/completions", json=request)
    assert resp.status_code == 400


async def test_cloud_first_serves_without_local(client, cloud_chain, upstream, glossary_file):
    cloud_chain.replies = [(200, "大吾先生在哪裡？")]
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request("ダイゴさんは　どこ？", context=[("こんにちは！", "你好")]),
    )
    assert resp.status_code == 200
    # Cloud output is already Taiwan Traditional — returned verbatim, no OpenCC.
    assert resp.json()["choices"][0]["message"]["content"] == "大吾先生在哪裡？"
    assert upstream.payload is None  # local backend never called
    payload = cloud_chain.payloads[0]
    assert payload["model"] == "cloud-a"
    prompt = payload["messages"][1]["content"]
    assert "ダイゴ → 大吾（人名）" in prompt
    assert "你好" in prompt  # cloud history stays Traditional as-is
    assert prompt.endswith("翻譯以下文本：\nダイゴさんは　どこ？")


async def test_cloud_429_slides_to_next_model(client, cloud_chain, upstream, glossary_file):
    cloud_chain.replies = [(429, ""), (200, "第二棒接手")]
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "第二棒接手"
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a", "cloud-b"]
    assert upstream.payload is None


async def test_cloud_exhausted_falls_back_to_local(client, cloud_chain, upstream, glossary_file):
    cloud_chain.replies = [(429, ""), (429, "")]
    upstream.content = "大吾先生在哪里？"
    resp = await client.post("/v1/chat/completions", json=pt_request("ダイゴさんは　どこ？"))
    assert resp.status_code == 200
    # Local Sakura path: its Simplified output still gets the OpenCC pass.
    assert resp.json()["choices"][0]["message"]["content"] == "大吾先生在哪裡？"
    assert upstream.payload["model"] == main.OLLAMA_MODEL
    assert upstream.payload["messages"][0]["content"] == sakura.SYSTEM_PROMPT


async def test_batch_mismatch_on_cloud_falls_back_to_local(client, cloud_chain, upstream, glossary_file):
    cloud_chain.replies = [(200, "只有一行")]  # wrong line count for both cloud models
    upstream.content = "大吾先生\n你好"
    resp = await client.post(
        "/v1/chat/completions", json=pt_batch_request(["ダイゴさん", "こんにちは"])
    )
    assert resp.status_code == 200
    content = resp.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {"translations": ["大吾先生", "你好"]}
    assert upstream.payload is not None  # local served after cloud mismatches


def test_is_sentence_complete_edges():
    assert sakura.is_sentence_complete("いるよ！")
    assert sakura.is_sentence_complete("ものがたり。")
    assert sakura.is_sentence_complete("なに？　")  # trailing full-width space
    assert sakura.is_sentence_complete("")
    # Manual/menu pages end sentences without punctuation; grammatical
    # sentence-final forms must count as complete or joins snowball there.
    assert sakura.is_sentence_complete("せつめい　します")
    assert sakura.is_sentence_complete("えらんで　ください")
    assert sakura.is_sentence_complete("ポケモンずかんだ")
    assert not sakura.is_sentence_complete("たのまれて")
    assert not sakura.is_sentence_complete("それは、")
    assert not sakura.is_sentence_complete("いろいろな　ことを")


async def test_models_requires_bearer_and_passes_through(client, monkeypatch):
    async def fake_get(path):
        return httpx.Response(200, json={"object": "list", "data": [{"id": "sakura-galtransl-v3.7"}]})

    monkeypatch.setattr(main, "get_upstream", fake_get)
    keyless = await client.get("/v1/models")
    assert keyless.status_code == 401
    keyed = await client.get("/v1/models", headers={"Authorization": "Bearer anything"})
    assert keyed.status_code == 200
    assert keyed.json()["data"][0]["id"] == "sakura-galtransl-v3.7"


async def test_models_non_json_200_maps_to_502(client, monkeypatch):
    async def fake_get(path):
        return httpx.Response(200, text="<html>captive portal</html>")

    monkeypatch.setattr(main, "get_upstream", fake_get)
    resp = await client.get("/v1/models", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 502
    assert "malformed" in resp.json()["error"]["message"]
