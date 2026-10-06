from html import escape

from bot.stats import STATS, power_index

STATUS_RU = {"peace": "мир", "alliance": "союз", "tension": "напряжённость", "war": "ВОЙНА"}

HELP = """<b>🌍 Геополитика — ролевая игра за современные страны</b>

Каждый игрок управляет реальной страной. ИИ-ведущий просчитывает последствия решений, пишет мировые новости и тайные доклады.
Один ход = один квартал.

<b>Начало</b>
/newgame — начать новую игру в группе (админы)
/take <i>страна</i> — взять страну под управление
/leave — отказаться от страны

<b>Информация</b>
/me — карточка вашей страны
/world — рейтинг держав
/relations — ваши отношения с другими странами
/news — последние мировые новости
/turn — состояние текущего хода

<b>Действия за ход</b>
/reform <i>текст</i> — внутренняя реформа (видят все)
/secret <i>текст</i> — тайное действие: незаметная реформа, спецоперация, шпионаж (только в ЛС боту!)
/foreign <i>текст</i> — внешнеполитический курс, заявление
/sanction <i>страна</i> | <i>причина</i> — ввести санкции
/war <i>страна</i> | <i>цель</i> — объявить войну
/propose <i>страна</i> | <i>предложение</i> — договор (игроку или стране под управлением ИИ)
/un <i>текст</i> — внести резолюцию ООН на голосование
/actions — ваши действия на этот ход
/undo — отменить последнее действие
/ready — завершить свой ход

<b>Прочее</b>
/advisor <i>вопрос</i> — спросить ИИ-советника (ответ придёт в ЛС)
/play — выбрать игру для команд в ЛС
/endturn — принудительно завершить ход (админы)
/endgame — завершить игру (админы)

Ход считается автоматически, когда все игроки нажали /ready."""


def fmt_num(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:,.0f}".replace(",", " ")
    return f"{value:g}"


def country_card(c: dict) -> str:
    lines = [
        f"{c['flag']} <b>{escape(c['name'])}</b>",
        f"<i>{escape(c['government'])}</i> · {escape(c['leader_title'])}: {escape(c['player_name'] or '—')}",
        f"Индекс силы: <b>{power_index(c)}</b>",
        "",
    ]
    for s in STATS:
        unit = f" {s.unit}" if s.unit else "/100"
        lines.append(f"{s.emoji} {s.title}: <b>{fmt_num(c[s.key])}</b>{unit}")
    return "\n".join(lines)


def delta_line(old: float, new: float) -> str:
    diff = new - old
    if abs(diff) < 0.05:
        return ""
    sign = "+" if diff > 0 else ""
    return f" ({sign}{fmt_num(round(diff, 1))})"


def stat_changes(changes: dict) -> str:
    lines = []
    for s in STATS:
        old, new = changes[s.key]
        d = delta_line(old, new)
        if d:
            lines.append(f"{s.emoji} {s.title}: {fmt_num(new)}{d}")
    return "\n".join(lines) or "Показатели без изменений."


def ranking(countries: list[dict]) -> str:
    if not countries:
        return "В игре пока нет стран. Возьмите страну: /take <i>название</i>"
    ordered = sorted(countries, key=power_index, reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = ["<b>🌍 Рейтинг держав</b>", ""]
    for i, c in enumerate(ordered):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(
            f"{mark} {c['flag']} <b>{escape(c['name'])}</b> — {power_index(c)} "
            f"<i>({escape(c['player_name'] or '')})</i>"
        )
    return "\n".join(lines)


def split_message(text: str, limit: int = 4000) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts
