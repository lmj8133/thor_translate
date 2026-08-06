"""Proxy route tests: glossary injection, PT request parsing, error mapping.

Upstream (Ollama) calls are intercepted by monkeypatching
``main.post_upstream`` / ``main.post_cloud``; the app is exercised through
``httpx.ASGITransport`` without a network.
"""

import json
import time
from pathlib import Path

import httpx
import pytest

from server.proxy import cloud, main, sakura
from server.proxy.glossary import Glossary, GlossaryError

_REAL_CLOUD_REACHABLE = main._cloud_reachable

GLOSSARY_TEXT = "ダイゴ->大吾 #人名\nポケモン->寶可夢\nこうもく->項目\n"


class FakeUpstream:
    """Records the forwarded payload and serves a configurable reply."""

    def __init__(self):
        self.payload: dict | None = None
        self.path: str | None = None
        self.status = 200
        self.content = "大吾先生在哪里？"
        self.raw_body: str | None = None
        self.calls = 0

    async def post(self, path: str, payload: dict) -> httpx.Response:
        self.calls += 1
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
        self.timeouts: list[float | None] = []
        self.api_keys: list[str] = []
        self.base_urls: list[str] = []
        self.replies: list[tuple[int, str]] = [(200, "雲端譯文")]

    async def post(
        self,
        payload: dict,
        read_timeout: float | None = None,
        api_key: str = "",
        base_url: str = "",
    ) -> httpx.Response:
        self.payloads.append(payload)
        self.timeouts.append(read_timeout)
        self.api_keys.append(api_key)
        self.base_urls.append(base_url)
        status, content = self.replies[min(len(self.payloads) - 1, len(self.replies) - 1)]
        if status != 200:
            # An empty content stands in for today's generic body; tests that
            # exercise the PerDay quota marking supply a real body instead.
            return httpx.Response(status, text=content or "quota exceeded")
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
    monkeypatch.setattr(main, "GEMINI_API_KEYS", "")
    monkeypatch.setattr(main, "CLOUD_ENDPOINTS", "")
    # Module-global state must not bleed between tests: the response cache is
    # off by default (cache tests opt in via `cache_on`) and the negative
    # memory / recent-source dicts start empty.
    monkeypatch.setattr(main, "TRANSLATION_CACHE_SIZE", 0)
    monkeypatch.setattr(main, "_translation_cache", main.OrderedDict())
    monkeypatch.setattr(main, "_cache_stats", {"requests": 0, "hits": 0, "repeat_misses": 0})
    monkeypatch.setattr(main, "_quota_backoff_s", {})
    monkeypatch.setattr(main, "_model_strikes", {})
    monkeypatch.setattr(main, "_model_strike_at", {})
    monkeypatch.setattr(main, "_model_cooldown_until", {})
    monkeypatch.setattr(main, "_recent_sources", {})
    monkeypatch.setattr(main, "_cloud_clients", {})
    # Startup's readiness wait must never touch the network or the
    # notification shade from a test; the readiness tests opt back in by
    # patching these themselves.
    async def _reachable(_endpoint):
        return True

    # Tests that exercise the real probe re-patch this with `real_probe`.
    monkeypatch.setattr(main, "_cloud_reachable", _reachable)
    monkeypatch.setattr(main, "_status", {
        "served": 0, "last_backend": "", "last_seconds": 0.0,
        "last_at": 0.0, "started_at": time.time(), "ready": "",
    })


@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setattr(main, "TRANSLATION_CACHE_SIZE", 8)


@pytest.fixture
def real_probe(monkeypatch):
    """Undo the autouse stub for tests of the readiness probe itself."""
    monkeypatch.setattr(main, "_cloud_reachable", _REAL_CLOUD_REACHABLE)


@pytest.fixture
def cloud_chain(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["cloud-a", "cloud-b"])
    monkeypatch.setattr(main, "post_cloud", fake.post)
    # Sticky-failover state is module-global; reset both halves per test.
    monkeypatch.setattr(main, "_cloud_leader", None)
    monkeypatch.setattr(main, "_leader_quota_day", None)
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
    # Japanese source passes through untouched; only the Chinese side converts.
    assert prompt.startswith("历史翻译：\nソフトのはなし。 → 这个软件不能用了\n")


async def test_context_becomes_history(client, upstream, glossary_file):
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request("ダイゴさんは　どこ？", context=[("こんにちは！", "你好"), ("行くぞ！", "走吧")]),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.startswith("历史翻译：\nこんにちは！ → 你好\n行くぞ！ → 走吧\n")
    assert "Recent dialogue lines" not in prompt
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nダイゴさんは　どこ？")


async def test_screen_label_is_not_treated_as_a_continuation(client, upstream, glossary_file):
    # Regression: ワカバタウン is a permanent map caption PlayTranslate keeps in
    # its context block. It ends on a noun, so it is neither a finished
    # sentence nor a cut-off one, and must never be glued onto dialogue.
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request(
            "やあ！　ヒビキなら　２かいに　いるよ！",
            context=[("ワカバタウン", "若葉鎮")],
        ),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.endswith(
        "将下面的文本从日文翻译成简体中文：\nやあ！　ヒビキなら　２かいに　いるよ！"
    )
    assert "ワカバタウン" in prompt  # still available as history, just not joined


def test_continues_into_next_box_needs_a_dangling_grammar_ending():
    # Cut-off utterances end on particles or conjunctive forms...
    assert sakura.continues_into_next_box("ダイゴさんに　たのまれて")
    assert sakura.continues_into_next_box("いろいろな　ことを")
    assert sakura.continues_into_next_box("それは、")
    # ...whereas labels end on a noun, and finished sentences end properly.
    assert not sakura.continues_into_next_box("ワカバタウン")
    assert not sakura.continues_into_next_box("ポケモンセンター")
    assert not sakura.continues_into_next_box("えらんで　ください")
    assert not sakura.continues_into_next_box("いるよ！")


async def test_stale_context_line_is_not_joined(client, upstream, glossary_file):
    # A dangling-looking line the proxy did not just translate (e.g. a caption
    # lingering in PT's context) is history only, never joined; the autouse
    # fixture guarantees a fresh _recent_sources.
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request("きたんだ！", context=[("たのまれて", "被拜託")]),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.endswith("将下面的文本从日文翻译成简体中文：\nきたんだ！")


async def test_incomplete_previous_box_joins_input(client, upstream, glossary_file):
    # The previous box ends mid-sentence, so its source joins the input and
    # its translation leaves the history. Translate it first: joining only
    # applies to lines this proxy handled moments ago.
    await client.post(
        "/v1/chat/completions", json=pt_request("ダイゴさんに　たのまれて")
    )
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
    for line in ("ダイゴさんに　たのまれて", "きみを　むかえに"):
        await client.post("/v1/chat/completions", json=pt_request(line))
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
    assert prompt.startswith("历史翻译：\nこんにちは！ → 你好\n")
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
    assert prompt.startswith("历史翻译：\nたのまれて → 被拜托\n")
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
    assert prompt.startswith("历史翻译：\nこんにちは！ → 你好\n")
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


async def test_cloud_history_carries_the_japanese_source(client, cloud_chain, upstream, glossary_file):
    # Translation-only history hid who was speaking, turning a
    # self-introduction into the third person; both sides are sent now.
    cloud_chain.replies = [(200, "大家都叫我寶可夢博士")]
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request(
            "みんなからは　はかせと　よばれておる",
            context=[("わしの　なまえは　オダマキ。", "我的名字是小田卷")],
        ),
    )
    assert resp.status_code == 200
    prompt = cloud_chain.payloads[0]["messages"][1]["content"]
    assert "わしの　なまえは　オダマキ。 → 我的名字是小田卷" in prompt


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


async def test_cloud_read_timeouts_are_impatient_except_last(client, cloud_chain, upstream, glossary_file):
    # Single-line requests get the tight timeout; the local backend is last in
    # the chain here, so every cloud attempt is allowed to be impatient.
    cloud_chain.replies = [(429, ""), (200, "第二棒")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert cloud_chain.timeouts == [
        main.CLOUD_READ_TIMEOUT_SINGLE,
        main.CLOUD_READ_TIMEOUT_SINGLE,
    ]

    # Batches are legitimately slower, so they get the roomier budget.
    cloud_chain.payloads.clear()
    cloud_chain.timeouts.clear()
    cloud_chain.replies = [(200, "大吾先生\n你好")]
    await client.post(
        "/v1/chat/completions", json=pt_batch_request(["ダイゴさん", "こんにちは"])
    )
    assert cloud_chain.timeouts == [main.CLOUD_READ_TIMEOUT_BATCH]


async def test_last_attempt_gets_unlimited_read_timeout(client, cloud_chain, glossary_file, monkeypatch):
    # With no local fallback configured, the final cloud model is the last
    # resort and must not be abandoned early.
    monkeypatch.setattr(main, "CLOUD_MODELS", ["cloud-a", "cloud-b"])
    monkeypatch.setattr(main, "OLLAMA_MODEL", "")
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")

    async def no_local(path, payload):
        raise httpx.ConnectError("no local backend")

    monkeypatch.setattr(main, "post_upstream", no_local)
    cloud_chain.replies = [(429, ""), (200, "最後一棒")]
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 200
    # cloud-a is impatient, cloud-b is not the last attempt (local is), so it
    # is impatient too — the local backend has no cloud timeout to record.
    assert cloud_chain.timeouts == [
        main.CLOUD_READ_TIMEOUT_SINGLE,
        main.CLOUD_READ_TIMEOUT_SINGLE,
    ]


async def test_cloud_429_slides_to_next_model(client, cloud_chain, upstream, glossary_file):
    cloud_chain.replies = [(429, ""), (200, "第二棒接手")]
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "第二棒接手"
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a", "cloud-b"]
    assert upstream.payload is None


async def test_failover_is_sticky_until_the_leader_itself_fails(client, cloud_chain, upstream, glossary_file):
    # 500s exercise the pure reordering: unlike 429s they carry no cooldown.
    # cloud-a fails once: cloud-b rescues the request and becomes the leader.
    cloud_chain.replies = [(500, "boom"), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a", "cloud-b"]

    # Next requests go straight to cloud-b - no re-probing of the sick model.
    cloud_chain.payloads.clear()
    cloud_chain.timeouts.clear()
    cloud_chain.replies = [(200, "b 繼續")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-b", "cloud-b"]

    # Only when the leader itself fails does the chain reach for cloud-a again.
    cloud_chain.payloads.clear()
    cloud_chain.replies = [(500, "boom"), (200, "a 回鍋")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-b", "cloud-a"]


async def test_leader_resets_when_the_quota_day_rolls_over(client, cloud_chain, upstream, glossary_file, monkeypatch):
    # cloud-a exhausts its quota; cloud-b takes over and sticks.
    cloud_chain.replies = [(429, ""), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    cloud_chain.payloads.clear()
    cloud_chain.replies = [(200, "b 繼續")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-b"]

    # Next Pacific day: quotas are fresh, so the preferred model leads again.
    monkeypatch.setattr(main, "_quota_day", lambda: main._leader_quota_day + 1)
    cloud_chain.payloads.clear()
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a"]


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
    assert sakura.is_sentence_complete("つよく　なりたいのさ")
    assert sakura.is_sentence_complete("まけないぞ")
    assert sakura.is_sentence_complete("どう　なるんだろう")
    assert not sakura.is_sentence_complete("おおきな")  # prenominal, continues
    assert not sakura.is_sentence_complete("たのまれて")
    assert not sakura.is_sentence_complete("それは、")
    assert not sakura.is_sentence_complete("いろいろな　ことを")


async def test_model_field_selects_per_game_glossary(client, upstream, glossary_file, tmp_path, monkeypatch):
    # model "dq7" → glossaries dir dq7.txt; unknown names fall back to default.
    game_dir = tmp_path / "games"
    game_dir.mkdir()
    (game_dir / "dq7.txt").write_text("ダイゴ->勇者大吾 #dq7专用\n", encoding="utf-8")
    monkeypatch.setattr(main, "GLOSSARY_DIR", str(game_dir))
    monkeypatch.setattr(main, "_game_glossaries", {})

    request = pt_request("ダイゴさんは　どこ？")
    request["model"] = "dq7"
    resp = await client.post("/v1/chat/completions", json=request)
    assert resp.status_code == 200
    assert "ダイゴ->勇者大吾 #dq7专用" in upstream.payload["messages"][1]["content"]

    request["model"] = "no-such-game"  # falls back to the default glossary
    resp = await client.post("/v1/chat/completions", json=request)
    assert "ダイゴ->大吾 #人名" in upstream.payload["messages"][1]["content"]

    request["model"] = "../evil"  # path-ish names are ignored, default used
    resp = await client.post("/v1/chat/completions", json=request)
    assert "ダイゴ->大吾 #人名" in upstream.payload["messages"][1]["content"]


async def test_models_lists_glossaries_behind_bearer(client, tmp_path, monkeypatch):
    game_dir = tmp_path / "games"
    game_dir.mkdir()
    (game_dir / "pokemon-oras.txt").write_text("ポケモン->寶可夢\n", encoding="utf-8")
    (game_dir / "dq7.txt").write_text("スライム->史萊姆\n", encoding="utf-8")
    monkeypatch.setattr(main, "GLOSSARY_DIR", str(game_dir))

    keyless = await client.get("/v1/models")
    assert keyless.status_code == 401
    keyed = await client.get("/v1/models", headers={"Authorization": "Bearer anything"})
    assert keyed.status_code == 200
    # The picker menu doubles as the game selector: ids are glossary names.
    assert [model["id"] for model in keyed.json()["data"]] == ["dq7", "pokemon-oras"]


# The Gemini OpenAI-compat endpoint wraps the error in a JSON array; captured
# from a real exhausted-quota reply on 2026-08-04.
class _NoOllama:
    """Stands in for the Ollama client so startup checks stay socket-free."""

    async def post(self, path, json=None):
        raise httpx.ConnectError("no local backend in tests")


async def test_429_benches_the_pair_for_its_cooldown(client, cloud_chain, upstream, glossary_file):
    # Any 429 cools the pair down - provider-neutral, no body signature
    # needed. Even after the rescuer later fails, the cooling pair is not
    # re-probed; the request degrades to local instead.
    cloud_chain.replies = [(429, ""), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a", "cloud-b"]

    cloud_chain.payloads.clear()
    cloud_chain.replies = [(500, "boom")]
    upstream.content = "本地接手"
    resp = await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert resp.status_code == 200
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-b"]


def test_429_backoff_escalates_and_is_capped():
    # No Retry-After hint: the base cooldown applies and doubles per repeat,
    # so an exhausted daily quota converges to one probe per cap window.
    pair = (main.Endpoint("u", "k", ("m",)), "m")
    resp = httpx.Response(429, text="quota exceeded")
    main._note_cloud_response(pair, resp)
    first = main._model_cooldown_until[pair] - time.monotonic()
    assert 0 < first <= main.CLOUD_429_COOLDOWN_S + 1
    assert main._quota_backoff_s[pair] == main.CLOUD_429_COOLDOWN_S * 2
    main._note_cloud_response(pair, resp)
    assert main._quota_backoff_s[pair] == main.CLOUD_429_COOLDOWN_S * 4
    main._quota_backoff_s[pair] = main.CLOUD_429_COOLDOWN_MAX_S
    main._note_cloud_response(pair, resp)
    capped = main._model_cooldown_until[pair] - time.monotonic()
    assert capped <= main.CLOUD_429_COOLDOWN_MAX_S + 1


def test_429_honors_the_providers_retry_after_hint():
    # The hint may arrive as the standard header or in Gemini's body phrasing;
    # either raises the cooldown floor above the escalating base.
    header_pair = (main.Endpoint("u1", "k", ("m",)), "m")
    resp = httpx.Response(429, headers={"retry-after": "600"}, text="slow down")
    main._note_cloud_response(header_pair, resp)
    assert main._model_cooldown_until[header_pair] - time.monotonic() > 500

    body_pair = (main.Endpoint("u2", "k", ("m",)), "m")
    resp = httpx.Response(429, text="You exceeded your quota. Please retry in 400.5s.")
    main._note_cloud_response(body_pair, resp)
    assert main._model_cooldown_until[body_pair] - time.monotonic() > 300


def test_quota_day_rollover_clears_the_benches(monkeypatch):
    # Fresh day, fresh chances: crossing the leader's daily boundary expires
    # every 429 backoff and strike bench alongside the leader itself.
    monkeypatch.setattr(main, "CLOUD_ENDPOINTS", "u|k|m")
    pair = (main._cloud_endpoints()[0], "m")
    main._model_cooldown_until[pair] = time.monotonic() + 999
    main._quota_backoff_s[pair] = 240.0
    monkeypatch.setattr(main, "_cloud_leader", pair)
    monkeypatch.setattr(main, "_leader_quota_day", main._quota_day() - 1)
    main._cloud_chain()
    assert main._model_cooldown_until == {}
    assert main._quota_backoff_s == {}


def test_transport_strikes_bench_after_limit():
    pair = ("k", "m")
    for _ in range(main.CLOUD_STRIKE_LIMIT):
        main._note_cloud_transport_error(pair, started=time.monotonic())
    assert not main._cloud_available(pair)
    # The bench expires: pretend the cooldown deadline has passed.
    main._model_cooldown_until[pair] = time.monotonic() - 1
    assert main._cloud_available(pair)
    assert pair not in main._model_cooldown_until


def test_overlapped_attempts_count_one_strike():
    # Two requests in flight during one blip: the second failure STARTED
    # before the first strike was recorded, so it must not double-count.
    pair = ("k", "m")
    started = time.monotonic() - 1.0
    main._note_cloud_transport_error(pair, started)
    main._note_cloud_transport_error(pair, started)
    assert main._model_strikes[pair] == 1


async def test_all_stalling_clouds_get_benched(client, cloud_chain, upstream, glossary_file, monkeypatch):
    # When EVERY cloud model keeps timing out, sticky failover alone never
    # demotes them (demotion needs a cloud success); the strike cooldown must
    # stop the per-request stall tax.
    calls: list[str] = []

    async def stall(payload, read_timeout=None, api_key="", base_url=""):
        calls.append(payload["model"])
        raise httpx.ReadTimeout("stall")

    monkeypatch.setattr(main, "post_cloud", stall)
    upstream.content = "本地"
    for _ in range(main.CLOUD_STRIKE_LIMIT):
        resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
        assert resp.status_code == 200
    benched = len(calls)
    resp = await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert resp.status_code == 200
    assert len(calls) == benched  # no further cloud probes while cooling down


async def test_cloud_success_clears_strikes(client, cloud_chain, upstream, glossary_file):
    pair = (main._cloud_endpoints()[0], "cloud-a")
    main._model_strikes[pair] = 2
    main._model_strike_at[pair] = time.monotonic()
    cloud_chain.replies = [(200, "好")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert pair not in main._model_strikes


async def test_startup_smoke_seeds_the_cooldown_table(monkeypatch):
    fake = FakeCloud()
    fake.replies = [(429, "quota exceeded")]
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["cloud-a"])
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_get_client", lambda: _NoOllama())
    await main._startup_checks()
    pair = (main._cloud_endpoints()[0], "cloud-a")
    assert pair in main._model_cooldown_until


async def test_startup_smoke_can_be_disabled(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "STARTUP_SMOKE", False)
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_get_client", lambda: _NoOllama())
    await main._startup_checks()
    assert fake.payloads == []


async def test_cache_hit_skips_the_second_cloud_call(client, cache_on, cloud_chain, upstream, glossary_file):
    cloud_chain.replies = [(200, "大吾先生在哪裡？")]
    first = await client.post("/v1/chat/completions", json=pt_request("ダイゴさんは　どこ？"))
    second = await client.post("/v1/chat/completions", json=pt_request("ダイゴさんは　どこ？"))
    assert len(cloud_chain.payloads) == 1  # one upstream call for two requests
    assert second.json() == first.json()  # usage replays verbatim on a hit
    assert second.json()["usage"]["completion_tokens"] == 8


async def test_cache_misses_on_different_history(client, cache_on, cloud_chain, upstream, glossary_file):
    # Same line under different context may translate differently — the exact
    # key includes history, so this is two upstream calls, not a hit.
    cloud_chain.replies = [(200, "譯文")]
    await client.post(
        "/v1/chat/completions", json=pt_request("はい", context=[("いくぞ！", "走吧")])
    )
    await client.post(
        "/v1/chat/completions", json=pt_request("はい", context=[("たべる？", "要吃嗎？")])
    )
    assert len(cloud_chain.payloads) == 2
    assert main._cache_stats["repeat_misses"] == 1  # evidence for a looser key


async def test_cache_key_includes_the_selected_glossary(client, cache_on, cloud_chain, upstream, glossary_file, tmp_path, monkeypatch):
    game_dir = tmp_path / "games"
    game_dir.mkdir()
    (game_dir / "dq7.txt").write_text("ダイゴ->勇者大吾\n", encoding="utf-8")
    monkeypatch.setattr(main, "GLOSSARY_DIR", str(game_dir))
    monkeypatch.setattr(main, "_game_glossaries", {})
    cloud_chain.replies = [(200, "譯文")]

    await client.post("/v1/chat/completions", json=pt_request("ダイゴさんは　どこ？"))
    dq7 = pt_request("ダイゴさんは　どこ？")
    dq7["model"] = "dq7"
    await client.post("/v1/chat/completions", json=dq7)
    assert len(cloud_chain.payloads) == 2  # different matched terms, different key


async def test_empty_reply_is_not_cached(client, cache_on, upstream, glossary_file):
    upstream.content = ""
    await client.post("/v1/chat/completions", json=pt_request("……"))
    await client.post("/v1/chat/completions", json=pt_request("……"))
    assert upstream.calls == 2  # a safety-filtered blank must not be pinned


async def test_failures_are_not_cached(client, cache_on, cloud_chain, upstream, glossary_file):
    # A batch that mismatches everywhere returns 400 — and the next attempt
    # must reach the backends again rather than replay the failure.
    cloud_chain.replies = [(200, "只有一行")]
    upstream.content = "也只有一行"
    for _ in range(2):
        resp = await client.post(
            "/v1/chat/completions", json=pt_batch_request(["ダイゴさん", "こんにちは"])
        )
        assert resp.status_code == 400
    assert len(cloud_chain.payloads) == 4  # 2 models x 2 requests
    assert upstream.calls == 2


async def test_batch_with_blank_line_is_not_cached(client, cache_on, cloud_chain, upstream, glossary_file):
    # A safety-filtered blank line inside an otherwise valid batch must not
    # be pinned: the JSON wrapper string is never empty, so cacheability has
    # to be judged on the raw lines before wrapping.
    cloud_chain.replies = [(200, "大吾\n\n你好")]
    for _ in range(2):
        resp = await client.post(
            "/v1/chat/completions",
            json=pt_batch_request(["ダイゴさん", "……", "こんにちは"]),
        )
        assert resp.status_code == 200
    assert len(cloud_chain.payloads) == 2  # second batch reached the cloud again


async def test_local_entries_expire_fast(client, cache_on, upstream, glossary_file):
    # Local-quality answers stop serving once their short TTL lapses, so
    # cloud recovery shows through within minutes.
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert upstream.calls == 1
    key, (expires, body) = next(iter(main._translation_cache.items()))
    assert expires - time.monotonic() <= main.TRANSLATION_CACHE_TTL_LOCAL_S + 1
    main._translation_cache[key] = (time.monotonic() - 1, body)
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert upstream.calls == 2


async def test_cache_hit_still_feeds_continuation_join(client, cache_on, upstream, glossary_file):
    # A hit must keep the source in _recent_sources: the pinned contract is
    # that only lines translated moments ago may join the next box.
    await client.post("/v1/chat/completions", json=pt_request("ダイゴさんに　たのまれて"))
    await client.post("/v1/chat/completions", json=pt_request("ダイゴさんに　たのまれて"))
    assert upstream.calls == 1  # second one served from cache
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


@pytest.fixture
def two_key_chain(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(main, "GEMINI_API_KEYS", "free-key,paid-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["cloud-a", "cloud-b"])
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_cloud_leader", None)
    monkeypatch.setattr(main, "_leader_quota_day", None)
    return fake


def test_cloud_keys_parsing(monkeypatch):
    monkeypatch.setattr(main, "GEMINI_API_KEYS", "")
    monkeypatch.setattr(main, "GEMINI_API_KEY", "solo")
    assert main._cloud_keys() == ["solo"]
    # The chain wins over the single key; whitespace and empties are dropped.
    monkeypatch.setattr(main, "GEMINI_API_KEYS", " a , ,b ")
    assert main._cloud_keys() == ["a", "b"]
    # A pasted-twice key must not double every probe and strike.
    monkeypatch.setattr(main, "GEMINI_API_KEYS", "a,a,b")
    assert main._cloud_keys() == ["a", "b"]
    # A chain that parses to nothing falls back to the single key.
    monkeypatch.setattr(main, "GEMINI_API_KEYS", " , ")
    assert main._cloud_keys() == ["solo"]


async def test_key_chain_is_key_major(client, two_key_chain, upstream, glossary_file):
    # The first key's whole model list is tried before the second key sees
    # any traffic - a free key is fully spent before a billed key costs.
    two_key_chain.replies = [(429, ""), (429, ""), (200, "第二把 key 接手")]
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "第二把 key 接手"
    attempts = list(zip(two_key_chain.api_keys, [p["model"] for p in two_key_chain.payloads]))
    assert attempts == [
        ("free-key", "cloud-a"),
        ("free-key", "cloud-b"),
        ("paid-key", "cloud-a"),
    ]


async def test_429_exhaustion_slides_to_the_next_key(client, two_key_chain, upstream, glossary_file):
    # Both first-key models 429-out: the rescuing pair becomes leader and
    # later requests go straight to the second key with zero re-probes while
    # the cooldowns hold.
    two_key_chain.replies = [
        (429, ""),
        (429, ""),
        (200, "第二把 key 接手"),
    ]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))

    two_key_chain.payloads.clear()
    two_key_chain.api_keys.clear()
    two_key_chain.replies = [(200, "繼續")]
    await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert two_key_chain.api_keys == ["paid-key"]
    free_endpoint = main._cloud_endpoints()[0]
    assert (free_endpoint, "cloud-a") in main._model_cooldown_until
    assert (free_endpoint, "cloud-b") in main._model_cooldown_until


async def test_exhausted_key_skipped_even_when_leader_fails(client, two_key_chain, upstream, glossary_file):
    # While the first key's pairs are cooling down, a failure on the second
    # key must not send traffic back to the first - it falls through to local.
    two_key_chain.replies = [
        (429, ""),
        (429, ""),
        (200, "第二把 key 接手"),
    ]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))

    two_key_chain.payloads.clear()
    two_key_chain.api_keys.clear()
    two_key_chain.replies = [(500, "boom")]
    upstream.content = "本地接手"
    resp = await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "本地接手"
    assert set(two_key_chain.api_keys) == {"paid-key"}  # free-key never re-probed


async def test_transient_blip_does_not_pin_traffic_to_the_paid_key(client, two_key_chain, upstream, glossary_file):
    # Transient 500s on the free pairs let the paid key rescue - but the very
    # next request must try the free key again: the leader floats only within
    # its own key's segment, so one hiccup cannot convert the rest of the day
    # to billed traffic. (429s are different: they carry a cooldown.)
    two_key_chain.replies = [(500, "boom"), (500, "boom"), (200, "第二把 key 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))

    two_key_chain.payloads.clear()
    two_key_chain.api_keys.clear()
    two_key_chain.replies = [(200, "免費恢復")]
    resp = await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert resp.json()["choices"][0]["message"]["content"] == "免費恢復"
    assert two_key_chain.api_keys == ["free-key"]


async def test_midnight_straddling_reply_still_resets_leader(client, cloud_chain, upstream, glossary_file, monkeypatch):
    # A success whose reply lands after Pacific midnight must stamp the day
    # its attempt began: stamping the response day would swallow the next
    # rollover reset and the preferred model would not lead the fresh day.
    cloud_chain.replies = [(429, ""), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    day_n = main._leader_quota_day
    current = {"day": day_n}
    monkeypatch.setattr(main, "_quota_day", lambda: current["day"])

    real_post = cloud_chain.post

    async def straddling_post(payload, read_timeout=None, api_key="", base_url=""):
        resp = await real_post(payload, read_timeout, api_key=api_key, base_url=base_url)
        current["day"] = day_n + 1  # midnight passes while the reply is in flight
        return resp

    monkeypatch.setattr(main, "post_cloud", straddling_post)
    cloud_chain.payloads.clear()
    cloud_chain.replies = [(200, "b 跨午夜")]
    await client.post("/v1/chat/completions", json=pt_request("さようなら"))

    monkeypatch.setattr(main, "post_cloud", cloud_chain.post)
    cloud_chain.payloads.clear()
    cloud_chain.replies = [(200, "a 回鍋")]
    await client.post("/v1/chat/completions", json=pt_request("はい"))
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a"]


async def test_error_labels_are_positional_not_key_material(client, two_key_chain, upstream, glossary_file, monkeypatch):
    # Failure labels flow into the client-visible 502 body: positional labels
    # only, never the key material itself.
    two_key_chain.replies = [(500, "boom")]

    async def no_local(path, payload):
        raise httpx.ConnectError("no local")

    monkeypatch.setattr(main, "post_upstream", no_local)
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 502
    message = resp.json()["error"]["message"]
    assert "key1/cloud-a" in message
    assert "key2/cloud-b" in message
    assert "free-key" not in message
    assert "paid-key" not in message


async def test_cloud_clients_bake_one_client_per_endpoint():
    # The same key under two URLs (and two keys under one URL) must get
    # distinct clients, each with its own base URL and baked-in credential.
    try:
        a_k1 = main._get_cloud_client("https://a.example/v1", "k1")
        b_k1 = main._get_cloud_client("https://b.example/v1", "k1")
        a_k2 = main._get_cloud_client("https://a.example/v1", "k2")
        assert a_k1 is not b_k1
        assert a_k1 is not a_k2
        # httpx normalizes the base URL with a trailing slash.
        assert str(a_k1.base_url) == "https://a.example/v1/"
        assert str(b_k1.base_url) == "https://b.example/v1/"
        assert a_k1.headers["authorization"] == "Bearer k1"
        assert a_k2.headers["authorization"] == "Bearer k2"
        assert main._get_cloud_client("https://a.example/v1", "k1") is a_k1  # cached
    finally:
        for cloud_client in main._cloud_clients.values():
            await cloud_client.aclose()
        main._cloud_clients.clear()


async def test_startup_smoke_probes_each_pair_with_its_own_key(monkeypatch):
    # key2's probe must carry key2's credential: probing with key1 would seed
    # cooldown entries under the wrong pair.
    fake = FakeCloud()
    fake.replies = [(429, "quota exceeded"), (200, "ok")]
    monkeypatch.setattr(main, "GEMINI_API_KEYS", "k1,k2")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["m"])
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_get_client", lambda: _NoOllama())
    await main._startup_checks()
    assert fake.api_keys == ["k1", "k2"]
    assert list(main._model_cooldown_until) == [(main._cloud_endpoints()[0], "m")]


def test_cloud_endpoints_parsing(monkeypatch):
    monkeypatch.setattr(main, "CLOUD_URL", "https://gemini.example/v1")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["g1", "g2"])
    monkeypatch.setattr(main, "GEMINI_API_KEY", "solo")
    # Unset: endpoints come from the legacy key vars + the globals.
    assert main._cloud_endpoints() == [
        main.Endpoint("https://gemini.example/v1", "solo", ("g1", "g2"))
    ]
    # Strictly symmetric entries: whitespace around entries, fields, and
    # models is trimmed; blank entries are dropped; the one allowed empty
    # field is an explicit empty key (keyless self-hosted servers).
    monkeypatch.setattr(
        main,
        "CLOUD_ENDPOINTS",
        " https://api.groq.com/openai/v1 | gsk_1 | qwen/qwen3.6-27b ; llama-fast ,"
        " https://self.example/v1||local-model , ,",
    )
    assert main._cloud_endpoints() == [
        main.Endpoint(
            "https://api.groq.com/openai/v1", "gsk_1", ("qwen/qwen3.6-27b", "llama-fast")
        ),
        main.Endpoint("https://self.example/v1", "", ("local-model",)),
    ]
    # Identical (url, key) endpoints collapse to their first occurrence.
    monkeypatch.setattr(main, "CLOUD_ENDPOINTS", "u|k|m1,u|k|m2")
    assert main._cloud_endpoints() == [main.Endpoint("u", "k", ("m1",))]


def test_cloud_endpoints_malformed_entries_abort(monkeypatch):
    # Nothing inherits: a bare key, a missing URL, a missing model list, and
    # a two-field entry are config errors that must stop the proxy loudly,
    # naming the offending entry's position.
    for broken, position in [
        ("u|k|m,bare-key", 2),
        ("|k|m", 1),
        ("u|k|m,u2|k2|", 2),
        ("u|k", 1),
    ]:
        monkeypatch.setattr(main, "CLOUD_ENDPOINTS", broken)
        with pytest.raises(ValueError, match=f"entry {position}"):
            main._cloud_endpoints()


def test_key_vars_build_the_same_chain_as_before(monkeypatch):
    # Back-compat: a GEMINI_API_KEYS-only config must yield the exact
    # key-major attempt sequence of the key chain, every pair on CLOUD_URL.
    monkeypatch.setattr(main, "GEMINI_API_KEYS", "free-key,paid-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["cloud-a", "cloud-b"])
    monkeypatch.setattr(main, "_cloud_leader", None)
    monkeypatch.setattr(main, "_leader_quota_day", None)
    chain = main._cloud_chain()
    assert [(endpoint.key, model) for endpoint, model in chain] == [
        ("free-key", "cloud-a"),
        ("free-key", "cloud-b"),
        ("paid-key", "cloud-a"),
        ("paid-key", "cloud-b"),
    ]
    assert {endpoint.url for endpoint, _ in chain} == {main.CLOUD_URL}


@pytest.fixture
def two_endpoint_chain(monkeypatch):
    """Two providers with different URLs and model lists. Endpoint B reuses
    endpoint A's key string on purpose: state keyed by (key, model) instead
    of endpoint identity would collide and fail these tests."""
    fake = FakeCloud()
    monkeypatch.setattr(
        main,
        "CLOUD_ENDPOINTS",
        "https://a.example/v1|shared-key|a1;a2,https://b.example/v1|shared-key|b1",
    )
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_cloud_leader", None)
    monkeypatch.setattr(main, "_leader_quota_day", None)
    return fake


async def test_endpoint_chain_is_endpoint_major(client, two_endpoint_chain, upstream, glossary_file):
    # Endpoint A's whole model list is tried before endpoint B sees any
    # traffic, and every attempt carries its own endpoint's base URL.
    two_endpoint_chain.replies = [(500, "boom"), (500, "boom"), (200, "B 端點接手")]
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "B 端點接手"
    attempts = list(
        zip(two_endpoint_chain.base_urls, [p["model"] for p in two_endpoint_chain.payloads])
    )
    assert attempts == [
        ("https://a.example/v1", "a1"),
        ("https://a.example/v1", "a2"),
        ("https://b.example/v1", "b1"),
    ]
    assert two_endpoint_chain.api_keys == ["shared-key"] * 3

    # 500s carry no cooldown: the very next request retries endpoint A first —
    # the rescuing leader floats only within its own endpoint's segment.
    two_endpoint_chain.payloads.clear()
    two_endpoint_chain.base_urls.clear()
    two_endpoint_chain.replies = [(200, "A 恢復")]
    resp = await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert resp.json()["choices"][0]["message"]["content"] == "A 恢復"
    assert two_endpoint_chain.base_urls == ["https://a.example/v1"]


async def test_post_cloud_routes_to_the_endpoint_client(monkeypatch):
    # The real post_cloud must pick the client for the attempt's (url, key) -
    # not the first endpoint's, and an empty base_url keeps the global default.
    calls: list[tuple[str, str]] = []

    class _StubClient:
        async def post(self, path, json=None, timeout=None):
            return httpx.Response(200, text="ok")

    def record_get(base_url, api_key):
        calls.append((base_url, api_key))
        return _StubClient()

    monkeypatch.setattr(main, "_get_cloud_client", record_get)
    await main.post_cloud({}, api_key="k", base_url="https://b.example/v1")
    await main.post_cloud({}, api_key="k")
    assert calls == [("https://b.example/v1", "k"), (main.CLOUD_URL, "k")]


async def test_429_benches_the_model_on_one_endpoint_only(client, upstream, glossary_file, monkeypatch):
    # The same model name (and even the same key string) under two base URLs:
    # a 429 cooldown on endpoint A's copy must not bench endpoint B's.
    fake = FakeCloud()
    monkeypatch.setattr(
        main,
        "CLOUD_ENDPOINTS",
        "https://a.example/v1|shared-key|m,https://b.example/v1|shared-key|m",
    )
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_cloud_leader", None)
    monkeypatch.setattr(main, "_leader_quota_day", None)
    fake.replies = [(429, "quota exceeded"), (200, "B 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert fake.base_urls == ["https://a.example/v1", "https://b.example/v1"]
    endpoint_a, endpoint_b = main._cloud_endpoints()
    assert list(main._model_cooldown_until) == [(endpoint_a, "m")]
    assert main._cloud_available((endpoint_b, "m"))

    # Next request: A's copy is benched, B's copy still serves cloud traffic.
    fake.payloads.clear()
    fake.base_urls.clear()
    fake.replies = [(200, "B 繼續")]
    resp = await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert resp.json()["choices"][0]["message"]["content"] == "B 繼續"
    assert fake.base_urls == ["https://b.example/v1"]


async def test_endpoint_error_labels_hide_keys_and_urls(client, two_endpoint_chain, upstream, glossary_file, monkeypatch):
    # Failure labels flow into the client-visible 502 body: positional labels
    # only - never the key material, never the base URL (Cloudflare-style
    # URLs embed an account id).
    two_endpoint_chain.replies = [(500, "boom")]

    async def no_local(path, payload):
        raise httpx.ConnectError("no local")

    monkeypatch.setattr(main, "post_upstream", no_local)
    resp = await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert resp.status_code == 502
    message = resp.json()["error"]["message"]
    assert "key1/a1" in message
    assert "key1/a2" in message
    assert "key2/b1" in message
    assert "shared-key" not in message
    assert "a.example" not in message
    assert "b.example" not in message


async def test_startup_smoke_probes_each_endpoint_with_its_own_url(monkeypatch):
    # Every (endpoint, model) pair is probed with that endpoint's key and URL
    # and that endpoint's OWN model list, not the global CLOUD_MODELS.
    fake = FakeCloud()
    monkeypatch.setattr(
        main,
        "CLOUD_ENDPOINTS",
        "https://a.example/v1|ka|a1;a2,https://b.example/v1|kb|b1",
    )
    monkeypatch.setattr(main, "CLOUD_MODELS", ["unused-global"])
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_get_client", lambda: _NoOllama())
    await main._startup_checks()
    probes = list(zip(fake.base_urls, fake.api_keys, [p["model"] for p in fake.payloads]))
    assert probes == [
        ("https://a.example/v1", "ka", "a1"),
        ("https://a.example/v1", "ka", "a2"),
        ("https://b.example/v1", "kb", "b1"),
    ]


def test_strip_reasoning_drops_leaked_blocks():
    # Closed blocks, the <thought> variant, and an unclosed block running to
    # the end of the text are all reasoning leaks, not translation.
    assert cloud.strip_reasoning("<think>plan...</think>大吾先生") == "大吾先生"
    assert cloud.strip_reasoning("<thought>* Input: ...") == ""
    assert cloud.strip_reasoning("大吾先生在哪裡？") == "大吾先生在哪裡？"


def test_request_tweaks_are_keyed_by_host_and_model():
    assert cloud.request_tweaks("https://api.groq.com/openai/v1", "qwen/qwen3.6-27b") == {
        "reasoning_effort": "none"
    }
    assert cloud.request_tweaks("https://api.z.ai/api/paas/v4", "glm-4.7-flash") == {
        "thinking": {"type": "disabled"}
    }
    # Model-scoped row: only gemini-3.6-flash gets the Gemini-side tweak -
    # 3.5-flash-lite 400s on the same field, so the host alone must not match.
    gemini = "https://generativelanguage.googleapis.com/v1beta/openai"
    assert cloud.request_tweaks(gemini, "gemini-3.6-flash") == {"reasoning_effort": "minimal"}
    assert cloud.request_tweaks(gemini, "gemini-3.5-flash-lite") == {}
    assert cloud.request_tweaks(gemini, "gemini-3.1-flash-lite") == {}
    assert cloud.request_tweaks("https://api.cloudflare.com/client/v4/accounts/x/ai/v1", "m") == {}


async def test_cloud_request_carries_the_hosts_own_tweaks(client, upstream, glossary_file, monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(
        main,
        "CLOUD_ENDPOINTS",
        "https://api.groq.com/openai/v1|gk|qwen,https://x.example/v1|xk|m",
    )
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_cloud_leader", None)
    monkeypatch.setattr(main, "_leader_quota_day", None)
    fake.replies = [(500, "boom"), (200, "譯文")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    groq_body, other_body = fake.payloads
    assert groq_body["reasoning_effort"] == "none"
    assert "reasoning_effort" not in other_body
    assert "thinking" not in other_body


async def test_leaked_reasoning_is_stripped_and_pure_reasoning_slides(client, cloud_chain, upstream, glossary_file):
    # cloud-a leaks an unclosed reasoning block (no translation): the chain
    # must slide on. cloud-b wraps its answer in a closed block: the proxy
    # serves the stripped translation.
    cloud_chain.replies = [
        (200, "<think>\nHere's a thinking process...\n"),
        (200, "<think>plan</think>大吾先生在哪裡？"),
    ]
    resp = await client.post("/v1/chat/completions", json=pt_request("ダイゴさんは　どこ？"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "大吾先生在哪裡？"
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a", "cloud-b"]


def test_join_drops_the_scrolled_overlap():
    # A box scrolled by one line: the surviving line reappears at the head of
    # the current capture and must be translated exactly once.
    joined, history = sakura.join_continuation(
        [("ダイゴさんに　たのまれて", "被拜託")],
        "たのまれて\nきみを　むかえに　きたんだ！",
        {"ダイゴさんに　たのまれて"},
    )
    assert joined == "ダイゴさんに　たのまれて\nきみを　むかえに　きたんだ！"
    assert history == []


def test_join_keeps_a_coincidental_short_seam():
    # Prefix ends on て and the new text genuinely starts with て: a
    # one-character seam without a line boundary must not eat real text.
    joined, _ = sakura.join_continuation(
        [("ダイゴさんに　たのまれて", "被拜託")],
        "てがみを　わたした",
        {"ダイゴさんに　たのまれて"},
    )
    assert joined == "ダイゴさんに　たのまれててがみを　わたした"


def test_join_accepts_a_short_overlap_at_a_line_boundary():
    # A re-captured short line ends where its newline was - accepted even
    # below the minimum seam length.
    joined, _ = sakura.join_continuation(
        [("やまへ　いくと", "去山上的話")],
        "いくと\nたのしいぞ",
        {"やまへ　いくと"},
    )
    assert joined == "やまへ　いくと\nたのしいぞ"


async def test_scrolled_line_is_not_translated_twice(client, upstream, glossary_file):
    # End-to-end: translate the first box, then a one-line-scrolled capture
    # whose head repeats the first box's tail - the model input must contain
    # the repeated text once.
    await client.post("/v1/chat/completions", json=pt_request("ダイゴさんに　たのまれて"))
    resp = await client.post(
        "/v1/chat/completions",
        json=pt_request(
            "たのまれて\nきみを　むかえに　きたんだ！",
            context=[("ダイゴさんに　たのまれて", "被大吾先生拜託")],
        ),
    )
    assert resp.status_code == 200
    prompt = upstream.payload["messages"][1]["content"]
    assert prompt.endswith(
        "将下面的文本从日文翻译成简体中文：\nダイゴさんに　たのまれて\\nきみを　むかえに　きたんだ！"
    )
    assert prompt.count("たのまれて") == 1  # the whole point: fed to the model once


def test_untranslated_echo_detection():
    assert cloud.is_untranslated_echo("ダイゴさんは　どこ？", "ダイゴさんは　どこ？")
    # U+3000 differences do not disguise an echo.
    assert cloud.is_untranslated_echo("ダイゴさんは　どこ？", "ダイゴさんはどこ？")
    # Symbol-only lines legitimately translate to themselves.
    assert not cloud.is_untranslated_echo("……", "……")
    assert not cloud.is_untranslated_echo("ダイゴさんは　どこ？", "大吾先生在哪裡？")


async def test_cloud_echo_slides_and_fullwidth_spaces_are_stripped(client, cloud_chain, upstream, glossary_file):
    # cloud-a hands the Japanese back untranslated -> slide on; cloud-b keeps
    # the source's U+3000 word gaps -> stripped before serving.
    cloud_chain.replies = [
        (200, "ダイゴさんは　どこ？"),
        (200, "大吾先生　在　哪裡？"),
    ]
    resp = await client.post("/v1/chat/completions", json=pt_request("ダイゴさんは　どこ？"))
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "大吾先生在哪裡？"
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a", "cloud-b"]


async def test_startup_declares_readiness_when_the_cloud_answers(monkeypatch):
    # Readiness is what /status leads with: it must wait out the boot race
    # (port up, Wi-Fi not yet) before saying it is safe to start the game.
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["m"])
    monkeypatch.setattr(main, "STARTUP_SMOKE", False)
    monkeypatch.setattr(main, "_get_client", lambda: _NoOllama())
    monkeypatch.setattr(main, "CLOUD_READY_PROBE_INTERVAL_S", 0.0)
    seen: list[str] = []
    answers = iter([False, False, True])

    async def reachable(_endpoint):
        seen.append(main._status["ready"])
        return next(answers)

    monkeypatch.setattr(main, "_cloud_reachable", reachable)
    await main._startup_checks()
    assert seen[0] == "Starting…"
    assert any("Waiting for network" in s for s in seen)
    assert "Ready" in main._status["ready"]





async def test_readiness_is_reported_even_when_smoke_tests_are_off(monkeypatch):
    # The Thor launcher runs STARTUP_SMOKE=0 to save quota; the readiness
    # banner must not be collateral damage of that choice.
    fake = FakeCloud()
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["m"])
    monkeypatch.setattr(main, "STARTUP_SMOKE", False)
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_get_client", lambda: _NoOllama())
    await main._startup_checks()
    assert fake.payloads == []  # smoke tests really were skipped
    assert "Ready" in main._status["ready"]


async def test_status_page_shows_the_live_backend(client, cloud_chain, upstream, glossary_file):
    # The page is the at-a-glance answer to "which model is translating?"
    cloud_chain.replies = [(200, "譯文")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    resp = await client.get("/status")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "cloud-a" in body          # the model that served
    assert "lines served" in body
    assert main.OLLAMA_MODEL in body  # local fallback row
    # Never leak the game script or key material onto an unauthenticated page.
    assert "こんにちは" not in body
    assert "譯文" not in body
    assert "test-key" not in body


async def test_status_marks_cooling_backends(client, cloud_chain, upstream, glossary_file):
    cloud_chain.replies = [(429, ""), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    body = (await client.get("/status")).text
    assert "cooling" in body   # cloud-a took a 429 cooldown
    assert "leader" in body    # cloud-b rescued and leads








async def test_readiness_requires_a_real_translation(monkeypatch, real_probe):
    # A green banner must mean "the next line comes back translated", so the
    # probe runs the real chain: an endpoint that answers 429 is NOT ready,
    # and readiness only lands once a model actually translates.
    fake = FakeCloud()
    fake.replies = [(429, ""), (200, "好")]
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["m1", "m2"])
    monkeypatch.setattr(main, "STARTUP_SMOKE", False)
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_get_client", lambda: _NoOllama())
    monkeypatch.setattr(main, "CLOUD_READY_PROBE_INTERVAL_S", 0.0)
    await main._startup_checks()
    assert "Ready" in main._status["ready"]
    # m1's 429 was recorded as a cooldown rather than treated as readiness.
    assert len(fake.payloads) == 2


async def test_readiness_probe_rejects_empty_output(monkeypatch, real_probe):
    # A 200 that carries only reasoning is not a translation.
    fake = FakeCloud()
    fake.replies = [(200, "<think>hmm</think>"), (200, "好")]
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["m1", "m2"])
    monkeypatch.setattr(main, "post_cloud", fake.post)
    endpoint = main._cloud_endpoints()[0]
    assert await main._cloud_reachable(endpoint) is True
    assert len(fake.payloads) == 2  # m1's reasoning-only reply was rejected


async def test_status_page_never_polls_on_its_own(client, upstream, glossary_file):
    # The page must be inert: no meta-refresh and no script, so an open tab
    # costs nothing behind the game. It updates when the user reloads it.
    body = (await client.get("/status")).text
    assert "http-equiv=\"refresh\"" not in body
    assert "<script" not in body
    assert "setInterval" not in body
