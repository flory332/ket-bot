import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from io import BytesIO
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from dotenv import load_dotenv

from schedule_parser import parse_file, parse_file_full

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
PAGE_URL = "https://spo-ket.gosuslugi.ru/grafik-zanyatiy/"
CHECK_EVERY = 10 * 60          # как часто проверять сайт, секунд
TZ = ZoneInfo("Europe/Moscow")  # Кострома
DB_PATH = os.environ.get("DB_PATH", "bot.db")  # на хостинге укажите путь на постоянном диске
PROXY = os.environ.get("PROXY_URL", "").strip() or None  # необязательно: http://логин:пароль@хост:порт
SEND_TIME = os.environ.get("SEND_TIME", "19:00")  # во сколько слать расписание на завтра (по Костроме)
TEACHERS_FILE = "teachers.txt"  # по одному преподавателю на строку; строки с # игнорируются
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()  # если задан, файлы принимаются только от него

# ссылки вида .../26_11_2026_v2.xlsx
FILE_RE = re.compile(r"""href=["']([^"']*?/(\d{2})_(\d{2})_(\d{4})_v(\d+)\.xlsx)["']""", re.I)
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ket-bot")

# ---------- база данных ----------
def open_db():
    """Открывает базу; если файл повреждён (не SQLite), переименовывает его и создаёт новую."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        broken = DB_PATH + ".broken"
        try:
            if os.path.exists(broken):
                os.remove(broken)
            os.replace(DB_PATH, broken)
            log.warning("Файл %s не является базой данных, переименован в %s", DB_PATH, broken)
        except OSError:
            os.remove(DB_PATH)
            log.warning("Файл %s не является базой данных, удалён", DB_PATH)
        conn = sqlite3.connect(DB_PATH)
    return conn


db = open_db()
db.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, grp TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, version INTEGER, data TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS sent (chat_id INTEGER, day TEXT, hash TEXT, PRIMARY KEY(chat_id, day))")
sync_state = {"ok_at": None, "error": None}
db.commit()


def get_group(chat_id):
    r = db.execute("SELECT grp FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return r[0] if r else None


def set_group(chat_id, grp):
    db.execute("INSERT INTO users(chat_id, grp) VALUES(?,?) "
               "ON CONFLICT(chat_id) DO UPDATE SET grp=excluded.grp", (chat_id, grp))
    db.commit()


def get_day(d: date):
    r = db.execute("SELECT data FROM days WHERE day=?", (d.isoformat(),)).fetchone()
    return json.loads(r[0]) if r else None


def all_groups():
    names = set()
    for (data,) in db.execute("SELECT data FROM days"):
        names.update(json.loads(data).keys())
    return sorted(names, key=_sort_key)


def _sort_key(g):
    m = re.match(r"(\d+)-(\d+)\s*(.*)", g)
    return (int(m[1]), m[3], int(m[2])) if m else (99, g, 0)


# ---------- оформление ----------
def format_day(group, d: date, data):
    head = f"<b>{html.escape(group)}</b> · {WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y}"
    g = data.get(group) if data else None
    if g is None:
        return (f"{head}\n\nДля вашей группы в файле на этот день ничего нет. "
                "Возможно, изменений нет и действует основное расписание.")
    lines = [head, ""]
    for slot, text in g["lessons"]:
        lines.append(f"<b>{slot} ур</b> — {html.escape(text)}")
    for note in g["notes"]:
        lines.append(f"ℹ️ {html.escape(note)}")
    return "\n".join(lines)


MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
              [KeyboardButton(text="Звонки"), KeyboardButton(text="Преподаватели")],
              [KeyboardButton(text="Сменить группу")]],
    resize_keyboard=True)


def groups_keyboard():
    buttons = [InlineKeyboardButton(text=g, callback_data=f"g:{g}") for g in all_groups()]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- загрузка расписания с сайта ----------
async def sync(bot: Bot):
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        async with s.get(PAGE_URL, proxy=PROXY) as r:
            page = await r.text()

        newest = {}  # дата -> (версия, ссылка)
        for url, dd, mm, yyyy, ver in FILE_RE.findall(page):
            d = date(int(yyyy), int(mm), int(dd))
            if d not in newest or int(ver) > newest[d][0]:
                newest[d] = (int(ver), urljoin(PAGE_URL, url))

        updated = []
        for d, (ver, url) in sorted(newest.items()):
            row = db.execute("SELECT version FROM days WHERE day=?", (d.isoformat(),)).fetchone()
            if row and row[0] >= ver:
                continue
            async with s.get(url, proxy=PROXY) as r:
                raw = await r.read()
            data = await asyncio.to_thread(parse_file, raw, "xlsx")
            if not data:
                log.warning("Файл %s разобран, но групп не найдено — проверьте формат", url)
                continue
            db.execute("INSERT OR REPLACE INTO days VALUES(?,?,?)",
                       (d.isoformat(), ver, json.dumps(data, ensure_ascii=False)))
            db.commit()
            updated.append((d, data))
            log.info("Загружено расписание на %s (версия %s, групп: %s)", d, ver, len(data))

    sync_state["ok_at"] = datetime.now(TZ)
    sync_state["error"] = None
    for d, data in updated:
        await notify(bot, d, data)


async def deliver(bot: Bot, chat_id: int, grp: str, d: date, data):
    """Отправляет расписание, если такое же ещё не отправляли. Правки в файле шлёт как «обновлено»."""
    if grp not in data:
        return
    text = format_day(grp, d, data)
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()
    row = db.execute("SELECT hash FROM sent WHERE chat_id=? AND day=?", (chat_id, d.isoformat())).fetchone()
    if row and row[0] == h:
        return
    today = datetime.now(TZ).date()
    if row:
        header = "🔔 Расписание обновлено"
    elif d == today:
        header = "📅 Расписание на сегодня"
    elif d == today + timedelta(days=1):
        header = "📅 Расписание на завтра"
    else:
        header = "📅 Новое расписание"
    try:
        await bot.send_message(chat_id, header + "\n\n" + text)
    except Exception as e:
        log.warning("Не удалось отправить %s: %s", chat_id, e)
        return
    db.execute("INSERT OR REPLACE INTO sent VALUES(?,?,?)", (chat_id, d.isoformat(), h))
    db.commit()


async def notify(bot: Bot, d: date, data):
    """Новый или обновлённый файл: разослать всем, у кого выбрана группа из файла."""
    if d < datetime.now(TZ).date():
        return
    for chat_id, grp in db.execute("SELECT chat_id, grp FROM users WHERE grp IS NOT NULL").fetchall():
        await deliver(bot, chat_id, grp, d, data)


async def daily_sender(bot: Bot):
    """Каждый вечер в SEND_TIME отправляет расписание на завтра."""
    hh, mm = (int(x) for x in SEND_TIME.split(":"))
    last_day = None
    while True:
        now = datetime.now(TZ)
        if (now.hour, now.minute) >= (hh, mm) and last_day != now.date():
            last_day = now.date()
            d = now.date() + timedelta(days=1)
            data = get_day(d)
            if data is not None:
                await notify(bot, d, data)
        await asyncio.sleep(60)


async def watcher(bot: Bot):
    while True:
        try:
            await sync(bot)
        except Exception as e:
            sync_state["error"] = repr(e)
            log.exception("Ошибка при проверке сайта")
        await asyncio.sleep(CHECK_EVERY)


# ---------- обработчики ----------
dp = Dispatcher()


async def ask_group(message: Message, bot: Bot):
    if not all_groups():
        await message.answer("Пока нет данных о группах. Отправьте мне файл расписания "
                             "(PDF или xlsx) — я его разберу и покажу список групп.")
        return
    await message.answer("Выберите свою группу:", reply_markup=groups_keyboard())


@dp.message(CommandStart())
@dp.message(Command("group"))
@dp.message(F.text == "Сменить группу")
async def on_start(message: Message, bot: Bot):
    await ask_group(message, bot)


@dp.callback_query(F.data.startswith("g:"))
async def on_group_chosen(cb: CallbackQuery):
    grp = cb.data[2:]
    set_group(cb.from_user.id, grp)
    await cb.message.edit_text(f"Группа сохранена: <b>{html.escape(grp)}</b>")
    await cb.message.answer("Готово! Теперь расписание вашей группы будет приходить само: "
                            "каждый вечер на завтра и при любых изменениях.", reply_markup=MENU)
    today = datetime.now(TZ).date()
    for d in (today, today + timedelta(days=1)):
        data = get_day(d)
        if data is not None and grp in data:
            await cb.message.answer(format_day(grp, d, data))
    await cb.answer()


async def show_day(message: Message, d: date):
    grp = get_group(message.chat.id)
    if not grp:
        await message.answer("Сначала выберите группу: /group")
        return
    data = get_day(d)
    if data is None:
        await message.answer(f"Расписание на {WEEKDAYS[d.weekday()]}, {d:%d.%m} пока не опубликовано на сайте.")
        return
    await message.answer(format_day(grp, d, data))


BELLS = (
    "🔔 <b>Расписание звонков</b>\n\n"
    "<b>Группы 1–2 курса</b>\n"
    "1 пара: 1 урок 8.30–9.15, 2 урок 9.20–10.05\n"
    "2 пара: 3 урок 10.15–11.00\n"
    "Большая перемена 11.00–11.40\n"
    "4 урок 11.40–12.25\n"
    "3 пара: 5 урок 12.30–13.15, 6 урок 13.20–14.05\n"
    "4 пара: 7 урок 14.15–15.00, 8 урок 15.05–15.50\n\n"
    "<b>Группы 3–4 курса</b>\n"
    "1 пара: 1 урок 8.30–9.15, 2 урок 9.20–10.05\n"
    "2 пара: 3 урок 10.15–11.00, 4 урок 11.05–11.50\n"
    "Большая перемена 11.50–12.30\n"
    "3 пара: 5 урок 12.30–13.15, 6 урок 13.20–14.05\n"
    "4 пара: 7 урок 14.15–15.00, 8 урок 15.05–15.50"
)


@dp.message(Command("bells"))
@dp.message(F.text == "Звонки")
async def on_bells(message: Message):
    await message.answer(BELLS)


@dp.message(Command("today"))
@dp.message(F.text == "Сегодня")
async def on_today(message: Message):
    await show_day(message, datetime.now(TZ).date())


@dp.message(Command("tomorrow"))
@dp.message(F.text == "Завтра")
async def on_tomorrow(message: Message):
    await show_day(message, datetime.now(TZ).date() + timedelta(days=1))


@dp.message(Command("week"))
async def on_week(message: Message):
    grp = get_group(message.chat.id)
    if not grp:
        await message.answer("Сначала выберите группу: /group")
        return
    today = datetime.now(TZ).date()
    found = False
    for i in range(7):
        d = today + timedelta(days=i)
        data = get_day(d)
        if data is not None:
            found = True
            await message.answer(format_day(grp, d, data))
    if not found:
        await message.answer("На ближайшие дни расписание пока не опубликовано.")


@dp.message(F.document)
async def on_document(message: Message, bot: Bot):
    if ADMIN_ID and str(message.from_user.id) != ADMIN_ID:
        await message.answer("Загружать расписание может только владелец бота.")
        return
    name = message.document.file_name or ""
    ext = name.lower().rsplit(".", 1)[-1]
    if ext not in ("pdf", "xlsx"):
        await message.answer("Пришлите файл расписания в формате PDF или xlsx.")
        return
    buf = await bot.download(message.document, destination=BytesIO())
    try:
        d, data = await asyncio.to_thread(parse_file_full, buf.getvalue(), ext, name)
    except Exception:
        log.exception("Ошибка разбора файла")
        await message.answer("Не получилось прочитать файл.")
        return
    if not data:
        await message.answer("В файле не нашлось групп. Проверьте, что это файл расписания.")
        return
    if d is None:
        await message.answer("Не нашёл в файле дату расписания. Назовите файл так: 27_11_2026.pdf и отправьте снова.")
        return
    db.execute("INSERT OR REPLACE INTO days VALUES(?,?,?)",
               (d.isoformat(), 0, json.dumps(data, ensure_ascii=False)))
    db.commit()
    await message.answer(f"Расписание на {d:%d.%m.%Y} загружено, групп: {len(data)}.")
    await notify(bot, d, data)
    if not get_group(message.chat.id):
        await ask_group(message, bot)


def load_teachers():
    try:
        with open(TEACHERS_FILE, encoding="utf-8-sig") as f:
            return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    except FileNotFoundError:
        return []


async def send_long(message: Message, lines):
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3800:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)


@dp.message(Command("status"))
async def on_status(message: Message):
    days = [r[0] for r in db.execute("SELECT day FROM days ORDER BY day DESC LIMIT 3")]
    users = db.execute("SELECT COUNT(*) FROM users WHERE grp IS NOT NULL").fetchone()[0]
    ok = sync_state["ok_at"]
    lines = ["Проверка сайта: " + (f"успешно в {ok:%d.%m %H:%M}" if ok else "ещё не удавалась"),
             f"Ошибка сайта: {sync_state['error']}" if sync_state["error"] else "Ошибок нет",
             "Последние дни в базе: " + (", ".join(days) if days else "нет"),
             f"Пользователей с группой: {users}",
             f"Рассылка на завтра: каждый день в {SEND_TIME} (Кострома)"]
    await message.answer("\n".join(html.escape(l) for l in lines))


@dp.message(Command("teachers"))
@dp.message(F.text == "Преподаватели")
async def on_teachers(message: Message):
    teachers = load_teachers()
    if not teachers:
        await message.answer("Список преподавателей пока пуст. Владелец бота может добавить "
                             "имена в файл teachers.txt.")
        return
    await message.answer("👩‍🏫 <b>Преподаватели</b>\nЧтобы найти человека, просто напишите его фамилию.")
    await send_long(message, [html.escape(t) for t in teachers])


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text_search(message: Message):
    q = message.text.strip().lower().replace("ё", "е")
    if len(q) < 3:
        return
    found = [t for t in load_teachers() if q in t.lower().replace("ё", "е")]
    if found:
        await send_long(message, [html.escape(t) for t in found[:30]])
    else:
        await message.answer("Никого не нашёл. Попробуйте написать только фамилию.")


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(watcher(bot))
    asyncio.create_task(daily_sender(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
