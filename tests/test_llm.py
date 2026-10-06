import json

import pytest
from aiohttp import web

from bot import llm
from bot.ai import GameMaster
from bot.llm import AIError, OpenRouterBackend, extract_json, make_backend

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Вот ответ: {"a": 3} — готово') == {"a": 3}
    with pytest.raises(json.JSONDecodeError):
        extract_json("нет json")


def test_make_backend_requires_key():
    with pytest.raises(RuntimeError):
        make_backend("openrouter", "x/y", None, None)
    assert isinstance(make_backend("openrouter", "x/y", None, "k"), OpenRouterBackend)


@pytest.fixture
async def fake_openrouter(aiohttp_unused_port=None):
    requests = []
    mode = {"value": "ok"}

    async def handler(request):
        body = await request.json()
        requests.append({"headers": dict(request.headers), "body": body})
        if mode["value"] == "no_schema" and "response_format" in body:
            return web.json_response({"error": {"message": "response_format not supported"}}, status=400)
        if mode["value"] == "credits":
            return web.json_response({"error": {"message": "Insufficient credits"}}, status=402)
        content = '```json\n{"ok": true}\n```' if "response_format" not in body else '{"ok": true}'
        return web.json_response({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    app = web.Application()
    app.router.add_post("/api/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    original = llm.OPENROUTER_URL
    llm.OPENROUTER_URL = f"http://127.0.0.1:{port}/api/v1/chat/completions"
    yield requests, mode
    llm.OPENROUTER_URL = original
    await runner.cleanup()


async def test_openrouter_structured_output(fake_openrouter):
    requests, _ = fake_openrouter
    gm = GameMaster(OpenRouterBackend("anthropic/some-model", "sk-or-test"))
    assert await gm._call_json("sys", "user", SCHEMA, effort="low", max_tokens=100_000) == {"ok": True}
    req = requests[0]
    assert req["headers"]["Authorization"] == "Bearer sk-or-test"
    body = req["body"]
    assert body["model"] == "anthropic/some-model"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["max_tokens"] == llm.OPENROUTER_MAX_TOKENS
    assert body["reasoning"] == {"effort": "low"}


async def test_openrouter_falls_back_without_schema(fake_openrouter):
    requests, mode = fake_openrouter
    mode["value"] = "no_schema"
    gm = GameMaster(OpenRouterBackend("m", "k"))
    assert await gm._call_json("sys", "user", SCHEMA, effort="medium", max_tokens=100) == {"ok": True}
    assert len(requests) == 2 and "response_format" not in requests[1]["body"]
    assert "JSON-схеме" in requests[1]["body"]["messages"][1]["content"]


async def test_openrouter_out_of_credits(fake_openrouter):
    _, mode = fake_openrouter
    mode["value"] = "credits"
    gm = GameMaster(OpenRouterBackend("m", "k"))
    with pytest.raises(AIError, match="кредиты"):
        await gm._call("sys", "user", effort="low", max_tokens=100)
