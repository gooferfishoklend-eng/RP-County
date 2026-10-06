import json
import logging

import anthropic

from bot.stats import STAT_KEYS

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

GM_SYSTEM = """Ты — ведущий (гейм-мастер) текстовой ролевой геополитической игры в Telegram-группе.
Игроки управляют реальными современными странами. Остальными странами (НИП) управляешь ты. Мир — наш реальный мир на момент начала игры, дальше он развивается по решениям игроков.

Твоя задача — честно, реалистично и интересно просчитывать последствия решений.

Правила симуляции:
- Один ход = один квартал. Изменения должны быть правдоподобными для такого срока: крупные реформы дают эффект постепенно, часто сначала падает рейтинг, а польза приходит позже.
- Показатели 0–100: stability, approval, military, tech, corruption (меньше — лучше), influence. Изменение за ход обычно от -10 до +10, крайние случаи до ±25.
- gdp и budget — в млрд $, population — в млн. Реформы, армия, соцпрограммы стоят денег из казны (budget). Казна может уйти в минус — это госдолг, он давит на стабильность.
- Нереалистичные, «читерские» или противоречивые приказы проваливаются или дают обратный эффект. Игрок — глава государства, но не всемогущ: есть элиты, бизнес, армия, общество, соседи.
- Тайные действия (kind = "secret") НЕ упоминаются в публичных текстах, если их не раскрыли. Шанс раскрытия зависит от масштаба операции, коррупции и технологий страны и её противников. Если раскрыли — secret_exposed = true и скандал в public_summary и world_news.
- Карта мира разбита на провинции. Территориальные изменения и военные потери от боёв уже посчитаны движком игры (war_results) — не дублируй их, а добавляй экономические и социальные последствия войны, реакцию мира, беженцев, санкции.
- Действия разных игроков взаимодействуют: санкции бьют по экономике цели, договоры улучшают отношения, помощь (aid) при бедствиях повышает влияние помогающего и смягчает удар.
- Страны-НИП живут своей жизнью и реагируют на происходящее.
- При очень низкой стабильности — протесты, забастовки, угроза переворота.
- Мировые события (эпидемии, катастрофы, кризисы) длятся несколько ходов: распространяются на соседей, усиливаются или затухают. Карантин, медицина, технологии и международная помощь помогают с ними справиться.

Стиль: живо, как сводка мировых СМИ, с конкретикой (цифры, ведомства, реакция рынков), без воды. Пиши по-русски.
Не выдумывай действия за игроков. Если игрок ничего не сделал — страна дрейфует по инерции.
"""

GENERALS_SYSTEM = """Ты — штаб генералов в геополитической игре. Ты планируешь военные операции за каждую воюющую сторону — и за игроков, и за страны-НИП — честно и умно, как опытные военачальники, не подыгрывая никому.
Карта разбита на шестиугольные провинции (cell_id). Для каждой стороны тебе даны: силы сторон, генералы с их характером, приказ лидера страны, провинции противника, которые можно атаковать (targets), и свои прифронтовые провинции (own_front).

Принципы:
- Выполняй приказ лидера (directive), но разумно: если он самоубийственный — смягчи и объясни в докладе.
- Концентрация сил: лучше 1–2 мощных удара, чем распыление. Сила удара (force) и обороны в сумме не больше 100 (остаток — общий резерв, он равномерно держит фронт).
- Слабая сторона чаще обороняется и контратакует там, где противник слаб, или освобождает свою землю (liberates_our_land).
- Сильная сторона наступает на столицу (hops_to_enemy_capital, is_enemy_capital) и крупные города, стремится окружать: маленькие отрезанные анклавы сдаются сами.
- via: land — по суше, sea — через пролив (тяжелее), expedition — десант на другой край света (очень тяжело).
- Характер генерала влияет на стиль: агрессивный — больше атак, осторожный — оборона, логист — сбалансированно.
- Атак не больше max_attacks. general_id — кто ведёт удар (0, если генералов нет).
- report — короткий доклад от имени генерала лидеру страны (2–4 предложения, по-русски, по-военному).
"""

COUNTRY_DELTAS = {
    "type": "object",
    "properties": {k: {"type": "number"} for k in STAT_KEYS},
    "required": STAT_KEYS,
    "additionalProperties": False,
}

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "Заголовок хода, одна строка"},
        "world_news": {"type": "string", "description": "Публичная сводка мировых новостей за квартал, 3-8 абзацев"},
        "world_event": {"type": "string", "description": "Главное мировое событие хода, 1-2 предложения (или пустая строка)"},
        "countries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "country_id": {"type": "integer"},
                    "deltas": COUNTRY_DELTAS,
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
        "events": {
            "type": "array",
            "description": "Обновления текущих событий (event_id из active_events) и новые события (event_id = 0)",
            "items": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["pandemic", "disaster", "economic", "climate", "tech", "political", "other"]},
                    "description": {"type": "string"},
                    "severity": {"type": "integer", "description": "0-100"},
                    "affected_country_ids": {"type": "array", "items": {"type": "integer"}},
                    "status": {"type": "string", "enum": ["active", "ended"]},
                },
                "required": ["event_id", "name", "kind", "description", "severity", "affected_country_ids", "status"],
                "additionalProperties": False,
            },
        },
        "npc_messages": {
            "type": "array",
            "description": "Публичные заявления стран-НИП в общем чате",
            "items": {
                "type": "object",
                "properties": {"country_id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["country_id", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "world_news", "world_event", "countries", "relations", "events", "npc_messages"],
    "additionalProperties": False,
}

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "government": {"type": "string", "description": "Форма правления"},
        "leader_title": {"type": "string", "description": "Титул главы государства, например «Президент»"},
        "description": {"type": "string", "description": "2-3 предложения о стартовом положении страны"},
        **{k: {"type": "number"} for k in STAT_KEYS},
    },
    "required": ["government", "leader_title", "description", *STAT_KEYS],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "country_id": {"type": "integer"},
                    "enemy_id": {"type": "integer"},
                    "attacks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target_cell_id": {"type": "integer"},
                                "general_id": {"type": "integer"},
                                "force": {"type": "integer"},
                            },
                            "required": ["target_cell_id", "general_id", "force"],
                            "additionalProperties": False,
                        },
                    },
                    "defense": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"cell_id": {"type": "integer"}, "force": {"type": "integer"}},
                            "required": ["cell_id", "force"],
                            "additionalProperties": False,
                        },
                    },
                    "report": {"type": "string"},
                },
                "required": ["country_id", "enemy_id", "attacks", "defense", "report"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["plans"],
    "additionalProperties": False,
}

GENERAL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Имя и фамилия, характерные для страны, по-русски"},
        "rank": {"type": "string", "description": "Воинское звание"},
        "trait": {"type": "string", "description": "Характер/специализация в 2-5 словах"},
    },
    "required": ["name", "rank", "trait"],
    "additionalProperties": False,
}

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": ["pandemic", "disaster", "economic", "climate", "tech", "political", "other"]},
        "description": {"type": "string"},
        "severity": {"type": "integer"},
        "affected_country_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["name", "kind", "description", "severity", "affected_country_ids"],
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


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


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

    async def country_profile(self, country: dict, start_year: int) -> dict:
        base = {k: country[k] for k in STAT_KEYS}
        prompt = (
            f"Игрок берёт под управление страну «{country['name']}». Год начала игры: {start_year}.\n"
            f"Черновые показатели из статистики: {_dump(base)}\n"
            "Уточни их по реальным данным: gdp (млрд $), budget (свободные средства казны, млрд $, может быть "
            "отрицательным при большом долге), population (млн), stability, approval, military, tech, corruption, "
            "influence (0–100). Укажи форму правления, титул главы государства и стартовое положение страны."
        )
        return await self._call_json(GM_SYSTEM, prompt, PROFILE_SCHEMA, effort="low", max_tokens=4000)

    async def resolve_turn(self, world: dict, *, npc_chat: bool, random_events: bool) -> dict:
        rules = [
            "Верни запись в countries для КАЖДОЙ страны игроков (по её id); можно добавить и страны-НИП, "
            "сильно затронутые событиями хода.",
            "В relations укажи только изменившиеся пары (любые страны по id).",
            "В events обнови все активные события (active_events) и опиши, как они развиваются.",
        ]
        rules.append(
            "Можешь придумать одно новое событие (event_id = 0): эпидемия, катастрофа, кризис, прорыв — с затронутыми странами."
            if random_events else
            "Случайные события ОТКЛЮЧЕНЫ: не создавай новых (event_id = 0), только развивай существующие."
        )
        rules.append(
            "npc_messages: 2–5 живых публичных заявлений стран-НИП в чат (реакции на действия игроков и события), "
            "от имени их МИД или лидеров."
            if npc_chat else "npc_messages: пустой список (общение НИП в чате отключено)."
        )
        prompt = (
            "Просчитай итоги хода. Состояние мира, действия игроков и итоги боёв (JSON):\n\n"
            + _dump(world) + "\n\n" + "\n".join(f"- {r}" for r in rules)
        )
        return await self._call_json(GM_SYSTEM, prompt, TURN_SCHEMA, effort="medium", max_tokens=48000)

    async def plan_operations(self, briefs: list[dict]) -> list[dict]:
        prompt = (
            "Спланируй операции на этот квартал для каждой стороны в каждой войне (по одному плану на пару "
            "country_id/enemy_id):\n\n" + _dump(briefs)
        )
        result = await self._call_json(GENERALS_SYSTEM, prompt, PLAN_SCHEMA, effort="medium", max_tokens=24000)
        return result["plans"]

    async def create_general(self, country: dict, existing: list[str]) -> dict:
        prompt = (
            f"Придумай генерала армии страны «{country['name']}» (реалистичное имя для этой страны, по-русски). "
            f"Уже есть: {', '.join(existing) or 'никого'} — не повторяйся."
        )
        return await self._call_json(GM_SYSTEM, prompt, GENERAL_SCHEMA, effort="low", max_tokens=1500)

    async def create_event(self, description: str, world: dict) -> dict:
        prompt = (
            "Администратор игры вводит мировое событие. Оформи его: название, тип, описание для новостей, "
            "тяжесть 0–100 и затронутые страны (id из списка).\n\n"
            f"Событие: {description}\n\nСтраны и мир (JSON):\n{_dump(world)}"
        )
        return await self._call_json(GM_SYSTEM, prompt, EVENT_SCHEMA, effort="low", max_tokens=4000)

    async def advise(self, country: dict, world: dict, question: str) -> str:
        prompt = (
            f"Ты — главный советник руководителя страны {country['name']}. Ответь лично ему, честно и по делу, "
            "как опытный политтехнолог, экономист и военный аналитик. Предложи 2–4 конкретных шага с рисками. "
            "Коротко, до 1200 символов, без markdown-заголовков.\n\n"
            f"Состояние мира (JSON):\n{_dump(world)}\n\nВопрос лидера: {question}"
        )
        return await self._call(GM_SYSTEM, prompt, effort="low", max_tokens=4000)

    async def npc_diplomacy(self, from_country: dict, target: dict, proposal: str, world: dict) -> dict:
        prompt = (
            f"Страна игрока {from_country['name']} направила официальное предложение стране «{target['name']}», "
            "которой управляешь ты (НИП). Реши, согласится ли её правительство, исходя из реальных интересов, "
            "союзов, положения на фронте и текущей ситуации, и напиши ответ её МИД.\n\n"
            f"Состояние мира (JSON):\n{_dump(world)}\n\nПредложение: {proposal}"
        )
        return await self._call_json(GM_SYSTEM, prompt, NPC_SCHEMA, effort="low", max_tokens=3000)

    async def npc_say(self, npc: dict, speaker: dict, text: str, world: dict) -> str:
        prompt = (
            f"В общем чате лидер страны {speaker['name']} публично обратился к стране «{npc['name']}» (НИП, ею управляешь ты). "
            "Ответь от имени её лидера или МИД — в характере этой страны, коротко (1–3 предложения), без кавычек и подписи.\n\n"
            f"Состояние мира (JSON):\n{_dump(world)}\n\nОбращение: {text}"
        )
        return await self._call(GM_SYSTEM, prompt, effort="low", max_tokens=2000)
