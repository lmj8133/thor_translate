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
        self.replies: list[tuple[int, str]] = [(200, "雲端譯文")]

    async def post(self, payload: dict, read_timeout: float | None = None) -> httpx.Response:
        self.payloads.append(payload)
        self.timeouts.append(read_timeout)
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
    # Module-global state must not bleed between tests: the response cache is
    # off by default (cache tests opt in via `cache_on`) and the negative
    # memory / recent-source dicts start empty.
    monkeypatch.setattr(main, "TRANSLATION_CACHE_SIZE", 0)
    monkeypatch.setattr(main, "_translation_cache", main.OrderedDict())
    monkeypatch.setattr(main, "_cache_stats", {"requests": 0, "hits": 0, "repeat_misses": 0})
    monkeypatch.setattr(main, "_model_exhausted_day", {})
    monkeypatch.setattr(main, "_unrecognized_429_day", {})
    monkeypatch.setattr(main, "_model_strikes", {})
    monkeypatch.setattr(main, "_model_strike_at", {})
    monkeypatch.setattr(main, "_model_cooldown_until", {})
    monkeypatch.setattr(main, "_recent_sources", {})


@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setattr(main, "TRANSLATION_CACHE_SIZE", 8)


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
    # cloud-a fails once: cloud-b rescues the request and becomes the leader.
    cloud_chain.replies = [(429, ""), (200, "b 接手")]
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
    cloud_chain.replies = [(429, ""), (200, "a 回鍋")]
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
PERDAY_429_BODY = json.dumps(
    [
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                        ],
                    }
                ],
            }
        }
    ]
)


class _NoOllama:
    """Stands in for the Ollama client so startup checks stay socket-free."""

    async def post(self, path, json=None):
        raise httpx.ConnectError("no local backend in tests")


async def test_perday_429_benches_the_model_for_the_day(client, cloud_chain, upstream, glossary_file):
    # cloud-a returns a PerDay quota 429: it must not be probed again today,
    # even after the rescuer later fails (a plain 429 would be re-probed —
    # that fail-open behavior is pinned by test_failover_is_sticky...).
    cloud_chain.replies = [(429, PERDAY_429_BODY), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-a", "cloud-b"]

    cloud_chain.payloads.clear()
    cloud_chain.replies = [(500, "boom")]
    upstream.content = "本地接手"
    resp = await client.post("/v1/chat/completions", json=pt_request("さようなら"))
    assert resp.status_code == 200
    assert [p["model"] for p in cloud_chain.payloads] == ["cloud-b"]


async def test_plain_429_does_not_mark_the_model(client, cloud_chain, upstream, glossary_file):
    # Fail-open: a 429 without PerDay evidence changes nothing.
    cloud_chain.replies = [(429, ""), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert main._model_exhausted_day == {}


async def test_perminute_mention_does_not_mark_the_model(client, cloud_chain, upstream, glossary_file):
    # A burst 429 can cite the minute quota while mentioning the daily limit;
    # benching for hours on that evidence would be wrong.
    body = '{"violations": [{"quotaId": "...PerMinute..."}, {"quotaId": "...PerDay..."}]}'
    cloud_chain.replies = [(429, body), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert main._model_exhausted_day == {}


async def test_perday_mark_expires_after_the_refill(client, cloud_chain, upstream, glossary_file, monkeypatch):
    cloud_chain.replies = [(429, PERDAY_429_BODY), (200, "b 接手")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    marked_day = main._model_exhausted_day["cloud-a"]

    monkeypatch.setattr(main, "_quota_day", lambda: marked_day + 1)
    assert main._cloud_available("cloud-a", main._quota_day())
    assert "cloud-a" not in main._model_exhausted_day  # stale mark pruned


def test_transport_strikes_bench_after_limit():
    for _ in range(main.CLOUD_STRIKE_LIMIT):
        main._note_cloud_transport_error("m", started=time.monotonic())
    assert not main._cloud_available("m", 0)
    # The bench expires: pretend the cooldown deadline has passed.
    main._model_cooldown_until["m"] = time.monotonic() - 1
    assert main._cloud_available("m", 0)
    assert "m" not in main._model_cooldown_until


def test_overlapped_attempts_count_one_strike():
    # Two requests in flight during one blip: the second failure STARTED
    # before the first strike was recorded, so it must not double-count.
    started = time.monotonic() - 1.0
    main._note_cloud_transport_error("m", started)
    main._note_cloud_transport_error("m", started)
    assert main._model_strikes["m"] == 1


async def test_all_stalling_clouds_get_benched(client, cloud_chain, upstream, glossary_file, monkeypatch):
    # When EVERY cloud model keeps timing out, sticky failover alone never
    # demotes them (demotion needs a cloud success); the strike cooldown must
    # stop the per-request stall tax.
    calls: list[str] = []

    async def stall(payload, read_timeout=None):
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
    main._model_strikes["cloud-a"] = 2
    main._model_strike_at["cloud-a"] = time.monotonic()
    cloud_chain.replies = [(200, "好")]
    await client.post("/v1/chat/completions", json=pt_request("こんにちは"))
    assert "cloud-a" not in main._model_strikes


async def test_startup_smoke_seeds_the_skip_table(monkeypatch):
    fake = FakeCloud()
    fake.replies = [(429, PERDAY_429_BODY)]
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "CLOUD_MODELS", ["cloud-a"])
    monkeypatch.setattr(main, "post_cloud", fake.post)
    monkeypatch.setattr(main, "_get_client", lambda: _NoOllama())
    await main._startup_checks()
    assert main._model_exhausted_day == {"cloud-a": main._quota_day()}


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
