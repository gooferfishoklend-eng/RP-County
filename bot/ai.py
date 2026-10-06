import json
import logging

from bot.llm import AIError, extract_json
from bot.stats import STAT_KEYS

__all__ = ["AIError", "GameMaster"]

log = logging.getLogger(__name__)

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
- Подписанные договоры (agreements_in_force) обязательны: их условия (торговля, демилитаризация, гарантии, выплаты и т.д.) реально действуют. Нарушение договора — международный скандал, падение влияния и доверия.
- При очень низкой стабильности — протесты, забастовки, угроза переворота.
- Мировые события (эпидемии, катастрофы, кризисы) длятся несколько ходов: распространяются на соседей, усиливаются или затухают. Карантин, медицина, технологии и международная помощь помогают с ними справиться.

- КАЖДЫЙ приказ игрока обязан дать ощутимый, конкретный результат: отрази его в deltas (обычно ±2..10 по связанным показателям, траты из казны, рост/падение ВВП) и в orders. Удачная реформа — заметный плюс (возможно, с ценой: деньги, рейтинг), провальная — заметный минус. Нулевые deltas у страны, которая отдавала приказы, недопустимы.
- Войну и мир между странами переключают только сами игроки (объявление войны, подписанный мир) — ты не меняешь статус war в relations.
- Поддержка (supports): деньги, оружие, войска, разведка, гуманитарка уже применены движком — учитывай её в событиях и реакции мира.
- У стран-НИП есть записные книжки (npc_notes): обещания, планы, доверие к игрокам. Действуй согласно им: выполняй обещанное, если это в интересах страны, или предавай, если доверие подорвано, — и обновляй записи.
- Летопись (chronicle_summary) — память обо всей игре. Сверяйся с ней, чтобы не противоречить прошлому.
- Союзы (alliances) — официальные блоки с главой. Союзники помогают друг другу, нападение на одного касается всех; страны-НИП в союзе следуют курсу главы, если это не идёт вразрез с их интересами.

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
                    "orders": {
                        "type": "array",
                        "description": "Итог КАЖДОГО приказа этой страны за ход (включая тайные) в том же порядке",
                        "items": {
                            "type": "object",
                            "properties": {
                                "order": {"type": "string", "description": "Кратко, что приказано"},
                                "result": {"type": "string", "description": "Что получилось, с цифрами, 1-2 предложения"},
                                "outcome": {"type": "string", "enum": ["success", "partial", "failure"]},
                            },
                            "required": ["order", "result", "outcome"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["country_id", "deltas", "public_summary", "private_report", "secret_exposed", "orders"],
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
        "chronicle_summary": {
            "type": "string",
            "description": "Обновлённая летопись всей игры с учётом этого хода: войны, договоры, союзы, ключевые реформы, "
                           "кто кому что обещал. До 3000 символов, сжимай старое",
        },
        "npc_notes": {
            "type": "array",
            "description": "Обновлённые записные книжки стран-НИП, у которых что-то изменилось",
            "items": {
                "type": "object",
                "properties": {"country_id": {"type": "integer"}, "notes": {"type": "string"}},
                "required": ["country_id", "notes"],
                "additionalProperties": False,
            },
        },
        "npc_support": {
            "type": "array",
            "description": "Поддержка, которую страны-НИП решили оказать в этом ходу (по обещаниям или своим интересам)",
            "items": {
                "type": "object",
                "properties": {
                    "from_id": {"type": "integer"},
                    "to_id": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["money", "weapons", "troops", "intel", "humanitarian"]},
                    "amount_bn": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["from_id", "to_id", "kind", "amount_bn", "reason"],
                "additionalProperties": False,
            },
        },
        "npc_private_messages": {
            "type": "array",
            "description": "Тайные личные послания стран-НИП лидерам стран-игроков (предложения, угрозы, сделки)",
            "items": {
                "type": "object",
                "properties": {"from_id": {"type": "integer"}, "to_id": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["from_id", "to_id", "text"],
                "additionalProperties": False,
            },
        },
        "summits": {
            "type": "array",
            "description": "Экстренный саммит, который созывает страна-НИП (не больше одного за ход, только при серьёзном поводе)",
            "items": {
                "type": "object",
                "properties": {
                    "initiator_id": {"type": "integer"},
                    "topic": {"type": "string"},
                    "invitee_ids": {"type": "array", "items": {"type": "integer"}},
                    "opening": {"type": "string", "description": "Вступительная речь организатора, 2-4 предложения"},
                },
                "required": ["initiator_id", "topic", "invitee_ids", "opening"],
                "additionalProperties": False,
            },
        },
        "npc_messages": {
            "type": "array",
            "description": "Публичные заявления стран-НИП в общем чате",
            "items": {
                "type": "object",
                "properties": {
                    "country_id": {"type": "integer"},
                    "to_country_id": {"type": "integer", "description": "К кому обращается (id страны), 0 — ко всем"},
                    "text": {"type": "string"},
                },
                "required": ["country_id", "to_country_id", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "world_news", "world_event", "countries", "relations", "events", "npc_messages",
                 "chronicle_summary", "npc_notes", "npc_support", "npc_private_messages", "summits"],
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

NOTES_FIELD = {
    "type": "string",
    "description": "Твоя обновлённая записная книжка (память страны): ключевые факты, договорённости и обещания "
                   "(кто, что, к какому ходу), планы, доверие к каждому лидеру. До 1500 символов, сохраняй важное из старой",
}
TRUST_FIELD = {"type": "integer", "description": "Изменение доверия к собеседнику после этого разговора, от -15 до 15"}
SUPPORT_FIELD = {
    "type": "object",
    "description": "Поддержка, которую страна решила оказать собеседнику прямо сейчас (kind = none, если нет)",
    "properties": {
        "kind": {"type": "string", "enum": ["none", "money", "weapons", "troops", "intel", "humanitarian"]},
        "amount_bn": {"type": "number"},
    },
    "required": ["kind", "amount_bn"],
    "additionalProperties": False,
}

NPC_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "reply": {"type": "string", "description": "Официальный ответ МИД страны-НИП, 1-3 предложения"},
        "notes": NOTES_FIELD,
        "trust_delta": TRUST_FIELD,
    },
    "required": ["accepted", "reply", "notes", "trust_delta"],
    "additionalProperties": False,
}

NPC_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Ответ от имени лидера/МИД, 1-4 предложения, без подписи"},
        "notes": NOTES_FIELD,
        "trust_delta": TRUST_FIELD,
        "support": SUPPORT_FIELD,
    },
    "required": ["reply", "notes", "trust_delta", "support"],
    "additionalProperties": False,
}

TREATY_EFFECTS_SCHEMA = {
    "type": "object",
    "properties": {
        "ends_war": {"type": "boolean", "description": "Договор прекращает войну / вводит перемирие или мир"},
        "alliance": {"type": "boolean", "description": "Договор создаёт военный или политический союз"},
        "non_aggression": {"type": "boolean"},
        "summary": {"type": "string", "description": "Суть договора в одном предложении"},
    },
    "required": ["ends_war", "alliance", "non_aggression", "summary"],
    "additionalProperties": False,
}

NPC_ROLE = (
    "Ты играешь страну «{name}» (её интересами управляешь ты). У тебя есть память: записная книжка (your_notes) и "
    "история разговоров (conversation, your_recent_contacts). Помни договорённости, обещания и обиды, будь "
    "последовательной. Можно договариваться о совместных планах на будущие ходы — запиши их в notes, чтобы выполнить. "
    "Ты можешь оказать реальную поддержку (деньги, оружие, войска, разведку, гуманитарку) из своей казны, если это в "
    "твоих интересах. Доверие растёт от выполненных обещаний и падает от обмана и угроз."
)


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class GameMaster:
    def __init__(self, backend):
        self.backend = backend

    async def _call(self, system: str, user: str, *, effort: str, max_tokens: int, schema: dict | None = None) -> str:
        text = await self.backend.complete(system, user, effort=effort, max_tokens=max_tokens, schema=schema)
        if not text.strip():
            raise AIError("ИИ вернул пустой ответ.")
        return text

    async def _call_json(self, system: str, user: str, schema: dict, *, effort: str, max_tokens: int) -> dict:
        text = await self._call(system, user, effort=effort, max_tokens=max_tokens, schema=schema)
        try:
            return extract_json(text)
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
            "npc_messages: 2–6 живых публичных реплик стран-НИП в общий чат: реакции на действия игроков и события, "
            "а также обращения стран-НИП ДРУГ К ДРУГУ (to_country_id) — споры, предложения, угрозы, поддержка; "
            "0 — заявление для всех. Пиши от имени их МИД или лидеров. "
            "npc_private_messages: 0–2 тайных личных послания стран-НИП игрокам, если стране есть что предложить или "
            "чем пригрозить. summits: если ситуация экстренная (большая война, эпидемия, кризис, угроза союзу) — "
            "одна страна-НИП может созвать саммит, пригласив затронутых игроков и страны-НИП (2–6 участников); "
            "иначе пустой список."
            if npc_chat else
            "npc_messages, npc_private_messages, summits: пустые списки (общение НИП отключено)."
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

    async def npc_diplomacy(self, from_country: dict, target: dict, proposal: str, context: dict) -> dict:
        prompt = (
            NPC_ROLE.format(name=target["name"]) + "\n\n"
            f"Страна игрока {from_country['name']} направила тебе официальное предложение договора. Реши, согласишься "
            "ли, исходя из интересов, союзов, положения на фронте, доверия и прошлых договорённостей, и напиши ответ МИД.\n\n"
            f"Контекст (JSON):\n{_dump(context)}\n\nПредложение: {proposal}"
        )
        return await self._call_json(GM_SYSTEM, prompt, NPC_SCHEMA, effort="low", max_tokens=4000)

    async def npc_respond(self, npc: dict, speaker: dict, text: str, context: dict, *, private: bool) -> dict:
        channel = ("в ЛИЧНЫХ тайных переговорах (никто, кроме вас двоих, этого не видит)" if private
                   else "публично в общем чате")
        prompt = (
            NPC_ROLE.format(name=npc["name"]) + "\n\n"
            f"Лидер страны {speaker['name']} обратился к тебе {channel}. Ответь в характере своей страны.\n\n"
            f"Контекст (JSON):\n{_dump(context)}\n\nСообщение: {text}"
        )
        return await self._call_json(GM_SYSTEM, prompt, NPC_REPLY_SCHEMA, effort="low", max_tokens=4000)

    async def interpret_treaty(self, text: str, a: dict, b: dict, status: str) -> dict:
        prompt = (
            f"Подписан договор между {a['name']} и {b['name']} (текущий статус отношений: {status}). "
            "Определи его правовые последствия.\n\nТекст договора: " + text
        )
        return await self._call_json(GM_SYSTEM, prompt, TREATY_EFFECTS_SCHEMA, effort="low", max_tokens=1500)

    async def conference_draft(self, context: dict) -> dict:
        prompt = (
            "Ты — секретариат международной конференции. Внимательно проанализируй стенограмму переговоров и составь "
            "проект итогового договора: только то, о чём стороны реально договорились (или к чему явно пришли). "
            "Не выдумывай уступок, которых не было. Спорное вынеси в unresolved.\n"
            "- peace: прекращение войн между участниками (mode front — по линии фронта, status_quo — возврат земель).\n"
            "- province_transfers: передачи провинций, ТОЛЬКО cell_id из списка provinces, from_id — текущий владелец.\n"
            "- payments: денежные выплаты (млрд $): репарации, кредиты, помощь, покупка территорий.\n"
            "- alliances, relation_changes: союзы и потепление/охлаждение отношений.\n"
            "- clauses: всё остальное, о чём договорились (демилитаризация, торговля, базы, гарантии, обмен пленными, "
            "технологии, ресурсы и т.д.) — чётко, по пунктам; ведущий учтёт их в следующих ходах.\n"
            "- npc_positions: для каждой страны-участника под управлением ИИ (player=false) реши, подпишет ли она этот "
            "проект, исходя из её интересов и хода переговоров, и коротко объясни.\n"
            "- ready_to_sign: true, если стороны пришли к согласию по главным вопросам.\n\n"
            + _dump(context)
        )
        return await self._call_json(GM_SYSTEM, prompt, CONFERENCE_SCHEMA, effort="medium", max_tokens=16000)

    async def conference_npc_turn(self, context: dict, delegations: list[dict], speaker: str) -> list[dict]:
        one_on_one = len(delegations) == 1 and sum(1 for p in context["participants"] if p["player"]) == 1
        rule = ("Это переговоры ОДИН НА ОДИН: делегация отвечает на каждое сообщение игрока."
                if one_on_one else
                "Сам определи по смыслу (имя страны называть не обязательно), к каким делегациям ИИ обращено последнее "
                "сообщение или кого оно касается, — ответить должны они. Если сообщение адресовано только другому "
                "игроку и делегаций ИИ не касается, верни пустой список. Отвечать могут несколько делегаций.")
        prompt = (
            "Ты управляешь делегациями стран-ИИ на международной конференции (delegations). У каждой есть записная "
            "книжка и память о разговорах с участниками — будь последовательным, помни обещания. Последнее сообщение "
            f"в стенограмме написал {speaker}. {rule} Каждая делегация отвечает от первого лица как дипломат своей "
            "страны, отстаивая её интересы: можно торговаться, выдвигать условия, соглашаться или отказывать. "
            "1–4 предложения, без подписи.\n\n" + _dump({**context, "delegations": delegations})
        )
        result = await self._call_json(GM_SYSTEM, prompt, CONF_NPC_SCHEMA, effort="low", max_tokens=4000)
        return result["replies"]


CONF_NPC_SCHEMA = {
    "type": "object",
    "properties": {
        "replies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"country_id": {"type": "integer"}, "reply": {"type": "string"}},
                "required": ["country_id", "reply"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["replies"],
    "additionalProperties": False,
}

PAIR = {
    "type": "object",
    "properties": {"a_id": {"type": "integer"}, "b_id": {"type": "integer"}},
    "required": ["a_id", "b_id"],
    "additionalProperties": False,
}

CONFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Название договора, например «Женевский мирный договор»"},
        "summary": {"type": "string", "description": "Суть договорённостей, 2-5 предложений"},
        "peace": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"a_id": {"type": "integer"}, "b_id": {"type": "integer"},
                               "mode": {"type": "string", "enum": ["front", "status_quo"]}},
                "required": ["a_id", "b_id", "mode"],
                "additionalProperties": False,
            },
        },
        "province_transfers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"cell_id": {"type": "integer"}, "from_id": {"type": "integer"}, "to_id": {"type": "integer"}},
                "required": ["cell_id", "from_id", "to_id"],
                "additionalProperties": False,
            },
        },
        "payments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"from_id": {"type": "integer"}, "to_id": {"type": "integer"},
                               "amount_bn": {"type": "number"}, "purpose": {"type": "string"}},
                "required": ["from_id", "to_id", "amount_bn", "purpose"],
                "additionalProperties": False,
            },
        },
        "alliances": {"type": "array", "items": PAIR},
        "relation_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"a_id": {"type": "integer"}, "b_id": {"type": "integer"}, "delta": {"type": "integer"}},
                "required": ["a_id", "b_id", "delta"],
                "additionalProperties": False,
            },
        },
        "clauses": {"type": "array", "items": {"type": "string"}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "npc_positions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"country_id": {"type": "integer"}, "accepts": {"type": "boolean"},
                               "statement": {"type": "string"}},
                "required": ["country_id", "accepts", "statement"],
                "additionalProperties": False,
            },
        },
        "ready_to_sign": {"type": "boolean"},
    },
    "required": ["title", "summary", "peace", "province_transfers", "payments", "alliances", "relation_changes",
                 "clauses", "unresolved", "npc_positions", "ready_to_sign"],
    "additionalProperties": False,
}
