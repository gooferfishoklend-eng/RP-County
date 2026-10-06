import json
import logging

import anthropic

from bot.stats import STAT_KEYS

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

GM_SYSTEM = """Ты — ведущий (гейм-мастер) текстовой ролевой геополитической игры в Telegram-группе.
Игроки управляют реальными современными странами. Мир — наш реальный мир на момент начала игры, дальше он развивается по решениям игроков.

Твоя задача — честно, реалистично и интересно просчитывать последствия решений.

Правила симуляции:
- Один ход = один квартал. Изменения должны быть правдоподобными для такого срока: крупные реформы дают эффект постепенно, часто сначала падает рейтинг, а польза приходит позже.
- Показатели 0–100: stability, approval, military, tech, corruption (меньше — лучше), influence. Изменение за ход обычно от -10 до +10, крайние случаи до ±25.
- gdp и budget — в млрд $, population — в млн. Реформы, армия, соцпрограммы стоят денег из казны (budget). Казна может уйти в минус — это госдолг, он давит на стабильность.
- Нереалистичные, «читерские» или противоречивые приказы (например «сделать ВВП в 10 раз больше») проваливаются или дают обратный эффект. Игрок — глава государства, но не всемогущ: есть элиты, бизнес, армия, общество, соседи.
- Тайные действия (kind = "secret") НЕ должны упоминаться в публичных текстах, если только их не раскрыли. Шанс раскрытия зависит от масштаба операции, коррупции и технологий страны и её противников. Если раскрыли — поставь secret_exposed = true и опиши скандал в public_summary и world_news.
- Войны: исход зависит от армии, технологий, экономики, союзов, географии. Войны дорогие и бьют по населению, стабильности и экономике обеих сторон.
- Действия разных игроков взаимодействуют: санкции бьют по экономике цели, договоры улучшают отношения, гонка вооружений тревожит соседей.
- Страны, которыми не управляют игроки (НИП), живут своей жизнью и реагируют на происходящее — используй их в новостях.
- Если у страны стабильность падает очень низко — протесты, забастовки, угроза переворота.
- Каждый ход добавляй одно мировое событие (кризис, открытие, катастрофа, скачок цен на нефть, выборы в крупной стране и т.п.), которое влияет на игроков.

Стиль: живо, как сводка мировых СМИ, с конкретикой (цифры, имена ведомств, реакция рынков), без воды. Пиши по-русски.
Не выдумывай действия за игроков. Если игрок ничего не сделал — страна дрейфует по инерции.
"""

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "Заголовок хода, одна строка"},
        "world_news": {"type": "string", "description": "Публичная сводка мировых новостей за квартал, 3-8 абзацев"},
        "world_event": {"type": "string", "description": "Случайное мировое событие этого хода, 1-2 предложения"},
        "countries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "country_id": {"type": "integer"},
                    "deltas": {
                        "type": "object",
                        "properties": {k: {"type": "number"} for k in STAT_KEYS},
                        "required": STAT_KEYS,
                        "additionalProperties": False,
                    },
                    "public_summary": {"type": "string", "description": "Что видит весь мир, 1-3 предложения"},
                    "private_report": {
                        "type": "string",
                        "description": "Секретный доклад для лидера страны: результаты всех его действий, включая тайные, и советы",
                    },
                    "secret_exposed": {"type": "boolean"},
                },
                "required": ["country_id", "deltas", "public_summary", "private_report", "secret_exposed"],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "a_id": {"type": "integer"},
                    "b_id": {"type": "integer"},
                    "delta": {"type": "integer"},
                    "status": {"type": "string", "enum": ["peace", "alliance", "tension", "war"]},
                    "reason": {"type": "string"},
                },
                "required": ["a_id", "b_id", "delta", "status", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "world_news", "world_event", "countries", "relations"],
    "additionalProperties": False,
}

COUNTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean", "description": "true, если это реально существующая современная страна"},
        "name": {"type": "string", "description": "Каноническое русское название страны"},
        "flag": {"type": "string", "description": "Эмодзи флага"},
        "government": {"type": "string", "description": "Форма правления"},
        "leader_title": {"type": "string", "description": "Титул главы государства, например «Президент»"},
        "description": {"type": "string", "description": "2-3 предложения о стартовом положении страны"},
        **{k: {"type": "number"} for k in STAT_KEYS},
    },
    "required": ["valid", "name", "flag", "government", "leader_title", "description", *STAT_KEYS],
    "additionalProperties": False,
}

NPC_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "reply": {"type": "string", "description": "Официальный ответ МИД страны-НИП, 1-3 предложения"},
    },
    "required": ["accepted", "reply"],
    "additionalProperties": False,
}


class AIError(Exception):
    pass


class GameMaster:
    def __init__(self, model: str, api_key: str | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self.model = model

    async def _call(self, system: str, user: str, *, effort: str, max_tokens: int, schema: dict | None = None) -> str:
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
        text = "".join(b.text for b in blocks[last_fallback + 1 :] if b.type == "text")
        if not text:
            raise AIError("ИИ вернул пустой ответ.")
        return text

    async def _call_json(self, system: str, user: str, schema: dict, *, effort: str, max_tokens: int) -> dict:
        text = await self._call(system, user, effort=effort, max_tokens=max_tokens, schema=schema)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise AIError("ИИ вернул некорректные данные. Попробуйте ещё раз.") from e

    async def create_country(self, name: str, start_year: int) -> dict:
        prompt = (
            f"Игрок хочет взять под управление страну: «{name}». Год начала игры: {start_year}.\n"
            "Если это не реальная современная страна (или явная шутка) — valid = false.\n"
            "Иначе дай реалистичные стартовые показатели на основе реальных данных: gdp (млрд $), "
            "budget (свободные средства в казне, млрд $, обычно 1–10% ВВП; может быть отрицательным), "
            "population (млн), stability, approval, military, tech, corruption, influence (0–100). "
            "Сверхдержавы — около 80–95 по армии/влиянию, малые страны — 5–30."
        )
        return await self._call_json(GM_SYSTEM, prompt, COUNTRY_SCHEMA, effort="low", max_tokens=4000)

    async def resolve_turn(self, world: dict) -> dict:
        prompt = (
            "Просчитай итоги хода. Вот текущее состояние мира и все действия игроков за квартал (JSON):\n\n"
            + json.dumps(world, ensure_ascii=False, indent=1)
            + "\n\nВерни запись в countries для КАЖДОЙ страны игроков (по её id). "
            "В relations укажи только пары стран игроков, отношения которых изменились."
        )
        return await self._call_json(GM_SYSTEM, prompt, TURN_SCHEMA, effort="medium", max_tokens=32000)

    async def advise(self, country: dict, world: dict, question: str) -> str:
        prompt = (
            f"Ты — главный советник руководителя страны {country['name']}. Ответь лично ему, честно и по делу, "
            "как опытный политтехнолог и экономист. Предложи 2–4 конкретных шага с рисками. Коротко, до 1200 символов, "
            "без markdown-заголовков.\n\n"
            f"Состояние мира (JSON):\n{json.dumps(world, ensure_ascii=False)}\n\nВопрос лидера: {question}"
        )
        return await self._call(GM_SYSTEM, prompt, effort="low", max_tokens=4000)

    async def npc_diplomacy(self, from_country: dict, target: str, proposal: str, world: dict) -> dict:
        prompt = (
            f"Страна игрока {from_country['name']} направила официальное предложение стране «{target}», "
            "которой управляет ИИ (НИП). Реши, согласится ли правительство этой страны, исходя из её реальных интересов, "
            "союзов и текущей ситуации, и напиши ответ её МИД.\n\n"
            f"Состояние мира (JSON):\n{json.dumps(world, ensure_ascii=False)}\n\nПредложение: {proposal}"
        )
        return await self._call_json(GM_SYSTEM, prompt, NPC_SCHEMA, effort="low", max_tokens=3000)
