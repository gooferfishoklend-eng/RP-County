"""LLM backends: Claude API (Anthropic SDK) or OpenRouter (OpenAI-compatible chat completions)."""

import json
import logging
import re

import aiohttp
import anthropic

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MAX_TOKENS = 32000


class AIError(Exception):
    pass


class AnthropicBackend:
    def __init__(self, model: str, api_key: str | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self.model = model

    async def complete(self, system: str, user: str, *, effort: str, max_tokens: int, schema: dict | None) -> str:
        output_config: dict = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        try:
            async with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                output_config=output_config,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                response = await stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise AIError("ИИ-ведущий перегружен, попробуйте через минуту.") from e
        except anthropic.AuthenticationError as e:
            raise AIError("Неверный ключ Claude API (ANTHROPIC_API_KEY).") from e
        except anthropic.APIStatusError as e:
            log.exception("Claude API error %s", e.status_code)
            raise AIError(f"Ошибка ИИ ({e.status_code}). Попробуйте позже.") from e
        except anthropic.APIConnectionError as e:
            raise AIError("Нет связи с ИИ. Попробуйте позже.") from e

        if response.stop_reason == "refusal":
            raise AIError("ИИ отказался обрабатывать этот запрос. Переформулируйте действие.")
        if response.stop_reason == "max_tokens":
            raise AIError("Ответ ИИ оказался слишком длинным. Попробуйте ещё раз.")
        blocks = list(response.content)
        last_fallback = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
        return "".join(b.text for b in blocks[last_fallback + 1 :] if b.type == "text")


class OpenRouterBackend:
    def __init__(self, model: str, api_key: str):
        self.model = model
        self.api_key = api_key

    async def _post(self, payload: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "RP-County geopolitics bot",
        }
        timeout = aiohttp.ClientTimeout(total=900)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(OPENROUTER_URL, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    status = resp.status
        except (aiohttp.ClientError, TimeoutError) as e:
            raise AIError("Нет связи с OpenRouter. Попробуйте позже.") from e
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        if status == 401:
            raise AIError("Неверный ключ OpenRouter (OPENROUTER_API_KEY).")
        if status == 402:
            raise AIError("На счёте OpenRouter закончились кредиты.")
        if status == 429:
            raise AIError("OpenRouter перегружен или превышен лимит, попробуйте через минуту.")
        if status >= 400 or "error" in data:
            message = (data.get("error") or {}).get("message", body[:300]) if isinstance(data, dict) else body[:300]
            raise _BadRequest(status, message)
        return data

    async def complete(self, system: str, user: str, *, effort: str, max_tokens: int, schema: dict | None) -> str:
        payload = {
            "model": self.model,
            "max_tokens": min(max_tokens, OPENROUTER_MAX_TOKENS),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "reasoning": {"effort": effort if effort in ("low", "medium", "high") else "medium"},
        }
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        try:
            data = await self._post(payload)
        except _BadRequest as e:
            if not schema:
                log.error("OpenRouter error %s: %s", e.status, e.message)
                raise AIError(f"Ошибка OpenRouter ({e.status}): {e.message[:200]}") from e
            log.warning("OpenRouter rejected json_schema (%s), retrying with prompt-only JSON", e.message)
            payload.pop("response_format")
            payload["messages"][1]["content"] = (
                user + "\n\nОтветь ТОЛЬКО валидным JSON-объектом (без markdown и пояснений) по этой JSON-схеме:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
            try:
                data = await self._post(payload)
            except _BadRequest as e2:
                raise AIError(f"Ошибка OpenRouter ({e2.status}): {e2.message[:200]}") from e2

        choices = data.get("choices") or []
        if not choices:
            raise AIError("OpenRouter вернул пустой ответ.")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise AIError("Ответ ИИ оказался слишком длинным. Попробуйте ещё раз.")
        content = (choice.get("message") or {}).get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content


class _BadRequest(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def extract_json(text: str) -> dict:
    """Parse a JSON object, tolerating code fences or prose around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def make_backend(provider: str, model: str, anthropic_key: str | None, openrouter_key: str | None):
    if provider == "openrouter":
        if not openrouter_key:
            raise RuntimeError("AI_PROVIDER=openrouter, но не задан OPENROUTER_API_KEY")
        return OpenRouterBackend(model, openrouter_key)
    return AnthropicBackend(model, anthropic_key)
