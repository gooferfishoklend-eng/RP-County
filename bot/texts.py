from html import escape

from bot.stats import STATS, power_index

STATUS_RU = {"peace": "мир", "alliance": "союз", "tension": "напряжённость", "war": "ВОЙНА"}

HELP = """<b>🌍 Геополитика — ролевая игра за современные страны</b>

Каждый игрок управляет реальной страной на карте мира из 4000 провинций. Остальные страны живут под управлением ИИ. ИИ-ведущий просчитывает последствия, генералы воюют, мир пишет новости. Один ход = один квартал.

<b>Начало</b>
/newgame — новая игра в группе (админы)
/take <i>страна</i> — взять страну · /leave — уйти

<b>Карта и информация</b>
/map — карта мира · /map <i>страна</i> — подробно, с номерами провинций · /map события — эпидемии и катастрофы
/me — ваша страна · /world — рейтинг · /relations — отношения
/news — новости · /events — мировые события · /turn — состояние хода

<b>Политика</b>
/reform <i>текст</i> — внутренняя реформа (видят все)
/secret <i>текст</i> — тайное действие, незаметная реформа, шпионаж (только в ЛС боту!)
/foreign <i>текст</i> — внешнеполитическое заявление
/say <i>страна</i> | <i>текст</i> — публичное обращение к стране (страны-ИИ отвечают)
/aid <i>страна</i> | <i>помощь</i> — помощь при эпидемиях и бедствиях
/sanction <i>страна</i> | <i>причина</i> — санкции
/un <i>текст</i> — резолюция ООН

<b>Договоры (подписывают обе страны)</b>
/propose <i>страна</i> | <i>условия</i> — любой договор
/peace <i>страна</i> | фронт <i>или</i> довоенные | <i>условия</i> — мир
/transfer <i>страна</i> | <i>номера провинций</i> — передать свои земли
/demand <i>страна</i> | <i>номера провинций</i> — потребовать чужие

<b>🕊 Мирные конференции</b>
/conference <i>страна, страна</i> | <i>тема</i> — созвать конференцию в отдельном чате
В зале: обычные сообщения — переговоры, /draft — ИИ составляет договор, все подписывают кнопкой → бот исполняет (мир, провинции, деньги, союзы, любые условия)
/endconf — закрыть без соглашения · /rooms — комнаты

<b>Война</b>
/war <i>страна</i> | <i>цель</i> — объявить войну (или дать армии приказ)
/generals — ваши генералы · /hire — нанять · /fire #id — отставка
/command #id | <i>приказ</i> — приказ генералу (он воюет сам, умно)

<b>Ход</b>
/actions — ваши приказы · /undo — отменить · /ready — завершить ход
/advisor <i>вопрос</i> — ИИ-советник (ответ в ЛС) · /play — выбрать игру для ЛС

<b>Админам</b>
/settings — включить/выключить болтовню стран-ИИ и случайные события
/event <i>описание</i> — запустить своё событие (болезнь, катастрофа, кризис)
/addroom — подключить комнату для конференций
/endturn — завершить ход · /endgame — завершить игру

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
