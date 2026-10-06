"""Export the whole game history to /data/history.txt and send it to the game group.

Run on the server:
  curl -fsSL https://raw.githubusercontent.com/gooferfishoklend-eng/RP-County/HEAD/tools/export_history.py | docker exec -i rp-county python -
"""
import json, os, sqlite3, urllib.request, uuid
DB, OUT = os.environ.get("EXPORT_DB", "/data/geopolitics.db"), os.environ.get("EXPORT_OUT", "/data/history.txt")
Y0 = int(os.environ.get("START_YEAR", "2026"))
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
q = lambda sql, *a: [dict(r) for r in c.execute(sql, a)]
lbl = lambda t: f"{(t - 1) % 4 + 1}-й квартал {Y0 + (t - 1) // 4} г."
KIND = {"reform": "Реформа", "foreign": "Внешняя политика", "war": "Война/приказ армии", "sanction": "Санкции",
        "aid": "Помощь", "treaty": "Договор", "npc_deal": "Договор с ИИ-страной"}
STATUS = {"peace": "мир", "alliance": "союз", "tension": "напряжённость", "war": "ВОЙНА"}
for g in q("SELECT * FROM games"):
    gid = g["chat_id"]
    cs = {x["id"]: x for x in q("SELECT * FROM countries WHERE chat_id=?", gid)}
    nm = lambda i: cs[i]["name"] if i in cs else "?"
    L = [f"ИСТОРИЯ ИГРЫ «{g['title']}» — сейчас {lbl(g['turn'])} (ход {g['turn']}), статус: {g['status']}", "=" * 70, "",
         "ИГРОКИ И ИХ СТРАНЫ", "-" * 70]
    for x in cs.values():
        if x["user_id"]:
            prov = c.execute("SELECT count(*) FROM cells WHERE chat_id=? AND owner_id=?", (gid, x["id"])).fetchone()[0]
            L.append(f"{x['flag']} {x['name']} — {x['player_name']} | {x['government']} | провинций: {prov}")
            L.append(f"   ВВП {x['gdp']:g} млрд $, казна {x['budget']:g}, население {x['population']:g} млн, стабильность "
                     f"{x['stability']}, рейтинг {x['approval']}, армия {x['military']}, технологии {x['tech']}, "
                     f"коррупция {x['corruption']}, влияние {x['influence']}")
    L += ["", "ОТНОШЕНИЯ", "-" * 70]
    for r in q("SELECT * FROM relations WHERE chat_id=? ORDER BY status='war' DESC, value", gid):
        L.append(f"{nm(r['a_id'])} — {nm(r['b_id'])}: {STATUS.get(r['status'], r['status'])} ({r['value']:+d})")
    L += ["", "ДОГОВОРЫ", "-" * 70]
    for t in q("SELECT * FROM treaties WHERE chat_id=? ORDER BY id", gid):
        L.append(f"[{lbl(t['turn'])}] {nm(t['a_id'])} ⟷ {nm(t['b_id'])} ({t['kind']}, {t['status']}): {t['text']}")
    for k in q("SELECT * FROM conferences WHERE chat_id=? ORDER BY id", gid):
        d = json.loads(k["draft"] or "{}")
        L.append(f"[{lbl(k['turn'])}] Конференция №{k['id']} «{k['topic']}» ({k['status']}): {d.get('title', '')} — {d.get('summary', '')}")
        L += [f"     • {cl}" for cl in d.get("clauses", [])]
    L += ["", "МИРОВЫЕ СОБЫТИЯ", "-" * 70]
    for e in q("SELECT * FROM events WHERE chat_id=? ORDER BY id", gid):
        L.append(f"[с {lbl(e['started_turn'])}] {e['name']} ({e['status']}, тяжесть {e['severity']}): {e['description']}")
    news = {h["turn"]: h for h in q("SELECT * FROM chronicle WHERE chat_id=? ORDER BY turn", gid)}
    acts = q("SELECT * FROM actions WHERE chat_id=? AND kind != 'secret' ORDER BY turn, id", gid)
    L += ["", "ХРОНИКА ПО ХОДАМ", "=" * 70]
    for t in range(1, g["turn"] + 1):
        L += ["", f"### {lbl(t)} (ход {t})"]
        for a in (a for a in acts if a["turn"] == t):
            L.append(f"  {cs.get(a['country_id'], {}).get('flag', '')} {nm(a['country_id'])} — {KIND.get(a['kind'], a['kind'])}"
                     + (f" → {a['target']}" if a["target"] else "") + f": {a['text']}")
        if t in news:
            h = news[t]
            L += ["", f"  ИТОГИ: {h['headline']}", "", h["news"], "", f"  Событие: {h['event'] or '—'}"]
    text = "\n".join(L) + "\n"
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Сохранено: {OUT} ({len(text)} символов)")
    token = os.environ.get("BOT_TOKEN")
    if token:
        b = uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{gid}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n📜 История игры «{g['title']}»\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"history.txt\"\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\n").encode() + text.encode() + f"\r\n--{b}--\r\n".encode()
        api = os.environ.get("TG_API", "https://api.telegram.org")
        req = urllib.request.Request(f"{api}/bot{token}/sendDocument", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        print("Отправлено в группу:", json.load(urllib.request.urlopen(req, timeout=60)).get("ok"))
